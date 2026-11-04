import concurrent.futures
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import store, vm


class StartPacing(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"TAUCETI_FLEET_STATE": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = dict(ci_kinds=["pr", "staging"])

    def test_slots_and_new_callers_share_a_nonblocking_minute_gap(self):
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm, "_start", return_value={"started": True}) as start:
            self.assertTrue(vm.start(dict(self.config, ci_slot=0)))
            clock.return_value = 159.9
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            self.assertEqual(vm.start(dict(self.config, ci_slot=2)), {})
            start.assert_called_once()
            clock.return_value = 160
            self.assertTrue(vm.start(dict(self.config, ci_slot=2)))
            self.assertEqual(start.call_count, 2)

    def test_adopting_existing_guest_does_not_wait_or_reset_the_gate(self):
        prior = dict(unit="existing.service")
        store.atomic(vm.slot_file(dict(ci_slot=0), "vm.json"), prior)
        with patch.object(vm.time, "monotonic", return_value=100), patch.object(vm, "_start", return_value={"started": True}) as start, patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertTrue(vm.start(dict(self.config, ci_slot=1)))
            self.assertEqual(vm.start(dict(self.config, ci_slot=0)), prior)
            self.assertEqual(vm.start(dict(self.config, ci_slot=2)), {})
            start.assert_called_once()

    def test_failed_slow_start_attempt_also_spaces_the_next_slot(self):
        with patch.object(vm.time, "monotonic", return_value=100) as clock:
            def fail(config):
                clock.return_value = 130
                raise store.FleetError("registration failed")
            with patch.object(vm, "_start", side_effect=fail) as start:
                with self.assertRaises(store.FleetError):
                    vm.start(dict(self.config, ci_slot=0))
                clock.return_value = 189
                self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
                start.assert_called_once()
            clock.return_value = 190
            with patch.object(vm, "_start", return_value={"started": True}):
                self.assertTrue(vm.start(dict(self.config, ci_slot=1)))

    def test_concurrent_callers_cannot_both_start_a_guest(self):
        entered = threading.Event()
        finish = threading.Event()
        def start(config):
            entered.set()
            self.assertTrue(finish.wait(2))
            return {"started": True}
        with patch.object(vm, "_start", side_effect=start) as launch, concurrent.futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(vm.start, dict(self.config, ci_slot=0))
            self.assertTrue(entered.wait(2))
            second = pool.submit(vm.start, dict(self.config, ci_slot=1))
            finish.set()
            results = [first.result(2), second.result(2)]
        self.assertEqual(sum(bool(value) for value in results), 1)
        launch.assert_called_once()

    def test_existing_recent_guest_seeds_the_gate_on_initial_install(self):
        store.atomic(vm.slot_file(dict(ci_slot=3), "vm.json"), dict(started_at=990))
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm.time, "time", return_value=1000), patch.object(vm, "_start", return_value={"started": True}) as start:
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            start.assert_not_called()
            clock.return_value = 150
            self.assertTrue(vm.start(dict(self.config, ci_slot=1)))

    def test_interrupted_start_gets_a_fresh_minute_after_recovery(self):
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        store.atomic(store.state_dir() / "ci-start-pace.json", dict(boot_id=boot, next_start_monotonic=10, starting=True))
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm, "_start", return_value={"started": True}) as start:
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            clock.return_value = 159
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            start.assert_not_called()
            clock.return_value = 160
            self.assertTrue(vm.start(dict(self.config, ci_slot=1)))

    def test_host_reboot_does_not_reuse_an_old_monotonic_deadline(self):
        store.atomic(store.state_dir() / "ci-start-pace.json", dict(boot_id="previous-boot", next_start_monotonic=10**12))
        with patch.object(vm, "_start", return_value={"started": True}):
            self.assertTrue(vm.start(dict(self.config, ci_slot=1)))
