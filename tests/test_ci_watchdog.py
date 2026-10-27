import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_fleet import ci_watchdog, github, host, store, vm


class Watchdog(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + key: str(Path(temp.name) / key)
                                     for key in ("STATE", "CONFIG", "DATA")})
        env.start()
        self.addCleanup(env.stop)
        self.config = dict(store.host_config(), ci_enabled=True)
        self.value = dict(slot=0, instance="guest", unit="guest.service", directory=temp.name,
                          runner_id=12, started_at=100, busy=True, phase="ready", run_id=42)
        self.path = vm.slot_file(self.config, "vm.json")
        store.atomic(self.path, self.value)
        self.job = dict(id=7, run_id=42, runner_id=12, status="completed",
                        completed_at=datetime.fromtimestamp(700, UTC).isoformat())
        self.response = dict(total_count=1, jobs=[self.job])

    def step(self, slot=None, *, now=1000, response=None, api_error=None):
        slot = slot or host.CISlot(0)
        with (
            patch.object(host.time, "time", return_value=now),
            patch.object(host, "ci_service_state", return_value=(True, "unknown")),
            patch.object(github, "ci_read", return_value=response or self.response, side_effect=api_error) as lookup,
            patch.object(host, "collect_ci_results"),
            patch.object(vm, "retire", side_effect=lambda *_: self.path.unlink()) as retire,
            patch.object(host, "run") as stop,
            patch.object(vm, "start") as start,
        ):
            result = slot.step(self.config, inventory=[], refresh=False, cooling=False)
        start.assert_not_called()
        return result, stop, retire, lookup

    def test_finished_job_retires_live_vm_and_preserves_result_for_replacement(self):
        slot = host.CISlot(0)
        result, stop, retire, lookup = self.step(slot)
        self.assertEqual(result, {})
        stop.assert_called_once_with(["systemctl", "--user", "stop", "guest.service"])
        self.assertEqual(retire.call_args.args[1]["watchdog_reason"], "github_job_completed")
        self.assertEqual(lookup.call_count, 1)
        receipt = store.read(store.state_dir() / "pending-ci.json")[0]
        self.assertEqual((receipt["run_id"], receipt["runner_id"]), (42, 12))
        with patch.object(host.time, "time", return_value=1006), patch.object(vm, "start", return_value={}) as start:
            slot.step(self.config, inventory=[], refresh=False, cooling=False)
        start.assert_called_once()

    def test_another_runner_active_attempt_partial_page_or_recent_finish_cannot_stop_build(self):
        responses = []
        for change in (dict(runner_id=13), dict(run_id=43), dict(status="in_progress"),
                       dict(completed_at=None), dict(completed_at="not-a-time"),
                       dict(completed_at=datetime.fromtimestamp(950, UTC).isoformat()),
                       dict(completed_at=datetime.fromtimestamp(50, UTC).isoformat())):
            responses.append(dict(total_count=1, jobs=[{**self.job, **change}]))
        responses.append(dict(total_count=2, jobs=[self.job]))
        responses.append(dict(total_count=0, jobs=[]))
        responses.append(dict(total_count=2, jobs=[self.job, {**self.job, "id": 8, "status": "in_progress"}]))
        for response in responses:
            with self.subTest(response=response):
                result, stop, retire, _ = self.step(response=response)
                self.assertTrue(result["busy"])
                stop.assert_not_called()
                retire.assert_not_called()

    def test_api_errors_leave_young_build_running_and_back_off_reads(self):
        slot = host.CISlot(0)
        with self.assertRaisesRegex(store.FleetError, "unavailable"):
            self.step(slot, api_error=store.FleetError("unavailable"))
        self.assertEqual(store.read(self.path), self.value)
        result, stop, _, lookup = self.step(slot, now=1030)
        self.assertTrue(result["busy"])
        lookup.assert_not_called()
        stop.assert_not_called()

    def test_local_deadline_survives_restart_missing_job_identity_and_api_outage(self):
        for known_run in (True, False):
            with self.subTest(known_run=known_run):
                value = copy.deepcopy(self.value)
                if not known_run:
                    del value["run_id"]
                store.atomic(self.path, value)
                _, stop, retire, lookup = self.step(now=100 + ci_watchdog.VM_MAX_SECONDS,
                                                   api_error=AssertionError("deadline must not query GitHub"))
                stop.assert_called_once()
                lookup.assert_not_called()
                self.assertEqual(retire.call_args.args[1]["watchdog_reason"], "guest_lifetime_exceeded")

    def test_lost_completion_hook_does_not_require_runner_inventory_refresh(self):
        _, stop, _, _ = self.step()
        stop.assert_called_once()

    def test_guest_completion_message_cannot_authorize_retirement(self):
        slot = host.CISlot(0)
        slot.client = Mock()
        slot.client.recv.return_value = (json.dumps(dict(kind="ci_completed", run_id=42)) + "\n").encode()
        result, stop, retire, _ = self.step(slot, response=dict(total_count=1, jobs=[{**self.job, "status": "in_progress"}]))
        self.assertTrue(result["busy"])
        stop.assert_not_called()
        retire.assert_not_called()

    def test_guest_is_stopped_even_if_a_full_disk_prevents_reporting(self):
        with (
            patch.object(host.time, "time", return_value=100 + ci_watchdog.VM_MAX_SECONDS),
            patch.object(host, "ci_service_state", return_value=(True, "unknown")),
            patch.object(host, "event", side_effect=OSError("disk full")),
            patch.object(host, "run") as stop,
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                host.CISlot(0).step(self.config, inventory=[], refresh=False, cooling=False)
        stop.assert_called_once_with(["systemctl", "--user", "stop", "guest.service"])

    def test_inventory_outage_cannot_bypass_local_deadline_in_daemon(self):
        caps = dict(memory_total=128 * 1024**3, memory_available=100 * 1024**3)
        with (
            patch.object(host.time, "time", return_value=100 + ci_watchdog.VM_MAX_SECONDS),
            patch.object(host.time, "sleep", side_effect=StopIteration),
            patch.object(host.signal, "signal"),
            patch.object(host, "host_config", return_value=self.config),
            patch.object(host, "resources", return_value=caps),
            patch.object(host, "cpu_sample", return_value=(100, 90)),
            patch.object(host, "worker_statuses", return_value=[]),
            patch.object(host, "collect_ci_results"),
            patch.object(github, "runners", side_effect=store.FleetError("inventory unavailable")),
            patch.object(host, "ci_service_state", return_value=(True, "unknown")),
            patch.object(vm, "retire", side_effect=lambda *_: self.path.unlink()),
            patch.object(host, "run") as stop,
        ):
            with self.assertRaises(StopIteration):
                host.daemon()
        stop.assert_called_once_with(["systemctl", "--user", "stop", "guest.service"])
        self.assertEqual(store.read(store.state_dir() / "status.json")["ci_errors"],
                         {"inventory": "inventory unavailable"})

    def test_systemd_bounds_guest_lifetime_without_controller(self):
        self.path.unlink()
        store.atomic(store.data_dir() / "images/prepared.json", dict(sha256="a" * 64))
        with (
            patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
            patch.object(vm, "executable", side_effect=lambda name: name),
            patch.object(github, "jit_config", return_value=(12, "test-jit")),
            patch.object(vm, "cloud_seed", return_value=Path(self.value["directory"]) / "seed.iso"),
        ):
            vm.start(self.config)
        launch = next(call.args[0] for call in run.call_args_list if call.args[0][0] == "systemd-run")
        self.assertIn("RuntimeMaxSec=25200", launch)


if __name__ == "__main__":
    unittest.main()
