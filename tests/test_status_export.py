import io
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import host, remote, status_view, store
from tauceti_fleet.cli import main


class Export(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {
            **{"TAUCETI_FLEET_" + key.upper(): str(self.root / key) for key in ("config", "state", "data")},
            "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.machine = store.host_config()["machine_id"]
        self.directory = self.root / "export"
        self.directory.mkdir(mode=0o755)
        self.target = self.directory / (self.machine + ".json")

    def configure(self):
        host.rpc(dict(action="status-export", directory=str(self.directory)))
        return store.host_config()

    def test_unset_writes_nothing_and_set_writes_the_status_object_world_readable(self):
        export = host.StatusExport()
        export.step(store.host_config(), 1000)
        self.assertEqual(list(self.directory.iterdir()), [])
        store.atomic(store.state_dir() / "status.json", dict(machine_id=self.machine, workers=[]))
        host.StatusExport().step(self.configure(), 1000)
        self.assertEqual(list(self.directory.iterdir()), [self.target])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)
        value = json.loads(self.target.read_text())
        self.assertEqual(value.pop("exported_at"), 1000)
        self.assertEqual(value, host.rpc(dict(action="status")))
        self.assertEqual(store.state_dir().stat().st_mode & 0o077, 0)

    def test_setting_is_validated_reported_and_cleared_without_other_changes(self):
        before = store.host_config()
        self.assertEqual(host.rpc(dict(action="status-export")), dict(status_export=None))
        for directory in ("relative", str(self.root / "missing"), 7):
            with self.assertRaises(store.FleetError):
                host.rpc(dict(action="status-export", directory=directory))
        self.assertEqual(store.host_config(), before)
        self.assertEqual(self.configure(), dict(before, status_export=str(self.directory)))
        self.assertEqual(host.rpc(dict(action="status-export")), dict(status_export=str(self.directory)))
        self.assertEqual(host.rpc(dict(action="status-export", clear=True)), dict(status_export=None))
        host.StatusExport().step(store.host_config(), 1000)
        self.assertFalse(self.target.exists())

    def test_cli_sends_directory_or_clear_and_queries_without_changing(self):
        entry = dict(target="machine", machine_id="one")
        with (
            patch.object(remote, "inventory", return_value={"test": entry}),
            patch.object(remote, "resolve", return_value=entry),
            patch.object(remote, "call", return_value=dict(status_export=None)) as call,
            patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(main(["status-export", "test", "--dir", "/srv/fleet-status", "--json"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="status-export", directory="/srv/fleet-status"))
            self.assertEqual(main(["status-export", "test", "--clear"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="status-export", clear=True))
            self.assertEqual(main(["status-export", "test"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="status-export"))

    def test_writes_are_at_least_thirty_seconds_apart(self):
        config, export = self.configure(), host.StatusExport()
        for now, written in ((1000, 1000), (1029.9, 1000), (1030, 1030), (1031, 1030), (1060, 1060)):
            export.step(config, now)
            self.assertEqual(json.loads(self.target.read_text())["exported_at"], written)

    def test_replacement_is_atomic_from_a_finished_file_in_the_same_directory(self):
        config, export = self.configure(), host.StatusExport()
        export.step(config, 1000)
        replace, seen = os.replace, []

        def observe(source, target):
            seen.append((Path(source).parent, Path(source).stat().st_mode & 0o777,
                         json.loads(Path(source).read_text())["exported_at"],
                         json.loads(Path(target).read_text())["exported_at"]))
            replace(source, target)

        with patch.object(store.os, "replace", side_effect=observe):
            export.step(config, 1030)
        self.assertEqual(seen, [(self.directory, 0o644, 1030, 1000)])
        with patch.object(store.os, "replace", side_effect=OSError("interrupted")), patch("sys.stderr", new=io.StringIO()):
            export.step(config, 1060)
        self.assertEqual(json.loads(self.target.read_text())["exported_at"], 1030)
        self.assertEqual(list(self.directory.iterdir()), [self.target])

    def test_failed_exports_are_logged_once_retried_and_never_raised(self):
        config, export = self.configure(), host.StatusExport()
        self.directory.rmdir()
        with patch("sys.stderr", new=io.StringIO()) as log:
            for now in (1000, 1001, 1030):
                export.step(config, now)
            self.assertEqual(log.getvalue().count("status export failed"), 1)
            # An absent directory is left for the operator to create with the access they intend.
            self.assertFalse(self.directory.exists())
            self.directory.mkdir()
            denied = [PermissionError(13, "Permission denied", str(self.directory / f".fleet-{n}")) for n in range(3)]
            with patch.object(store.tempfile, "mkstemp", side_effect=denied):
                for now in (1060, 1090, 1120):
                    export.step(config, now)
            self.assertEqual(log.getvalue().count("status export failed"), 2)
            self.assertIn(".fleet-0", log.getvalue())
            with patch.object(host, "status_report", side_effect=TypeError("mixed-version report")):
                export.step(config, 1150)
            self.assertEqual(log.getvalue().count("status export failed"), 3)
            export.step(config, 1180)
        self.assertEqual(json.loads(self.target.read_text())["exported_at"], 1180)

    def run_controller_once(self, **patches):
        handlers = {}
        with (
            patch.object(host, "install_pressure_service"),
            patch.object(host, "resources", **patches or dict(return_value=dict(memory_total=32 * 1024**3, memory_available=30 * 1024**3))),
            patch.object(host, "cpu_sample", return_value=(10, 9)),
            patch.object(host, "collect_ci_results"),
            patch.object(host.vm, "slots_in_use", return_value=[]),
            patch.object(host.vm, "outstanding", return_value=False),
            patch.object(host.CISlot, "step", return_value={}),
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
            patch.object(host.time, "sleep", side_effect=lambda _: handlers[signal.SIGTERM]()),
            patch("sys.stderr", new=io.StringIO()) as log,
        ):
            host.daemon()
        return log.getvalue()

    def test_controller_exports_each_report_and_an_unwritable_directory_does_not_disturb_it(self):
        self.run_controller_once()
        self.assertEqual(list(self.directory.iterdir()), [])
        self.configure()
        self.run_controller_once()
        value = json.loads(self.target.read_text())
        self.assertEqual(value["status"]["machine_id"], self.machine)
        self.assertIsNone(value["status"]["error"])
        self.run_controller_once(side_effect=store.FleetError("measurement failed"))
        self.assertEqual(json.loads(self.target.read_text())["status"]["error"], "measurement failed")
        self.target.unlink()
        self.directory.rmdir()
        self.directory.write_text("not a directory")
        (store.state_dir() / "status.json").unlink()
        self.assertIn("status export failed", self.run_controller_once())
        self.assertIsNone(store.read(store.state_dir() / "status.json")["error"])
        self.assertEqual(store.read(store.state_dir() / "admission.json")["reason"], "controller stopped")


class View(unittest.TestCase):
    def render(self, objects, home):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("TAUCETI_")}
        offline = AssertionError("network or subprocess during standalone rendering")
        with (
            patch.dict(os.environ, dict(environment, HOME=str(home)), clear=True),
            patch.object(socket.socket, "connect", side_effect=offline),
            patch.object(socket, "create_connection", side_effect=offline),
            patch.object(socket, "getaddrinfo", side_effect=offline),
            patch.object(subprocess, "Popen", side_effect=offline),
        ):
            return status_view.render(objects)

    def exported(self, root):
        with patch.dict(os.environ, {"TAUCETI_FLEET_" + key.upper(): str(root / key) for key in ("config", "state", "data")}
                        | {"TAUCETI_ACTIVITY_DIR": str(root / "activity")}):
            directory = root / "export"
            directory.mkdir()
            host.rpc(dict(action="status-export", directory=str(directory)))
            workers = [dict(id="w1", state="running", detail="authoring"), dict(id="w2", state="waiting-capacity")]
            store.atomic(store.state_dir() / "status.json", dict(workers=workers, active_worker_limit=2, cpu_idle_fraction=.5))
            host.StatusExport().step(store.host_config(), 1000)
            return {path.stem: json.loads(path.read_text()) for path in directory.glob("*.json")}

    def test_exported_objects_render_without_host_configuration_caches_or_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (machine, value), = self.exported(root).items()
            self.assertEqual(value["machine_id"], machine)
            empty, poisoned = root / "empty", root / "poisoned"
            empty.mkdir()
            for name in ("host.json", "inventory.json", "status-cache/pool-activity.json",
                         "status-cache/merge-queue.json", "status-cache/queue-history.json"):
                path = poisoned / ".config/tauceti-fleet" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{unreadable")
            objects = dict(alpha=value, beta=dict(value, machine_id="b" * 32))
            text = self.render(objects, empty)
            self.assertEqual(self.render(objects, poisoned), text)
            self.assertEqual(list(empty.iterdir()), [])
            self.assertEqual(self.render({}, empty), "")
        for expected in ("alpha:\nWorker rounds: 1 running, 0 surveying, 1 waiting for admission\nNew-round admission limit: 2 / 8",
                         "  w1: running — authoring", "\nbeta:\n", "CI jobs running: unknown", "Fleet totals:", "Bors: unknown"):
            self.assertIn(expected, text)
        for absent in ("CI pool activity", "Merge queue", "Queue history"):
            self.assertNotIn(absent, text)

    def test_sections_come_from_the_objects_and_pools_take_the_given_labels(self):
        now = time.time()
        history = dict(windows=[dict(window=w, completed_batches=3, successful_batches=2, failed_batches=1, canceled_batches=0,
                                     landed_prs=5, mean_staging_runs_per_pr=1.5, landed_batches_without_receipts=0,
                                     staging_pr_runs=6, staging_unique_prs=4, batches_with_unknown_membership=0)
                                for w in ("1h", "3h", "24h", "7d")], durations=[])
        sources = dict(pools=dict(value=dict(counts={"github": 3, "fleet-" + "a" * 32: 2}), observed_at=now),
                       bors=dict(value=dict(batches=[dict(id=7, size=4, state="running")], held=0), observed_at=now),
                       bors_history=dict(value=history, observed_at=now))
        value = dict(machine_id="a" * 32, max_workers=4, status=dict(workers=[]),
                     ci_overview=dict(version=1, repository="TauCetiProject/TauCeti", sources=sources))
        with tempfile.TemporaryDirectory() as home:
            text = self.render(dict(alpha=value), home)
        self.assertIn("Alpha 2", text)
        self.assertIn("Bors: 4=4; held 0", text)
        self.assertIn("Bors history (", text)
        self.assertIn("PRs landed", text)
        self.assertNotIn("Fleet totals:", text)


if __name__ == "__main__":
    unittest.main()
