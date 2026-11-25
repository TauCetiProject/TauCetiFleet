import datetime
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import github, host, hostos, store, vm

hostos.MACOS = False


class SlotBackoff(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + k.upper(): str(self.root / k) for k in ("state", "config", "data")})
        env.start()
        self.addCleanup(env.stop)

    def test_native_infrastructure_failure_uses_ordinary_slot_recovery(self):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        job = dict(id=7, runner_id=12, status="completed", conclusion="failure", started_at=now, completed_at=now, steps=[])
        receipt = dict(slot=1, run_id=42, runner_id=12, ci_kinds=["pr"], runtime="native-disk", host_health="fault", started_at=time.time())
        store.atomic(store.state_dir() / "pending-ci.json", [receipt])
        with patch.object(github, "ci_read", return_value=dict(jobs=[job])):
            host.collect_ci_results(store.host_config())
        self.assertTrue(vm.slot_file({"ci_slot": 1}, "ci-slot-backoff.json").exists())
        with store.database() as db:
            value = db.execute("SELECT data FROM events WHERE kind='ci_result'").fetchone()[0]
        self.assertIn("host_service_fault", value)

    def test_one_failed_setup_does_not_stop_other_slots(self):
        config = dict(store.host_config(), ci_enabled=True, ci_max_jobs=12, ci_kinds=["pr", "staging"])
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        job = dict(id=7, runner_id=12, status="completed", conclusion="failure", started_at=now, completed_at=now,
                   steps=[dict(name="Install bubblewrap (pinned + checksum)", conclusion="failure")])
        receipt = dict(slot=5, run_id=42, runner_id=12, ci_kinds=["pr", "staging"], host_health="healthy", started_at=time.time())
        store.atomic(store.state_dir() / "pending-ci.json", [receipt])
        def read(config, path, **kwargs):
            return dict(jobs=[job]) if "/jobs?" in path else dict(event="pull_request_target", display_title="pr-build 123")
        with patch.object(github, "ci_read", side_effect=read):
            host.collect_ci_results(config)
        self.assertFalse((store.state_dir() / "ci-backoff.json").exists())
        circuit = store.read(vm.slot_file({"ci_slot": 5}, "ci-slot-backoff.json"))
        self.assertEqual(circuit["job_id"], 7)
        self.assertLess(circuit["until"], time.time() + 61)
        with patch.object(vm, "start", return_value={}) as start:
            host.CISlot(5).step(config, inventory=[], refresh=False, cooling=False)
            start.assert_not_called()
            host.CISlot(0).step(config, inventory=[], refresh=False, cooling=False)
            self.assertEqual(start.call_count, 1)

    def test_package_updates_finish_before_runner_starts(self):
        store.atomic(store.data_dir() / "images/prepared.json", {})
        with patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(vm, "executable", side_effect=lambda x: x), patch.object(github, "jit_config", return_value=(12, "test")), patch.object(vm, "cloud_seed", return_value=self.root / "seed") as seed:
            vm.start(store.host_config())
        commands = seed.call_args.args[1]
        self.assertIn("systemctl mask --now apt-daily.timer apt-daily-upgrade.timer", commands)
        self.assertIn("systemctl mask apt-daily.service apt-daily-upgrade.service", commands)
        wait = next(i for i, command in enumerate(commands) if command.startswith("while systemctl is-active"))
        run = next(i for i, command in enumerate(commands) if "./run.sh --jitconfig" in command)
        self.assertLess(wait, run)
        self.assertFalse(any("systemctl stop apt-daily.service" in command for command in commands))
