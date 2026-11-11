import contextlib
import hashlib
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capabilities, focus, host, remote, store, targets
from tauceti_fleet.cli import main
from tauceti_fleet.store import FleetError

# The example in TauCetiWorker's docs/target-lists.md.
EXAMPLE = """\
# Law of large numbers
<!-- tauceti-targets:v1 -->
Goal: the strong law for i.i.d. integrable variables. Everything before the
first `## ` heading is shown to the agent with the items of its area.

## MeasureTheory
- [x] `borel-cantelli` — both Borel–Cantelli lemmas (serves: G1)
- [ ] `kolmogorov-zero-one` — tail events have probability 0 or 1 (serves: G2; needs: `borel-cantelli`)

## Probability
- [~] `truncation-bounds` — variance bounds for truncated variables (needs: none; in flight: #1234)
- [ ] `strong-law` — the strong law (serves: G1; needs: `borel-cantelli`, `truncation-bounds`)
> A line that is not an item is ignored.

## Notes (not targets)
- [ ] `second-moment-route` — nothing under this heading is a target
"""
ROUNDS = [
    dict(id="w1", state="running", roadmap_target="MeasureTheory/kolmogorov-zero-one"),
    dict(id="w2", state="backoff", roadmap_target="Probability/strong-law"),
    dict(id="w3", state="running"),
]


class TargetLists(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environ = patch.dict(os.environ, {
            "TAUCETI_FLEET_CONFIG": str(self.root / "config"), "TAUCETI_FLEET_STATE": str(self.root / "state"),
            "TAUCETI_FLEET_DATA": str(self.root / "data"), "TAUCETI_GITHUB_SHARED_DIR": str(self.root / "shared"),
            "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
        })
        environ.start()
        self.addCleanup(environ.stop)
        self.protocol = dict(version=1, roadmap_focus=1, roadmap_targets=1)
        self.manager_protocol = None
        probe = patch.object(capabilities, "worker_protocol",
                             side_effect=lambda worker=None: self.protocol if worker is None else self.manager_protocol)
        probe.start()
        self.addCleanup(probe.stop)
        self.find_manager = targets.running_manager
        manager = patch.object(targets, "running_manager", side_effect=lambda: self.manager)
        self.manager = None
        manager.start()
        self.addCleanup(manager.stop)
        store.update_host(github_account="operator", ci_allowed=False)

    def mirror(self, *directories):
        """The workers' shared roadmap mirror, holding these directories."""
        source = self.root / "roadmap"
        for directory in directories:
            (source / directory).mkdir(parents=True)
            (source / directory / "README.md").write_text("roadmap\n")
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(source)]
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "roadmaps"], check=True)
        sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        base = self.root / "shared/repositories" / hashlib.sha256(targets.ROADMAP.encode()).hexdigest()
        base.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(source), str(base / "mirror.git")], check=True)
        (base / "state.json").write_text(json.dumps(dict(sha=sha, fetched=1)))

    def worker_inventory(self, prs):
        """The worker's prepared `prs` table and a stand-in for its read-only `activity --inventory` command."""
        (self.root / "activity").mkdir()
        with contextlib.closing(sqlite3.connect(self.root / "activity/activity.sqlite")) as db, db:
            db.execute("CREATE TABLE prs(repo TEXT,number INTEGER,worker TEXT,scope TEXT,created REAL,state TEXT,"
                       "landed REAL,observed REAL,checked REAL,head TEXT,source TEXT,PRIMARY KEY(repo,number))")
            db.executemany("INSERT INTO prs(repo,number,scope,state,landed) VALUES (:repo,:number,:scope,:state,:landed)", prs)
        worker = store.data_dir() / "bin/tauceti"
        worker.parent.mkdir(parents=True, exist_ok=True)
        worker.write_text(f"""#!{sys.executable}
import json, os, sqlite3, sys
assert sys.argv[1:4] == ["activity", "--inventory", "--group"], sys.argv
db = sqlite3.connect(os.environ["TAUCETI_ACTIVITY_DIR"] + "/activity.sqlite")
db.row_factory = sqlite3.Row
print(json.dumps(dict(version=1, prs=[dict(r) for r in db.execute("SELECT * FROM prs WHERE scope=?", sys.argv[4:5])])))
""")
        worker.chmod(0o755)

    def definitions(self):
        host.worker_config(store.host_config())
        return (store.config_dir() / "workers.toml").read_text()

    def test_counts_follow_the_documented_example_and_rounds_in_progress(self):
        report = targets.summarize(targets.parse(EXAMPLE), [])
        self.assertEqual({k: report[k] for k in ("items", *targets.STATES)},
                         dict(items=4, done=1, inflight=1, eligible=1, blocked=1))
        report = targets.summarize(targets.parse(EXAMPLE), ROUNDS)
        self.assertEqual(report["rounds"], [dict(worker="w1", item="MeasureTheory/kolmogorov-zero-one")])
        self.assertEqual(report["areas"], dict(MeasureTheory=dict(done=1, inflight=1, eligible=0, blocked=0),
                                               Probability=dict(done=0, inflight=1, eligible=0, blocked=1)))
        held = [dict(worker="w1", item="MeasureTheory/kolmogorov-zero-one")]
        for state in ("verifying", "working", "verification-pending", "verification-incomplete"):
            verifying = dict(id="w1", state=state, roadmap_target="MeasureTheory/kolmogorov-zero-one", next_action_at=None)
            self.assertEqual(targets.summarize(targets.parse(EXAMPLE), [verifying])["rounds"], held, state)
            # A continuation between rounds keeps the retry time the ended round left.
            background = dict(verifying, next_action_at=1.0)
            self.assertEqual(targets.summarize(targets.parse(EXAMPLE), [background])["rounds"], [], state)
        ticked = EXAMPLE.replace("[~] `truncation", "[x] `truncation")
        self.assertEqual(targets.summarize(targets.parse(ticked), [])["eligible"], 2)
        mixed = targets.parse(EXAMPLE.replace("`borel-cantelli`, `truncation-bounds`", "borel-cantelli, `truncation-bounds`"))
        self.assertEqual(mixed["items"][-1]["needs"], ["borel-cantelli", "truncation-bounds"])
        # As in the worker, the last `needs:` clause naming slugs is the one that counts.
        for repeated, needs in (("needs: `borel-cantelli`; needs: `borel-cantelli`", ["borel-cantelli"]),
                                ("needs: `strong-law`; needs: `borel-cantelli`", ["borel-cantelli"]),
                                ("needs: `borel-cantelli`; needs: none", ["borel-cantelli"])):
            item = targets.parse(EXAMPLE.replace("needs: `borel-cantelli`)", repeated + ")"))["items"][1]
            self.assertEqual(item["needs"], needs, repeated)
        undefined = EXAMPLE.replace("needs: none", "needs: no-such-slug")
        self.assertEqual(targets.summarize(targets.parse(undefined), [])["blocked"], 1)
        with self.assertRaisesRegex(FleetError, "marker"):
            targets.parse(EXAMPLE.replace("tauceti-targets:v1", "targets"))

    def test_transferred_list_reaches_every_definition_and_clearing_restores_them(self):
        plain = self.definitions()
        self.mirror("TauCetiRoadmap/MeasureTheory", "TauCetiRoadmap/Probability")
        source = self.root / "list.md"
        source.write_text(EXAMPLE)
        entry = dict(target="local", machine_id=store.host_config()["machine_id"])
        with patch.object(remote, "inventory", return_value={"here": entry}), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["targets", "here", "--file", str(source)]), 0)
        stored = targets.stored()
        self.assertEqual(stored.read_text(), EXAMPLE)
        self.assertEqual(stored.stat().st_mode & 0o077, 0)
        self.assertEqual(store.host_config()["roadmap_targets"], str(stored))
        self.assertIn("(transferred copy)", out.getvalue())
        self.assertIn("4 items in 2 areas: 1 done, 1 in flight, 1 eligible, 1 blocked", out.getvalue())
        workers = tomllib.loads(self.definitions())["workers"]
        self.assertEqual({w["roadmap_targets"] for w in workers}, {str(stored)})
        self.assertEqual(len(workers), store.host_config()["max_workers"])
        self.assertIsNone(host.rpc(dict(action="targets", clear=True))["roadmap_targets"])
        self.assertEqual(self.definitions(), plain)
        self.assertTrue(stored.exists())

    def test_list_on_the_host_is_read_in_place(self):
        shared = self.root / "checkout/list.md"
        shared.parent.mkdir()
        shared.write_text(EXAMPLE)
        result = targets.configure(dict(path=str(shared)), [])
        self.assertEqual((result["roadmap_targets"], result["transferred"]), (str(shared.resolve()), False))
        self.assertIn("Areas not checked: this host has no roadmap mirror yet", result["notes"])
        self.assertFalse(targets.stored().exists())
        shared.write_text(EXAMPLE.replace("[~] `truncation", "[x] `truncation"))
        self.assertEqual(targets.status(store.host_config(), [])["done"], 2)
        shared.unlink()
        self.assertIn("Cannot read target list", targets.status(store.host_config(), [])["error"])

    def test_a_path_outside_the_basic_plane_is_valid_in_the_definitions(self):
        shared = self.root / "lists/goal-\U0001d6fc\x7f.md"
        shared.parent.mkdir()
        shared.write_text(EXAMPLE)
        targets.configure(dict(path=str(shared)), [])
        self.assertEqual({w["roadmap_targets"] for w in tomllib.loads(self.definitions())["workers"]}, {str(shared.resolve())})

    def test_setting_a_symlinked_list_again_follows_the_link(self):
        one, two, link = self.root / "one.md", self.root / "two.md", self.root / "current.md"
        one.write_text(EXAMPLE)
        two.write_text(EXAMPLE.replace("[~] `truncation", "[x] `truncation"))
        link.symlink_to(one)
        result = targets.configure(dict(path=str(link)), [])
        self.assertEqual(result["roadmap_targets"], str(one.resolve()))
        self.assertIn(f"{link} resolves to {one.resolve()}", result["notes"][0])
        link.unlink()
        link.symlink_to(two)
        self.assertEqual(targets.configure(dict(path=str(link)), [])["list"]["done"], 2)
        self.assertEqual({w["roadmap_targets"] for w in tomllib.loads(self.definitions())["workers"]}, {str(two.resolve())})

    def test_a_link_is_checked_by_the_file_it_names(self):
        link = self.root / "current.md"
        (self.root / "goal.md ").write_text(EXAMPLE)
        link.symlink_to(self.root / "goal.md ")
        with self.assertRaisesRegex(FleetError, "begins or ends with whitespace"):
            targets.configure(dict(path=str(link)), [])
        undecodable = os.fsdecode(bytes(self.root / "goal") + b"\xff.md")
        try:
            Path(undecodable).write_text(EXAMPLE)
            link.unlink()
            link.symlink_to(undecodable)
            resolving = contextlib.nullcontext()
        except OSError:
            # A file system that takes only UTF-8 names: stand in for the link's resolution.
            resolving = patch.object(Path, "resolve", return_value=Path(undecodable))
        with resolving, self.assertRaisesRegex(FleetError, "valid UTF-8"):
            targets.configure(dict(path=str(link)), [])
        self.assertIsNone(store.host_config().get("roadmap_targets"))

    def test_refused_lists_change_nothing(self):
        self.mirror("TauCetiRoadmap/MeasureTheory", "Completed/Probability")
        (self.root / "list.md ").write_text(EXAMPLE)
        repeated = EXAMPLE.replace("`strong-law`", "`borel-cantelli`")
        for payload, message in (
            (dict(text=EXAMPLE.replace("tauceti-targets:v1", "targets")), "marker"),
            (dict(text=repeated.replace("## Probability", "## MeasureTheory")), "more than once: `borel-cantelli` \\(lines 7, 12\\)"),
            (dict(text=EXAMPLE), "Unfinished items under archived roadmaps: Probability \\(2 unfinished\\)"),
            (dict(text=EXAMPLE.replace("## Probability", "## Probabilty")), "No roadmap directory for Probabilty"),
            (dict(text="x" * (targets.LIMIT + 1)), "at most 256 KiB"),
            (dict(path="list.md"), "absolute path"),
            (dict(path="/lists/\ud835.md"), "valid UTF-8"),
            (dict(path=str(self.root / "list.md ")), "begins or ends with whitespace"),
            (dict(path=str(self.root / "absent.md")), "Cannot read target list"),
            (dict(text=EXAMPLE, path="/list.md"), "not both"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(FleetError, message):
                targets.configure(payload, [])
        done = EXAMPLE.replace("[~]", "[x]").replace("- [ ] `strong-law`", "- [x] `strong-law`")
        self.assertEqual(targets.configure(dict(text=done), [])["list"]["done"], 3)
        store.update_host(roadmap_targets=None)
        self.protocol = dict(version=1, roadmap_focus=1)
        with self.assertRaisesRegex(FleetError, "no target-list support"):
            targets.configure(dict(text=done), [])
        self.assertIsNone(store.host_config()["roadmap_targets"])

    def test_a_running_manager_without_list_support_refuses_the_list(self):
        self.manager, self.manager_protocol = "/old/bin/tauceti", dict(version=1, roadmap_focus=1)
        with self.assertRaisesRegex(FleetError, "running worker manager started from a TauCetiWorker without target-list support"):
            targets.configure(dict(text=EXAMPLE), [])
        self.manager = ""
        with self.assertRaisesRegex(FleetError, "Cannot tell which worker runtime the running manager uses; on the host run"):
            targets.configure(dict(text=EXAMPLE), [])
        self.assertIsNone(store.host_config().get("roadmap_targets"))
        self.assertFalse(targets.stored().exists())
        self.manager, self.manager_protocol = "/new/bin/tauceti", self.protocol
        self.assertEqual(targets.configure(dict(text=EXAMPLE), [])["roadmap_targets"], str(targets.stored()))
        self.assertIsNone(targets.configure(dict(clear=True), [])["roadmap_targets"])

    def test_the_running_manager_is_the_runtime_it_started_from(self):
        active = []
        with patch.object(targets, "run", side_effect=lambda argv, **_: subprocess.CompletedProcess(argv, 0 if active else 3)):
            self.assertIsNone(self.find_manager())
            active.append(True)
            with patch.object(host, "manager_process", return_value=None):
                self.assertEqual(self.find_manager(), "")
            store.atomic(store.state_dir() / "worker-manager.json", dict(binary="/old/bin/tauceti"))
            self.assertEqual(self.find_manager(), "/old/bin/tauceti")

    def test_controller_rewrites_definitions_and_reports_the_list(self):
        store.update_host(workers_enabled=True, max_workers=2)
        handlers, written = {}, {}
        gib = 1024**3
        workers = store.state_dir() / "workers"
        workers.mkdir(parents=True)
        for row in ROUNDS:
            (workers / (row["id"] + ".json")).write_text(json.dumps(row))
        (workers / "w4.json").write_text(json.dumps(dict(id="w4", state="verification-pending", next_action_at=None,
                                                         roadmap_target="Probability/strong-law")))

        def one_pass():
            written.clear()
            with (
                patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
                patch.object(host.time, "sleep", side_effect=lambda _: handlers[signal.SIGTERM]()),
                patch.object(host, "cpu_sample", return_value=(1, 1)),
                patch.object(host, "resources", return_value=dict(cpus=8, memory_total=64 * gib, memory_available=64 * gib)),
                patch.object(host, "worker_memory", return_value=None),
                patch.object(host, "start_workers"),
                patch.object(host, "atomic", side_effect=lambda path, value: written.setdefault(Path(path).name, value)),
            ):
                host.daemon()
            return written["status.json"]

        self.assertIsNone(one_pass()["roadmap_targets"])
        self.assertNotIn("roadmap_targets", (store.config_dir() / "workers.toml").read_text())
        result = targets.configure(dict(text=EXAMPLE), [])
        self.assertIn("controller's next pass", result["notes"][-1])
        report = one_pass()["roadmap_targets"]
        self.assertEqual((report["inflight"], sorted(r["worker"] for r in report["rounds"])), (3, ["w1", "w4"]))
        self.assertEqual({w["roadmap_targets"] for w in tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]},
                         {str(targets.stored())})
        targets.stored().write_text("no marker")
        self.assertIn("marker", one_pass()["roadmap_targets"]["error"])
        self.assertEqual(written["admission.json"]["workers"], ["fleet-" + store.host_config()["machine_id"][:8] + f"-{n}" for n in (1, 2)])

    def test_audit_corrects_only_what_the_host_can_see(self):
        self.mirror("TauCetiRoadmap/MeasureTheory")
        machine = store.host_config()["machine_id"]
        listed = EXAMPLE.replace("## Notes", "- [~] `item-1` — text (in flight: #1; partial: #01; partial: #9)\n- [~] `item-2` — text (in flight: #2)\n"
                                 "- [~] `item-3` — text (in flight: #3)\n- [~] `item-4` — text (in flight: #4)\n- [~] `item-5` — text (in flight: #5)\n"
                                 "- [~] `item-6` — text (in flight: https://forge.example/o/r/pulls/1234)\n"
                                 "- [ ] no slug\n\n## Notes")
        # A Forgejo pull request is named by URL; the inventory and `#N` are GitHub's.
        listed = listed.replace("in flight: #1234)", "in flight: #1234; partial: https://forge.example/o/r/pulls/1)")
        path = self.root / "checkout/list.md"
        path.parent.mkdir()
        path.write_text(listed)
        path.chmod(0o640)
        link = self.root / "list.md"
        link.symlink_to(path)
        store.update_host(roadmap_targets=str(link))
        prs = [dict(scope=machine, repo=targets.REPOSITORY, number=n, state=state, landed=landed)
               for n, state, landed in ((1, "MERGED", 5.0), (2, "CLOSED", None), (3, "OPEN", None), (1234, "CLOSED", 9.0))]
        prs.append(dict(scope=machine, repo="TauCetiProject/TauCetiRoadmap", number=4, state="MERGED", landed=1.0))
        prs.append(dict(scope="another-host", repo=targets.REPOSITORY, number=5, state="MERGED", landed=1.0))
        self.worker_inventory(prs)
        rounds = [dict(id="w1", state="running", roadmap_target="MeasureTheory/borel-cantelli"),
                  dict(id="w2", state="surveying", roadmap_target="Probability/renamed")]
        result = targets.configure(dict(audit=True), rounds)
        details = [f["detail"] for f in result["findings"]]
        self.assertEqual((result["corrections"], result["applied"]), (2, False))
        self.assertEqual(path.read_text(), listed)
        for expected in (
            "Line 21 is an item line without a `slug`; workers skip it",
            "No roadmap directory for Probability in the workers' roadmap mirror",
            "w1 is authoring MeasureTheory/borel-cantelli, which the file marks done",
            "w2 is authoring Probability/renamed, which the file does not list",
            "Probability/item-1: #1 landed and the item records it as partial -> [ ]",
            "Probability/item-2: #2 closed with no landing on record",
            "Probability/truncation-bounds: #1234 landed -> [x]",
        ):
            self.assertTrue(any(d.startswith(expected) for d in details), expected)
        self.assertFalse(any(f"item-{n}" in d for d in details for n in (3, 4, 5, 6)))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            targets.display(targets.configure(dict(audit=True, apply=True), rounds))
        self.assertIn(f"2 line(s) rewritten in {link}", out.getvalue())
        self.assertEqual(link.resolve(), path.resolve())
        after = path.read_text()
        self.assertIn("- [x] `truncation-bounds` — variance bounds for truncated variables (needs: none; landed: #1234; partial: https://forge.example/o/r/pulls/1)\n", after)
        self.assertIn("- [ ] `item-1` — text (partial: #01; partial: #9)\n", after)
        self.assertEqual(after.count("\n- [~]"), 5)
        self.assertIn("(in flight: https://forge.example/o/r/pulls/1234)\n", after)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(targets.configure(dict(audit=True, apply=True), [])["corrections"], 0)
        store.update_host(roadmap_targets=None)
        with self.assertRaisesRegex(FleetError, "no target list"):
            targets.configure(dict(audit=True), [])

    def test_apply_keeps_edits_made_while_the_audit_ran(self):
        machine = store.host_config()["machine_id"]
        path = self.root / "list.md"
        path.write_text(EXAMPLE)
        store.update_host(roadmap_targets=str(path))
        self.worker_inventory([dict(scope=machine, repo=targets.REPOSITORY, number=1234, state="MERGED", landed=9.0)])
        lookup = targets.landings

        def edited(text):
            def landings(machine):
                targets.write(path, text)
                return lookup(machine)
            return patch.object(targets, "landings", side_effect=landings)

        added = EXAMPLE.replace("> A line", "- [ ] `added-meanwhile` — new\n> A line")
        with edited(added):
            self.assertTrue(targets.configure(dict(audit=True, apply=True), [])["applied"])
        self.assertIn("`added-meanwhile`", path.read_text())
        self.assertIn("[x] `truncation-bounds`", path.read_text())
        path.write_text(EXAMPLE)
        clashing = EXAMPLE.replace("(needs: none; in flight: #1234)", "(needs: none; in flight: #1235)")
        with edited(clashing), self.assertRaisesRegex(FleetError, "changed on the corrected lines"):
            targets.configure(dict(audit=True, apply=True), [])
        self.assertEqual(path.read_text(), clashing)

    def test_cli_requests_and_focus_say_when_a_list_overrides_focus(self):
        entry = dict(target="machine", machine_id="one")
        source = self.root / "list.md"
        source.write_text(EXAMPLE)
        reply = dict(roadmap_targets=None, transferred=False, list=None, notes=[])
        with (
            patch.object(remote, "inventory", return_value={"test": entry}),
            patch.object(remote, "resolve", return_value=entry),
            patch.object(remote, "call", return_value=reply) as call,
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            for arguments, payload in (
                (["--file", str(source)], dict(text=EXAMPLE)),
                (["--file", "/srv/lists/goal.md", "--on-host"], dict(path="/srv/lists/goal.md")),
                (["--clear"], dict(clear=True)),
                (["--audit", "--apply"], dict(audit=True, apply=True)),
                ([], {}),
            ):
                self.assertEqual(main(["targets", "test", *arguments, "--json"]), 0)
                self.assertEqual(call.call_args.args[1], dict(action="targets", **payload))
            calls = call.call_count
            for arguments in (["--apply"], ["--on-host"], ["--clear", "--apply"], ["--file", str(self.root / "absent.md")]):
                self.assertEqual(main(["targets", "test", *arguments]), 1)
            self.assertEqual(call.call_count, calls)
            self.assertEqual(main(["targets", "test"]), 0)
        self.assertIn("Target list: none; workers choose their own roadmap areas", out.getvalue())
        store.update_host(roadmap_focus=dict(percent=20, areas=["PDE"]))
        self.assertNotIn("note", focus.configure({}))
        targets.configure(dict(text=EXAMPLE), [])
        self.assertIn("take no roadmap focus", focus.configure({})["note"])
        self.assertIn("take no roadmap focus", focus.configure(dict(percent=30))["note"])
        self.assertNotIn("note", focus.configure(dict(clear=True)))
        store.update_host(roadmap_focus=dict(percent=20, areas=["PDE"]))
        self.assertIn("Roadmap focus (20%) is not applied", targets.configure({}, [])["notes"][0])

    def test_status_shows_the_list_under_the_focus_line(self):
        report = dict(path="/srv/list.md", **targets.summarize(targets.parse(EXAMPLE), ROUNDS))
        policy = dict(percent=20, areas=["PDE"], workers=[], fractional_worker=None, fractional_percent=0)
        status = dict(workers=ROUNDS, roadmap_focus=policy, roadmap_targets=report, cpu_idle_fraction=0.5)
        entry = dict(target="machine", machine_id="one")

        def shown(status):
            with (
                patch.object(remote, "inventory", return_value={"test": entry}),
                patch.object(remote, "resolve", return_value=entry),
                patch("tauceti_fleet.pool_activity.cached", return_value={}),
                contextlib.redirect_stdout(io.StringIO()) as out,
            ):
                main(["status", "test"], _status_results={"test": dict(machine_id="one", status=status)})
            return out.getvalue()

        text = shown(status)
        self.assertIn(
            "  Not applied: workers under a target list take no roadmap focus\n"
            "Target list: 4 items in 2 areas: 1 done, 2 in flight, 0 eligible, 1 blocked\n"
            "  Counted from the file's marks and this host's rounds; workers also count open and merged pull requests\n"
            "  Rounds in progress: w1 MeasureTheory/kolmogorov-zero-one\n", text)
        self.assertIn("Target list unusable: gone", shown(dict(status, roadmap_targets=dict(path="/srv/list.md", error="gone"))))
        self.assertNotIn("arget list", shown(dict(status, roadmap_targets=None)))

    def test_doctor_blocks_workers_on_an_unsupported_or_unusable_list(self):
        def check():
            config = dict(store.host_config(), ci_allowed=False)
            with (
                patch.object(capabilities, "resources", return_value=dict(cpus=64, memory_total=128 * 1024**3, memory_available=128 * 1024**3)),
                patch.object(capabilities, "run", return_value=subprocess.CompletedProcess([], 0, "0")),
                patch.object(capabilities, "executable", return_value="/fake/tool"),
                patch.object(capabilities.os, "open", side_effect=PermissionError),
            ):
                checks = capabilities.doctor(config, probe=False)["checks"]
            return next((c for c in checks if c["name"] == "roadmap-targets"), None)

        self.assertIsNone(check())
        targets.configure(dict(text=EXAMPLE), [])
        self.assertEqual((check()["status"], check()["evidence"]), ("ok", f"4 items in 2 areas at {targets.stored()}"))
        targets.stored().unlink()
        self.assertEqual(check()["status"], "blocked")
        self.assertIn("Cannot read target list", check()["evidence"])
        targets.stored().write_text(EXAMPLE)
        self.protocol = dict(version=1, roadmap_focus=1)
        self.assertEqual((check()["status"], check()["evidence"]), ("blocked", "Installed worker has no target-list support"))


if __name__ == "__main__":
    unittest.main()
