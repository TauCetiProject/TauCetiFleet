import os
import tempfile
import unittest
from unittest.mock import patch

from tauceti_fleet import broker, capacity, ci_autoscale, ci_pool, ci_priority, github, native_runner, store, vm
from tests.test_nightly_runner_policy import harness_api


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
            capacity.protected_slots(dict(self.config, ci_broker_url="https://broker",
                                          ci_protected_slots={"0": dict(kind="control", runner_group_id=3)}))

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

class InfraCI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_STATE": temporary.name})
        env.start()
        self.addCleanup(env.stop)
        # A PR host with two infra slots added above its PR slots.
        self.config = dict(ci_enabled=True, ci_max_jobs=5, ci_staging_slots=0, ci_idle_pool=True,
                           ci_kinds=["pr"], runner_group_id=3,
                           ci_protected_slots={"3": dict(kind="infra", runner_group_id=8),
                                               "4": dict(kind="infra", runner_group_id=8)})

    def test_infra_slots_are_native_protected_and_below_pr_priority(self):
        infra = capacity.for_slot(self.config, 3)
        self.assertEqual(capacity.kinds(infra), ["infra"])
        self.assertEqual(infra["ci_slot_runner_group_id"], 8)
        # Infra runs on the native-disk backend like PR slots, never in a VM.
        self.assertTrue(native_runner.selected(infra))
        self.assertTrue(native_runner.selected(capacity.for_slot(self.config, 0)))
        # A full PR backlog cannot take infra slots, and infra never takes PR slots.
        busy = [dict(slot=i, runner_id=i + 100, busy=True) for i in range(3)]
        self.assertEqual(ci_pool.plan(self.config, busy, [], dict(queued=[])), set(range(5)))
        self.assertEqual(ci_pool.recovery_slots(self.config, busy[:2], [], dict(queued=[["tauceti-fleet-pr"]]),
                         [dict(instance="a" * 16, ci_kinds=["infra"], labels=["tauceti-fleet-pr"])]), {})
        infra["unit"] = "infra-guest"
        with patch.object(ci_priority, "run") as run, patch.object(github, "ci_read") as metadata:
            ci_priority.classify(self.config, infra, 100)
        metadata.assert_not_called()
        self.assertEqual(infra["ci_job_kind"], "infra")
        self.assertFalse(ci_priority.staging(infra))
        self.assertEqual(infra["cpu_weight"], capacity.INFRA_CPU_WEIGHT)
        self.assertLess(capacity.INFRA_CPU_WEIGHT, 100)
        run.assert_called_once_with(["systemctl", "--user", "set-property", "--runtime", "infra-guest",
                                     "CPUWeight=" + str(capacity.INFRA_CPU_WEIGHT)])

    def test_infra_requires_the_broker_and_its_results_stay_with_github(self):
        with self.assertRaises(store.FleetError):
            github.protected_policy(dict(ci_kinds=["infra"], ci_slot_runner_group_id=8), {})
        from tauceti_fleet import host
        store.atomic(store.state_dir() / "pending-ci.json",
                     [dict(run_id=1, runner_id=2, instance="c" * 16, ci_kinds=["infra"], started_at=__import__("time").time())])
        with patch.object(github, "ci_read") as metadata:
            host.collect_ci_results(dict(self.config, machine_id="a" * 32))
        metadata.assert_not_called()
        self.assertEqual(store.read(store.state_dir() / "pending-ci.json", None), [])


class InfraSlotsCommand(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + k: os.path.join(root, k) for k in ("STATE", "CONFIG", "DATA")})
        env.start()
        self.addCleanup(env.stop)
        store.update_host(machine_id="a" * 32, ci_enabled=True, workers_enabled=False, ci_allowed=True, max_workers=0,
                          ci_kinds=["pr"], ci_max_jobs=15, ci_broker_url="https://fleet.example")
        self.policy = dict(runner_group_id=3, groups={"queue-control": 6, "pr-status": 7, "infra": 8}, kinds=["pr", "infra"])

    def run_command(self, count, policy=None):
        from tauceti_fleet import host
        with patch.object(github, "capacity_policy", return_value=policy or self.policy), \
                patch.object(native_runner, "prepare_for_ci") as prepare:
            result = host.rpc(dict(action="infra-slots", count=count))
        return result, prepare

    def test_infra_slots_go_above_the_pr_slots_and_can_change_live(self):
        result, prepare = self.run_command(8)
        self.assertEqual(result, dict(infra_slots=list(range(15, 23)), pr_slots=15, ci_max_jobs=23, ci_enabled=True))
        config = store.host_config()
        self.assertEqual(config["ci_protected_slots"]["22"], dict(kind="infra", runner_group_id=8))
        prepare.assert_called_once()
        self.assertEqual(self.run_command(2)[0]["infra_slots"], [15, 16])
        self.assertEqual(self.run_command(0)[0], dict(infra_slots=[], pr_slots=15, ci_max_jobs=15, ci_enabled=True))
        self.assertEqual(store.host_config()["ci_protected_slots"], {})

    def test_infra_slots_require_the_broker_class_and_keep_other_protected_slots_below(self):
        with self.assertRaises(store.FleetError):
            self.run_command(4, dict(self.policy, kinds=["pr"]))
        with self.assertRaises(store.FleetError):
            self.run_command(4, dict(self.policy, groups={"queue-control": 6}))
        self.assertEqual(store.host_config()["ci_max_jobs"], 15)
        store.update_host(ci_protected_slots={"14": dict(kind="pr-status", runner_group_id=7)})
        self.policy["kinds"].append("pr-status")
        self.assertEqual(self.run_command(2)[0]["infra_slots"], [15, 16])
        self.assertEqual(store.host_config()["ci_protected_slots"]["14"]["kind"], "pr-status")


class PRStatusCI(unittest.TestCase):
    def test_status_slots_cannot_be_borrowed_by_builds_or_queue_finalizers(self):
        config = dict(ci_enabled=True, ci_max_jobs=4, ci_staging_slots=1,
                      ci_idle_pool=True, ci_kinds=["pr", "staging"], runner_group_id=3,
                      ci_protected_slots={"3": dict(kind="pr-status", runner_group_id=7)})
        status = capacity.for_slot(config, 3)
        status["unit"] = "status-guest"
        self.assertEqual(capacity.kinds(status), ["pr-status"])
        self.assertEqual(capacity.guest_shape(status), dict(vcpus=2, memory_gib=4))
        labels = github.runner_labels(dict(status, machine_id="a"*32), "fleet-"+"b"*16)
        self.assertIn("tauceti-fleet-pr-status", labels)
        for label in ("tauceti-fleet-pr", "tauceti-fleet-staging", "tauceti-fleet-queue-control"):
            self.assertNotIn(label, labels)
        busy = [dict(slot=i, runner_id=i+100, busy=True) for i in range(3)]
        self.assertEqual(ci_pool.plan(config, busy, [], None), set(range(4)))
        self.assertEqual(ci_pool.recovery_slots(config, busy, [], dict(queued=[]),
                         [dict(instance="a"*16, ci_kinds=["pr"])]), {})
        self.assertFalse(native_runner.selected(status))
        with patch.object(vm, "start_memory_available", return_value=6*1024**3):
            self.assertTrue(vm.start_memory_ok(status))
        with patch.object(ci_priority, "run"), patch.object(github, "ci_read") as metadata:
            ci_priority.classify(config, status, 100)
        metadata.assert_not_called()
        self.assertEqual(status["cpu_weight"], 10000)

    def test_status_group_never_accepts_candidate_or_other_control_workflows(self):
        config = dict(ci_kinds=["pr-status"], ci_slot_runner_group_id=7)
        workflows = ["TauCetiProject/TauCeti/.github/workflows/"+name+"@refs/heads/main"
                     for name in ("pr-status.yml", "pr-labels.yml")]
        group = dict(name="TauCetiPRStatus", visibility="selected", restricted_to_workflows=True,
                     selected_workflows=workflows)
        repos = dict(total_count=1, repositories=[dict(full_name="TauCetiProject/TauCeti")])
        with patch.object(github, "request", return_value=repos):
            self.assertEqual(github.protected_policy(config, group), 7)
            for invalid in (dict(group, restricted_to_workflows=False),
                            dict(group, selected_workflows=workflows[:1]),
                            dict(group, selected_workflows=workflows+["TauCetiProject/TauCeti/.github/workflows/pr-build.yml@refs/heads/main"]),
                            dict(group, selected_workflows=[w.replace("refs/heads/main", "refs/heads/untrusted") for w in workflows])):
                with self.assertRaises(store.FleetError):
                    github.protected_policy(config, invalid)
        with patch.object(github, "request", return_value=dict(total_count=1, repositories=[dict(full_name="other/repo")])):
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
        with patch.object(github, "request", side_effect=harness_api(repos)):
            self.assertEqual(github.protected_policy(control, group), 6)
            main = ["TauCetiProject/TauCetiTools/.github/workflows/" + name + "@refs/heads/main"
                    for name in ("pr-build.yml", "publish-lake-cache.yml")]
            self.assertEqual(github.protected_policy(control, dict(group, selected_workflows=list(workflows) + main)), 6)
            source = ["TauCetiProject/TauCetiTools/.github/workflows/" + name +
                      "@150b56c3ab743b1f2a120f0639d98586455d7f2a"
                      for name in ("pr-build.yml", "publish-lake-cache.yml")]
            for bad in (dict(group, restricted_to_workflows=False),
                        dict(group, selected_workflows=list(workflows)+["untrusted"]),
                        dict(group, selected_workflows=list(workflows)+[source[0].rsplit("@", 1)[0]+"@refs/heads/other"]),
                        dict(group, selected_workflows=list(workflows)+[main[0].replace("TauCetiProject/", "someone/")]),
                        dict(group, selected_workflows=list(workflows)+[
                            "TauCetiProject/TauCetiTools/.github/workflows/pick-runner.yml@refs/heads/main"]),
                        dict(group, selected_workflows=list(workflows)+main+source),
                        dict(group, selected_workflows=list(workflows)+[
                            "TauCetiProject/TauCetiTools/.github/workflows/pick-runner.yml@abf810bf5064bf549ba76fac568da9d1dbb32362"]),
                        dict(group, selected_workflows=[])):
                with self.assertRaises(store.FleetError):
                    github.protected_policy(control, bad)
        with patch.object(github, "request", return_value=dict(total_count=1, repositories=[dict(full_name="other/repo")])):
            with self.assertRaises(store.FleetError):
                github.protected_policy(control, group)


class BrokerSpecialSlots(unittest.TestCase):
    """A broker-enrolled host keeps its slot map; the broker chooses each kind's group."""

    def setUp(self):
        self.config = dict(ci_enabled=True, ci_max_jobs=15, ci_staging_slots=1, ci_idle_pool=True,
                           ci_kinds=["pr", "staging"], runner_group_id=3, machine_id="7" * 32,
                           ci_broker_url=broker.DEFAULT_URL,
                           ci_protected_slots={"7": dict(kind="queue-control", runner_group_id=6),
                                               "11": dict(kind="pr-status", runner_group_id=7)})
        self.policy = dict(runner_group_id=3, max_jobs=4096, groups={"queue-control": 6, "pr-status": 7},
                           kinds=["pr", "staging", "queue-control", "pr-status"])
        self.groups = dict(groups=self.policy["groups"], kinds=self.policy["kinds"])

    def test_enrolled_host_may_reserve_special_slots(self):
        self.assertEqual(capacity.protected_slots(self.config),
                         {7: dict(kind="queue-control", runner_group_id=6), 11: dict(kind="pr-status", runner_group_id=7)})
        self.assertEqual(capacity.staging_slots(self.config), 1)
        capacity.authorize(self.config, self.policy)

    def test_special_slots_request_their_own_kind_and_never_choose_a_group(self):
        for index, kind in ((7, "queue-control"), (11, "pr-status"), (2, None)):
            slot = capacity.for_slot(self.config, index)
            with patch.object(github, "request", side_effect=AssertionError("No GitHub credential on this host")), \
                    patch.object(broker, "request") as call:
                call.side_effect = lambda config, path, **kw: (
                    self.groups if path == "/v1/groups" else dict(runner_id=9, encoded_jit_config="jit"))
                self.assertEqual(github.jit_config(slot, "fleet-" + "7" * 12 + "-" + "a" * 16), (9, "jit"))
            post = call.call_args
            self.assertEqual(post.args[1], "/v1/runners")
            self.assertEqual(post.kwargs["data"]["slot"], index)
            self.assertEqual(post.kwargs["data"]["kinds"], [kind] if kind else ["pr", "staging"])
            self.assertNotIn("runner_group_id", post.kwargs["data"])
            # Only special slots compare their group with the broker before registering.
            self.assertEqual([c.args[1] for c in call.call_args_list][:-1], ["/v1/groups"] if kind else [])

    def test_a_disallowed_kind_or_different_group_refuses_the_slot(self):
        for policy in (dict(self.policy, kinds=["pr", "staging", "pr-status"]),
                       dict(self.policy, groups={"queue-control": 8, "pr-status": 7}),
                       {k: v for k, v in self.policy.items() if k != "groups"}):
            with self.assertRaisesRegex(store.FleetError, "slot 7"):
                capacity.authorize(self.config, policy)
            slot = capacity.for_slot(self.config, 7)
            with patch.object(broker, "request", return_value=policy) as call:
                with self.assertRaisesRegex(store.FleetError, "slot 7"):
                    github.jit_config(slot, "fleet-" + "7" * 12 + "-" + "b" * 16)
            self.assertEqual([c.args[1] for c in call.call_args_list], ["/v1/groups"])
        # The build slots and the pr-status slot still start under a queue-control mismatch.
        mismatch = dict(self.policy, groups={"queue-control": 8, "pr-status": 7})
        with patch.object(broker, "request", return_value=mismatch):
            self.assertEqual(github.runner_policy(capacity.for_slot(self.config, 11)), 7)
            self.assertEqual(github.runner_policy(capacity.for_slot(self.config, 2)), 3)

    def test_group_3_drift_does_not_stop_special_slots(self):
        def answer(config, path, **kw):
            if path == "/v1/policy":
                raise store.FleetError("CI broker returned HTTP 503")
            return self.groups if path == "/v1/groups" else dict(runner_id=9, encoded_jit_config="jit")

        for index in (7, 11):
            with patch.object(broker, "request", side_effect=answer):
                self.assertEqual(github.jit_config(capacity.for_slot(self.config, index), "fleet-" + "7" * 12 + "-" + "c" * 16),
                                 (9, "jit"))

    def test_direct_credential_hosts_keep_their_own_group_checks(self):
        config = {k: v for k, v in self.config.items() if k != "ci_broker_url"}
        capacity.authorize(config, dict(runner_group_id=3, kinds=["pr", "staging"]))
