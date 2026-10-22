import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import broker, capacity, ci, github, host, store, vm


class Capacity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + key.upper(): str(self.root / key)
                                     for key in ("config", "state", "data")})
        env.start()
        self.addCleanup(env.stop)

    def test_shared_capacity_and_classes_require_explicit_authorized_values(self):
        config = capacity.settings(store.host_config(), ci="both", maximum=2, vcpus=16, memory_gib=32)
        self.assertEqual(capacity.kinds(config), ["pr", "staging"])
        self.assertEqual(capacity.jobs(config), 2)
        capacity.authorize(config, dict(max_jobs=2, kinds=["pr", "staging"]))
        for policy in ({}, dict(max_jobs=1, kinds=["pr", "staging"]), dict(max_jobs=2, kinds=["staging"])):
            with self.assertRaises(store.FleetError):
                capacity.authorize(config, policy)
        for maximum in (0, True, 1.5, 17):
            with self.assertRaises(store.FleetError):
                capacity.settings(config, ci="both", maximum=maximum)
        for kind in (True, {}, "unknown"):
            with self.assertRaises(store.FleetError):
                capacity.settings(config, ci=kind)

    def test_pr_only_runners_cannot_match_legacy_staging_workflows(self):
        config = capacity.settings(store.host_config(), ci="pr")
        labels = github.runner_labels(config, "fleet-host-" + "a" * 16)
        self.assertIn("tauceti-fleet-pr", labels)
        self.assertNotIn("tauceti-fleet", labels)
        self.assertNotIn("tauceti-fleet-staging", labels)
        self.assertIn("fleet-instance-" + "a" * 16, labels)

    def test_normal_pr_cancellations_and_deleted_forks_do_not_cool_staging_capacity(self):
        for job in (dict(conclusion="cancelled", steps=[]),
                    dict(conclusion="failure", steps=[dict(name="Resolve PR head", conclusion="failure")]),
                    dict(conclusion="failure", steps=[dict(name="Checkout PR", conclusion="failure")])):
            self.assertFalse(ci.classify(job, kind="pr", host_health="healthy")["cooldown"])
            self.assertTrue(ci.classify(job, kind="pr", host_health="fault")["cooldown"])
        self.assertTrue(ci.classify(dict(conclusion="failure", steps=[dict(name="Set up job", conclusion="failure")]),
                                    kind="pr", host_health="healthy")["cooldown"])
        self.assertTrue(ci.classify(dict(conclusion="startup_failure"), kind="pr", host_health="healthy")["cooldown"])

    def test_guests_and_registration_retries_are_isolated_by_slot(self):
        config = dict(store.host_config(), ci_broker_url=broker.DEFAULT_URL)
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        registered = []

        def register(config, name):
            registered.append((config["ci_slot"], name))
            return 100 + config["ci_slot"], "single-job"

        with patch.object(github, "jit_config", side_effect=register), patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(vm, "executable", side_effect=lambda name: name), patch.object(vm, "cloud_seed", return_value=self.root / "seed.iso"), patch.object(github, "retire_runner"):
            zero = vm.start(dict(config, ci_slot=0))
            one = vm.start(dict(config, ci_slot=1))
            self.assertNotEqual(zero["instance"], one["instance"])
            self.assertEqual(vm.start(dict(config, ci_slot=0)), zero)
            self.assertEqual(len(registered), 2)
            self.assertEqual(store.read(vm.slot_file({"ci_slot": 1}, "vm.json")), one)
            vm.retire(config, zero)
            self.assertTrue(vm.outstanding())
            self.assertEqual(store.read(vm.slot_file({"ci_slot": 1}, "vm.json")), one)
            vm.retire(config, one)
            self.assertFalse(vm.outstanding())

    def test_every_slot_blocks_enrollment_preparation_and_updates_until_retired(self):
        pending = vm.slot_file({"ci_slot": 1}, "ci-registration.json")
        store.atomic(pending, dict(instance="a" * 16))
        for operation in (broker.enroll, host.prepare, lambda: host.rpc(dict(action="prepare_update"))):
            with self.assertRaises(store.FleetError):
                operation()

    def test_result_receipts_do_not_overwrite_other_slot_state(self):
        for slot in (0, 1):
            value = dict(slot=slot, instance=str(slot) * 16, run_id=42 + slot, runner_id=100 + slot)
            store.atomic(vm.slot_file({"ci_slot": slot}, "vm.json"), value)
            self.assertTrue(host.queue_ci_result(value, "healthy"))
            self.assertFalse(host.queue_ci_result(value, "healthy"))
        self.assertEqual(len(store.read(store.state_dir() / "pending-ci.json")), 2)
        for slot in (0, 1):
            value = store.read(vm.slot_file({"ci_slot": slot}, "vm.json"))
            self.assertEqual(value["slot"], slot)
            self.assertTrue(value["result_queued"])

    def test_enabling_chris_starts_exactly_two_shared_slots_with_the_selected_shape(self):
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        with patch.object(host, "doctor", return_value=dict(readiness=dict(ci="ready", workers="blocked"))), patch.object(github, "capacity_policy", return_value=dict(max_jobs=2, kinds=["pr", "staging"])), patch.object(host, "write_slices"), patch.object(host, "resources", return_value={}), patch.object(host, "worker_config"), patch.object(vm, "start", return_value={}) as start, patch.object(host, "install_service"):
            result = host.enable(ci="both", maximum=2, vcpus=16, memory_gib=32)
        self.assertEqual(result["ci_max_jobs"], 2)
        self.assertFalse(result["workers_enabled"])
        self.assertTrue(result["ci_enabled"])
        self.assertEqual([c.args[0]["ci_slot"] for c in start.call_args_list], [0, 1])
        self.assertTrue(all(c.args[0]["ci_vcpus"] == 16 and c.args[0]["ci_memory_gib"] == 32 for c in start.call_args_list))
        self.assertEqual(store.host_config()["ci_kinds"], ["pr", "staging"])

    def test_draining_retires_idle_slots_and_preserves_busy_work(self):
        config = dict(store.host_config(), ci_enabled=False, ci_max_jobs=2)
        values = []
        for slot in (0, 1):
            value = dict(slot=slot, unit=f"vm-{slot}", directory=str(self.root / str(slot)), runner_id=100 + slot,
                         started_at=0, phase="ready", busy=False)
            store.atomic(vm.slot_file({"ci_slot": slot}, "vm.json"), value)
            values.append(value)
        inventory = [dict(id=100, status="online", busy=False), dict(id=101, status="online", busy=True)]
        with patch.object(host, "ci_service_state", return_value=(True, "healthy")), patch.object(vm, "retire") as retire:
            idle = host.CISlot(0).step(config, inventory=inventory, refresh=True, cooling=False)
            busy = host.CISlot(1).step(config, inventory=inventory, refresh=True, cooling=False)
        self.assertFalse(idle)
        self.assertTrue(busy["busy"])
        self.assertEqual(retire.call_count, 1)
        self.assertEqual(retire.call_args.args[1]["slot"], 0)

    def test_slice_reservation_counts_both_guests_and_their_overheads(self):
        gib = 1024**3
        config = capacity.settings(store.host_config(), ci="both", maximum=2, vcpus=16, memory_gib=32)
        with patch.object(host, "units", return_value=self.root), patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")):
            host.write_slices(config, dict(memory_total=128 * gib, memory_available=120 * gib))
        self.assertIn(f"MemoryMax={68 * gib}", (self.root / "tauceti-fleet-ci.slice").read_text())
