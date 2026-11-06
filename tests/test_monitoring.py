import json
import tempfile
import unittest
from pathlib import Path

from tauceti_fleet import monitoring


class Monitoring(unittest.TestCase):
    def test_projection_has_no_private_fields_and_preserves_unknowns(self):
        sentinel = "PRIVATE_CREDENTIAL_OR_PATH"
        entry = dict(alias="yael", machine_id="a" * 32, target=sentinel)
        value = dict(
            machine_id="a" * 32,
            credentials=sentinel,
            status=dict(
                observed_at=100,
                error=sentinel,
                resources=dict(cpus=16, memory_total=1000, memory_available=200, cgroup=sentinel),
                ci_vms=[
                    dict(
                        slot=1,
                        instance="f" * 16,
                        runner_id=55,
                        directory=sentinel,
                        runner_name="runner",
                        phase="starting",
                    )
                ],
                workers=[dict(worker="w1", alive=True, state="running", command=sentinel)],
            ),
        )
        result = monitoring.project(entry, value, 115, True)
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertEqual(result["slots"][0]["runner_id"], "55")
        self.assertEqual(result["observed_at"], "1970-01-01T00:01:40Z")
        self.assertEqual(result["transport_at"], "1970-01-01T00:01:55Z")
        self.assertIsNone(result["cpu_idle_fraction"])
        self.assertIsNone(result["ci_enabled"])
        self.assertTrue(result["controller_error"])
        result = monitoring.project(entry, value, 130, False)
        self.assertFalse(result["connected"])
        self.assertEqual(result["observed_at"], "1970-01-01T00:01:40Z")

    def test_wrong_machine_cannot_replace_a_host_and_every_explicit_host_can_be_unobserved(self):
        entry = dict(alias="worker-only", machine_id="b" * 32, target="local")
        with self.assertRaises(ValueError):
            monitoring.project(entry, dict(machine_id="c" * 32))
        value = monitoring.project(entry, {})
        self.assertEqual(value["coverage"], "unavailable")
        self.assertEqual(value["slots"], [])
        self.assertIsNone(value["resources"]["cpus"])

    def test_registry_requires_explicit_unique_full_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            entry = dict(alias="yael", machine_id="a" * 32, target="server")
            path.write_text(json.dumps([entry]))
            self.assertEqual(monitoring.registry(path), [entry])
            path.write_text(json.dumps([entry, entry]))
            with self.assertRaises(ValueError):
                monitoring.registry(path)
            entry["machine_id"] = "a" * 12
            path.write_text(json.dumps([entry]))
            with self.assertRaises(ValueError):
                monitoring.registry(path)


class SlotEvidence(unittest.TestCase):
    def test_per_vm_compiler_counters_drop_private_paths_and_keep_their_observation_time(self):
        value = monitoring.project(
            {"machine_id": "a" * 32, "alias": "yael", "target": "private"},
            {
                "machine_id": "a" * 32,
                "status": {"ci_vms": [{"instance": "b" * 16, "runner_id": 77}]},
                "lean_statistics": {
                    "instances": {
                        "b" * 16: {
                            "observed_at": 100,
                            "waiting_now": 3,
                            "running_now": 1,
                            "completed": 10,
                            "oldest_wait": 20,
                            "private_socket": "SECRET",
                        }
                    }
                },
            },
        )
        compiler = value["slots"][0]["compiler"]
        self.assertEqual(compiler["waiting_now"], 3)
        self.assertEqual(compiler["running_now"], 1)
        self.assertEqual(compiler["observed_at"], "1970-01-01T00:01:40Z")
        self.assertNotIn("SECRET", json.dumps(value))


class HostAdmission(unittest.TestCase):
    def test_admission_and_activity_are_numeric_allowlists_with_independent_time(self):
        value = monitoring.project(
            dict(machine_id="a" * 32, alias="yael", target="private"),
            dict(
                ci_kinds=["pr", "staging"],
                ci_staging_slots=2,
                ci_memory_budget_gib=128,
                activity=dict(
                    summary=dict(
                        prepared_at=100,
                        private_path="SECRET",
                        rounds={
                            "24h": {
                                "fix": dict(finished=3, failed=1, unknown=2),
                                "review": dict(finished=5, failed=0, unknown=1),
                            }
                        },
                    )
                ),
            ),
        )
        self.assertEqual(value["admission"]["ci_kinds"], ["pr", "staging"])
        self.assertEqual(value["activity"]["finished_rounds"], 8)
        self.assertEqual(value["activity"]["unknown_rounds"], 3)
        self.assertEqual(value["activity"]["observed_at"], "1970-01-01T00:01:40Z")
        self.assertNotIn("SECRET", json.dumps(value))


class UsualEnable(unittest.TestCase):
    def test_install_uses_broker_policy_without_extra_volunteer_arguments(self):
        from unittest.mock import patch

        from tauceti_fleet import host, process, store

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with (
                patch.object(store, "host_config", return_value={"ci_broker_url": "https://broker.example"}),
                patch.object(store, "state_dir", return_value=path),
                patch.object(host, "units", return_value=path),
                patch.object(host, "command", return_value=["python3", "-m", "tauceti_fleet"]),
                patch.object(process, "run") as run,
            ):
                result = monitoring.install_local()
            unit = (path / "tauceti-fleet-monitoring.service").read_text()
            self.assertIn("--local-broker", unit)
            self.assertIn("PYTHONPATH=" + str(Path(monitoring.__file__).resolve().parent.parent), unit)
            self.assertNotIn("--alias", unit)
            self.assertNotIn("--token-file", unit)
            self.assertFalse(result["admissions_changed"])
            self.assertEqual(run.call_count, 3)
            self.assertEqual(
                run.call_args.args[0], ["systemctl", "--user", "restart", "tauceti-fleet-monitoring.service"]
            )

    def test_retained_release_launches_without_a_site_package_install(self):
        import os
        import subprocess
        import sys

        release = Path(monitoring.__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-S", "-m", "tauceti_fleet.monitoring", "--help"],
                cwd=directory,
                env=dict(os.environ, PYTHONPATH=str(release)),
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--local-broker", result.stdout)

    def test_telemetry_install_failure_cannot_fail_normal_enable(self):
        from contextlib import ExitStack
        from unittest.mock import patch

        from tauceti_fleet import host
        from tauceti_fleet.store import FleetError

        config = dict(
            ci_enabled=True,
            workers_enabled=True,
            ci_allowed=True,
            ci_broker_url="https://broker.example",
            ci_memory_budget_gib=16,
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(host, "host_config", return_value=config))
            stack.enter_context(patch.object(host, "doctor", return_value={"readiness": {}}))
            for name in ("write_slices", "worker_config", "install_service", "resources"):
                stack.enter_context(patch.object(host, name))
            updated = stack.enter_context(patch.object(host, "update_host", return_value=config))
            install = stack.enter_context(patch.object(monitoring, "install_local", side_effect=FleetError("offline")))
            stack.enter_context(patch("sys.stderr"))
            result = host.enable()
        install.assert_called_once_with()
        self.assertTrue(result["ci_enabled"])
        self.assertTrue(result["workers_enabled"])
        self.assertEqual(updated.call_args.kwargs["draining"], False)


if __name__ == "__main__":
    unittest.main()
