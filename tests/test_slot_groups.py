import io
import os
import signal
import subprocess
import tempfile
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
        handlers, written = {}, []

        def record(path, value):
            if Path(path).name == "admission.json":
                written.append(value)

        gib = 1024**3
        with (
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
            patch.object(host.time, "sleep", side_effect=lambda _: handlers[signal.SIGTERM]()),
            patch.object(host, "cpu_sample", return_value=(1, 1)),
            patch.object(host, "resources", return_value=dict(cpus=8, memory_total=64 * gib, memory_available=64 * gib)),
            patch.object(host, "worker_memory", return_value=None),
            patch.object(host, "start_workers"),
            patch.object(host, "atomic", side_effect=record),
        ):
            host.daemon()
        return written[0]

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
        self.assertEqual({w.rsplit("-", 1)[1] for w in host.maintenance_workers(store.host_config())}, set("789"))
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
        for counts in ([6, 2, 1], [40, 8, 2], [1, 1], [3], [5, 5, 5, 1]):
            placed = [group for group, count in enumerate(counts) for _ in range(count)]
            order = slot_groups.order(placed + [None, None])
            self.assertEqual(sorted(order), list(range(sum(counts))))
            for ceiling in range(sum(counts) + 1):
                admitted = [placed[position] for position in order[:ceiling]]
                for group, count in enumerate(counts):
                    self.assertLess(abs(admitted.count(group) - ceiling * count / sum(counts)), 1, (counts, ceiling))
                    members = [position for position in order[:ceiling] if placed[position] == group]
                    self.assertEqual(members, [p for p in range(len(placed)) if placed[p] == group][:len(members)])

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
        policy = self.daemon_pass()
        self.assertEqual(numbers(policy["workers"]), [1, 2, 3])
        self.assertEqual(policy["roadmap_focus"]["workers"], policy["workers"])

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
