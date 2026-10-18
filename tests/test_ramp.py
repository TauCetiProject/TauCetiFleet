"""Resource experiments must ramp slowly, survive restarts and distinguish evidence gaps."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import host, ramp, store


class RampTests(unittest.TestCase):
    def state(self):
        return dict(
            phase="running",
            started_at=1000,
            maximum=1,
            next_increase_at=2800,
            interval_seconds=1800,
            queue_limit=40,
            ceiling=64,
            baseline_oom_kill=0,
            history=[],
            condition_since={},
        )

    def observation(self, at=1000, **changes):
        value = dict(
            at=at,
            maximum=1,
            admitted=1,
            workers_enabled=True,
            ci_busy=False,
            cpu_idle=0.8,
            memory_available=40 * 1024**3,
            memory_total=128 * 1024**3,
            worker_memory=2 * 1024**3,
            worker_memory_high=36 * 1024**3,
            worker_oom_kill=0,
            worker_states={"working": 1},
            workers_alive=1,
            queue_prs=10,
            spent="0",
            pending="0",
            available="50",
            grant_rate="100",
            waiting_budget=0,
        )
        value.update(changes)
        return value

    def test_half_hour_stages_and_no_restart_catchup(self):
        value = self.state()
        for at in range(1000, 2800, 60):
            value, change = ramp.advance(value, self.observation(at))
            self.assertIsNone(change)
        value, change = ramp.advance(copy.deepcopy(value), self.observation(2800))
        self.assertEqual(change, 2)
        self.assertEqual(value["next_increase_at"], 4600)
        value, change = ramp.advance(value, self.observation(9000, workers_alive=2, admitted=2))
        self.assertIsNone(change)
        self.assertEqual(value["next_increase_at"], 10800)
        for at in range(9060, 10801, 60):
            value, change = ramp.advance(value, self.observation(at, workers_alive=2, admitted=2))
        self.assertEqual(change, 3)

    def test_queue_over_40_stops_and_withdraws_new_admissions(self):
        value, change = ramp.advance(self.state(), self.observation(queue_prs=41))
        self.assertEqual(change, 0)
        self.assertEqual(value["limiting_factors"], ["merge_queue"])
        self.assertEqual(value["maximum_at_stop"], 1)

    def test_cpu_transient_does_not_count_but_sustained_pressure_does(self):
        value, _ = ramp.advance(self.state(), self.observation(cpu_idle=0.1))
        value, change = ramp.advance(value, self.observation(1200))
        self.assertIsNone(change)
        self.assertNotIn("cpu", value["condition_since"])
        value, _ = ramp.advance(value, self.observation(1300, cpu_idle=0.1))
        for at in range(1360, 1601, 60):
            value, change = ramp.advance(value, self.observation(at, cpu_idle=0.1))
        self.assertEqual(change, 0)
        self.assertEqual(value["limiting_factors"], ["cpu"])

    def test_memory_and_simultaneous_limits_remain_distinct(self):
        value = self.state()
        for at in range(1000, 1301, 60):
            value, change = ramp.advance(value, self.observation(at, cpu_idle=0.1, memory_available=1024))
        self.assertEqual(change, 0)
        self.assertEqual(set(value["limiting_factors"]), {"cpu", "memory"})

    def test_oom_is_an_immediate_memory_limit(self):
        value, change = ramp.advance(self.state(), self.observation(worker_oom_kill=1))
        self.assertEqual(change, 0)
        self.assertEqual(value["limiting_factors"], ["memory"])

    def test_initial_credit_wait_is_not_a_sustainable_budget_limit(self):
        value = self.state()
        for at in range(1000, 2800, 60):
            value, change = ramp.advance(value, self.observation(at, waiting_budget=1))
            self.assertIsNone(change)
        value, _ = ramp.advance(value, self.observation(2800, waiting_budget=1))
        for at in range(2860, 3101, 60):
            value, change = ramp.advance(value, self.observation(at, waiting_budget=1))
        self.assertEqual(change, 0)
        self.assertEqual(value["limiting_factors"], ["budget"])

    def test_unlaunched_workers_do_not_cause_runaway_increases(self):
        value = self.state()
        value, change = ramp.advance(value, self.observation(3000, workers_alive=0, admitted=0))
        self.assertIsNone(change)
        self.assertEqual(value["maximum"], 1)
        self.assertEqual(value["hold_reason"], "workers not ready")

    def test_reducing_maximum_keeps_definitions_and_existing_paid_work(self):
        import os
        import tomllib

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "TAUCETI_FLEET_CONFIG": str(Path(directory) / "config"),
                    "TAUCETI_FLEET_STATE": str(Path(directory) / "state"),
                },
            ),
        ):
            config = store.update_host(max_workers=3, workers_enabled=True)
            ids = host.worker_config(config)
            ramp.set_maximum(1)
            self.assertEqual(host.worker_config(store.host_config()), ids)
            ramp.set_maximum(0)
            self.assertEqual(host.worker_config(store.host_config()), ids)
            specs = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
            self.assertEqual(len(specs), 3)
            self.assertEqual(store.read(store.state_dir() / "admission.json")["workers"], [])
