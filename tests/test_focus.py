import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capabilities, focus, host, remote
from tauceti_fleet.cli import main
from tauceti_fleet.store import FleetError


class FocusTests(unittest.TestCase):
    def test_every_capacity_prefix_has_requested_share_and_stable_slots(self):
        config = dict(roadmap_focus=dict(percent=20, areas=["PDE", "SymplecticGeometry"]))
        workers = [f"w{n}" for n in range(1, 41)]
        self.assertEqual(focus.policy(config, workers)["workers"], [f"w{n}" for n in range(5, 41, 5)])
        for percent in (0, 1, 20, 33, 100):
            config["roadmap_focus"]["percent"] = percent
            for n in range(41):
                result = focus.policy(config, workers[:n])
                self.assertEqual(len(result["workers"]), n * percent // 100)
                self.assertEqual(len(result["workers"]) * 100 + result["fractional_percent"], n * percent)
                if result["fractional_percent"]:
                    self.assertIn(result["fractional_worker"], workers[:n])
                    self.assertNotIn(result["fractional_worker"], result["workers"])
                else:
                    self.assertIsNone(result["fractional_worker"])
                self.assertEqual(
                    result["workers"], [w for w in focus.policy(config, workers)["workers"] if w in workers[:n]]
                )
        self.assertEqual(focus.policy({}, workers)["workers"], [])

    def test_bad_updates_do_not_write_and_live_focus_does_not_change_admissions(self):
        for value in (True, -1, 101, 20.0, "20"):
            with self.assertRaises(FleetError):
                focus.validate(value, ["PDE"])
        for areas in ([], "PDE", [True], ["../PDE"], ["PDE,Other"], [""]):
            with self.assertRaises(FleetError):
                focus.validate(20, areas)
        with (
            patch.object(focus, "host_config", return_value={}),
            patch.object(focus, "update_host") as update,
            patch.object(host, "install_service") as service,
        ):
            self.assertEqual(
                host.rpc(dict(action="focus", areas=["PDE", "PDE"], percent=20)), dict(percent=20, areas=["PDE"])
            )
            update.assert_called_once_with(roadmap_focus=dict(percent=20, areas=["PDE"]))
            service.assert_not_called()
            update.reset_mock()
            with self.assertRaises(FleetError):
                focus.configure(dict(preset="unknown"))
            update.assert_not_called()
            self.assertEqual(focus.configure(dict(clear=True)), dict(percent=0, areas=[]))

    def test_query_and_percent_only_update(self):
        current = dict(roadmap_focus=dict(percent=20, areas=["PDE"]))
        with patch.object(focus, "host_config", return_value=current), patch.object(focus, "update_host") as update:
            self.assertEqual(focus.configure({}), current["roadmap_focus"])
            update.assert_not_called()
            self.assertEqual(focus.configure(dict(percent=30)), dict(percent=30, areas=["PDE"]))

    def test_cli_sends_explicit_host_and_preset_and_queries_without_changing(self):
        entry = dict(target="machine", machine_id="one")
        with (
            patch.object(remote, "inventory", return_value={"test": entry}),
            patch.object(remote, "resolve", return_value=entry),
            patch.object(remote, "call", return_value=dict(percent=20, areas=["PDE"])) as call,
            patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(main(["focus", "test", "--preset", "pde-symplectic", "--percent", "20", "--json"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="focus", preset="pde-symplectic", percent=20))
            self.assertEqual(main(["focus", "test", "--json"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="focus"))

    def test_local_cli_needs_no_inventory_enrollment(self):
        with (
            patch.object(remote, "inventory", return_value={}),
            patch("tauceti_fleet.cli.read", return_value=dict(machine_id="local-id")),
            patch.object(remote, "call", return_value=dict(percent=0, areas=[])) as call,
            patch("sys.stdout", new=io.StringIO()),
        ):
            self.assertEqual(main(["focus", "local", "--json"]), 0)
            self.assertEqual(call.call_args.args, ("local", dict(action="focus")))
            self.assertEqual(call.call_args.kwargs["expected_machine_id"], "local-id")

    def test_missing_local_host_config_does_not_enroll_a_machine(self):
        with (
            patch.object(remote, "inventory", return_value={}),
            patch("tauceti_fleet.cli.read", return_value=None),
            patch.object(host, "host_config") as create,
            patch.object(remote, "call") as call,
            patch("sys.stderr", new=io.StringIO()),
        ):
            self.assertEqual(main(["focus", "local", "--json"]), 1)
            create.assert_not_called()
            call.assert_not_called()

    def test_advisory_policy_errors_do_not_raise_and_new_keys_are_ignored(self):
        for raw in (None, [], "PDE", dict(percent="bad", areas=["PDE"]), dict(percent=20, areas=[])):
            with self.subTest(raw=raw):
                result = focus.policy(dict(roadmap_focus=raw), ["w"])
                self.assertEqual(result["percent"], 0)
                self.assertEqual(result["workers"], [])
                self.assertTrue(result["error"])
        result = focus.policy(dict(roadmap_focus=dict(percent=100, areas=["PDE"], future=True)), ["w"])
        self.assertEqual(result["workers"], ["w"])
        self.assertNotIn("error", result)

    def test_replacing_areas_keeps_the_saved_percentage(self):
        with (
            patch.object(focus, "host_config", return_value=dict(roadmap_focus=dict(percent=35, areas=["PDE"]))),
            patch.object(focus, "update_host"),
        ):
            self.assertEqual(focus.configure(dict(areas=["DGFloer"]))["percent"], 35)
            self.assertEqual(focus.configure(dict(preset="pde-symplectic"))["percent"], 35)
            self.assertEqual(focus.configure(dict(areas=["DGFloer"], percent=15))["percent"], 15)
        with patch.object(focus, "host_config", return_value={}), patch.object(focus, "update_host"):
            self.assertEqual(focus.configure(dict(areas=["PDE"]))["percent"], 20)

    def test_doctor_reports_worker_focus_capability_without_blocking_admissions(self):
        config = dict(
            machine_id="test",
            ci_allowed=False,
            ci_memory_gib=64,
            ci_vcpus=32,
            ci_disk_gib=100,
            roadmap_focus=dict(percent=20, areas=["PDE"]),
        )
        for version, expected in ((None, "warning"), (1, "ok")):
            protocol = dict(version=1)
            if version is not None:
                protocol["roadmap_focus"] = version

            def run(args, *, protocol=protocol, **kwargs):
                return subprocess.CompletedProcess(args, 0, json.dumps(protocol) if "_fleet-protocol" in args else "0")

            with (
                tempfile.TemporaryDirectory() as tmp,
                patch.object(
                    capabilities,
                    "resources",
                    return_value=dict(cpus=64, memory_total=128 * 1024**3, memory_available=128 * 1024**3),
                ),
                patch.object(capabilities, "run", side_effect=run),
                patch.object(capabilities, "executable", return_value="/fake/tool"),
                patch.object(capabilities.os, "open", side_effect=PermissionError),
                patch.object(capabilities, "data_dir", return_value=Path(tmp)),
                patch.object(capabilities, "state_dir", return_value=Path(tmp)),
            ):
                checks = capabilities.doctor(config, probe=False)["checks"]
            check = next(c for c in checks if c["name"] == "roadmap-focus")
            self.assertEqual(check["status"], expected)

    def test_status_counts_focused_rounds_in_progress_not_current_slots(self):
        from tauceti_fleet.cli import focused_rounds

        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", TAUCETI_FLEET_STATE=tmp):
            workers = Path(tmp) / "workers"
            workers.mkdir()
            rows = dict(
                a=dict(state="running", roadmap_focus="PDE"),
                b=dict(state="surveying", roadmap_focus="DGFloer"),
                c=dict(state="running", roadmap_focus=None),
                d=dict(state="waiting-capacity", roadmap_focus="PDE"),
                e=dict(state="running"),
            )
            for name, row in rows.items():
                (workers / f"{name}.json").write_text(json.dumps(dict(id=name, **row)))
            statuses = host.worker_statuses()
        self.assertNotIn("roadmap_focus", next(w for w in statuses if w["id"] == "e"))
        # A waiting slot is not a round in progress; an older worker's round is not counted either way.
        self.assertEqual(
            focused_rounds(statuses),
            "2 of 3 rounds in progress focused, 1 more on a worker release without focus reporting",
        )
        self.assertEqual(focused_rounds([dict(state="running", roadmap_focus=None)]), "0 of 1 rounds in progress focused")
        old = [dict(state="running"), dict(state="surveying")]
        self.assertEqual(focused_rounds(old), "2 rounds in progress, focus not reported by this worker release")
        self.assertEqual(focused_rounds(None), "0 of 0 rounds in progress focused")

    def test_preset_is_the_six_direct_focus_roadmaps(self):
        areas = focus.PRESETS["pde-symplectic"]
        self.assertEqual(areas, sorted(set(areas)))
        self.assertEqual(
            set(areas),
            {"PDE", "SymplecticGeometry", "HeegaardFloer", "DGFloer", "HamiltonianFloerOperations", "TaubesGromov"},
        )
        json.dumps(focus.validate(20, areas))


if __name__ == "__main__":
    unittest.main()
