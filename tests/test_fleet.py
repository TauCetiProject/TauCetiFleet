import io
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capabilities, host, remote, report, store, vm
from tauceti_fleet.cli import main


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environ = patch.dict(
            os.environ,
            {
                "TAUCETI_FLEET_CONFIG": str(self.root / "config"),
                "TAUCETI_FLEET_STATE": str(self.root / "state"),
                "TAUCETI_FLEET_DATA": str(self.root / "data"),
            },
        )
        self.environ.start()

    def tearDown(self):
        self.environ.stop()
        self.temporary.cleanup()

    def test_identity_and_private_configuration_survive_reinstallation(self):
        one = store.host_config()
        store.update_host(workers_enabled=True)
        two = store.host_config()
        self.assertEqual(one["machine_id"], two["machine_id"])
        self.assertTrue(two["workers_enabled"])
        self.assertEqual((store.config_dir() / "host.json").stat().st_mode & 0o077, 0)

    def test_configure_does_not_activate_or_reset_grants_and_transfers_only_named_secrets(self):
        store.host_config()
        with patch.object(host, "install_service"):
            result = host.configure(
                dict(profile="test", ci_allowed=False, credentials={"anthropic_api_key_file": "test-secret"})
            )
        self.assertFalse(result["ci_enabled"])
        self.assertFalse(result["workers_enabled"])
        value = store.host_config()
        secret = Path(value["anthropic_api_key_file"])
        self.assertEqual(secret.read_text(), "test-secret\n")
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertEqual(secret.stat().st_mode & 0o077, 0)
        self.assertFalse((self.root / "state/budget/events.jsonl").exists())

    def test_funding_requires_explicit_host(self):
        with patch("sys.stderr", new=io.StringIO()), patch.object(remote, "inventory", return_value={}):
            self.assertEqual(main(["budget", "--grant", "10"]), 1)

    def test_reconfiguration_requires_drain_and_invalid_credentials_do_not_partially_install(self):
        store.update_host(workers_enabled=True)
        with self.assertRaisesRegex(store.FleetError, "Drain"):
            host.configure(dict(profile="different"))
        with self.assertRaisesRegex(store.FleetError, "JSON object"):
            host.configure(dict(credentials=[]))

    def test_unsupported_user_services_still_allow_actionable_doctor_reporting(self):
        with patch.object(host, "install_service", side_effect=store.FleetError("user manager unavailable")):
            result = host.configure(dict(ci_allowed=False))
        self.assertIn("user manager", result["service_error"])
        self.assertFalse(result["workers_enabled"])
        self.assertFalse(result["ci_enabled"])

    def test_memory_high_blocks_admissions_using_slice_usage(self):
        result = subprocess.CompletedProcess([], 0, stdout="MemoryCurrent=101\nMemoryHigh=100\n")
        with patch.object(host, "run", return_value=result):
            self.assertTrue(host.worker_pool_pressure())
        result.stdout = "MemoryCurrent=99\nMemoryHigh=100\n"
        with patch.object(host, "run", return_value=result):
            self.assertFalse(host.worker_pool_pressure())

    def test_scaler_yields_then_recovers_gradually(self):
        scaler = host.Scaler()
        args = dict(enabled=True, available=20, reserve=10, idle=0.8, maximum=8)
        self.assertEqual(scaler.step(now=100, ci_busy=False, **args)[0], 1)
        self.assertEqual(scaler.step(now=110, ci_busy=False, **args)[0], 1)
        self.assertEqual(scaler.step(now=170, ci_busy=False, **args)[0], 2)
        self.assertEqual(scaler.step(now=175, ci_busy=True, **args)[0], 1)
        self.assertEqual(scaler.step(now=240, ci_busy=True, **args)[0], 2)
        args["available"] = 1
        self.assertEqual(scaler.step(now=180, ci_busy=False, **args)[0], 0)

    def test_drain_changes_admission_not_desired_worker_configuration(self):
        store.host_config()
        store.update_host(ci_enabled=True, workers_enabled=True)
        host.drain()
        policy = store.read(store.state_dir() / "admission.json")
        self.assertEqual(policy["workers"], [])
        self.assertFalse(store.host_config()["ci_enabled"])
        self.assertFalse(store.host_config()["workers_enabled"])

    def test_no_ci_cannot_be_enabled_by_accident(self):
        store.update_host(ci_allowed=False)
        with self.assertRaisesRegex(store.FleetError, "--no-ci"):
            host.enable(ci=True)

    def test_update_refuses_active_round_and_only_stops_an_idle_manager(self):
        store.host_config()
        with patch.object(host, "worker_statuses", return_value=[dict(alive=True, state="authoring")]):
            with self.assertRaisesRegex(store.FleetError, "draining"):
                host.rpc(dict(action="prepare_update"))
        with (
            patch.object(host, "worker_statuses", return_value=[dict(alive=True, state="waiting-capacity")]),
            patch.object(host, "run") as run,
        ):
            self.assertTrue(host.rpc(dict(action="prepare_update"))["quiescent"])
            self.assertEqual(run.call_args.args[0][-1], "tauceti-fleet-workers.service")

    def test_worker_launcher_uses_only_the_selected_credential_without_saved_gh_selection(self):
        secret = self.root / "token"
        secret.write_text("fake-selected-token")
        store.update_host(github_token_file=str(secret))
        with patch.object(host, "executable", return_value="/fake/tauceti"), patch.object(host.os, "execv") as launch:
            host.run_workers()
            self.assertEqual(os.environ["GH_TOKEN"], "fake-selected-token")
            self.assertEqual(Path(os.environ["GH_CONFIG_DIR"]), store.state_dir() / "gh")
            self.assertNotIn("fake-selected-token", str(launch.call_args))

    def test_report_with_unreachable_uncached_host_has_unknown_fleet_total(self):
        entry = dict(machine_id="a", target="unreachable", profile="test")
        output = io.StringIO()
        with (
            patch.object(remote, "inventory", return_value={"missing": entry}),
            patch.object(remote, "call", side_effect=store.FleetError("offline")),
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(["report", "--json"]), 1)
        result = json.loads(output.getvalue())
        self.assertIsNone(result["reports"][-1]["windows"]["24h"]["settled_api_usd"])
        self.assertIn("missing", result["unavailable_hosts"])

    def test_ssh_target_cannot_inject_shell_options_or_commands(self):
        for target in ["-oProxyCommand=bad", "host;bad", "host$(bad)", "user@host/path", "host\ncommand"]:
            with self.assertRaises(store.FleetError):
                remote.validate_target(target)
        self.assertEqual(remote.validate_target("kim@ci-host"), "kim@ci-host")

    def test_atomic_worker_event_import_and_partial_tail(self):
        store.host_config()
        p = store.state_dir() / "worker-events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        event = dict(
            version=1, machine_id="a", id="1", kind="pr_created", repository="TauCetiProject/TauCeti", pr=1, at=1
        )
        p.write_text(json.dumps(event) + "\n" + '{"partial":')
        self.assertEqual(len(store.export_events()), 1)
        self.assertEqual(len(store.export_events()), 1)
        p.write_text(json.dumps(event) + "\n" + json.dumps(dict(event, id="2", pr=2)) + "\n")
        self.assertEqual(len(store.export_events()), 2)

    def test_budget_reading_restates_settlements_and_rejects_corruption(self):
        path = self.root / "budget.jsonl"
        records = [
            dict(version=1, seq=1, timestamp=1, events=[dict(type="admit", id="a", worker="w", model="m")]),
            dict(version=1, seq=2, timestamp=2, events=[dict(type="settle", id="a", cost="4")]),
            dict(version=1, seq=3, timestamp=3, events=[dict(type="correction", id="a", cost="3")]),
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.assertEqual(report.settled_costs(path)[0], dict(id="a", worker="w", model="m", at=2, usd="3"))
        with path.open("a") as out:
            out.write("partial")
        with self.assertRaises(store.FleetError):
            report.settled_costs(path)
        for amount in ("NaN", "Infinity", "-1", "broken"):
            records[-1]["events"][0]["cost"] = amount
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
            with self.assertRaises(store.FleetError):
                report.settled_costs(path)

    def test_empty_or_incomplete_reports_do_not_invent_zero_spending(self):
        empty = report.summarize([], now=1000)["reports"][-1]
        self.assertIsNone(empty["windows"]["3h"]["settled_api_usd"])
        self.assertIsNone(empty["current"]["pending_estimates_usd"])
        snap = dict(
            machine_id="a",
            observed_at=1000,
            instrumentation_started_at=900,
            events=[dict(id="x", kind="pr_created", at=950, repository="a/b", pr=1)],
            prs=[],
            costs=[],
            coverage_errors=[],
            spending_complete=False,
        )
        value = report.summarize([snap], now=1000)["reports"][0]
        self.assertEqual(value["pr_metadata_unknown"], 1)
        self.assertEqual(value["windows"]["3h"]["prs_originated"], 1)
        self.assertFalse(value["windows"]["3h"]["attribution_covers_window"])
        self.assertIsNone(value["windows"]["3h"]["settled_api_usd"])

    def test_release_archive_contains_only_code_and_manifest(self):
        import zipfile

        with zipfile.ZipFile(io.BytesIO(remote.archive())) as zipped:
            self.assertIn("release.json", zipped.namelist())
            self.assertTrue(all(n.startswith("tauceti_fleet/") or n == "release.json" for n in zipped.namelist()))
            manifest = json.loads(zipped.read("release.json"))
            self.assertEqual(len(manifest["worker_revision"]), 40)

    def test_duplicate_hosts_cannot_double_count_reports(self):
        pr = dict(
            repository="TauCetiProject/TauCeti",
            pr=1,
            state="closed",
            landed_at=900,
            observed_at=1000,
            lean_loc=dict(added=10, deleted=4, net=6),
            head_sha="a" * 40,
        )
        authored = dict(id="created", at=100, kind="pr_created", repository=pr["repository"], pr=1)
        assisted = dict(id="assisted", at=800, kind="pr_activity", repository=pr["repository"], pr=1)
        a = dict(
            machine_id="a",
            observed_at=1000,
            events=[authored],
            prs=[pr],
            costs=[dict(id="x", at=900, usd="2")],
            coverage_errors=[],
        )
        b = dict(machine_id="b", observed_at=1000, events=[assisted], prs=[pr], costs=[], coverage_errors=[])
        result = report.summarize([a, a, b], now=1000, windows={"3h": 10800})
        fleet = result["reports"][-1]["windows"]["3h"]
        self.assertEqual(fleet["prs_landed"], 1)
        self.assertEqual(fleet["prs_assisted"], 1)
        self.assertEqual(fleet["settled_api_usd"], "2")
        self.assertEqual(fleet["lean_loc"]["net"], 6)

    def test_closed_pr_without_landing_is_not_credited(self):
        snap = dict(
            machine_id="a",
            observed_at=1000,
            events=[dict(id="x", kind="pr_created", at=1, repository="a/b", pr=1)],
            prs=[dict(repository="a/b", pr=1, state="closed", landed_at=None)],
            costs=[],
            coverage_errors=[],
        )
        self.assertEqual(report.summarize([snap], now=1000)["reports"][-1]["windows"]["3h"]["prs_landed"], 0)

    def test_bors_requires_landed_head_and_matching_member(self):
        dbpath = self.root / "ci.sqlite"
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute("CREATE TABLE merge_builds(repo,head_sha,members_json,landed)")
            db.execute("CREATE TABLE main_landings(repo,head_sha,landed_at)")
            db.execute(
                "INSERT INTO merge_builds VALUES (?,?,?,1)",
                ("TauCeti", "a" * 40, json.dumps([dict(pr=42, head_sha="b" * 40)])),
            )
            self.assertEqual(report.bors_landings(dbpath), {})
            db.execute("INSERT INTO main_landings VALUES (?,?,?)", ("TauCeti", "a" * 40, "2026-10-15T00:00:00Z"))
        known = report.bors_landings(dbpath)
        self.assertIn(("TauCetiProject/TauCeti", 42, "b" * 40), known)

    def test_download_does_not_replace_good_file_with_wrong_image(self):
        target = self.root / "disk"
        target.write_bytes(b"original")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaisesRegex(store.FleetError, "checksum"):
                vm.download("https://example.invalid/image", target, "a" * 64)
        self.assertEqual(target.read_bytes(), b"original")

    def test_cloud_seed_is_data_not_shell_or_yaml_interpolation(self):
        directory = self.root / "seed"
        with patch.object(vm, "run", side_effect=lambda args, **_: Path(args[1]).touch()):
            vm.cloud_seed(directory, ["echo 'quoted'"], name="test")
        user = json.loads((directory / "user-data").read_text().split("\n", 1)[1])
        self.assertEqual(user["runcmd"], [["bash", "-euc", "echo 'quoted'"]])
        self.assertEqual((directory / "seed.iso").stat().st_mode & 0o077, 0)

    def test_controller_restart_adopts_existing_guest_without_second_registration(self):
        store.atomic(store.data_dir() / "images/prepared.json", dict(sha256="a" * 64))
        value = dict(unit="existing.service", instance="existing")
        store.atomic(store.state_dir() / "vm.json", value)
        with (
            patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)),
            patch.object(vm.github, "jit_config") as jit,
        ):
            self.assertEqual(vm.start(store.host_config()), value)
            jit.assert_not_called()

    def test_failed_ci_job_sets_persistent_cooldown(self):
        store.atomic(store.state_dir() / "pending-ci.json", [dict(run_id=42, runner_id=12)])
        job = dict(
            id=7,
            runner_id=12,
            status="completed",
            conclusion="failure",
            started_at="2026-10-15T00:00:00Z",
            completed_at="2026-10-15T00:01:00Z",
        )
        with (
            patch.object(host.github, "request", return_value=dict(jobs=[job])),
            patch.object(host.time, "time", return_value=1000),
        ):
            host.collect_ci_results(store.host_config())
        self.assertEqual(store.read(store.state_dir() / "ci-backoff.json")["until"], 1900)
        self.assertEqual(store.read(store.state_dir() / "pending-ci.json"), [])

    def test_resource_probe_verifies_kernel_not_just_systemd_exit(self):
        fake = subprocess.CompletedProcess([], 0, stdout='{"cpu.weight":"100"}', stderr="")
        with patch.object(capabilities, "run", return_value=fake):
            with self.assertRaisesRegex(store.FleetError, "not enforced"):
                capabilities.resource_probe()


if __name__ == "__main__":
    unittest.main()
