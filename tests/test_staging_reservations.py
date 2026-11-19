import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import broker, capacity, github, host, store, vm


class StagingReservations(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TAUCETI_FLEET_" + key.upper(): str(self.root / key)
                                             for key in ("config", "state", "data")})
        environment.start()
        self.addCleanup(environment.stop)
        memory = patch.object(vm, "start_memory_available", return_value=100 * 1024**3)
        memory.start()
        self.addCleanup(memory.stop)
        prepare = patch.object(host.native_runner, "prepare", return_value={})
        self.prepare = prepare.start()
        self.addCleanup(prepare.stop)
        self.config = store.update_host(ci_enabled=True, workers_enabled=True, max_workers=40,
                                        ci_max_jobs=2, ci_kinds=["pr", "staging"])

    def test_reserved_guest_has_no_pr_label_and_other_guest_remains_shared(self):
        config = dict(self.config, ci_staging_slots=1)
        zero = capacity.for_slot(config, 0)
        one = capacity.for_slot(config, 1)
        labels = github.runner_labels(zero, "fleet-host-" + "a" * 16)
        self.assertIn("tauceti-fleet-staging", labels)
        self.assertNotIn("tauceti-fleet-pr", labels)
        self.assertIn("tauceti-fleet-pr", github.runner_labels(one, "fleet-host-" + "b" * 16))
        self.assertEqual(capacity.jobs(zero), 2)
        self.assertEqual(config["ci_kinds"], ["pr", "staging"])
        capacity.authorize(zero, dict(max_jobs=2, kinds=["pr", "staging"]))

    def test_live_reservation_changes_no_admissions_or_guest_processes(self):
        guest = dict(slot=0, unit="vm-0", directory=str(self.root / "guest"), runner_id=100,
                     started_at=0, phase="ready", busy=True, run_id=42, ci_kinds=["pr", "staging"])
        store.atomic(vm.slot_file(self.config, "vm.json"), guest)
        with patch.object(host, "run") as run, patch.object(vm, "retire") as retire:
            result = host.rpc(dict(action="reserve-staging", maximum=1))
        run.assert_not_called()
        retire.assert_not_called()
        self.assertEqual(result["pending_slots"], [0])
        self.assertEqual(store.read(vm.slot_file(self.config, "vm.json")), guest)
        config = store.host_config()
        self.assertTrue(config["ci_enabled"])
        self.assertTrue(config["workers_enabled"])
        self.assertEqual(config["max_workers"], 40)
        self.assertEqual(config["ci_max_jobs"], 2)
        self.prepare.assert_called_once()

    def test_failed_native_preparation_leaves_live_slot_classes_unchanged(self):
        before = store.host_config()
        self.prepare.side_effect = store.FleetError("image export failed")
        with self.assertRaisesRegex(store.FleetError, "image export failed"):
            host.reserve_staging(1)
        self.assertEqual(store.host_config(), before)

    def test_busy_old_guest_finishes_with_its_original_receipt_classes(self):
        config = dict(self.config, ci_staging_slots=1)
        guest = dict(slot=0, unit="vm-0", directory=str(self.root / "guest"), runner_id=100,
                     started_at=0, phase="ready", busy=True, run_id=42, ci_kinds=["pr", "staging"])
        store.atomic(vm.slot_file(config, "vm.json"), guest)
        with (patch.object(host, "ci_service_state", return_value=(True, "healthy")),
              patch.object(vm, "retire") as retire, patch.object(vm, "start") as start):
            value = host.CISlot(0).step(config, inventory=[dict(id=100, status="online", busy=True)],
                                      refresh=True, cooling=False)
        self.assertEqual(value["ci_kinds"], ["pr", "staging"])
        self.assertTrue(value["busy"])
        retire.assert_not_called()
        start.assert_not_called()

    def test_next_guest_receives_the_slot_policy_in_every_start_path(self):
        config = dict(self.config, ci_staging_slots=1)
        with patch.object(vm, "_start", return_value={}) as start:
            vm.start(config)
        self.assertEqual(start.call_args.args[0]["ci_kinds"], ["staging"])
        for index, expected in ((0, ["staging"]), (1, ["pr", "staging"])):
            with patch.object(vm, "start", return_value={}) as start:
                host.CISlot(index).step(config, inventory=[], refresh=False, cooling=False)
            self.assertEqual(start.call_args.args[0]["ci_kinds"], expected)

    def test_invalid_reservations_and_incompatible_capacity_changes_are_rejected(self):
        for count in (-1, True, 1.5, "1", 3):
            with self.assertRaises(store.FleetError):
                host.reserve_staging(count)
        host.reserve_staging(2)
        with self.assertRaises(store.FleetError):
            capacity.settings(store.host_config(), ci="both", maximum=1)
        with self.assertRaises(store.FleetError):
            capacity.settings(store.host_config(), ci="pr")
        with self.assertRaises(store.FleetError):
            host.configure(dict(ci_max_jobs=1))
        host.reserve_staging(0)
        self.assertEqual(capacity.kinds(capacity.for_slot(store.host_config(), 0)), ["pr", "staging"])

    def test_reservation_validation_uses_the_current_locked_capacity(self):
        def racing_write(**changes):
            store.update_host(ci_max_jobs=1)
            return store.update_host(**changes)

        with patch.object(host, "update_host", side_effect=racing_write):
            with self.assertRaises(store.FleetError):
                host.reserve_staging(2)
        self.assertEqual(store.host_config()["ci_max_jobs"], 1)
        self.assertNotIn("ci_staging_slots", store.host_config())

    def test_corrupt_reservation_is_reported_without_disabling_workloads(self):
        config = store.update_host(ci_staging_slots="invalid")
        status = host.rpc(dict(action="status"))
        self.assertEqual(status["ci_staging_slots"], 0)
        self.assertTrue(status["ci_staging_reservation_error"])
        self.assertTrue(status["workers_enabled"])
        self.assertTrue(status["ci_enabled"])
        with patch.object(vm, "start", return_value={}) as start:
            host.CISlot(0).step(config, inventory=[], refresh=False, cooling=False)
        self.assertEqual(start.call_args.args[0]["ci_kinds"], ["pr", "staging"])
        host.reserve_staging(1)
        self.assertEqual(capacity.reservation(store.host_config()), (1, None))

    def test_pending_registration_is_reported_and_canceled_before_class_change(self):
        config = dict(self.config, ci_broker_url=broker.DEFAULT_URL, ci_staging_slots=1)
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        intent = dict(instance="a" * 16, ci_kinds=["pr", "staging"])
        path = vm.slot_file(config, "ci-registration.json")
        store.atomic(path, intent)
        self.assertEqual(host.reserve_staging(1)["pending_slots"], [0])
        with (patch.object(broker, "request", return_value={}) as cancel,
              patch.object(github, "jit_config", return_value=(100, "jit")) as register,
              patch.object(vm, "run"), patch.object(vm, "executable", side_effect=lambda name: name),
              patch.object(vm, "cloud_seed", return_value=self.root / "seed.iso"),
              patch.object(vm.secrets, "token_hex", return_value="b" * 16)):
            vm.start(config)
        self.assertEqual(cancel.call_args.args[1], "/v1/slots/0/cancel-registration")
        self.assertEqual(register.call_args.args[0]["ci_kinds"], ["staging"])
        self.assertIsNone(store.read(path))
        guest = store.read(vm.slot_file(config, "vm.json"))
        self.assertEqual(guest["instance"], "b" * 16)
        self.assertEqual(guest["ci_kinds"], ["staging"])

    def test_ambiguous_pending_registration_retains_ownership_without_registering_twice(self):
        config = dict(self.config, ci_broker_url=broker.DEFAULT_URL, ci_staging_slots=1)
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        path = vm.slot_file(config, "ci-registration.json")
        intent = dict(instance="a" * 16)  # legacy registration without stored classes
        store.atomic(path, intent)
        with (patch.object(broker, "request", side_effect=store.FleetError("registration recovery pending")),
              patch.object(github, "jit_config") as register):
            with self.assertRaises(store.FleetError):
                vm.start(config)
        register.assert_not_called()
        self.assertEqual(store.read(path), intent)


if __name__ == "__main__":
    unittest.main()


class StagingGuestMemory(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TAUCETI_FLEET_" + key.upper(): str(self.root / key)
                                             for key in ("config", "state", "data")})
        environment.start()
        self.addCleanup(environment.stop)
        prepare = patch.object(host.native_runner, "prepare", return_value={})
        prepare.start()
        self.addCleanup(prepare.stop)
        self.config = store.update_host(ci_enabled=True, workers_enabled=True, max_workers=40, ci_max_jobs=2,
                                        ci_kinds=["pr", "staging"], ci_vcpus=16, ci_memory_gib=32)
        self.caps = dict(cpus=96, memory_total=125 * 1024**3)

    def test_reserved_staging_guest_gets_the_configured_memory_and_others_the_ceiling(self):
        shared = capacity.guest_shape(capacity.for_slot(dict(self.config, ci_staging_slots=1), 0), self.caps)
        self.assertEqual(shared, dict(vcpus=96, memory_gib=112))
        host.reserve_staging(1, 64)
        config = store.host_config()
        self.assertEqual(capacity.guest_shape(capacity.for_slot(config, 0), self.caps), dict(vcpus=96, memory_gib=64))
        self.assertEqual(capacity.guest_shape(capacity.for_slot(config, 1), self.caps), dict(vcpus=96, memory_gib=112))
        self.assertEqual(capacity.guest_shape(config, self.caps), dict(vcpus=96, memory_gib=112))
        capacity.controller_config(config)

    def test_memory_setting_is_kept_by_default_and_cleared_by_zero(self):
        self.assertEqual(host.reserve_staging(1, 64)["ci_staging_memory_gib"], 64)
        self.assertEqual(host.rpc(dict(action="reserve-staging", maximum=1))["ci_staging_memory_gib"], 64)
        self.assertEqual(host.rpc(dict(action="status"))["ci_staging_memory_gib"], 64)
        self.assertIsNone(host.rpc(dict(action="reserve-staging", maximum=1, memory_gib=0))["ci_staging_memory_gib"])
        config = store.host_config()
        self.assertEqual(capacity.guest_shape(capacity.for_slot(config, 0), self.caps)["memory_gib"], 112)

    def test_running_guest_of_another_size_is_reported_and_left_running(self):
        guest = dict(slot=0, unit="vm-0", directory=str(self.root / "guest"), runner_id=100, started_at=0,
                     phase="ready", busy=True, run_id=42, ci_kinds=["staging"], vcpus=96, memory_gib=112)
        store.atomic(vm.slot_file(self.config, "vm.json"), guest)
        with patch.object(host, "run") as run, patch.object(vm, "retire") as retire:
            result = host.reserve_staging(1, 64)
        run.assert_not_called()
        retire.assert_not_called()
        self.assertEqual(result["pending_slots"], [0])
        self.assertEqual(store.read(vm.slot_file(self.config, "vm.json")), guest)
        store.atomic(vm.slot_file(self.config, "vm.json"), dict(guest, memory_gib=64))
        self.assertEqual(host.reserve_staging(1)["pending_slots"], [])

    def test_invalid_memory_settings_change_nothing(self):
        before = store.host_config()
        for value in (-1, True, 1.5, "64", 4097):
            with self.assertRaises(store.FleetError):
                host.reserve_staging(1, value)
        self.assertEqual(store.host_config(), before)
        with self.assertRaises(store.FleetError):
            capacity.controller_config(dict(before, ci_staging_memory_gib="64"))
