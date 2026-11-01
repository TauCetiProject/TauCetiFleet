import io
import json
import unittest
from unittest.mock import patch

from tauceti_fleet import focus, host, remote
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
        with patch.object(focus, "update_host") as update, patch.object(host, "install_service") as service:
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

    def test_preset_has_core_and_foundations_without_duplicates(self):
        areas = focus.PRESETS["pde-symplectic"]
        self.assertEqual(areas, sorted(set(areas)))
        self.assertTrue(
            {"PDE", "SymplecticGeometry", "HeegaardFloer", "DGFloer", "TaubesGromov", "Yamabe"} <= set(areas)
        )
        json.dumps(focus.validate(20, areas))


if __name__ == "__main__":
    unittest.main()
