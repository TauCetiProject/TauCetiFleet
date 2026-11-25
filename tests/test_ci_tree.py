"""The shared CI build tree: leases, collection, publication and its fail-closed checks."""

import gzip
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import ci_tree, ci_tree_guest, native_runner, store
from tauceti_fleet.store import FleetError

COMMIT = "c" * 40
INSTANCE = "0123456789abcdef"
OTHER = "fedcba9876543210"


def stopped(args, **_kwargs):
    return subprocess.CompletedProcess(args, 3, "inactive\n", "")


class Pool(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TAUCETI_FLEET_" + key: str(self.root / key)
                                               for key in ("STATE", "CONFIG", "DATA")})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("entries", "staging", "trash", "leases"):
            (ci_tree.root() / name).mkdir(parents=True)
        self.runs = []

        def fake_run(args, **kwargs):
            self.runs.append([str(a) for a in args])
            return stopped(args)
        runner = patch.object(ci_tree, "run", side_effect=fake_run)
        runner.start()
        self.addCleanup(runner.stop)

    def entry(self, stamp, commit=COMMIT, created_at=None):
        identity = f"{stamp}-{commit[:12]}-{int(stamp[9:15]):08x}"
        path = ci_tree.root() / "entries" / identity
        (path / "lake").mkdir(parents=True)
        store.atomic(path / "ready.json", dict(id=identity, commit=commit, key_digest="d" * 64,
                                               created_at=created_at or time.time()))
        return identity

    def instance(self, name):
        (store.state_dir() / "vms" / name).mkdir(parents=True, exist_ok=True)

    def test_lease_takes_the_newest_entry_and_release_is_idempotent(self):
        self.assertIsNone(ci_tree.lease(INSTANCE))
        older = self.entry("20261125T010000Z")
        newer = self.entry("20261125T020000Z")
        value = ci_tree.lease(INSTANCE)
        self.assertEqual(value["entry"], newer)
        self.assertEqual(Path(value["path"]), ci_tree.root() / "entries" / newer)
        self.assertEqual(ci_tree.leases(newer), [INSTANCE])
        self.assertEqual(ci_tree.lease(INSTANCE)["entry"], newer)
        self.assertEqual(ci_tree.leases(older), [])
        ci_tree.release(INSTANCE)
        ci_tree.release(INSTANCE)
        self.assertEqual(ci_tree.leases(newer), [])
        with self.assertRaises(FleetError):
            ci_tree.lease("../etc")

    def test_collection_keeps_leased_and_newest_two(self):
        oldest = self.entry("20261125T010000Z")
        middle = self.entry("20261125T020000Z")
        self.entry("20261125T030000Z")
        self.entry("20261125T040000Z")
        self.instance(INSTANCE)
        directory = ci_tree.root() / "leases" / oldest
        directory.mkdir()
        (directory / INSTANCE).touch()
        retired = ci_tree.collect()
        self.assertEqual(retired, [middle])
        remaining = {entry for entry, _ in ci_tree.entries()}
        self.assertIn(oldest, remaining)
        self.assertNotIn(middle, remaining)
        self.assertTrue((ci_tree.root() / "trash" / middle).exists())
        self.assertTrue(any("tauceti-ci-tree-remove-" in " ".join(r) for r in self.runs))

    def test_stale_lease_needs_both_directory_and_unit_gone(self):
        oldest = self.entry("20261125T010000Z")
        self.entry("20261125T020000Z")
        self.entry("20261125T030000Z")
        directory = ci_tree.root() / "leases" / oldest
        directory.mkdir()
        (directory / INSTANCE).touch()
        # Directory gone but the unit may still be running: keep the lease and the entry.
        with patch.object(ci_tree, "unit_active", return_value=True):
            ci_tree.collect()
        self.assertTrue((directory / INSTANCE).exists())
        self.assertIn(oldest, {entry for entry, _ in ci_tree.entries()})
        with patch.object(ci_tree, "unit_active", return_value=False):
            ci_tree.collect()
        self.assertNotIn(oldest, {entry for entry, _ in ci_tree.entries()})

    def test_entries_without_a_valid_ready_record_are_retired(self):
        good = self.entry("20261125T010000Z")
        broken = ci_tree.root() / "entries" / "20261125T020000Z-cccccccccccc-00000002"
        (broken / "lake").mkdir(parents=True)
        (ci_tree.root() / "entries" / "not-an-entry").mkdir()
        self.assertEqual([entry for entry, _ in ci_tree.entries()], [good])
        ci_tree.collect()
        self.assertEqual(sorted(p.name for p in (ci_tree.root() / "entries").iterdir()), [good])

    def test_guest_bind_accepts_only_the_leased_entry_path(self):
        identity = self.entry("20261125T010000Z")
        path = ci_tree.root() / "entries" / identity
        self.assertEqual(ci_tree.guest_bind(dict(entry=identity, path=str(path))), path)
        self.assertIsNone(ci_tree.guest_bind(None))
        self.assertIsNone(ci_tree.guest_bind(dict(entry=identity, path="/etc")))
        self.assertIsNone(ci_tree.guest_bind(dict(entry="../x", path=str(path))))
        (path / "ready.json").unlink()
        self.assertIsNone(ci_tree.guest_bind(dict(entry=identity, path=str(path))))

    def test_due_reasons(self):
        config = {ci_tree.SETTING: "on"}
        self.assertEqual(ci_tree.due({}), "disabled")
        with patch.object(ci_tree, "free_bytes", return_value=10**15), patch.object(native_runner, "ready", return_value=True):
            self.assertIsNone(ci_tree.due(config))
            store.atomic(ci_tree.root() / "builder.json", dict(last_attempt_at=time.time()))
            self.assertEqual(ci_tree.due(config), "checked recently")
            store.atomic(ci_tree.root() / "builder.json", dict(last_attempt_at=time.time() - 3600, last_outcome="failed"))
            self.assertEqual(ci_tree.due(config), "backing off after a failure")
            store.atomic(ci_tree.root() / "builder.json", dict(unit="u.service"))
            with patch.object(ci_tree, "unit_active", return_value=True):
                self.assertEqual(ci_tree.due(config), "building")
            store.atomic(ci_tree.root() / "builder.json", {})
            self.entry("20261125T010000Z")
            self.assertEqual(ci_tree.due(config), "newest entry is recent")
            for stamp in ("20261125T020000Z", "20261125T030000Z", "20261125T040000Z"):
                self.entry(stamp, created_at=1)
            self.assertEqual(ci_tree.due(config), "entry limit")
        with patch.object(ci_tree, "free_bytes", return_value=10 * 1024**3):
            for path in list((ci_tree.root() / "entries").iterdir()):
                os.rename(path, ci_tree.root() / "trash" / path.name)
            self.assertEqual(ci_tree.due(config), "disk headroom")

    def test_finish_records_the_outcome_and_retires_staging(self):
        directory = ci_tree.root() / "staging" / "20261125T010000Z-00000000"
        directory.mkdir()
        store.atomic(directory / "build-result.json", dict(outcome="published", entry="x", seconds=10))
        bridge = store.state_dir() / "vms" / "ci-tree-x"
        bridge.parent.mkdir(parents=True)
        bridge.symlink_to(directory)
        store.atomic(ci_tree.root() / "builder.json", dict(unit="u.service", directory=str(directory), bridge=str(bridge)))
        with patch.object(ci_tree, "unit_active", return_value=False):
            self.assertEqual(ci_tree.finish(), "published")
        state = store.read(ci_tree.root() / "builder.json")
        self.assertIsNone(state["unit"])
        self.assertEqual(state["last_outcome"], "published")
        self.assertFalse(directory.exists())
        self.assertFalse(bridge.is_symlink())
        self.assertEqual(json.loads((ci_tree.root() / "builds.jsonl").read_text())["entry"], "x")
        self.assertIsNone(ci_tree.finish())

    def test_validate_setting(self):
        ci_tree.validate({})
        ci_tree.validate({ci_tree.SETTING: "off"})
        with self.assertRaises(FleetError):
            ci_tree.validate({ci_tree.SETTING: True})

    def test_native_cleanup_releases_the_lease_after_the_unit_stops(self):
        self.entry("20261125T010000Z")
        directory = store.state_dir() / "vms" / INSTANCE
        directory.mkdir(parents=True)
        ci_tree.lease(INSTANCE)
        with patch.object(native_runner, "run", side_effect=lambda args, **kw: subprocess.CompletedProcess(args, 0, "active\n", "")):
            with self.assertRaises(FleetError):
                native_runner.cleanup(dict(directory=str(directory), unit="tauceti-fleet-vm-x.service"))
        self.assertEqual(sum(len(ci_tree.leases(e)) for e, _ in ci_tree.entries()), 1)
        with patch.object(native_runner, "run", side_effect=stopped), patch.object(native_runner, "remove_root"):
            native_runner.cleanup(dict(directory=str(directory), unit="tauceti-fleet-vm-x.service"))
        self.assertEqual(sum(len(ci_tree.leases(e)) for e, _ in ci_tree.entries()), 0)

    def test_stage_records_the_lease(self):
        directory = self.root / "instance"
        directory.mkdir()
        with patch.object(native_runner, "image", return_value=dict(sha256="a" * 64)):
            native_runner.stage(directory, "jit", shared_tree=dict(entry="e", path="/p"))
            self.assertEqual(store.read(directory / "native.json")["shared_tree"]["entry"], "e")
            other = self.root / "other"
            other.mkdir()
            native_runner.stage(other, "jit")
            self.assertNotIn("shared_tree", store.read(other / "native.json"))


class Inventory(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree = self.root / "lake"
        (self.tree / "build/lib").mkdir(parents=True)
        (self.tree / "cache/artifacts").mkdir(parents=True)
        (self.tree / "cache/artifacts/0123456789abcdef.olean").write_bytes(b"olean")
        os.chmod(self.tree / "cache/artifacts/0123456789abcdef.olean", 0o444)
        os.link(self.tree / "cache/artifacts/0123456789abcdef.olean", self.tree / "build/lib/A.olean")
        (self.tree / "build/lib/A.trace").write_text("{}")
        os.chmod(self.tree / "build/lib/A.trace", 0o600)
        (self.tree / "build/lib/link").symlink_to("A.olean")
        for name, value in (("RUNNER_UID", os.getuid()), ("RUNNER_GID", os.getgid())):
            owner = patch.object(ci_tree, name, value)
            owner.start()
            self.addCleanup(owner.stop)

    def check(self):
        return ci_tree.inventory(self.tree, self.root / "inventory.jsonl.gz")

    def test_valid_tree_records_every_file(self):
        summary = self.check()
        self.assertEqual(summary["files"], 8)
        records = [json.loads(line) for line in gzip.open(self.root / "inventory.jsonl.gz", "rt")]
        self.assertEqual({r[0] for r in records} >= {"build/lib/A.olean", "cache/artifacts/0123456789abcdef.olean"}, True)
        olean = next(r for r in records if r[0] == "build/lib/A.olean")
        self.assertEqual(olean[2], 0o444)
        self.assertEqual(olean[7], 2)

    def test_forbidden_modes_types_and_links_fail(self):
        cases = []
        def world_writable():
            os.chmod(self.tree / "build/lib/A.trace", 0o666)
        def setuid():
            os.chmod(self.tree / "build/lib/A.trace", 0o4755)
        def escaping_link():
            (self.tree / "build/lib/escape").symlink_to("../../../etc/passwd")
        def absolute_link():
            (self.tree / "build/lib/absolute").symlink_to("/etc/passwd")
        def fifo():
            os.mkfifo(self.tree / "build/lib/pipe")
        def shared_inode():
            os.link(self.tree / "build/lib/A.trace", self.root / "outside")
        cases = [world_writable, setuid, escaping_link, absolute_link, fifo, shared_inode]
        for case in cases:
            with self.subTest(case.__name__):
                self.setUp()
                case()
                with self.assertRaises(FleetError):
                    self.check()

    def test_foreign_owner_fails(self):
        with patch.object(ci_tree, "RUNNER_UID", os.getuid() + 1):
            with self.assertRaises(FleetError):
                self.check()


class Publish(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TAUCETI_FLEET_" + key: str(self.root / key)
                                               for key in ("STATE", "CONFIG", "DATA")})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("entries", "staging", "trash", "leases"):
            (ci_tree.root() / name).mkdir(parents=True)
        self.directory = ci_tree.root() / "staging" / "20261125T010000Z-00000000"
        (self.directory / "tree/build").mkdir(parents=True)
        (self.directory / "tree/build/A.olean").write_bytes(b"x")
        for name, value in (("RUNNER_UID", os.getuid()), ("RUNNER_GID", os.getgid())):
            owner = patch.object(ci_tree, name, value)
            owner.start()
            self.addCleanup(owner.stop)

        def fake_run(args, **kwargs):
            args = [str(a) for a in args]
            if "mv" in args:
                os.rename(args[-2], args[-1])
                return subprocess.CompletedProcess(args, 0, "", "")
            if "inventory" in args:
                try:
                    return subprocess.CompletedProcess(args, 0, json.dumps(ci_tree.inventory(args[-2], args[-1])) + "\n", "")
                except FleetError as error:
                    return subprocess.CompletedProcess(args, 1, f"FleetError: {error}\n", "")
            return stopped(args)
        runner = patch.object(ci_tree, "run", side_effect=fake_run)
        runner.start()
        self.addCleanup(runner.stop)

    def test_publish_renames_a_checked_entry_atomically(self):
        report = dict(ok=True, commit=COMMIT, key=dict(schema=1), key_digest="e" * 64, modules=3)
        result = ci_tree.publish(self.directory, report)
        entries = ci_tree.entries()
        self.assertEqual([entry for entry, _ in entries], [result["entry"]])
        ready = entries[0][1]
        self.assertEqual(ready["commit"], COMMIT)
        self.assertEqual(ready["files"], 2)
        self.assertEqual(ready["verifier"]["modules"], 3)
        self.assertTrue((ci_tree.root() / "entries" / result["entry"] / "lake/build/A.olean").is_file())
        self.assertFalse((self.directory / "entry").exists())

    def test_failed_structure_check_publishes_nothing(self):
        os.chmod(self.directory / "tree/build/A.olean", 0o666)
        with self.assertRaises(FleetError):
            ci_tree.publish(self.directory, dict(ok=True, commit=COMMIT, key_digest="e" * 64))
        self.assertEqual(ci_tree.entries(), [])

    def test_report_without_commit_or_key_is_refused(self):
        for report in (dict(ok=True, key_digest="e" * 64), dict(ok=True, commit=COMMIT), dict(ok=True, commit="x", key_digest="e" * 64)):
            with self.assertRaises(FleetError):
                ci_tree.publish(self.directory, report)
        self.assertEqual(ci_tree.entries(), [])

    def test_build_records_failure_without_publishing(self):
        def serve(directory, offline):
            store.atomic(directory / "control/telemetry/ci-tree-report.json", dict(ok=False, error="2 outputs differ"))
            return 1
        (self.directory / "control/telemetry").mkdir(parents=True)
        with patch.object(native_runner, "serve", side_effect=serve):
            self.assertEqual(ci_tree.build(self.directory, self.directory), 1)
        result = store.read(self.directory / "build-result.json")
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("2 outputs differ", result["error"])
        self.assertEqual(ci_tree.entries(), [])

    def test_build_reports_an_unchanged_commit(self):
        def serve(directory, offline):
            store.atomic(directory / "control/telemetry/ci-tree-report.json", dict(ok=False, unchanged=True, commit=COMMIT))
            return 0
        (self.directory / "control/telemetry").mkdir(parents=True)
        with patch.object(native_runner, "serve", side_effect=serve):
            self.assertEqual(ci_tree.build(self.directory, self.directory), 0)
        self.assertEqual(store.read(self.directory / "build-result.json")["outcome"], "unchanged")


HASH = {}


def fake_hash(paths):
    """A stand-in for Lake's hash: the file's bytes are the hash, for test files only."""
    return {path: Path(path).read_text().strip()[:16] for path in paths}


class GuestVerify(unittest.TestCase):
    """A miniature restored tree: two published root modules, one package module."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "pr"
        lake = self.project / ".lake"
        self.lake = lake
        self.rows = {}
        artifacts = lake / "cache/artifacts"
        artifacts.mkdir(parents=True)
        for index, module in enumerate(("A", "B")):
            dep = f"{index + 1:016x}"
            olean, ilean, archive = f"{index + 10:016x}", f"{index + 20:016x}", f"{index + 30:016x}"
            self.rows[dep] = archive + ".ltar"
            for name in (olean + ".olean", ilean + ".ilean", archive + ".ltar"):
                (artifacts / name).write_text(name.split(".")[0])
            build = lake / "build/lib/lean"
            build.mkdir(parents=True, exist_ok=True)
            (build / f"{module}.olean").write_text(olean)
            (build / f"{module}.olean.hash").write_text(olean)
            (build / f"{module}.ilean").write_text(ilean)
            (build / f"{module}.trace").write_text(json.dumps(dict(depHash=dep, outputs=dict(o=[olean + ".olean"], i=ilean + ".ilean", m=True))))
            mapping = lake / "cache/outputs/TauCeti"
            mapping.mkdir(parents=True, exist_ok=True)
            (mapping / f"{dep}.json").write_text(json.dumps(dict(data=dict(o=[olean + ".olean"], i=ilean + ".ilean", l=archive + ".ltar", m=True))))
        (lake / "cache/outputs/TauCeti" / ("9" * 16 + ".json")).write_text(json.dumps(dict(data={})))
        (artifacts / ("8" * 16 + ".olean")).write_text("8" * 16)  # unreferenced history
        revisions = lake / "cache/revisions/TauCeti"
        revisions.mkdir(parents=True)
        map_text = '"2026-03-17"\n' + "".join(json.dumps([dep, archive]) + "\n" for dep, archive in self.rows.items())
        (revisions / f"{COMMIT}.jsonl").write_text(map_text)
        (revisions / ("d" * 40 + ".jsonl")).write_text(map_text)
        self.map = Path(temporary.name) / "map.jsonl"
        self.map.write_text(map_text)
        package = lake / "packages/batteries/.lake/build/lib/lean"
        package.mkdir(parents=True)
        (package / "Batteries.olean").write_text("a" * 16)
        (package / "Batteries.trace").write_text(json.dumps(dict(depHash="b" * 16, outputs=dict(o=["a" * 16 + ".olean"]))))
        (self.project / "lake-manifest.json").write_text(json.dumps(dict(packages=[dict(name="batteries", rev="r" * 40)])))
        (lake / "tmp").mkdir()
        (lake / "tmp/junk").write_text("x")
        self.heads = {str(self.project): COMMIT, str(lake / "packages/batteries"): "r" * 40}

    def git(self, *args):
        if args[2] == "rev-parse":
            return self.heads[args[1]]
        return ""

    def verify(self):
        return ci_tree_guest.verify(self.project, COMMIT, self.map, "CacheHash.lean", None,
                                    hash_function=fake_hash, git=self.git)

    def test_valid_tree_is_pruned_and_reported(self):
        report = self.verify()
        self.assertTrue(report["ok"])
        self.assertEqual(report["modules"], 2)
        self.assertEqual(report["package_outputs"], 1)
        self.assertEqual(report["removed"]["artifacts"], 1)
        self.assertEqual(report["removed"]["root_mappings"], 1)
        self.assertEqual(report["removed"]["revisions"], 1)
        self.assertFalse((self.lake / "tmp/junk").exists())
        self.assertTrue((self.lake / "tmp").is_dir())
        self.assertFalse((self.lake / "cache/artifacts" / ("8" * 16 + ".olean")).exists())

    def expect_invalid(self, text):
        with self.assertRaises(ci_tree_guest.Invalid) as raised:
            self.verify()
        self.assertIn(text, str(raised.exception))

    def test_tampered_output_fails(self):
        (self.lake / "build/lib/lean/A.olean").write_text("f" * 16)
        self.expect_invalid("differ from their recorded hashes")

    def test_tampered_artifact_fails(self):
        (self.lake / "cache/artifacts" / (f"{10:016x}.olean")).write_text("f" * 16)
        self.expect_invalid("differ from their recorded hashes")

    def test_wrong_hash_record_fails(self):
        (self.lake / "build/lib/lean/A.olean.hash").write_text("f" * 16)
        self.expect_invalid("differ from their recorded hashes")

    def test_missing_published_module_fails(self):
        (self.lake / "build/lib/lean/B.trace").unlink()
        self.expect_invalid("published modules are not in the tree")

    def test_locally_built_module_outside_map_is_bounded(self):
        with patch.object(ci_tree_guest, "MAX_UNMAPPED_ROOT", 0):
            trace = self.lake / "build/lib/lean/C.trace"
            trace.write_text(json.dumps(dict(depHash="c" * 16, outputs={})))
            self.expect_invalid("not in the published map")

    def test_mapping_naming_another_archive_fails(self):
        dep = next(iter(self.rows))
        path = self.lake / "cache/outputs/TauCeti" / f"{dep}.json"
        data = json.loads(path.read_text())
        data["data"]["l"] = "e" * 16 + ".ltar"
        path.write_text(json.dumps(data))
        self.expect_invalid("archive the published map does not name")

    def test_package_at_wrong_revision_fails(self):
        self.heads[str(self.lake / "packages/batteries")] = "s" * 40
        self.expect_invalid("not at its manifest revision")

    def test_missing_package_output_fails(self):
        (self.lake / "packages/batteries/.lake/build/lib/lean/Batteries.olean").unlink()
        self.expect_invalid("lacks its olean")

    def test_unexpected_artifact_name_fails(self):
        (self.lake / "cache/artifacts/evil.sh").write_text("x")
        self.expect_invalid("unexpected artifact")

    def test_wrong_commit_fails(self):
        self.heads[str(self.project)] = "d" * 40
        self.expect_invalid("not the entry's commit")

    def test_main_writes_a_failure_report(self):
        report = Path(self.project.parent) / "report.json"
        self.heads[str(self.project)] = "d" * 40
        with patch.object(ci_tree_guest, "run", side_effect=lambda args, cwd=None: self.git(*args[1:])):
            code = ci_tree_guest.main(["verify", "--project", str(self.project), "--commit", COMMIT, "--map", str(self.map),
                                       "--hasher", "x", "--watchdog", "w", "--report", str(report)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(report.read_text())["ok"])

    def test_newest_published_walks_first_parents_and_caches_scopes(self):
        commits = ["1" * 40, "2" * 40, "3" * 40]
        calls = []
        def fake_run(args, cwd=None):
            if "rev-list" in args:
                return "\n".join(commits)
            if "rev-parse" in args:
                return "blob"
            if "cat-file" in args:
                calls.append("cat")
                return "leanprover/lean4:v4.37.0-rc1"
            raise AssertionError(args)
        def fetcher(url):
            if "/" + commits[1] in url:
                return b'"2026"\n'
            raise ci_tree_guest.urllib.error.HTTPError(url, 404, "missing", None, None)
        out = Path(self.project.parent) / "map"
        with patch.object(ci_tree_guest, "run", side_effect=fake_run):
            self.assertEqual(ci_tree_guest.newest_published("repo", "main", 10, out, fetcher), commits[1])
        self.assertEqual(calls, ["cat"])
        self.assertIn("leanprover--lean4---v4.37.0-rc1", ci_tree_guest.map_url("leanprover--lean4---v4.37.0-rc1", "x"))
        self.assertEqual(out.read_bytes(), b'"2026"\n')

    def test_tree_digest_names_links_and_content(self):
        directory = Path(self.project.parent) / "watchdog"
        (directory / "bin").mkdir(parents=True)
        (directory / "bin/lean").write_text("#!/bin/sh\n")
        (directory / "bin/lean-real").symlink_to("/real/lean")
        first = ci_tree_guest.tree_digest(directory)
        (directory / "bin/lean-real").unlink()
        (directory / "bin/lean-real").symlink_to("/other/lean")
        self.assertNotEqual(first, ci_tree_guest.tree_digest(directory))


if __name__ == "__main__":
    unittest.main()
