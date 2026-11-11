import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_fleet import host, mathlib_store, remote, store
from tauceti_fleet.cli import main

LAKE = """#!/bin/sh
here=$(dirname "$0")
echo "$(basename "$PWD") $* ${LAKE_ARTIFACT_CACHE-unset}" >> "$here/lake.log"
if [ -e "$here/lake.fail" ]; then echo "cache unavailable" >&2; exit 1; fi
if [ "$1" = exe ]; then
  mkdir -p .lake/build/lib .lake/packages/batteries/.lake/build "$MATHLIB_CACHE_DIR"
  echo olean > .lake/build/lib/Mathlib.olean
  echo olean > .lake/packages/batteries/.lake/build/Batteries.olean
  echo archive > "$MATHLIB_CACHE_DIR/Mathlib.ltar"
fi
"""
TAUCETI = """#!/bin/sh
here=$(dirname "$0")
echo "$*" > "$here/tauceti.args"
cat "$here/inventory.json"
"""
TOOLCHAIN = "leanprover/lean4:v4.1.0"
OBSERVED = 10**6


def git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "user.name=test", "-c", "user.email=test@example.org",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(directory, files):
    for name, content in files.items():
        (directory / name).write_text(content)
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "-m", "change")
    return git(directory, "rev-parse", "HEAD")


def manifest(revision):
    return json.dumps(dict(packages=[dict(name="batteries", rev="b" * 40), dict(name="mathlib", rev=revision)]))


class Store(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.template = Path(temporary.name)
        mathlib, tauceti = cls.template / "mathlib", cls.template / "tauceti"
        for repository, branch in ((mathlib, "master"), (tauceti, "main")):
            repository.mkdir()
            git(repository, "init", "-q", "-b", branch)
            for key in ("allowFilter", "allowAnySHA1InWant"):
                git(repository, "config", "uploadpack." + key, "true")
        pinned = json.dumps(dict(packages=[dict(name="batteries", type="git", rev="b" * 40)]))
        cls.old = commit(mathlib, {"lean-toolchain": TOOLCHAIN + "\n", "lake-manifest.json": pinned, "Mathlib": "old"})
        cls.new = commit(mathlib, {"Mathlib": "new"})
        # A commit served by the upstream remote but on no trusted branch, as a fork's pull request is.
        git(mathlib, "checkout", "-q", "--detach")
        cls.fork = commit(mathlib, {"Mathlib": "fork"})
        git(mathlib, "update-ref", "refs/pull/1/head", cls.fork)
        git(mathlib, "checkout", "-q", "master")
        commit(tauceti, {"lake-manifest.json": manifest(cls.new), "lean-toolchain": TOOLCHAIN + "\n"})
        git(tauceti, "checkout", "-q", "--detach")
        cls.head = commit(tauceti, {"lake-manifest.json": manifest(cls.old)})
        git(tauceti, "update-ref", "refs/pull/7/head", cls.head)
        git(tauceti, "update-ref", "refs/pull/12/head", cls.head)
        git(tauceti, "update-ref", "refs/pull/11/head", commit(tauceti, {"lake-manifest.json": manifest(cls.fork)}))
        git(tauceti, "checkout", "-q", "main")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + key.upper(): str(self.root / key) for key in ("config", "state", "data")}
                         | {"LAKE_ARTIFACT_CACHE": "1"})
        env.start()
        self.addCleanup(env.stop)
        self.machine = store.host_config()["machine_id"]
        self.directory = self.root / "store"
        self.mounted = self.directory / "store"
        self.bin = store.data_dir() / "bin"
        self.bin.mkdir(parents=True)
        for name, text in (("lake", LAKE), ("tauceti", TAUCETI)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.inventory(dict(number=7, state="OPEN", head=self.head))
        self.mathlib, self.tauceti = self.root / "mathlib", self.root / "tauceti"
        for name, repository in (("SOURCE", self.tauceti), ("MATHLIB", self.mathlib)):
            shutil.copytree(self.template / repository.name, repository)
            source = patch.object(mathlib_store, name, repository.as_uri())
            source.start()
            self.addCleanup(source.stop)
        self.start_unit = host.start_mathlib_store
        start = patch.object(host, "start_mathlib_store")
        self.start = start.start()
        self.addCleanup(start.stop)

    def inventory(self, *rows):
        """What the worker prepared: these TauCeti pull requests among rows that are not candidates."""
        rows = [dict(repo=mathlib_store.REPOSITORY, scope=self.machine, observed=OBSERVED) | row for row in rows]
        rows += [dict(repo="TauCetiProject/TauCetiRoadmap", number=7, state="OPEN", head="c" * 40),
                 dict(repo=mathlib_store.REPOSITORY, number=8, state="MERGED", head="d" * 40)]
        (self.bin / "inventory.json").write_text(json.dumps(dict(version=1, prs=rows)))

    def configure(self):
        host.rpc(dict(action="mathlib-store", directory=str(self.directory)))
        return store.host_config()

    def line(self, now):
        return mathlib_store.describe(host.rpc(dict(action="status"))["mathlib_store"], now)

    def test_setting_is_validated_reported_and_cleared_and_starts_a_pass(self):
        before = store.host_config()
        self.assertEqual(host.rpc(dict(action="mathlib-store")), dict(mathlib_store=None))
        self.assertNotIn("mathlib_store", host.rpc(dict(action="status")))
        for directory in ("relative", "/srv/a:b", "/srv/with space", 7):
            with self.assertRaises(store.FleetError):
                host.rpc(dict(action="mathlib-store", directory=directory))
        self.assertEqual(store.host_config(), before)
        self.assertIsNone(mathlib_store.warm())
        self.start.assert_not_called()
        self.assertEqual(self.configure(), dict(before, mathlib_store=str(self.directory)))
        self.assertTrue(self.mounted.is_dir())
        self.start.assert_called_once_with()
        self.assertEqual(self.line(0), "pinned revisions not checked yet")
        self.start.side_effect = store.FleetError("no user manager")
        self.assertEqual(self.configure()["mathlib_store"], str(self.directory))
        self.assertEqual(host.rpc(dict(action="mathlib-store", clear=True)), dict(mathlib_store=None))
        self.assertEqual(self.start.call_count, 2)
        self.assertTrue(self.mounted.is_dir())

    def test_cli_sends_directory_or_clear_and_queries_without_changing(self):
        entry = dict(target="machine", machine_id="one")
        with (
            patch.object(remote, "inventory", return_value={"test": entry}),
            patch.object(remote, "resolve", return_value=entry),
            patch.object(remote, "call", return_value=dict(mathlib_store=None)) as call,
            patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(main(["mathlib-store", "test", "--dir", "/srv/mathlib", "--json"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="mathlib-store", directory="/srv/mathlib"))
            self.assertEqual(main(["mathlib-store", "test", "--clear"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="mathlib-store", clear=True))
            self.assertEqual(main(["mathlib-store", "test"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="mathlib-store"))

    def test_pass_publishes_what_main_and_open_pull_requests_pin_as_the_worker_reads_it(self):
        state = mathlib_store.warm(self.configure(), now=1000)
        self.assertEqual((self.bin / "tauceti.args").read_text().split(), ["activity", "--inventory", "--group", self.machine])
        self.assertEqual(state["needed"], {self.new: dict(toolchain=TOOLCHAIN, pinned_by=["main"]),
                                           self.old: dict(toolchain=TOOLCHAIN, pinned_by=["#7"])})
        self.assertEqual((state["checked_at"], state["building"], state["failures"], state["error"]), (1000, None, {}, None))
        self.assertEqual(sorted(p.name for p in self.mounted.iterdir()), sorted([self.new, self.old]))
        for revision, content in ((self.new, "new"), (self.old, "old")):
            entry = self.mounted / revision
            self.assertEqual((entry / ".tauceti-store-ready").read_text(), revision + "\n")
            self.assertEqual((entry / "lean-toolchain").read_text().strip(), TOOLCHAIN)
            self.assertEqual(json.loads((entry / "lake-manifest.json").read_text())["packages"][0]["rev"], "b" * 40)
            self.assertEqual((entry / "Mathlib").read_text(), content)
            self.assertEqual(git(entry, "remote", "get-url", "origin"), self.mathlib.as_uri())
            for built in (entry / ".lake/build/lib/Mathlib.olean", entry / ".lake/packages/batteries/.lake/build/Batteries.olean"):
                self.assertEqual(built.read_text(), "olean\n")
                for path in (built, *built.relative_to(entry).parents):
                    self.assertEqual((entry / path).stat().st_mode & 0o044, 0o044)
        self.assertEqual((self.bin / "lake.log").read_text().splitlines(),
                         [f"{revision} {arguments} unset" for revision in (self.new, self.old)
                          for arguments in ("exe cache get", "build --no-build Mathlib")])
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["staging", "store"])
        self.assertEqual(list((self.directory / "staging").iterdir()), [])
        self.assertEqual(self.line(1000 + 125), "2 of 2 pinned revisions ready (checked 2 min ago)")
        with patch("sys.stdout", new=io.StringIO()) as out:
            main(["status", "local"], _status_results=dict(local=host.rpc(dict(action="status"))))
        self.assertIn("Mathlib store: 2 of 2 pinned revisions ready", out.getvalue())
        # A second pass has nothing to fetch, build or remove, and leaves published entries untouched.
        (self.bin / "lake.log").unlink()
        before = {path: path.stat().st_mtime_ns for path in self.mounted.rglob("*")}
        mathlib_store.warm(now=1060)
        self.assertFalse((self.bin / "lake.log").exists())
        self.assertEqual({path: path.stat().st_mtime_ns for path in self.mounted.rglob("*")}, before)

    def test_entry_is_removed_once_unpinned_for_two_round_timeouts_and_never_on_a_failed_check(self):
        config, hold = self.configure(), 2 * 5400
        self.assertEqual([mathlib_store.hold(dict(env=env)) for env in (None, {"TAUCETI_ROUND_TIMEOUT": "9000"},
                                                                         {"TAUCETI_ROUND_TIMEOUT": "60"}, "x")],
                         [hold, 18000, hold, hold])
        mathlib_store.warm(config, now=1000)
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        other = self.mounted / "notes"
        other.mkdir()
        self.tauceti.rename(self.root / "unreachable")
        state = mathlib_store.warm(config, now=50000)
        self.assertEqual((state["checked_at"], set(state["needed"]), state["unpinned"]), (1000, {self.new, self.old}, {}))
        self.assertEqual(state["error"], "Cannot fetch TauCeti main to read its Mathlib pin")
        (self.root / "unreachable").rename(self.tauceti)
        # The clock starts at the first check that finds the revision unpinned, not at the last one that found it pinned.
        state = mathlib_store.warm(config, now=50000)
        self.assertEqual((state["error"], list(state["needed"]), state["unpinned"]), (None, [self.new], {self.old: 50000}))
        self.assertEqual(self.line(50000), "1 of 1 pinned revisions ready (checked 0 min ago); 1 unpinned awaiting removal")
        self.tauceti.rename(self.root / "unreachable")
        mathlib_store.warm(config, now=50000 + hold)
        (self.root / "unreachable").rename(self.tauceti)
        mathlib_store.warm(config, now=50000 + hold - 1)
        self.assertTrue((self.mounted / self.old).is_dir())
        # Pinned again, the entry is readmitted and starts over; unpinned for the whole wait, it goes.
        self.inventory(dict(number=7, state="OPEN", head=self.head))
        self.assertEqual(mathlib_store.warm(config, now=50000 + hold)["unpinned"], {})
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        mathlib_store.warm(config, now=60000 + hold)
        mathlib_store.warm(config, now=60000 + 2 * hold - 1)
        self.assertTrue((self.mounted / self.old).is_dir())
        state = mathlib_store.warm(config, now=60000 + 2 * hold)
        self.assertEqual((state["unpinned"], state["failures"]), ({}, {}))
        self.assertEqual(sorted(p.name for p in self.mounted.iterdir()), sorted([self.new, "notes"]))

    def test_entry_whose_marker_was_withdrawn_is_rebuilt_only_after_the_same_wait(self):
        config, hold = self.configure(), 2 * 5400
        mathlib_store.warm(config, now=1000)
        entry = self.mounted / self.new
        (entry / ".tauceti-store-ready").unlink()
        os.utime(entry, (50000, 50000))
        (self.bin / "lake.log").unlink()
        state = mathlib_store.warm(config, now=50000 + hold - 1)
        self.assertIn("marker withdrawn", state["failures"][self.new])
        self.assertFalse((self.bin / "lake.log").exists())
        self.assertEqual(entry.stat().st_mtime, 50000)
        self.assertEqual(mathlib_store.warm(config, now=50000 + hold)["failures"], {})
        self.assertTrue(mathlib_store.ready(self.mounted, self.new))

    def test_failed_build_leaves_nothing_is_reported_and_retried(self):
        config = self.configure()
        (self.bin / "lake.fail").touch()
        with patch("sys.stderr", new=io.StringIO()) as log:
            state = mathlib_store.warm(config, now=1000)
        self.assertIn("cache unavailable", log.getvalue())
        self.assertEqual(set(state["failures"]), {self.new, self.old})
        self.assertEqual([list(path.iterdir()) for path in sorted(self.directory.iterdir())], [[], []])
        line = self.line(1000)
        self.assertTrue(line.startswith("0 of 2 pinned revisions ready (checked 0 min ago); "))
        for revision, who in ((self.new, "main"), (self.old, "#7")):
            self.assertIn(f"; {revision[:10]} ({TOOLCHAIN}; {who}): lake exe cache get failed; see journalctl", line)
        (self.bin / "lake.fail").unlink()
        with patch.object(mathlib_store.shutil, "disk_usage", return_value=Mock(free=mathlib_store.MIN_FREE - 1)):
            state = mathlib_store.warm(config, now=1001)
        self.assertEqual(state["failures"][self.new], "not built: under 20 GiB free")
        self.assertEqual(list(self.mounted.iterdir()), [])
        self.assertEqual(mathlib_store.warm(config, now=1002)["failures"], {})
        self.assertTrue(mathlib_store.ready(self.mounted, self.new))

    def test_inventory_outage_keeps_known_pull_requests_and_a_vanished_ref_only_costs_itself(self):
        config = self.configure()
        mathlib_store.warm(config, now=1000)
        (self.bin / "tauceti").write_text("#!/bin/sh\nexit 1\n")
        state = mathlib_store.warm(config, now=1001)
        self.assertEqual(state["needed"][self.old]["pinned_by"], ["#7"])
        self.assertIn("activity inventory", state["error"])
        (self.bin / "tauceti").write_text(TAUCETI)
        self.inventory(dict(number=7, state="OPEN", head=self.head), dict(number=9, state="OPEN", head="f" * 40))
        state = mathlib_store.warm(config, now=1002)
        self.assertEqual((list(state["needed"]), state["error"]), ([self.new, self.old],
                         "some pull request heads could not be fetched; pins unreadable at #9; keeping their last known revisions"))

    def test_unreadable_pins_keep_their_last_known_revision_and_remove_nothing(self):
        config, hold = self.configure(), 2 * 5400
        mathlib_store.warm(config, now=1000)
        git(self.tauceti, "checkout", "-q", "--detach", self.head)
        moved = commit(self.tauceti, {"lake-manifest.json": "{"})
        git(self.tauceti, "update-ref", "refs/pull/7/head", moved)
        git(self.tauceti, "checkout", "-q", "main")
        self.inventory(dict(number=7, state="OPEN", head=moved))
        for now in (50000, 50000 + hold):
            state = mathlib_store.warm(config, now=now)
            self.assertEqual((state["needed"][self.old]["pinned_by"], state["unpinned"]), (["#7"], {}))
            self.assertEqual(state["error"], "pins unreadable at #7; keeping their last known revisions")
        self.assertTrue(mathlib_store.ready(self.mounted, self.old))
        # A clock that has run out is not acted on while the inventory is unavailable.
        self.inventory(dict(number=7, state="CLOSED", head=moved))
        self.assertEqual(mathlib_store.warm(config, now=60000)["unpinned"], {self.old: 60000})
        (self.bin / "tauceti").write_text("#!/bin/sh\nexit 1\n")
        state = mathlib_store.warm(config, now=60000 + hold)
        self.assertIn("activity inventory", state["error"])
        self.assertTrue((self.mounted / self.old).is_dir())
        (self.bin / "tauceti").write_text(TAUCETI)
        mathlib_store.warm(config, now=60000 + hold)
        self.assertFalse((self.mounted / self.old).exists())

    def test_an_unavailable_ref_does_not_keep_other_pull_requests_from_being_fetched(self):
        self.inventory(dict(number=7, state="OPEN", head=self.head), dict(number=9, state="OPEN", head="f" * 40))
        state = mathlib_store.warm(self.configure(), now=1000)
        self.assertEqual(state["needed"][self.old]["pinned_by"], ["#7"])
        self.assertTrue(state["error"].startswith("some pull request heads could not be fetched"))
        self.assertTrue(mathlib_store.ready(self.mounted, self.old))

    def test_a_revision_outside_trusted_mathlib_history_is_never_built(self):
        self.inventory(dict(number=7, state="OPEN", head=self.head), dict(number=11, state="OPEN", head="e" * 40))
        state = mathlib_store.warm(self.configure(), now=1000)
        self.assertEqual(state["needed"][self.fork]["pinned_by"], ["#11"])
        self.assertEqual(state["failures"], {self.fork: "untrusted: not in the history of Mathlib master"})
        self.assertEqual(sorted(p.name for p in self.mounted.iterdir()), sorted([self.new, self.old]))
        self.assertNotIn(self.fork, (self.bin / "lake.log").read_text())
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["staging", "store"])
        self.assertIn(f"{self.fork[:10]} ({TOOLCHAIN}; #11): untrusted: not in the history of Mathlib master",
                      self.line(1000))

    def test_a_pin_seen_by_a_partial_check_cancels_the_unpinned_clock(self):
        config = self.configure()
        mathlib_store.warm(config, now=1000)
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        self.assertEqual(mathlib_store.warm(config, now=2000)["unpinned"], {self.old: 2000})
        self.inventory(dict(number=7, state="OPEN", head=self.head), dict(number=9, state="OPEN", head="f" * 40))
        state = mathlib_store.warm(config, now=12500)
        self.assertIn("could not be fetched", state["error"])
        self.assertEqual(state["unpinned"], {})
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        self.assertEqual(mathlib_store.warm(config, now=12800)["unpinned"], {self.old: 12800})
        self.assertTrue((self.mounted / self.old).is_dir())

    def test_a_pull_request_of_unobserved_state_is_read_and_makes_the_check_incomplete(self):
        config, hold = self.configure(), 2 * 5400
        mathlib_store.warm(config, now=1000)
        self.inventory(dict(number=7, state="CLOSED", head=self.head), dict(number=12, state=None, head=None))
        for now in (2000, 2000 + hold):
            state = mathlib_store.warm(config, now=now)
            self.assertEqual((state["needed"][self.old]["pinned_by"], state["unpinned"]), (["#12"], {}))
            self.assertIn("not observed in the last 30 min: #12", state["error"])
        self.assertTrue(mathlib_store.ready(self.mounted, self.old))

    def test_an_unpinned_entry_stops_admitting_rounds_at_once_and_is_readmitted_when_pinned_again(self):
        config, hold = self.configure(), 2 * 5400
        mathlib_store.warm(config, now=1000)
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        self.assertEqual(mathlib_store.warm(config, now=2000)["unpinned"], {self.old: 2000})
        self.assertFalse(mathlib_store.ready(self.mounted, self.old))
        self.assertTrue((self.mounted / self.old / ".lake/build/lib/Mathlib.olean").is_file())
        (self.bin / "lake.log").unlink()
        self.inventory(dict(number=7, state="OPEN", head=self.head))
        state = mathlib_store.warm(config, now=3000)
        self.assertEqual((state["unpinned"], state["failures"]), ({}, {}))
        self.assertTrue(mathlib_store.ready(self.mounted, self.old))
        self.assertFalse((self.bin / "lake.log").exists())
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        mathlib_store.warm(config, now=4000)
        mathlib_store.warm(config, now=4000 + hold - 1)
        self.assertTrue((self.mounted / self.old).is_dir())
        mathlib_store.warm(config, now=4000 + hold)
        self.assertFalse((self.mounted / self.old).exists())

    def test_a_pass_interrupted_after_readmitting_an_entry_does_not_leave_its_old_clock(self):
        config, hold = self.configure(), 2 * 5400
        mathlib_store.warm(config, now=1000)
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        mathlib_store.warm(config, now=2000)
        # #11 pins a revision that is missing, so the pass goes on to fetch Mathlib history after readmitting.
        self.inventory(dict(number=7, state="OPEN", head=self.head), dict(number=11, state="OPEN", head="e" * 40))
        with patch.object(mathlib_store, "trusted_history", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            mathlib_store.warm(config, now=2000 + hold)
        self.assertTrue(mathlib_store.ready(self.mounted, self.old))
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        self.assertEqual(mathlib_store.warm(config, now=2300 + hold)["unpinned"], {self.old: 2300 + hold})
        self.assertTrue((self.mounted / self.old).is_dir())

    def test_an_interrupted_removal_leaves_nothing_in_the_store_to_readmit(self):
        config, hold = self.configure(), 2 * 5400
        mathlib_store.warm(config, now=1000)
        self.inventory(dict(number=7, state="CLOSED", head=self.head))
        mathlib_store.warm(config, now=2000)
        rmtree = shutil.rmtree

        def interrupted(path, *args, **kwargs):
            if Path(path).name.endswith(self.old):
                (Path(path) / "lean-toolchain").unlink()
                raise KeyboardInterrupt
            return rmtree(path, *args, **kwargs)

        with patch.object(mathlib_store.shutil, "rmtree", interrupted), self.assertRaises(KeyboardInterrupt):
            mathlib_store.warm(config, now=2000 + hold)
        self.inventory(dict(number=7, state="OPEN", head=self.head))
        state = mathlib_store.warm(config, now=2100 + hold)
        self.assertEqual((state["unpinned"], state["failures"], state["error"]), ({}, {}, None))
        self.assertTrue(mathlib_store.ready(self.mounted, self.old))
        self.assertTrue((self.mounted / self.old / "lean-toolchain").is_file())
        self.assertIn(f"{self.old} exe cache get", (self.bin / "lake.log").read_text().splitlines()[-2])

    def test_a_stale_inventory_observation_is_fetched_and_makes_the_check_incomplete(self):
        config, hold = self.configure(), 2 * 5400
        mathlib_store.warm(config, now=1000)
        git(self.tauceti, "checkout", "-q", "--detach", self.head)
        moved = commit(self.tauceti, {"lake-manifest.json": manifest(self.new)})
        git(self.tauceti, "update-ref", "refs/pull/7/head", moved)
        git(self.tauceti, "checkout", "-q", "main")
        self.inventory(dict(number=7, state="OPEN", head=moved, observed=1990))
        self.assertEqual(mathlib_store.warm(config, now=2000)["unpinned"], {self.old: 2000})
        # The pull request pins the old revision again while the worker cannot observe it.
        git(self.tauceti, "update-ref", "refs/pull/7/head", self.head)
        state = mathlib_store.warm(config, now=2000 + hold)
        self.assertEqual((state["needed"][self.old]["pinned_by"], state["unpinned"]), (["#7"], {}))
        self.assertIn("not observed in the last 30 min: #7", state["error"])
        self.assertTrue(mathlib_store.ready(self.mounted, self.old))

    def test_retained_downloads_are_reclaimed_when_they_leave_too_little_space_to_build(self):
        config = self.configure()
        (self.bin / "lake.fail").touch()
        with patch("sys.stderr", new=io.StringIO()):
            mathlib_store.warm(config, now=1000)
        (self.bin / "lake.fail").unlink()
        downloads = self.directory / "downloads"
        downloads.mkdir()
        (downloads / "Mathlib.ltar").write_text("archive\n")
        free = Mock(side_effect=lambda _: Mock(free=mathlib_store.MIN_FREE - (downloads / "Mathlib.ltar").exists()))
        with patch.object(mathlib_store.shutil, "disk_usage", free):
            state = mathlib_store.warm(config, now=1001)
        self.assertEqual(state["failures"], {})
        self.assertTrue(mathlib_store.ready(self.mounted, self.new))

    def test_toolchain_of_the_pinning_head_is_compared_with_the_build(self):
        commit(self.tauceti, {"lean-toolchain": "leanprover/lean4:v4.2.0\n"})
        state = mathlib_store.warm(self.configure(), now=1000)
        self.assertEqual(state["failures"], {self.new: f"built with {TOOLCHAIN}, pinned with leanprover/lean4:v4.2.0"})
        self.assertIn(f"{self.new[:10]} (leanprover/lean4:v4.2.0; main): built with {TOOLCHAIN}", self.line(1000))

    def test_only_one_pass_runs_at_a_time(self):
        config = self.configure()
        with store.try_lock(store.state_dir() / "mathlib-store.lock") as owned:
            self.assertTrue(owned)
            self.assertIsNone(mathlib_store.warm(config, now=1000))
        self.assertEqual(list(self.mounted.iterdir()), [])

    def test_controller_starts_a_pass_per_interval_only_while_the_setting_is_present(self):
        start = Mock(side_effect=[store.FleetError("no user manager"), store.FleetError("no user manager"), None])
        warmer = mathlib_store.Warmer(start)
        warmer.step(store.host_config(), 5000)
        start.assert_not_called()
        config = self.configure()
        with patch("sys.stderr", new=io.StringIO()) as log:
            for now in (5000, 5001, 5000 + mathlib_store.INTERVAL, 5000 + 2 * mathlib_store.INTERVAL):
                warmer.step(config, now)
        self.assertEqual(start.call_count, 3)
        self.assertEqual(log.getvalue().count("Mathlib store pass not started"), 1)
        self.start.reset_mock()
        handlers = {}
        with (
            patch.object(host, "install_pressure_service"),
            patch.object(host, "resources", return_value=dict(memory_total=32 * 1024**3, memory_available=30 * 1024**3)),
            patch.object(host, "cpu_sample", return_value=(10, 9)),
            patch.object(host, "collect_ci_results"),
            patch.object(host.vm, "slots_in_use", return_value=[]),
            patch.object(host.vm, "outstanding", return_value=False),
            patch.object(host.CISlot, "step", return_value={}),
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
            patch.object(host.time, "sleep", side_effect=lambda _: handlers[signal.SIGTERM]()),
        ):
            host.daemon()
            self.start.assert_called_once_with()
            host.rpc(dict(action="mathlib-store", clear=True))
            host.daemon()
        self.start.assert_called_once_with()
        self.assertIsNone(store.read(store.state_dir() / "status.json")["error"])

    def test_pass_unit_runs_beside_the_ci_and_worker_slices_and_is_started_without_waiting(self):
        with patch.object(host, "units", return_value=self.root), patch.object(host, "run") as run:
            self.start_unit()
        unit = (self.root / "tauceti-fleet-mathlib-store.service").read_text()
        self.assertIn('"warm-mathlib-store"\n', unit)
        for line in ("Type=oneshot", "Slice=tauceti-fleet.slice", "CPUWeight=10", "UMask=0022",
                     'Environment="TAUCETI_FLEET_DATA=' + str(self.root / "data") + '"'):
            self.assertIn(line + "\n", unit)
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [["systemctl", "--user", "daemon-reload"],
                          ["systemctl", "--user", "start", "--no-block", "tauceti-fleet-mathlib-store.service"]])

    def test_worker_manager_exports_the_store_to_bubble_workers_only(self):
        token = self.root / "token"
        token.write_text("token\n")
        store.update_host(github_token_file=str(token))
        seen = []
        for changes in ({}, dict(mathlib_store=str(self.directory)), dict(sandbox="bubble")):
            store.update_host(**changes)
            with patch.dict(os.environ), patch.object(host.os, "execv"):
                host.run_workers()
                seen.append(os.environ.get("TAUCETI_MATHLIB_STORE"))
        self.assertEqual(seen, [None, None, str(self.mounted)])


if __name__ == "__main__":
    unittest.main()
