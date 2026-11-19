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
        memory = patch.object(vm, "start_memory_available", return_value=100 * 1024**3)
        memory.start()
        self.addCleanup(memory.stop)
        self.config = dict(ci_kinds=["pr", "staging"])

    def test_slots_and_new_callers_share_a_nonblocking_ten_second_gap(self):
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm, "_start", return_value={"started": True}) as start:
            self.assertTrue(vm.start(dict(self.config, ci_slot=0)))
            clock.return_value = 109.9
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            self.assertEqual(vm.start(dict(self.config, ci_slot=2)), {})
            start.assert_called_once()
            clock.return_value = 110
            self.assertTrue(vm.start(dict(self.config, ci_slot=2)))
            self.assertEqual(start.call_count, 2)

    def test_low_memory_defers_start_without_consuming_the_pacing_interval(self):
        gib = 1024**3
        with (
            patch.object(vm.time, "monotonic", return_value=100),
            patch.object(vm, "_start", return_value={"started": True}) as start,
            patch.object(vm, "start_memory_available", return_value=19 * gib) as memory,
        ):
            self.assertEqual(vm.start(dict(self.config, ci_slot=0)), {})
            start.assert_not_called()
            # Once memory recovers, the start proceeds at once without an additional startup delay.
            memory.return_value = 20 * gib
            self.assertTrue(vm.start(dict(self.config, ci_slot=0)))
            # Unmeasurable memory never blocks CI; pacing still applies.
            memory.return_value = None
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            start.assert_called_once()

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
                clock.return_value = 139.9
                self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
                start.assert_called_once()
            clock.return_value = 140
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
        store.atomic(vm.slot_file(dict(ci_slot=3), "vm.json"), dict(started_at=995))
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm.time, "time", return_value=1000), patch.object(vm, "_start", return_value={"started": True}) as start:
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            start.assert_not_called()
            clock.return_value = 105
            self.assertTrue(vm.start(dict(self.config, ci_slot=1)))

    def test_interrupted_start_gets_a_fresh_ten_seconds_after_recovery(self):
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        store.atomic(store.state_dir() / "ci-start-pace.json", dict(boot_id=boot, next_start_monotonic=10, starting=True))
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm, "_start", return_value={"started": True}) as start:
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            clock.return_value = 109
            self.assertEqual(vm.start(dict(self.config, ci_slot=1)), {})
            start.assert_not_called()
            clock.return_value = 110
            self.assertTrue(vm.start(dict(self.config, ci_slot=1)))

    def test_host_reboot_does_not_reuse_an_old_monotonic_deadline(self):
        store.atomic(store.state_dir() / "ci-start-pace.json", dict(boot_id="previous-boot", next_start_monotonic=10**12))
        with patch.object(vm, "_start", return_value={"started": True}):
            self.assertTrue(vm.start(dict(self.config, ci_slot=1)))

    def control_config(self):
        return dict(self.config, ci_max_jobs=4, ci_protected_slots={
            str(i): dict(kind="queue-control", runner_group_id=6) for i in (1, 2, 3)
        })

    def test_small_controls_start_faster_but_heavy_boot_still_settles_ten_seconds(self):
        config = self.control_config()
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm, "_start", return_value={"started": True}) as launch:
            self.assertTrue(vm.start(dict(config, ci_slot=1)))
            clock.return_value = 101.9
            self.assertEqual(vm.start(dict(config, ci_slot=2)), {})
            clock.return_value = 102
            self.assertTrue(vm.start(dict(config, ci_slot=2)))
            clock.return_value = 104
            self.assertTrue(vm.start(dict(config, ci_slot=0)))
            clock.return_value = 113.9
            self.assertEqual(vm.start(dict(config, ci_slot=3)), {})
            clock.return_value = 114
            self.assertTrue(vm.start(dict(config, ci_slot=3)))
            self.assertEqual(launch.call_count, 4)

    def test_unobserved_control_boots_reserve_their_full_ram(self):
        config = self.control_config()
        guest = dict(ci_kinds=["queue-control"], memory_gib=8, started_at=1)
        store.atomic(vm.slot_file(dict(ci_slot=1), "vm.json"), guest)
        with patch.object(vm.time, "monotonic", return_value=100), patch.object(vm, "start_memory_available", return_value=16 * 1024**3), patch.object(vm, "_start", return_value={"started": True}) as launch:
            # 16 GiB measured free minus an 8 GiB boot is below the 9 GiB gate.
            self.assertEqual(vm.start(dict(config, ci_slot=2)), {})
            launch.assert_not_called()
            # A job-start hook proves the guest has booted, without waiting
            # for the periodic GitHub inventory observation.
            store.atomic(vm.slot_file(dict(ci_slot=1), "vm.json"), dict(guest, busy=True))
            self.assertTrue(vm.start(dict(config, ci_slot=2)))

    def test_boot_reservations_also_protect_heavy_starts(self):
        store.atomic(vm.slot_file(dict(ci_slot=1), "vm.json"), dict(ci_kinds=["pr-status"], memory_gib=4, started_at=1))
        with patch.object(vm, "start_memory_available", return_value=23 * 1024**3), patch.object(vm, "_start") as launch:
            self.assertEqual(vm.start(dict(self.config, ci_slot=0)), {})
            launch.assert_not_called()

    def test_unmeasurable_memory_keeps_controls_on_the_old_ten_second_gap(self):
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm, "start_memory_available", return_value=None), patch.object(vm, "_start", return_value={"started": True}):
            self.assertTrue(vm.start(dict(self.control_config(), ci_slot=1)))
            clock.return_value = 102
            self.assertEqual(vm.start(dict(self.control_config(), ci_slot=2)), {})
            clock.return_value = 110
            self.assertTrue(vm.start(dict(self.control_config(), ci_slot=2)))

    def test_online_observation_releases_a_boot_reservation(self):
        guest = dict(ci_kinds=["queue-control"], memory_gib=8, started_at=1, online_observed_at=2)
        store.atomic(vm.slot_file(dict(ci_slot=1), "vm.json"), guest)
        self.assertEqual(vm.control_boot_reservation(), 0)

    def test_already_charged_ram_is_not_reserved_twice(self):
        guest = dict(ci_kinds=["queue-control"], memory_gib=8, started_at=1, unit="booting.service")
        store.atomic(vm.slot_file(dict(ci_slot=1), "vm.json"), guest)
        original = Path.read_text
        def read_text(path, *args, **kwargs):
            return str(2 * 1024**3) if str(path) == "/sys/fs/cgroup/test-boot/memory.current" else original(path, *args, **kwargs)
        with patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0, stdout="/test-boot\n")), patch.object(Path, "read_text", read_text):
            self.assertEqual(vm.control_boot_reservation(), 6 * 1024**3)

    def test_fast_launch_records_boot_reservation_before_next_start(self):
        config = self.control_config()
        def launch(config):
            guest = dict(ci_kinds=["queue-control"], memory_gib=8, started_at=1)
            store.atomic(vm.slot_file(config, "vm.json"), guest)
            return guest
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm, "start_memory_available", return_value=16 * 1024**3), patch.object(vm, "_start", side_effect=launch) as start:
            self.assertTrue(vm.start(dict(config, ci_slot=1)))
            clock.return_value = 102
            self.assertEqual(vm.start(dict(config, ci_slot=2)), {})
            start.assert_called_once()

    def test_interrupted_control_start_retains_ten_second_crash_recovery(self):
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        store.atomic(store.state_dir() / "ci-start-pace.json", dict(boot_id=boot, next_start_monotonic=10, starting=True))
        with patch.object(vm.time, "monotonic", return_value=100) as clock, patch.object(vm, "_start", return_value={"started": True}):
            self.assertEqual(vm.start(dict(self.control_config(), ci_slot=1)), {})
            clock.return_value = 109
            self.assertEqual(vm.start(dict(self.control_config(), ci_slot=1)), {})
            clock.return_value = 110
            self.assertTrue(vm.start(dict(self.control_config(), ci_slot=1)))
