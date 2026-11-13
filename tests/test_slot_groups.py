import io
import os
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capabilities, host, remote, slot_groups, store
from tauceti_fleet.cli import main

MIXED = [dict(count=6, agent="auto"), dict(count=2, agent="claude", roles=["review"]),
         dict(count=1, agent="codex", roles=["progress"])]


class SlotGroups(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        environ = patch.dict(os.environ, {"TAUCETI_FLEET_CONFIG": str(root / "config"),
                                          "TAUCETI_FLEET_STATE": str(root / "state"),
                                          "TAUCETI_FLEET_DATA": str(root / "data")})
        environ.start()
        self.addCleanup(environ.stop)
        service = patch.object(host, "install_service")
        service.start()
        self.addCleanup(service.stop)
        host.configure(dict(claude_billing="subscription", github_account="operator", ci_allowed=False))

    def definitions(self):
        """Write the definitions as the controller does: (admission order, worker tables by id number)."""
        ids = host.worker_config(store.host_config())
        workers = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
        self.assertEqual([w["id"].rsplit("-", 1)[1] for w in workers], [str(n + 1) for n in range(len(workers))])
        return [int(wid.rsplit("-", 1)[1]) for wid in ids], {n + 1: w for n, w in enumerate(workers)}

    def shape(self, workers):
        return {n: (w["agent"], " ".join(w.get("only", []))) for n, w in workers.items()}

    def daemon_pass(self):
        """One controller iteration; returns the admission policy it published."""
        return self.daemon_passes()[0]

    def daemon_passes(self, *between):
        """Controller iterations, each of `between` run before the next; the admission policy of each."""
        handlers, written, steps, sleeps = {}, [], list(between), []
        atomic = store.atomic

        def record(path, value):
            if Path(path).name == "admission.json":
                written.append(value)
            atomic(path, value)

        def sleep(_):
            # A pass ends in ten half-second sleeps.
            sleeps.append(1)
            if len(sleeps) % 10:
                return
            if steps:
                steps.pop(0)()
            else:
                handlers[signal.SIGTERM]()

        gib = 1024**3
        with (
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
            patch.object(host.time, "sleep", side_effect=sleep),
            patch.object(host, "cpu_sample", return_value=(1, 1)),
            patch.object(host, "resources", return_value=dict(cpus=8, memory_total=64 * gib, memory_available=64 * gib)),
            patch.object(host, "worker_memory", return_value=None),
            patch.object(host, "start_workers"),
            patch.object(host, "atomic", side_effect=record),
        ):
            host.daemon()
        return [policy for policy in written if policy["reason"] != "controller stopped"]

    def test_profile_groups_become_per_worker_agent_and_phases(self):
        store.atomic(store.config_dir() / "profiles.json", {
            "mixed": dict(github_account="operator", claude_billing="subscription", slots=MIXED),
            "plain": dict(github_account="operator", claude_billing="subscription"),
        })
        with patch.object(remote, "run", return_value=subprocess.CompletedProcess([], 1, stdout="")):
            self.assertNotIn("slots", remote.profile("plain"))
            host.configure(dict(remote.profile("mixed"), profile="mixed", ci_allowed=False))
        order, workers = self.definitions()
        self.assertEqual(self.shape(workers), {
            **{n: ("auto", "") for n in range(1, 7)}, 7: ("claude", "review"), 8: ("claude", "review"), 9: ("codex", "progress"),
        })
        for worker in workers.values():
            self.assertEqual((worker["github_account"], worker["claude_billing"]), ("operator", "subscription"))
        # Each group is spread through the admission order, so the default ceiling of 8 drops one of the six.
        self.assertEqual(order, [1, 2, 7, 3, 9, 4, 5, 8, 6])
        self.assertEqual({w.rsplit("-", 1)[1] for w in host.maintenance_workers()}, set("789"))
        with (
            patch.object(capabilities, "resources", return_value=dict(cpus=8, memory_total=64 * 1024**3, memory_available=64 * 1024**3)),
            patch.object(capabilities, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")),
            patch.object(capabilities, "executable", return_value=None),
            patch.object(capabilities.os, "open", side_effect=PermissionError),
        ):
            checks = {c["name"] for c in capabilities.doctor(store.host_config(), probe=False)["checks"]}
            plain = {c["name"] for c in capabilities.doctor(dict(store.host_config(), slots=None), probe=False)["checks"]}
        self.assertLessEqual({"worker-tool-claude", "worker-tool-codex"}, checks)
        self.assertNotIn("worker-tool-codex", plain)

    def test_every_ceiling_admits_each_group_in_proportion(self):
        for counts in ([6, 2, 1], [40, 8, 2], [1, 1], [3], [5, 5, 5, 1], [8, 1, 1, 1], [1, 1, 1, 5]):
            placed = [group for group, count in enumerate(counts) for _ in range(count)]
            order = slot_groups.order(placed + [None, None])
            self.assertEqual(sorted(order), list(range(sum(counts))))
            for ceiling in range(sum(counts) + 1):
                admitted = [placed[position] for position in order[:ceiling]]
                for group, count in enumerate(counts):
                    self.assertLess(abs(admitted.count(group) - ceiling * count / sum(counts)), 1, (counts, ceiling))
                    members = [position for position in order[:ceiling] if placed[position] == group]
                    self.assertEqual(members, [p for p in range(len(placed)) if placed[p] == group][:len(members)])

    def test_the_largest_shapes_order_within_the_bound_in_well_under_a_lease(self):
        for counts in ((4067,) + (1,) * 29, (1,) * 2048 + (2048,), (1, 1, 1, 4093), tuple(range(1, 91))):
            slot_groups.sequence.cache_clear()
            began = time.monotonic()
            sequence = slot_groups.sequence(counts)
            self.assertLess(time.monotonic() - began, 5, counts[:3])
            total, held = sum(counts), [0] * len(counts)
            for prefix, group in enumerate(sequence, 1):
                held[group] += 1
                checked = range(len(counts)) if len(counts) <= 100 else (group, len(counts) - 1)
                for g in checked:
                    self.assertLessEqual(prefix * counts[g] // total, held[g])
                    self.assertLessEqual(held[g], -(-prefix * counts[g] // total))

    def test_an_edit_rewrites_only_the_definitions_that_change_group(self):
        store.update_host(workers_enabled=True, max_workers=16)
        path = store.config_dir() / "workers.toml"

        def edit(*groups):
            before = path.read_text().split("\n\n") if path.exists() else []
            result = host.rpc(dict(action="slots", groups=[slot_groups.parse(group) for group in groups]))
            order, workers = self.definitions()
            after = path.read_text().split("\n\n")
            changed = {n for n, (old, new) in enumerate(zip(before, after, strict=False)) if old != new}
            return result, order, self.shape(workers), changed

        _, order, shape, _ = edit("6:auto", "2:claude:review")
        self.assertEqual(sorted(order), list(range(1, 9)))
        _, order, shape, changed = edit("4:auto", "4:claude:review")
        self.assertEqual(changed, {5, 6})
        self.assertEqual([n for n, own in shape.items() if own == ("claude", "review")], [5, 6, 7, 8])
        # A smaller group gives up admissions and keeps its definitions.
        result, order, shape, changed = edit("4:auto", "2:claude:review")
        self.assertEqual((changed, sorted(order), len(shape)), (set(), [1, 2, 3, 4, 5, 6], 8))
        self.assertEqual([[w.rsplit("-", 1)[1] for w in group["workers"]] for group in result["slots"]],
                         [list("1234"), list("56")])
        # A new group takes the idle definitions before any is added.
        _, order, shape, changed = edit("4:auto", "2:claude:review", "3:codex:progress")
        self.assertEqual((changed, sorted(order), len(shape)), ({7, 8}, list(range(1, 10)), 9))
        self.assertEqual([shape[n] for n in (7, 8, 9)], [("codex", "progress")] * 3)
        _, order, shape, changed = edit("2:claude:review", "4:auto", "3:codex:progress")
        self.assertEqual((changed, len(order)), (set(), 9))
        self.assertIsNone(host.rpc(dict(action="slots", clear=True))["slots"])
        order, workers = self.definitions()
        self.assertEqual((order, set(self.shape(workers).values())), (list(range(1, 17)), {("claude", "")}))

    def test_rejected_groups_leave_the_saved_ones_and_a_live_profile_edit_needs_no_drain(self):
        host.configure(dict(slots=MIXED, author_effort=None))
        saved = store.host_config()["slots"]
        self.assertEqual(saved, MIXED)
        rejected = [
            {}, [], [[]], [dict(agent="claude")], [dict(count=0, agent="claude")], [dict(count=True)],
            [dict(count=1, agent="kiro")], [dict(count=1, agent=["claude"])], [dict(count=1, roles="review")],
            [dict(count=1, roles=["security-audit"])], [dict(count=1, model="m")], [dict(count=4000), dict(count=97)],
        ]
        for groups in rejected:
            with self.assertRaises(store.FleetError, msg=groups):
                host.rpc(dict(action="slots", groups=groups))
            with self.assertRaises(store.FleetError, msg=groups):
                host.configure(dict(slots=groups))
        # The profile's other keys are held to every group, in either order of arrival.
        for change in (dict(claude_billing=None), dict(author_model="model")):
            with self.assertRaises(store.FleetError, msg=change):
                host.configure(change)
        self.assertEqual(store.host_config()["slots"], saved)
        self.assertNotIn("author_model", store.host_config())
        store.update_host(workers_enabled=True)
        with self.assertRaisesRegex(store.FleetError, "Drain"):
            host.configure(dict(slots=MIXED, sandbox="bubble"))
        host.configure(dict(slots=[dict(count=2, roles=["review", "roadmap", "rebase"])]))
        self.assertEqual(store.host_config()["slots"], [dict(count=2, roles=["rebase", "review", "roadmap"])])
        _, workers = self.definitions()
        self.assertEqual(set(self.shape(workers).values()), {("claude", "rebase review roadmap")})
        host.configure(dict(slots=None))
        self.assertEqual(host.rpc(dict(action="slots")), dict(slots=None, max_workers=8))

    def test_a_definition_outside_every_group_follows_the_profile_and_stays_loadable(self):
        host.configure(dict(slots=[dict(count=1, agent="claude"), dict(count=1, agent="codex")]))
        store.update_host(workers_enabled=True)
        self.definitions()
        host.rpc(dict(action="slots", groups=[dict(count=1, agent="claude")]))
        store.update_host(workers_enabled=False)
        host.configure(dict(claude_billing=None))
        store.update_host(workers_enabled=True)
        order, workers = self.definitions()
        self.assertEqual(order, [1])
        self.assertEqual({n: (w["agent"], w["claude_billing"], w["budget"]) for n, w in workers.items()},
                         {1: ("claude", "api", True), 2: ("claude", "api", True)})

    def test_controller_admits_groups_in_proportion_and_focuses_only_authoring_slots(self):
        host.configure(dict(slots=MIXED))
        store.update_host(workers_enabled=True, max_workers=5, roadmap_focus=dict(percent=100, areas=["PDE"]))

        def numbers(ids):
            return [int(wid.rsplit("-", 1)[1]) for wid in ids]

        policy = self.daemon_pass()
        self.assertEqual(numbers(policy["workers"]), [1, 2, 7, 3, 9])
        self.assertEqual(numbers(policy["roadmap_focus"]["workers"]), [1, 2, 3])
        store.update_host(max_workers=32)
        self.assertEqual(len(self.daemon_pass()["workers"]), 9)
        self.assertEqual(host.rpc(dict(action="slots"))["slots"][0]["count"], 6)
        store.update_host(slots=None, max_workers=3)
        # Every definition changed agent or phases, so each waits until it runs the new one.
        self.assertEqual(self.daemon_pass()["workers"], [])
        prefix = "fleet-" + store.host_config()["machine_id"][:8] + "-"
        for n in range(1, 10):
            store.atomic(store.state_dir() / "workers" / f"{prefix}{n}.json", dict(alive=True, applied_spec=dict(agent="claude")))
        policy = self.daemon_pass()
        self.assertEqual(numbers(policy["workers"]), [1, 2, 3])
        self.assertEqual(policy["roadmap_focus"]["workers"], policy["workers"])

    def test_progress_settings_reach_only_their_group_and_the_source_reaches_every_worker(self):
        reports = dict(count=1, agent="codex", roles=["progress"], progress_strategy="threshold",
                       progress_max_open=3, progress_skip=["Sandbox", "Drafts", "Sandbox"])
        source = dict(progress_repo="operator/TauCetiProgress", progress_ref="0123456789abcdef" * 2 + "01234567")
        store.atomic(store.config_dir() / "profiles.json", {
            "reports": dict(github_account="operator", claude_billing="subscription", env={"A": "1"},
                            slots=[dict(count=2, agent="auto"), reports], **source),
            "plain": dict(github_account="operator", claude_billing="subscription"),
        })
        with patch.object(remote, "run", return_value=subprocess.CompletedProcess([], 1, stdout="")):
            host.configure(dict(remote.profile("reports"), profile="reports", ci_allowed=False))
            plain = remote.profile("plain")
        _, workers = self.definitions()
        own = {n: {k: v for k, v in w.items() if k.startswith("progress_")} for n, w in workers.items()}
        self.assertEqual(own, {1: {}, 2: {}, 3: dict(progress_strategy="threshold", progress_max_open=3,
                                                      progress_skip=["Drafts", "Sandbox"])})
        for worker in workers.values():
            self.assertEqual(worker["env"], {"A": "1", "TAUCETI_PROGRESS_REPO": source["progress_repo"],
                                             "TAUCETI_PROGRESS_REF": source["progress_ref"]})
        store.update_host(workers_enabled=True)
        path = store.config_dir() / "workers.toml"
        before = path.read_text().split("\n\n")
        edited = "1:codex:progress:progress_strategy=threshold:progress_max_open=3:progress_skip=Drafts"
        result = host.rpc(dict(action="slots", groups=[slot_groups.parse("2:auto"), slot_groups.parse(edited)]))
        self.assertEqual(result["slots"][1]["progress_skip"], ["Drafts"])
        _, workers = self.definitions()
        after = path.read_text().split("\n\n")
        self.assertEqual([n for n, (old, new) in enumerate(zip(before, after, strict=True)) if old != new], [3])
        self.assertEqual(workers[3]["progress_skip"], ["Drafts"])
        with self.assertRaisesRegex(store.FleetError, "Drain"):
            host.configure(dict(progress_ref="f" * 40))
        store.update_host(workers_enabled=False)
        # A profile without the two keys sends them empty, which returns the host to the worker's pin.
        host.configure(dict(plain, slots=None))
        _, workers = self.definitions()
        self.assertFalse([k for w in workers.values() for k in w if k == "env" or k.startswith("progress_")])

    def test_rejected_progress_settings(self):
        def group(**settings):
            return dict(slots=[dict(count=1, **settings)])

        rejected = [
            group(roles=["review"], progress_strategy="threshold"), group(roles=["review", "fix"], progress_skip=["PDE"]),
            group(roles=["roadmap"], progress_max_open=1), group(progress_strategy="due"), group(progress_strategy=["threshold"]),
            group(progress_max_open=0), group(progress_max_open=True), group(progress_max_open="3"),
            group(progress_skip="PDE"), group(progress_skip=["a/b"]), group(progress_skip=[3]), group(progress_poll=60),
            dict(progress_repo="TauCetiProgress"), dict(progress_repo=["a/b"]), dict(progress_ref="main"),
            dict(progress_ref="0123abc"), dict(progress_ref="f" * 40, env={"TAUCETI_PROGRESS_REF": "e" * 40}),
        ]
        for shape in rejected:
            with self.assertRaises(store.FleetError, msg=shape):
                host.configure(shape)
        self.assertFalse({"slots", "progress_repo", "progress_ref", "env"} & set(store.host_config()))
        host.configure(group(progress_strategy="threshold", roles=[]))
        self.assertEqual(store.host_config()["slots"], [dict(count=1, progress_strategy="threshold")])
        for text in ("1:codex:progress:review", "1:codex:progress:progress_poll=60", "1:codex:progress:progress_skip=A:progress_skip=B"):
            with self.assertRaises(store.FleetError, msg=text):
                slot_groups.parse(text)
        self.assertEqual(slot_groups.parse("1::progress_strategy=threshold:progress_max_open=x"),
                         dict(count=1, progress_strategy="threshold", progress_max_open="x"))

    def test_a_reassigned_worker_waits_for_its_new_definition_before_admission(self):
        store.update_host(workers_enabled=True, max_workers=2)
        prefix = "fleet-" + store.host_config()["machine_id"][:8] + "-"

        def running(n, agent, only, alive=True):
            store.atomic(store.state_dir() / "workers" / f"{prefix}{n}.json", dict(
                id=f"{prefix}{n}", alive=alive, state="waiting-capacity", agent=agent, only=only,
                applied_spec=dict(id=f"{prefix}{n}", enabled=True, agent=agent, only=only)))

        def numbers(policy):
            return [int(wid.rsplit("-", 1)[1]) for wid in policy["workers"]]

        def admitted():
            return numbers(self.daemon_pass())

        host.rpc(dict(action="slots", groups=[slot_groups.parse("2:claude:review")]))
        running(1, "claude", ["review"])
        running(2, "claude", ["review"])
        self.assertEqual(admitted(), [1, 2])
        host.rpc(dict(action="slots", groups=[slot_groups.parse("1:claude:review")]))
        self.assertEqual(admitted(), [1])
        # Worker 2 is rewritten for the new group while its old loop still polls for admission.
        host.rpc(dict(action="slots", groups=[slot_groups.parse("1:claude:review"), slot_groups.parse("1:codex:progress")]))
        self.assertEqual(self.shape(self.definitions()[1])[2], ("codex", "progress"))
        self.assertEqual(admitted(), [1])
        self.assertEqual(host.rpc(dict(action="slots"))["slots"][1]["admitted"], 0)
        running(2, "codex", ["progress"])
        self.assertEqual(admitted(), [1, 2])
        # Clearing the groups rewrites both definitions; each waits until it runs the profile's.
        host.rpc(dict(action="slots", clear=True))
        self.assertEqual(admitted(), [])
        running(1, "claude", [])
        self.assertEqual(admitted(), [1])
        # A manager pass that read the old definitions can still launch them after the rewrite.
        running(2, "codex", ["progress"], alive=False)
        policies = self.daemon_passes(lambda: running(2, "codex", ["progress"]), lambda: running(2, "claude", []))
        self.assertEqual([numbers(policy) for policy in policies], [[1], [1], [1, 2]])

    def test_an_auto_full_rotation_worker_is_held_when_reassigned(self):
        store.update_host(workers_enabled=True, max_workers=2)
        prefix = "fleet-" + store.host_config()["machine_id"][:8] + "-"

        def running(n, agent, only):
            # The worker's applied_spec leaves out agent "auto" and an empty phase list.
            applied = dict(id=f"{prefix}{n}", enabled=True, **({} if agent == "auto" else dict(agent=agent)),
                           **(dict(only=only) if only else {}))
            store.atomic(store.state_dir() / "workers" / f"{prefix}{n}.json", dict(
                id=f"{prefix}{n}", alive=True, state="waiting-capacity", agent=agent, only=only, applied_spec=applied))

        def admitted():
            return [int(wid.rsplit("-", 1)[1]) for wid in self.daemon_pass()["workers"]]

        host.rpc(dict(action="slots", groups=[slot_groups.parse("2:auto")]))
        self.assertEqual(admitted(), [1, 2])
        running(1, "auto", [])
        running(2, "auto", [])
        self.assertEqual(admitted(), [1, 2])
        host.rpc(dict(action="slots", groups=[slot_groups.parse("1:auto")]))
        self.assertEqual(admitted(), [1])
        host.rpc(dict(action="slots", groups=[slot_groups.parse("1:auto"), slot_groups.parse("1:codex:progress")]))
        self.assertEqual(admitted(), [1])
        self.assertEqual(admitted(), [1])
        running(2, "codex", ["progress"])
        self.assertEqual(admitted(), [1, 2])

    def test_a_hold_recorded_while_holds_are_acknowledged_is_kept(self):
        store.update_host(workers_enabled=True, max_workers=2)
        host.rpc(dict(action="slots", groups=[slot_groups.parse("2:claude:review")]))
        self.definitions()
        host.rpc(dict(action="slots", groups=[slot_groups.parse("1:claude:review"), slot_groups.parse("1:codex:progress")]))
        acknowledging, writing = threading.Event(), threading.Event()
        status = host.read

        def paused(path, default=None):
            # The acknowledgment stops after its first status read until enable() is under way.
            if Path(path).parent.name == "workers" and not acknowledging.is_set():
                acknowledging.set()
                writing.wait(5)
                time.sleep(0.3)
            return status(path, default)

        def enable():
            acknowledging.wait(5)
            writing.set()
            host.enable(workers=True)

        writer = threading.Thread(target=enable)
        with (
            patch.object(host, "read", side_effect=paused),
            patch.object(host, "doctor", return_value=dict(readiness=dict(ci="ready", workers="ready"), checks=[])),
            patch.object(host, "write_slices"),
            patch.object(host, "resources", return_value={}),
        ):
            writer.start()
            host.unapplied_workers()
            writer.join(10)
        self.assertEqual(self.shape(self.definitions()[1])[2], ("codex", "progress"))
        self.assertEqual([wid.rsplit("-", 1)[1] for wid in host.unapplied_workers()], ["2"])

    def worker_status(self, n, agent, only):
        prefix = "fleet-" + store.host_config()["machine_id"][:8] + "-"
        store.atomic(store.state_dir() / "workers" / f"{prefix}{n}.json", dict(
            id=f"{prefix}{n}", alive=True, state="waiting-capacity", agent=agent, only=only,
            applied_spec=dict(id=f"{prefix}{n}", enabled=True, agent=agent, only=only)))

    def ready(self):
        return (
            patch.object(host, "doctor", return_value=dict(readiness=dict(ci="ready", workers="ready"), checks=[])),
            patch.object(host, "write_slices"),
            patch.object(host, "resources", return_value={}),
        )

    def test_an_enable_that_read_older_groups_writes_the_saved_ones(self):
        store.update_host(workers_enabled=True, max_workers=2)
        host.rpc(dict(action="slots", groups=[slot_groups.parse("2:claude:review")]))
        self.worker_status(1, "claude", ["review"])
        self.worker_status(2, "claude", ["review"])
        self.daemon_pass()
        ready, slices, resources = self.ready()

        def readiness(config):
            # A newer edit is saved and applied while enable() is in its readiness checks.
            host.rpc(dict(action="slots", groups=[slot_groups.parse("1:claude:review"), slot_groups.parse("1:codex:progress")]))
            self.daemon_pass()
            return dict(readiness=dict(ci="ready", workers="ready"), checks=[])

        with patch.object(host, "doctor", side_effect=readiness), slices, resources:
            host.enable(workers=True)
        with open(store.config_dir() / "workers.toml", "rb") as stream:
            self.assertEqual(self.shape({n + 1: w for n, w in enumerate(tomllib.load(stream)["workers"])})[2], ("codex", "progress"))
        self.assertEqual([wid.rsplit("-", 1)[1] for wid in self.daemon_pass()["workers"]], ["1"])

    def test_a_definition_written_beside_a_publication_is_not_admitted(self):
        store.update_host(workers_enabled=True, max_workers=2)
        host.rpc(dict(action="slots", groups=[slot_groups.parse("2:claude:review")]))
        self.worker_status(1, "claude", ["review"])
        self.worker_status(2, "claude", ["review"])
        self.daemon_pass()
        publishing, writing = threading.Event(), threading.Event()
        policy = host.focus.policy

        def paused(config, workers):
            # Between the controller's acknowledgment and its publication.
            if not publishing.is_set():
                publishing.set()
                writing.wait(5)
                time.sleep(0.3)
            return policy(config, workers)

        def edit():
            publishing.wait(5)
            host.rpc(dict(action="slots", groups=[slot_groups.parse("1:claude:review"), slot_groups.parse("1:codex:progress")]))
            writing.set()
            host.enable(workers=True)

        def numbers(ids):
            return [wid.rsplit("-", 1)[1] for wid in ids]

        between = []

        def settled():
            writer.join(10)
            between.append(store.read(store.state_dir() / "admission.json")["workers"])

        writer = threading.Thread(target=edit)
        ready, slices, resources = self.ready()
        with patch.object(host.focus, "policy", side_effect=paused), ready, slices, resources:
            writer.start()
            policies = self.daemon_passes(settled)
        self.assertEqual(numbers(host.unapplied_workers()), ["2"])
        self.assertEqual([numbers(ids) for ids in between + [policies[1]["workers"]]], [["1"], ["1"]])

    def test_a_running_controller_restores_the_saved_layout_after_another_writer(self):
        store.update_host(workers_enabled=True, max_workers=2)
        first = [slot_groups.parse("2:claude:review")]
        host.rpc(dict(action="slots", groups=first))
        self.worker_status(1, "claude", ["review"])
        self.worker_status(2, "claude", ["review"])
        ready, slices, resources = self.ready()

        def detour():
            host.rpc(dict(action="slots", groups=[slot_groups.parse("1:claude:review"), slot_groups.parse("1:codex:progress")]))
            with ready, slices, resources:
                host.enable(workers=True)
            host.rpc(dict(action="slots", groups=first))
            # The manager consumed the definition enable() wrote.
            self.worker_status(2, "codex", ["progress"])

        policies = self.daemon_passes(detour, lambda: None)
        _, workers = self.definitions()
        self.assertEqual(self.shape(workers)[2], ("claude", "review"))
        self.assertEqual([[wid.rsplit("-", 1)[1] for wid in p["workers"]] for p in policies], [["1", "2"], ["1"], ["1"], ["1"]])

    def test_groups_are_checked_against_the_profile_they_are_saved_into(self):
        host.rpc(dict(action="slots", groups=[slot_groups.parse("1:claude:review")]))
        update, switched = store.update_host, []

        def concurrent(**changes):
            # A profile change lands between the slots request's check and its save.
            if "slots" in changes and not switched:
                switched.append(host.configure(dict(claude_billing="api")))
            return update(**changes)

        with patch.object(host, "update_host", side_effect=concurrent), self.assertRaises(store.FleetError):
            host.rpc(dict(action="slots", groups=[slot_groups.parse("1:codex:progress")]))
        saved = store.host_config()
        self.assertEqual((saved["claude_billing"], saved["slots"]), ("api", [dict(count=1, agent="claude", roles=["review"])]))
        host.worker_config(saved)

    def test_an_unchanged_pass_renews_admission_without_rereading_definitions(self):
        host.configure(dict(env={"A": "x" * 4096}))
        store.update_host(workers_enabled=True, max_workers=8)
        host.rpc(dict(action="slots", groups=[slot_groups.parse("4096:claude:review")]))
        parse, plan, timing = tomllib.loads, host.worker_plan, []

        def unchanged():
            timing.append(time.monotonic())
            loads.reset_mock()
            planned.reset_mock()

        with (
            patch("tomllib.loads", side_effect=parse) as loads,
            patch.object(host, "worker_plan", side_effect=plan) as planned,
        ):
            policies = self.daemon_passes(unchanged, lambda: timing.append(time.monotonic()))
            self.assertEqual((loads.call_count, planned.call_count), (0, 0))
        self.assertEqual([len(policy["workers"]) for policy in policies], [8, 8, 8])
        self.assertLess(timing[1] - timing[0], 5)

    def test_an_edit_to_a_parked_slot_renews_without_reparsing_or_revalidating_each_slot(self):
        host.configure(dict(env={f"E{n}": "x" * 26 for n in range(64)}))
        store.update_host(workers_enabled=True, max_workers=8)
        host.rpc(dict(action="slots", groups=[slot_groups.parse("256:claude:review")]))
        parse, fields = tomllib.loads, host.worker_fields

        def edit():
            host.rpc(dict(action="slots", groups=[slot_groups.parse("255:claude:review"), slot_groups.parse("1:codex:progress")]))
            loads.reset_mock()
            checked.reset_mock()

        with (
            patch("tomllib.loads", side_effect=parse) as loads,
            patch.object(host, "worker_fields", side_effect=fields) as checked,
        ):
            policies = self.daemon_passes(edit, lambda: None)
            parsed, built = loads.call_count, checked.call_count
        _, workers = self.definitions()
        self.assertEqual(self.shape(workers)[256], ("codex", "progress"))
        self.assertEqual(host.written_workers(), list(workers.values()))
        self.assertEqual([len(policy["workers"]) for policy in policies], [8, 8, 8])
        # The changed pass builds one definition per agent and reads none back from disk.
        self.assertEqual(parsed, 0)
        self.assertLessEqual(built, 2)

    def test_cli_sends_groups_clears_and_queries(self):
        entry = dict(target="machine", machine_id="one")
        with (
            patch.object(remote, "inventory", return_value={"test": entry}),
            patch.object(remote, "resolve", return_value=entry),
            patch.object(remote, "call", return_value=dict(slots=None, max_workers=8)) as call,
            patch("sys.stdout", new=io.StringIO()),
            patch("sys.stderr", new=io.StringIO()),
        ):
            self.assertEqual(main(["slots", "test", "--group", "6:auto", "--group", "2::review,fix", "--group", "1:codex:progress"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="slots", groups=[
                dict(count=6, agent="auto"), dict(count=2, roles=["review", "fix"]), dict(count=1, agent="codex", roles=["progress"]),
            ]))
            self.assertEqual(main(["slots", "test", "--clear", "--json"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="slots", clear=True))
            self.assertEqual(main(["slots", "test"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="slots"))
            call.reset_mock()
            self.assertEqual(main(["slots", "test", "--group", "two:auto"]), 1)
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
