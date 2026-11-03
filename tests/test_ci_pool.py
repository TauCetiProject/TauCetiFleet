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

    def test_withdrawal_waits_for_new_observation_then_restores_queued_assignment(self):
        value = dict(instance="a", runner_id=100)
        own = dict(labels=[dict(name=n) for n in ("self-hosted", "fleet-instance-a", "tauceti-fleet-pr")])
        observation = dict(observed_at=time.time(), queued=[])
        with patch.object(github, "set_runner_labels") as labels:
            self.assertFalse(ci_pool.withdraw(self.config, value, own, observation, self.root / "vm.json"))
            self.assertEqual(labels.call_args.args[2], ["self-hosted", "fleet-instance-a"])
            self.assertFalse(ci_pool.withdraw(self.config, value, own, observation, self.root / "vm.json"))
            observation["observed_at"] = value["withdrawing_at"] + ci_pool.WITHDRAWAL_SECONDS + 1
            self.assertTrue(ci_pool.withdraw(self.config, value, own, observation, self.root / "vm.json"))
            observation["queued"] = [{"self-hosted", "fleet-instance-a", "tauceti-fleet-pr"}]
            self.assertFalse(ci_pool.withdraw(self.config, value, own, observation, self.root / "vm.json"))
            self.assertEqual(labels.call_args.args[2], value.get("routing_labels", [r["name"] for r in own["labels"]]))
            self.assertNotIn("withdrawing_at", value)
            self.assertNotIn("routing_withdrawn", value)

    def test_idle_delete_rejection_never_stops_vm(self):
        value = dict(slot=2, instance="a", runner_id=100, unit="vm", directory=str(self.root / "vms/a"))
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

    def test_assigned_withdrawing_guest_can_restore_labels_while_admitted(self):
        value = dict(slot=2, instance="a", runner_id=100, unit="vm", directory=str(self.root),
                     started_at=time.time() - 300, withdrawing_at=time.time() - 200)
        store.atomic(vm.slot_file(dict(ci_slot=2), "vm.json"), value)
        inventory = [dict(id=100, status="online", busy=False)]
        with patch.object(host, "ci_service_state", return_value=(True, "healthy")), patch.object(ci_pool, "withdraw", return_value=False) as withdraw:
            host.CISlot(2).step(self.config, inventory=inventory, refresh=True, cooling=False,
                                admitted=True, reservations=dict(queued=[{"fleet-instance-a"}]))
            withdraw.assert_called_once()

    def test_no_observation_never_withdraws(self):
        with patch.object(github, "set_runner_labels") as labels:
            self.assertFalse(ci_pool.withdraw(self.config, {}, {}, None, self.root / "vm.json"))
            labels.assert_not_called()
