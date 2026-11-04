import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import ci_pool, github, host, store, vm


class Pool(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_STATE": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.config = dict(ci_enabled=True, ci_max_jobs=8, ci_staging_slots=1,
                           ci_kinds=["pr", "staging"], ci_idle_pool=True)

    def test_cap_reduction_counts_busy_guests_above_new_slot_range(self):
        values = [dict(slot=i, runner_id=i + 100, busy=True) for i in range(10, 19)]
        values += [dict(slot=i, runner_id=i + 100) for i in range(8)]
        self.assertEqual(ci_pool.plan(self.config, values, [], dict(queued=[])), set(range(10, 19)))

    def test_empty_host_keeps_reserved_staging_and_one_general_spare(self):
        self.assertEqual(ci_pool.plan(self.config, [], [], dict(queued=[])), {0, 1})
        values = [dict(slot=i, runner_id=i + 100, busy=True) for i in range(7)]
        self.assertEqual(ci_pool.plan(self.config, values, [], dict(queued=[])), set(range(8)))

    def test_github_busy_observation_counts_even_before_local_hook(self):
        values = [dict(slot=i, runner_id=i + 100) for i in range(8)]
        inventory = [dict(id=i + 100, busy=True) for i in range(1, 8)]
        self.assertEqual(ci_pool.plan(self.config, values, inventory, dict(queued=[])), set(range(8)))

    def test_idle_shutdown_preserves_queued_assignments_without_mutating_labels(self):
        value = dict(instance="a", runner_id=100)
        own = dict(labels=[dict(name=n) for n in ("self-hosted", "fleet-instance-a", "tauceti-fleet-pr")])
        observation = dict(observed_at=time.time(), queued=[])
        self.assertTrue(ci_pool.idle_ready(value, own, observation))
        observation["queued"] = [{"self-hosted", "fleet-instance-a", "tauceti-fleet-pr"}]
        self.assertFalse(ci_pool.idle_ready(value, own, observation))
        self.assertFalse(ci_pool.idle_ready(value, dict(own, busy=True), dict(queued=[])))

    def test_idle_delete_rejection_never_stops_vm(self):
        value = dict(slot=2, instance="a", runner_id=100, unit="vm", directory=str(self.root / "vms/a"),
                     ci_kinds=["pr", "staging"], routing_labels=["fleet-instance-a"])
        store.atomic(vm.slot_file(dict(ci_slot=2), "vm.json"), value)
        with patch.object(github, "retire_runner", side_effect=store.FleetError("busy")), patch.object(vm, "run") as stop:
            with self.assertRaises(store.FleetError):
                vm.retire_idle(self.config, value)
            stop.assert_not_called()
            self.assertEqual(store.read(vm.slot_file(dict(ci_slot=2), "vm.json")), value)

    def test_assigned_high_slot_uses_capacity_before_it_is_marked_busy(self):
        values = [dict(slot=i, runner_id=i + 100, busy=True) for i in range(1, 8)]
        values.append(dict(slot=12, runner_id=112))
        inventory = [dict(id=112, labels=[dict(name="fleet-instance-a")])]
        observation = dict(queued=[{"fleet-instance-a"}])
        self.assertEqual(ci_pool.plan(self.config, values, inventory, observation), set(range(1, 8)) | {12})

    def test_unknown_queue_blocks_growth_and_preserves_existing_guests(self):
        self.assertEqual(ci_pool.plan(self.config, [], [], None), set())
        self.assertEqual(ci_pool.plan(self.config, [dict(slot=12)], [], None), {12})

    def test_deleted_instance_is_resurrected_when_a_racing_picker_targets_it(self):
        value = dict(instance="a" * 16, ci_kinds=["pr", "staging"],
                     routing_labels=["self-hosted", "fleet-instance-" + "a" * 16])
        ci_pool.remember(value)
        queued = dict(queued=[set(value["routing_labels"])])
        records = ci_pool.recoveries([], queued)
        self.assertEqual(len(records), 1)
        slots = ci_pool.recovery_slots(self.config, [], [], queued, records)
        self.assertEqual(set(slots), {1})
        self.assertEqual(slots[1]["instance"], value["instance"])
        desired = ci_pool.plan(self.config, [], [], queued, slots, True)
        self.assertEqual(desired, {0, 1})
        self.assertEqual(ci_pool.recoveries([dict(instance=value["instance"])], queued), [])
        self.assertEqual(ci_pool.recoveries([], dict(queued=[])), [])

    def test_recovery_waits_for_global_capacity_and_does_not_steal_staging_slot(self):
        record = dict(instance="a" * 16, ci_kinds=["pr", "staging"])
        busy = [dict(slot=i, runner_id=i + 100, busy=True) for i in range(1, 8)]
        self.assertEqual(ci_pool.recovery_slots(self.config, busy, [], dict(queued=[]), [record]), {})
        busy.pop()
        self.assertEqual(set(ci_pool.recovery_slots(self.config, busy, [], dict(queued=[]), [record])), {7})

    def test_recovery_start_uses_original_instance_and_job_classes(self):
        config = dict(self.config, ci_vcpus=16, ci_memory_gib=32)
        recovery = dict(instance="a" * 16, ci_kinds=["pr"])
        with patch.object(vm, "start", return_value={}) as start:
            host.CISlot(2).step(config, inventory=[], refresh=False, cooling=False,
                                admitted=True, reservations=dict(queued=[]), recovery=recovery)
            self.assertEqual(start.call_args.args[0]["ci_recover_instance"], recovery["instance"])
            self.assertEqual(start.call_args.args[0]["ci_kinds"], ["pr"])

    def test_no_observation_never_retires(self):
        self.assertFalse(ci_pool.idle_ready({}, {}, None))
