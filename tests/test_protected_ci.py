import os
import tempfile
import unittest
from unittest.mock import patch

from tauceti_fleet import capacity, ci_autoscale, ci_pool, ci_priority, github, hostos, native_runner, store, vm

hostos.MACOS = False


class ProtectedCI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_STATE": temporary.name})
        env.start()
        self.addCleanup(env.stop)
        self.config = dict(ci_enabled=True, ci_max_jobs=6, ci_staging_slots=0,
                           ci_idle_pool=True, ci_kinds=["pr"], runner_group_id=3,
                           ci_protected_slots={"0": dict(kind="control", runner_group_id=5),
                                               "1": dict(kind="roadmap", runner_group_id=4)})

    def test_full_tauceti_backlog_cannot_take_protected_slots(self):
        busy = [dict(slot=i, runner_id=i + 100, busy=True) for i in range(2, 6)]
        self.assertEqual(ci_pool.plan(self.config, busy, [], dict(queued=[])), set(range(6)))
        # Starting these fixed-label slots does not depend on TauCeti's discovery.
        self.assertEqual(ci_pool.plan(self.config, busy, [], None), set(range(6)))
        for index, kind, group in ((0, "control", 5), (1, "roadmap", 4)):
            config = capacity.for_slot(self.config, index)
            self.assertEqual(config["ci_slot_runner_group_id"], group)
            self.assertEqual(capacity.for_slot(config, index), config)
            self.assertEqual(config["ci_kinds"], [kind])
            labels = github.runner_labels(dict(config, machine_id="a" * 32), "fleet-" + "b" * 16)
            self.assertIn("tauceti-fleet-" + kind, labels)
            self.assertNotIn("tauceti-fleet-pr", labels)
            self.assertNotIn("tauceti-fleet", labels)
            self.assertFalse(native_runner.selected(config))

    def test_small_slot_does_not_consume_general_spare(self):
        self.assertEqual(ci_pool.plan(self.config, [], [], dict(queued=[])), {0, 1, 2})
        self.assertEqual(ci_pool.plan(dict(self.config, ci_enabled=False), [], [], None), set())

    def test_pr_recovery_cannot_borrow_either_protected_slot(self):
        record = dict(instance="a" * 16, ci_kinds=["pr"])
        busy = [dict(slot=i, runner_id=i + 100, busy=True) for i in range(2, 6)]
        self.assertEqual(ci_pool.recovery_slots(self.config, busy, [], dict(queued=[]), [record]), {})
        busy.pop()
        self.assertEqual(set(ci_pool.recovery_slots(self.config, busy, [], dict(queued=[]), [record])), {5})
        ci_pool.remember(dict(ci_kinds=["control"]))
        self.assertEqual(store.read(store.state_dir() / "ci-idle-retired.json", []), [])
        with patch.object(store, "host_config", return_value=self.config):
            for index in (0, 1):
                with self.assertRaises(store.FleetError):
                    ci_autoscale.host_action(dict(mode="retire", slot=index))

    def test_small_slot_is_small_and_can_boot_under_build_memory_pressure(self):
        control = capacity.for_slot(self.config, 0)
        self.assertEqual(capacity.guest_shape(control), dict(vcpus=2, memory_gib=4))
        roadmap = capacity.for_slot(self.config, 1)
        self.assertEqual(capacity.guest_shape(roadmap), dict(vcpus=16, memory_gib=32))
        with patch.object(vm, "start_memory_available", return_value=6 * 1024**3):
            self.assertTrue(vm.start_memory_ok(control))
            self.assertFalse(vm.start_memory_ok(roadmap))
        with patch.object(vm, "start_memory_available", return_value=6 * 1024**3), patch.object(vm, "_start", return_value={}) as start:
            vm.start(control)
            self.assertEqual(start.call_args.args[0]["ci_slot_runner_group_id"], 5)
            self.assertEqual(start.call_args.args[0]["ci_kinds"], ["control"])
        for kinds in (["control"], ["roadmap"]):
            value = dict(ci_kinds=kinds, unit="guest")
            with patch.object(ci_priority, "run"), patch.object(github, "ci_read") as metadata:
                ci_priority.classify(self.config, value, 100)
            metadata.assert_not_called()
            self.assertEqual(value["cpu_weight"], 10000)

    def test_bad_reservation_cannot_lower_the_capacity_or_change_trust_groups(self):
        for values in ({"6": dict(kind="control", runner_group_id=5)},
                       {"0": dict(kind="pr", runner_group_id=5)},
                       {"0": dict(kind="control", runner_group_id=3)}):
            with self.assertRaises(store.FleetError):
                capacity.staging_slots(dict(self.config, ci_protected_slots=values))
        with self.assertRaises(store.FleetError):
            capacity.staging_slots(dict(self.config, ci_max_jobs=1))
        with self.assertRaises(store.FleetError):
            capacity.protected_slots(dict(self.config, ci_broker_url="https://broker"))

    def test_group_policy_enforces_repository_isolation_without_workflow_pin_edits(self):
        repositories = dict(total_count=1, repositories=[dict(full_name="TauCetiProject/TauCetiRoadmap")])
        for index, name in ((0, "TauCetiControl"), (1, "TauCetiRoadmap")):
            config = capacity.for_slot(self.config, index)
            group = dict(name=name, visibility="selected", restricted_to_workflows=False)
            with patch.object(github, "request", return_value=repositories):
                self.assertEqual(github.protected_policy(config, group), config["ci_slot_runner_group_id"])
                for invalid in (dict(group, name="TauCetiFleet"), dict(group, visibility="all")):
                    with self.assertRaises(store.FleetError):
                        github.protected_policy(config, invalid)
            for invalid in (dict(total_count=2, repositories=[]),
                            dict(total_count=1, repositories=[dict(full_name="TauCetiProject/TauCeti")])):
                with patch.object(github, "request", return_value=invalid):
                    with self.assertRaises(store.FleetError):
                        github.protected_policy(config, group)

class QueueControlCI(unittest.TestCase):
    def setUp(self):
        self.config = dict(ci_enabled=True, ci_max_jobs=15, ci_staging_slots=1,
                           ci_idle_pool=True, ci_kinds=["pr", "staging"], runner_group_id=3,
                           ci_protected_slots={"13": dict(kind="queue-control", runner_group_id=6),
                                               "14": dict(kind="queue-control", runner_group_id=6)})

    def test_reserved_finishing_slots_stay_warm_and_cannot_take_pr_jobs(self):
        busy = [dict(slot=i, runner_id=i+100, busy=True) for i in range(13)]
        self.assertEqual(ci_pool.plan(self.config, busy, [], dict(queued=[])), set(range(15)))
        control = capacity.for_slot(self.config, 13)
        self.assertEqual(capacity.guest_shape(control), dict(vcpus=4, memory_gib=8))
        labels = github.runner_labels(dict(control, machine_id="a"*32), "fleet-"+"b"*16)
        self.assertIn("tauceti-fleet-queue-control", labels)
        self.assertNotIn("tauceti-fleet-pr", labels)
        self.assertNotIn("tauceti-fleet-staging", labels)
        self.assertFalse(native_runner.selected(control))
        self.assertTrue(ci_priority.staging(dict(ci_kinds=["queue-control"])))
        self.assertFalse(ci_priority.staging_build(dict(ci_kinds=["queue-control"])))
        with patch.object(vm, "start_memory_available", return_value=10*1024**3):
            self.assertTrue(vm.start_memory_ok(control))
        with patch.object(vm, "start_memory_available", return_value=8*1024**3):
            self.assertFalse(vm.start_memory_ok(control))

    def test_queue_group_accepts_only_the_trusted_main_workflows_and_repository(self):
        control = capacity.for_slot(self.config, 13)
        workflows = {"TauCetiProject/TauCeti/.github/workflows/"+name+"@refs/heads/main"
                     for name in ("pr-build.yml", "pick-runner.yml", "publish-lake-cache.yml")}
        group = dict(name="TauCetiQueueControl", visibility="selected", restricted_to_workflows=True,
                     selected_workflows=list(workflows))
        repos = dict(total_count=1, repositories=[dict(full_name="TauCetiProject/TauCeti")])
        with patch.object(github, "request", return_value=repos):
            self.assertEqual(github.protected_policy(control, group), 6)
            for bad in (dict(group, restricted_to_workflows=False),
                        dict(group, selected_workflows=list(workflows)+["untrusted"]),
                        dict(group, selected_workflows=[])):
                with self.assertRaises(store.FleetError):
                    github.protected_policy(control, bad)
        with patch.object(github, "request", return_value=dict(total_count=1, repositories=[dict(full_name="other/repo")])):
            with self.assertRaises(store.FleetError):
                github.protected_policy(control, group)
