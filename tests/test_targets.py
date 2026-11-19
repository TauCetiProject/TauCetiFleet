"""Fleet wiring and cross-repository target-list contract tests."""

import json
import os
import signal
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capabilities, host, store, targets
from tauceti_fleet.store import FleetError

INSPECT_FILE = targets.inspect_file

EXAMPLE = """<!-- tauceti-targets:v1 -->
## Algebra
- [x] `first` — prerequisite
- [ ] `second` — next (needs: first)
- [~] `third` — held (needs: none; in flight: #42)
- [ ] `fourth` — dependent (needs: third)
"""
# Captured canonical Worker inspection output; no Fleet-side grammar implementation.
CANONICAL = dict(schema="tauceti-target-list/v1", preamble="", areas=["Algebra"], items=[
    dict(area="Algebra", slug="first", status="done", needs=[], meta=[], text="prerequisite"),
    dict(area="Algebra", slug="second", status="open", needs=["first"], meta=[], text="next"),
    dict(area="Algebra", slug="third", status="inflight", needs=[], meta=["in flight: #42"], text="held"),
    dict(area="Algebra", slug="fourth", status="open", needs=["third"], meta=[], text="dependent"),
])


class TargetLists(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environ = patch.dict(os.environ, {
            "TAUCETI_FLEET_CONFIG": str(self.root / "config"), "TAUCETI_FLEET_STATE": str(self.root / "state"),
            "TAUCETI_FLEET_DATA": str(self.root / "data"),
        })
        environ.start()
        self.addCleanup(environ.stop)
        store.update_host(github_account="operator", ci_allowed=False)
        protocol = patch.object(capabilities, "worker_protocol", return_value=dict(version=1, roadmap_targets=1, targets_inspect=1))
        protocol.start()
        self.addCleanup(protocol.stop)
        self.find_manager = targets.running_manager
        manager = patch.object(targets, "running_manager", return_value=None)
        manager.start()
        self.addCleanup(manager.stop)
        inspect = patch.object(targets, "inspect_file", return_value=CANONICAL)
        inspect.start()
        self.addCleanup(inspect.stop)

    def test_transfers_clear_preserve_worker_environment_and_native_routing(self):
        config = store.update_host(max_workers=2, forgejo_workers=1, worker_env={"OTHER": "value"})
        host.worker_config(config)
        plain = (store.config_dir() / "workers.toml").read_text()
        result = targets.configure(dict(text=EXAMPLE), [])
        self.assertEqual(result["list"]["eligible"], 1)
        self.assertEqual(targets.stored().read_text(), EXAMPLE)
        self.assertEqual(targets.stored().stat().st_mode & 0o777, 0o600)
        host.worker_config(store.host_config())
        specs = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
        self.assertEqual({s["roadmap_targets"] for s in specs}, {str(targets.stored())})
        self.assertEqual([s["env"] for s in specs], [{"OTHER": "value"}] * 2)
        self.assertEqual((specs[1]["github"], specs[1]["forgejo"], specs[1]["new_pr_forge"]), (False, True, "forgejo"))
        targets.configure(dict(clear=True), [])
        host.worker_config(store.host_config())
        self.assertEqual((store.config_dir() / "workers.toml").read_text(), plain)
        self.assertTrue(targets.stored().exists())

    def test_controller_writer_does_not_restore_a_stale_target_path(self):
        old = store.host_config()
        targets.configure(dict(text=EXAMPLE), [])
        host.worker_config(old)
        specs = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
        self.assertEqual(specs[0]["roadmap_targets"], str(targets.stored()))

    def test_resolved_unicode_path_and_symlink_retargeting(self):
        one, two, link = self.root / "goal-𝛼\x7f.md", self.root / "other.md", self.root / "current.md"
        one.write_text(EXAMPLE)
        two.write_text(EXAMPLE)
        link.symlink_to(one)
        targets.configure(dict(path=str(link)), [])
        host.worker_config(store.host_config())
        self.assertEqual(tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"][0]["roadmap_targets"], str(one))
        link.unlink()
        link.symlink_to(two)
        self.assertEqual(targets.configure(dict(path=str(link)), [])["roadmap_targets"], str(two))
        whitespace = self.root / "bad.md "
        whitespace.write_text(EXAMPLE)
        link.unlink()
        link.symlink_to(whitespace)
        with self.assertRaisesRegex(FleetError, "whitespace"):
            targets.configure(dict(path=str(link)), [])
        self.assertEqual(store.host_config()["roadmap_targets"], str(two))

    def test_invalid_list_changes_neither_stored_copy_nor_configuration(self):
        targets.configure(dict(text=EXAMPLE), [])
        old = store.host_config()
        with patch.object(targets, "inspect_file", side_effect=FleetError("undefined dependency")):
            with self.assertRaisesRegex(FleetError, "undefined"):
                targets.configure(dict(text="invalid"), [])
        self.assertEqual(store.host_config(), old)
        self.assertEqual(targets.stored().read_text(), EXAMPLE)

    def test_runtime_capabilities_checked_before_writing_and_on_controller_rewrite(self):
        with patch.object(capabilities, "worker_protocol", return_value=dict(version=1, roadmap_targets=1)):
            with self.assertRaisesRegex(FleetError, "canonical target-list"):
                targets.configure(dict(text=EXAMPLE), [])
        self.assertFalse(targets.stored().exists())
        with patch.object(targets, "running_manager", return_value="/old/bin/tauceti"), patch.object(host, "manager_ping", return_value={}):
            with self.assertRaisesRegex(FleetError, "running worker manager lacks"):
                targets.configure(dict(text=EXAMPLE), [])
        targets.configure(dict(text=EXAMPLE), [])
        with patch.object(targets, "running_manager", return_value="/old/bin/tauceti"), patch.object(host, "manager_ping", return_value={}):
            with self.assertRaises(FleetError):
                host.worker_config(store.host_config())
        self.assertFalse((store.config_dir() / "workers.toml").exists())

    def test_live_support_uses_capability_reply_from_the_expected_configuration(self):
        reply = dict(ok=True, roadmap_targets=1, targets_inspect=1, graceful_restart=1,
                     config=str(store.config_dir() / "workers.toml"))
        with patch.object(targets, "running_manager", return_value="/live/bin/tauceti"), patch.object(host, "manager_ping", return_value=reply):
            self.assertEqual(targets.require_support(), "/live/bin/tauceti")
            reply["config"] = "/other/workers.toml"
            with self.assertRaisesRegex(FleetError, "running worker manager"):
                targets.require_support()

    def test_symlink_config_uses_workers_resolved_configuration_identity(self):
        actual = self.root / "actual-config"
        actual.mkdir()
        alias = self.root / "alias-config"
        alias.symlink_to(actual, target_is_directory=True)
        reply = dict(ok=True, roadmap_targets=1, targets_inspect=1, graceful_restart=1,
                     config=str(actual / "workers.toml"))
        with patch.dict(os.environ, TAUCETI_FLEET_CONFIG=str(alias)), patch.object(targets, "running_manager", return_value="/live/bin/tauceti"), patch.object(host, "manager_ping", return_value=reply):
            targets.require_support()

    def test_inspection_is_cached_but_file_replacement_and_runtime_change_invalidate_it(self):
        path = self.root / "cached.md"
        path.write_text(EXAMPLE)
        result = subprocess.CompletedProcess([], 0, json.dumps(CANONICAL), "")
        with patch.object(targets, "running_manager", return_value="/live/bin/tauceti") as manager, patch.object(targets, "run", return_value=result) as run:
            INSPECT_FILE(path)
            INSPECT_FILE(path)
            self.assertEqual(run.call_count, 1)
            replacement = path.with_suffix(".new")
            replacement.write_text(EXAMPLE)
            replacement.replace(path)
            INSPECT_FILE(path)
            self.assertEqual(run.call_count, 2)
            manager.return_value = "/other/bin/tauceti"
            INSPECT_FILE(path)
            self.assertEqual(run.call_count, 3)

    def test_worker_policy_incompatible_manager_refuses_before_persisting(self):
        targets.configure(dict(text=EXAMPLE), [])
        before = store.host_config()
        with patch.object(targets, "require_support", side_effect=FleetError("old manager")):
            with self.assertRaisesRegex(FleetError, "old manager"):
                host.set_worker_policy(dict(worker_env={"OTHER": "value"}, maximum=2))
        self.assertEqual(store.host_config(), before)
        self.assertFalse((store.state_dir() / "worker-admission-lease.json").exists())

    def test_admission_decreases_remain_available_when_the_manager_cannot_be_queried(self):
        targets.configure(dict(text=EXAMPLE), [])
        store.update_host(max_workers=4)
        with patch.object(targets, "require_support", side_effect=FleetError("unavailable manager")):
            self.assertEqual(host.set_worker_policy(dict(maximum=2))["max_workers"], 2)
            self.assertEqual(host.set_worker_policy(dict(maximum=0))["max_workers"], 0)
            self.assertEqual(store.read(store.state_dir() / "admission.json")["workers"], [])
            with self.assertRaisesRegex(FleetError, "unavailable manager"):
                host.set_worker_policy(dict(maximum=1))
        self.assertEqual(store.host_config()["max_workers"], 0)

    def test_clear_does_not_require_installed_parser_but_requires_graceful_live_manager(self):
        targets.configure(dict(text=EXAMPLE), [])
        with patch.object(targets, "running_manager", return_value="/old/bin/tauceti"), patch.object(host, "manager_ping", return_value={}):
            with self.assertRaisesRegex(FleetError, "running worker manager"):
                targets.configure(dict(clear=True), [])
        self.assertIsNotNone(store.host_config()["roadmap_targets"])
        with patch.object(capabilities, "worker_protocol", return_value={}):
            self.assertIsNone(targets.configure(dict(clear=True), [])["roadmap_targets"])

    def test_manager_identity_uses_actual_process_not_stale_metadata(self):
        store.atomic(store.state_dir() / "worker-manager.json", dict(binary="/stale/bin/tauceti"))
        with patch.object(targets, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(
            host, "manager_process", return_value=(["/live/bin/tauceti", "workers", "manager"], {})
        ):
            self.assertEqual(self.find_manager(), "/live/bin/tauceti")
        with patch.object(targets, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(host, "manager_process", return_value=None):
            self.assertEqual(self.find_manager(), "")

    def test_audit_never_ticks_a_possible_partial_merge_or_overwrites_concurrent_edits(self):
        path = self.root / "goal.md"
        path.write_text(EXAMPLE)
        store.update_host(roadmap_targets=str(path))
        with patch.object(targets, "parse", return_value=CANONICAL):
            result = targets.configure(dict(audit=True, apply=True), [dict(id="worker", state="running", roadmap_target="Algebra/first")])
        self.assertEqual((result["corrections"], result["applied"]), (0, False))
        self.assertEqual(path.read_text(), EXAMPLE)
        self.assertTrue(any("nonpartial landing markers" in f["detail"] for f in result["findings"]))
        self.assertTrue(any("marks done" in f["detail"] for f in result["findings"]))

    def test_active_verifier_holders_and_stale_between_round_status(self):
        for state in ("running", "verifying", "working", "verification-pending", "verification-incomplete"):
            worker = dict(id="w", state=state, roadmap_target="Algebra/second", next_action_at=None)
            self.assertEqual(targets.summarize(CANONICAL, [worker])["inflight"], 2)
        worker.update(state="verification-pending", next_action_at=1)
        self.assertEqual(targets.summarize(CANONICAL, [worker])["inflight"], 1)

    def test_controller_publishes_list_errors_without_suppressing_valid_admissions(self):
        targets.configure(dict(text=EXAMPLE), [])
        store.update_host(workers_enabled=True, max_workers=2)
        handlers, written = {}, {}
        with (
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
            patch.object(host.time, "sleep", side_effect=lambda _: handlers[signal.SIGTERM]()),
            patch.object(host, "cpu_sample", side_effect=[(1, 0), (2, 1)]),
            patch.object(host, "resources", return_value=dict(cpus=8, memory_total=64 * 1024**3, memory_available=64 * 1024**3)),
            patch.object(host, "worker_memory", return_value=None),
            patch.object(host, "start_workers"),
            patch.object(host, "worker_statuses", return_value=[]),
            patch.object(host, "atomic", side_effect=lambda path, value: written.setdefault(Path(path).name, value)),
            patch.object(targets, "inspect_file", side_effect=FleetError("strict list error")),
        ):
            host.daemon()
        self.assertEqual(written["status.json"]["roadmap_targets"]["error"], "strict list error")
        self.assertEqual(len(written["admission.json"]["workers"]), 2, {k: written["status.json"].get(k) for k in ("scaling_reason", "worker_admission", "error")})
        specs = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
        self.assertEqual({s["roadmap_targets"] for s in specs}, {str(targets.stored())})

    def test_status_error_is_reported_without_disrupting_admission(self):
        with patch.object(targets, "inspect_file", side_effect=FleetError("list invalid")):
            self.assertEqual(targets.status(dict(roadmap_targets="/goal.md"), [])["error"], "list invalid")

    def test_inspection_transport_uses_actual_runtime_and_validates_schema(self):
        # Undo fixture inspection mocking only for this transport-level check.
        original = INSPECT_FILE
        result = subprocess.CompletedProcess([], 0, json.dumps(CANONICAL), "")
        path = self.root / "goal.md"
        path.write_text(EXAMPLE)
        with patch.object(targets, "running_manager", return_value="/live/bin/tauceti"), patch.object(targets, "run", return_value=result) as run:
            self.assertEqual(original(path), CANONICAL)
        self.assertEqual(run.call_args.args[0], ["/live/bin/tauceti", "_targets-inspect", "--file", str(path)])
        targets._INSPECTIONS.clear()
        with patch.object(targets, "running_manager", return_value="/live/bin/tauceti"), patch.object(targets, "run", return_value=subprocess.CompletedProcess([], 0, '{}', '')):
            with self.assertRaisesRegex(FleetError, "invalid.*inspection"):
                original(path)


@unittest.skipUnless(os.environ.get("TAUCETI_TEST_WORKER"), "cross-repository Worker executable not supplied")
class WorkerContract(unittest.TestCase):
    def inspect(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "goal-𝛼.md"
            path.write_text(text)
            with patch.object(targets, "running_manager", return_value=None), patch.object(targets, "executable", return_value=os.environ["TAUCETI_TEST_WORKER"]):
                return targets.inspect_file(path)

    def test_exact_worker_parser_and_dependency_summary(self):
        value = self.inspect(EXAMPLE)
        self.assertEqual(value["items"], CANONICAL["items"])
        self.assertEqual(targets.summarize(value, [])["eligible"], value["summary"]["eligible"])
        for invalid in (EXAMPLE.replace("needs: first", "needs: missing"), EXAMPLE.replace('`second`', '`first`'),
                        EXAMPLE.replace("needs: first", "needs: fourth").replace("needs: third", "needs: second"),
                        EXAMPLE + "- [ ] missing slug\n"):
            with self.subTest(invalid=invalid), self.assertRaises(FleetError):
                self.inspect(invalid)

    def test_worker_accepts_generated_definition_without_any_credential_or_network(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, TAUCETI_FLEET_CONFIG=directory + "/config", TAUCETI_FLEET_STATE=directory + "/state", TAUCETI_FLEET_DATA=directory + "/data"):
            store.update_host(ci_allowed=False, max_workers=2, forgejo_workers=1, worker_env={"OTHER": "value"})
            with patch.object(targets, "running_manager", return_value=None), patch.object(capabilities, "worker_protocol", return_value=dict(version=1, roadmap_targets=1, targets_inspect=1)), patch.object(targets, "executable", return_value=os.environ["TAUCETI_TEST_WORKER"]):
                targets.configure(dict(text=EXAMPLE), [])
                host.worker_config(store.host_config())
            environment = dict(os.environ, TAUCETI_RUNTIME_DIR=directory + "/runtime", TAUCETI_WORKERS_STATE_DIR=directory + "/workers", TAUCETI_ACTIVITY_DIR=directory + "/activity")
            result = subprocess.run([os.environ["TAUCETI_TEST_WORKER"], "workers", "--config", str(store.config_dir() / "workers.toml"), "status", "--json"], capture_output=True, text=True, env=environment)
            self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(len(value["workers"]), 2, value)


if __name__ == "__main__":
    unittest.main()
