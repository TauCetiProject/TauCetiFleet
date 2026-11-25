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

    def test_idle_delete_rejection_never_stops_vm_or_native_runner(self):
        for runtime in ("vm", "native-disk"):
            with self.subTest(runtime=runtime):
                value = dict(slot=2, instance="a", runtime=runtime, runner_id=100, unit="runner",
                             directory=str(self.root / "vms/a"), ci_kinds=["pr"], routing_labels=["fleet-instance-a"])
                store.atomic(vm.slot_file(dict(ci_slot=2), "vm.json"), value)
                with patch.object(github, "retire_runner", side_effect=store.FleetError("busy")), patch.object(vm, "run") as stop:
                    with self.assertRaises(store.FleetError):
                        vm.retire_idle(self.config, value)
                    stop.assert_not_called()
                    self.assertEqual(store.read(vm.slot_file(dict(ci_slot=2), "vm.json")), value)

    def test_idle_native_retirement_deregisters_before_stop_and_cleanup(self):
        from tauceti_fleet import native_runner

        directory = self.root / "vms" / ("a" * 16)
        directory.mkdir(parents=True)
        value = dict(slot=2, instance=directory.name, runtime="native-disk", runner_id=100,
                     unit="native.service", directory=str(directory), ci_kinds=["pr"], routing_labels=["fleet-instance-a"])
        receipt = vm.slot_file(dict(ci_slot=2), "vm.json")
        store.atomic(receipt, value)
        operations = []
        with (patch.object(github, "retire_runner", side_effect=lambda *_: operations.append("deregister")),
              patch.object(vm, "run", side_effect=lambda argv, **_: operations.append(argv[2])),
              patch.object(native_runner, "cleanup", side_effect=lambda *_a, **_k: operations.append("cleanup") or True)):
            self.assertTrue(vm.retire_idle(self.config, value))
        self.assertEqual(operations, ["deregister", "stop", "reset-failed", "cleanup"])
        self.assertFalse(receipt.exists())

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


class PriorityRegistration(unittest.TestCase):
    """While priority PR builds wait, freed general PR slots register runners only they can take."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_STATE": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.config = dict(ci_enabled=True, ci_max_jobs=8, ci_staging_slots=1, ci_kinds=["pr", "staging"],
                           ci_protected_slots={"7": dict(kind="queue-control", runner_group_id=6)})

    priority = {"self-hosted", "linux", "x64", "tauceti-fleet-pr-priority"}
    ordinary = {"self-hosted", "linux", "x64", "tauceti-fleet-pr"}

    def test_waiting_priority_builds_not_covered_by_spare_priority_guests(self):
        observation = dict(queued=[self.priority, self.priority, self.ordinary, {"TAUCETI-FLEET-PR-PRIORITY"}])
        self.assertEqual(ci_pool.priority_waiting(observation), 3)
        self.assertEqual(ci_pool.priority_waiting(None), 0)
        values = [dict(ci_priority_only=True), dict(ci_priority_only=True, busy=True), dict(), {}]
        self.assertEqual(ci_pool.priority_wanted(values, observation), 2)
        self.assertEqual(ci_pool.priority_wanted(values, dict(queued=[self.ordinary])), 0)
        self.assertEqual(ci_pool.priority_wanted([], None), 0)

    def test_only_general_pr_slots_register_priority_only_runners(self):
        self.assertEqual([i for i in range(8) if ci_pool.priority_slot(self.config, i)], list(range(1, 7)))
        self.assertFalse(any(ci_pool.priority_slot(dict(self.config, ci_kinds=["staging"]), i) for i in range(8)))

    def test_observation_needs_a_pr_host_with_a_shared_state_credential(self):
        with patch.object(ci_pool, "reservations", return_value=dict(queued=[])) as read:
            self.assertIsNone(ci_pool.priority_observation(dict(self.config)))
            self.assertIsNone(ci_pool.priority_observation(dict(self.config, ci_kinds=["staging"], github_state_token_file="t")))
            self.assertEqual(ci_pool.priority_observation(dict(self.config, github_state_token_file="t")), dict(queued=[]))
        read.assert_called_once()

    def test_idle_priority_only_guest_is_surplus_after_its_grace(self):
        now = 1000.0
        value = dict(ci_priority_only=True, online_observed_at=now - ci_pool.PRIORITY_IDLE_SECONDS)
        online = dict(status="online", busy=False)
        self.assertTrue(ci_pool.priority_surplus(value, online, now))
        self.assertFalse(ci_pool.priority_surplus(value, online, now - 1))
        self.assertFalse(ci_pool.priority_surplus(dict(value, busy=True), online, now))
        self.assertFalse(ci_pool.priority_surplus(value, dict(online, busy=True), now))
        self.assertFalse(ci_pool.priority_surplus(value, dict(online, status="offline"), now))
        self.assertFalse(ci_pool.priority_surplus(dict(value, ci_priority_only=False), online, now))
        self.assertFalse(ci_pool.priority_surplus(dict(ci_priority_only=True), online, now))

    def test_slot_start_requests_priority_only_unless_recovering_or_recently_retired(self):
        config = dict(self.config, ci_vcpus=16, ci_memory_gib=32)
        with patch.object(vm, "start", return_value={}) as start:
            slot = host.CISlot(2)
            slot.step(config, inventory=[], refresh=False, cooling=False, admitted=True, priority_only=True)
            self.assertTrue(start.call_args.args[0]["ci_priority_only"])
            slot.next_vm = 0
            slot.step(config, inventory=[], refresh=False, cooling=False, admitted=True)
            self.assertNotIn("ci_priority_only", start.call_args.args[0])
            slot.next_vm, slot.priority_retired_at = 0, time.time()
            slot.step(config, inventory=[], refresh=False, cooling=False, admitted=True, priority_only=True)
            self.assertNotIn("ci_priority_only", start.call_args.args[0])
            recovery = dict(instance="a" * 16, ci_kinds=["pr"])
            host.CISlot(3).step(config, inventory=[], refresh=False, cooling=False, admitted=True,
                                recovery=recovery, priority_only=True)
            self.assertNotIn("ci_priority_only", start.call_args.args[0])

    def test_local_labels_match_the_broker(self):
        config = dict(machine_id="a" * 32, ci_kinds=["pr"])
        name = "fleet-aaaaaaaaaaaa-" + "b" * 16
        self.assertEqual(github.runner_labels(config, name)[-2:], ["tauceti-fleet-pr", "tauceti-fleet-pr-priority"])
        self.assertEqual(github.runner_labels(dict(config, ci_priority_only=True), name)[-1:], ["tauceti-fleet-pr-priority"])
        self.assertNotIn("tauceti-fleet-pr", github.runner_labels(dict(config, ci_priority_only=True), name))
        self.assertNotIn("tauceti-fleet-pr-priority", github.runner_labels(dict(config, ci_kinds=["staging"]), name))

