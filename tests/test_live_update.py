import io
import fcntl
import json
import os
import shlex
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import cli, host, hostos, remote, store, update

hostos.MACOS = False


class LiveUpdate(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        env = patch.dict(os.environ, dict(HOME=str(self.home), **{
            "TAUCETI_FLEET_" + name.upper(): str(self.home / name) for name in ("config", "state", "data")
        }))
        env.start()
        self.addCleanup(env.stop)
        self.config = store.update_host(ci_enabled=True, workers_enabled=True, ci_max_jobs=5)
        self.previous = store.data_dir() / "releases/previous"
        self.release = store.data_dir() / "releases/new"
        self.previous.mkdir(parents=True)
        self.release.mkdir()
        update.symlink(store.data_dir() / "current", self.previous)
        self.old_worker = store.data_dir() / "worker-venv/bin/tauceti"
        self.old_worker.parent.mkdir(parents=True)
        self.old_worker.write_text("old worker stays in use")
        update.symlink(store.data_dir() / "bin/tauceti", self.old_worker)
        self.revision = "a" * 40
        latest = patch.object(update, "latest_worker_revision", return_value=self.revision)
        latest.start()
        self.addCleanup(latest.stop)
        store.atomic(self.release / "release.json", dict(worker_revision=self.revision))
        store.atomic(store.data_dir() / "release.json", dict(worker_revision="b" * 40))
        store.atomic(store.data_dir() / "worker-runtime.json", dict(revision="b" * 40))
        self.binary = self.home / ".local/bin/tauceti-fleet"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("old launcher")
        self.binary.chmod(0o755)
        store.atomic(store.state_dir() / "admission.json", dict(workers=["paid-session"], expires_at=time.time() + 15))
        store.atomic(store.state_dir() / "ci/slot-0/vm.json", dict(unit="running-guest", runner_id=123))
        self.protected = {path: path.read_bytes() for path in (
            store.config_dir() / "host.json", store.state_dir() / "admission.json",
            store.state_dir() / "ci/slot-0/vm.json", self.old_worker)}

    def assert_work_preserved(self):
        for path, value in self.protected.items():
            self.assertEqual(path.read_bytes(), value, str(path))

    def install(self):
        return update.install(self.release, expected_machine_id=self.config["machine_id"])

    def test_staging_failure_does_not_activate_or_touch_running_work(self):
        with patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")), \
                patch.object(update, "stage_worker", side_effect=store.FleetError("download failed")), \
                patch.object(host, "install_service") as service:
            with self.assertRaisesRegex(store.FleetError, "download failed"):
                self.install()
        service.assert_not_called()
        self.assertEqual((store.data_dir() / "current").resolve(), self.previous)
        self.assertEqual((store.data_dir() / "bin/tauceti").resolve(), self.old_worker)
        self.assert_work_preserved()

    def test_live_activation_preserves_credentials_roles_guests_and_workers(self):
        staged = store.data_dir() / "worker-runtimes" / self.revision
        def handover(release, **kwargs):
            self.assertEqual((store.data_dir() / "current").resolve(), release)
            self.assertEqual((store.data_dir() / "bin/tauceti").readlink(), staged / "bin/tauceti")
            return dict(controller_release=str(release))
        with patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")), \
                patch.object(update, "stage_worker", return_value=staged), \
                patch.object(host, "install_service") as service, patch.object(update, "handover", side_effect=handover):
            result = self.install()
        service.assert_not_called()
        self.assertTrue(result["installed"])
        self.assertIn("running managers and workers retain", result["worker_runtime_scope"])
        self.assert_work_preserved()

    def test_update_uses_main_instead_of_bundled_or_independently_selected_revision(self):
        store.atomic(self.release / "release.json", dict(worker_revision="c" * 40))
        staged = store.data_dir() / "worker-runtimes" / self.revision
        with (patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")),
              patch.object(update, "stage_worker", return_value=staged) as stage,
              patch.object(update, "handover", return_value=dict(controller_release=str(self.release)))):
            result = self.install()
        stage.assert_called_once_with(self.revision)
        self.assertEqual(result["worker_revision"], self.revision)
        self.assertEqual(store.read(store.data_dir() / "worker-runtime.json"), dict(revision=self.revision))
        self.assert_work_preserved()

    def test_main_resolution_failure_leaves_installed_release_and_worker_untouched(self):
        with (patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")),
              patch.object(update, "latest_worker_revision", side_effect=store.FleetError("main unavailable")),
              patch.object(update, "stage_worker") as stage, patch.object(update, "handover") as handover):
            with self.assertRaisesRegex(store.FleetError, "main unavailable"):
                self.install()
        stage.assert_not_called()
        handover.assert_not_called()
        self.assertEqual((store.data_dir() / "current").resolve(), self.previous)
        self.assertEqual((store.data_dir() / "bin/tauceti").resolve(), self.old_worker)
        self.assertEqual(store.read(store.data_dir() / "worker-runtime.json")["revision"], "b" * 40)
        self.assert_work_preserved()

    def test_unchanged_fleet_still_updates_worker_when_main_advances(self):
        update.symlink(store.data_dir() / "current", self.release)
        store.atomic(store.state_dir() / "status.json", dict(controller_release=str(self.release), controller_pid=1, observed_at=time.time()))
        staged = store.data_dir() / "worker-runtimes" / self.revision
        with (patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")),
              patch.object(update, "stage_worker", return_value=staged),
              patch.object(update, "handover", return_value=dict(controller_release=str(self.release))) as handover):
            result = self.install()
        self.assertNotIn("unchanged", result)
        handover.assert_called_once()
        self.assertEqual(store.read(store.data_dir() / "worker-runtime.json")["revision"], self.revision)

    def test_same_worker_and_fleet_are_unchanged_through_symlinked_data_directory(self):
        actual_data = store.data_dir()
        alias = self.home / "data-alias"
        alias.symlink_to(actual_data, target_is_directory=True)
        staged = actual_data / "worker-runtimes" / self.revision
        binary = staged / "bin/tauceti"
        binary.parent.mkdir(parents=True)
        binary.write_text("installed worker")
        update.symlink(actual_data / "current", self.release)
        update.symlink(actual_data / "bin/tauceti", binary)
        store.atomic(actual_data / "worker-runtime.json", dict(revision=self.revision))
        store.atomic(store.state_dir() / "status.json", dict(controller_release=str(self.release), controller_pid=1, observed_at=time.time()))
        with (patch.dict(os.environ, {"TAUCETI_FLEET_DATA": str(alias)}),
              patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")),
              patch.object(update, "stage_worker", return_value=alias / "worker-runtimes" / self.revision),
              patch.object(update, "handover") as handover):
            result = self.install()
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["worker_revision"], self.revision)
        handover.assert_not_called()

    def test_completed_alternate_runtime_is_reused_after_interrupted_install(self):
        root = store.data_dir() / "worker-runtimes" / self.revision
        root.mkdir(parents=True)
        completed = root.with_name(self.revision + "-" + "c" * 32)
        (completed / "bin").mkdir(parents=True)
        (completed / "bin/tauceti").write_text("ready worker")
        store.atomic(completed / "ready.json", dict(revision=self.revision))
        with patch.object(update, "run", side_effect=AssertionError("must reuse completed installation")):
            self.assertEqual(update.stage_worker(self.revision), completed)
            self.assertEqual(update.stage_worker(self.revision), completed)

    def test_unsupported_service_refuses_before_worker_lookup_or_install(self):
        with (patch.object(update, "service", return_value=dict(MainPID="1", Restart="no")),
              patch.object(update, "latest_worker_revision") as latest,
              patch.object(update, "stage_worker") as stage):
            with self.assertRaisesRegex(store.FleetError, "Restart=on-failure"):
                self.install()
        latest.assert_not_called()
        stage.assert_not_called()
        self.assert_work_preserved()

    def test_prepare_selects_main_and_does_not_restore_manifest_pin(self):
        config = store.update_host(workers_enabled=False, ci_enabled=False, ci_allowed=False)
        staged = store.data_dir() / "worker-runtimes" / self.revision
        with (patch.object(update, "stage_worker", return_value=staged) as stage,
              patch.object(update.vm, "outstanding", return_value=False)):
            result = host.prepare(config)
        stage.assert_called_once_with(self.revision)
        self.assertEqual(result["worker_revision"], self.revision)
        self.assertEqual(store.read(store.data_dir() / "worker-runtime.json")["revision"], self.revision)

    def test_standard_prepare_exports_native_image_for_existing_pr_role(self):
        config = store.update_host(ci_kinds=["pr"], workers_enabled=False, ci_enabled=False)
        staged = store.data_dir() / "worker-runtimes" / self.revision
        with (patch.object(update, "stage_worker", return_value=staged),
              patch.object(update.vm, "outstanding", return_value=False),
              patch.object(update.vm, "prepare_image", return_value={"sha256": "a" * 64}),
              patch.object(update.native_runner, "prepare", return_value={"sha256": "b" * 64}) as prepare,
              patch.object(update.vm, "smoke", return_value={})):
            result = host.prepare(config)
        prepare.assert_called_once_with(config)
        self.assertEqual(result["native_image"]["sha256"], "b" * 64)

    def test_live_update_prepares_native_before_handover_and_preserves_active_vm(self):
        self.config = store.update_host(ci_kinds=["pr"])
        self.protected[store.config_dir() / "host.json"] = (store.config_dir() / "host.json").read_bytes()
        operations = []

        def prepare(config):
            self.assertEqual((store.data_dir() / "current").resolve(), self.previous)
            self.assert_work_preserved()
            operations.append("image")

        def handover(release, **kwargs):
            self.assertEqual(operations, ["image"])
            operations.append("handover")
            return dict(controller_release=str(release))

        with (patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")),
              patch.object(update.native_runner, "prepare", side_effect=prepare),
              patch.object(update, "stage_worker", return_value=store.data_dir() / "worker-runtimes" / self.revision),
              patch.object(update, "handover", side_effect=handover)):
            self.assertTrue(self.install()["installed"])
        self.assertEqual(operations, ["image", "handover"])
        self.assert_work_preserved()

    def test_native_preparation_failure_preserves_live_release_and_admissions(self):
        self.config = store.update_host(ci_kinds=["pr"])
        self.protected[store.config_dir() / "host.json"] = (store.config_dir() / "host.json").read_bytes()
        with (patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")),
              patch.object(update.native_runner, "prepare", side_effect=store.FleetError("image export failed")),
              patch.object(update, "handover") as handover):
            with self.assertRaisesRegex(store.FleetError, "image export failed"):
                self.install()
        handover.assert_not_called()
        self.assertEqual((store.data_dir() / "current").resolve(), self.previous)
        self.assert_work_preserved()

    def test_failed_activation_restores_both_runtimes_and_recovers_old_controller(self):
        def fail_then_recover(release, **kwargs):
            if "on_signal" in kwargs:
                kwargs["on_signal"]()
                raise store.FleetError("unhealthy")
            return {}
        with patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")), \
                patch.object(update, "stage_worker", return_value=self.home / "new-worker"), \
                patch.object(host, "install_service"), \
                patch.object(update, "handover", side_effect=fail_then_recover) as handover:
            with self.assertRaisesRegex(store.FleetError, "previous release restored and controller recovered"):
                self.install()
        self.assertEqual(handover.call_args.args, (self.previous,))
        self.assertEqual((store.data_dir() / "current").resolve(), self.previous)
        self.assertEqual((store.data_dir() / "bin/tauceti").resolve(), self.old_worker)
        self.assertEqual(store.read(store.data_dir() / "worker-runtime.json")["revision"], "b" * 40)
        self.assertEqual(store.read(store.data_dir() / "release.json")["worker_revision"], "b" * 40)
        self.assertEqual(self.binary.read_text(), "old launcher")
        self.assert_work_preserved()

    def test_identity_mismatch_prevents_even_runtime_staging(self):
        with patch.object(update, "stage_worker") as stage:
            with self.assertRaisesRegex(store.FleetError, "identity differs"):
                update.install(self.release, expected_machine_id="c" * 32)
        stage.assert_not_called()
        self.assert_work_preserved()

    def test_legacy_ssh_identity_is_verified_before_transferring_an_update(self):
        with patch.object(remote, "call", side_effect=store.FleetError("identity differs")), \
                patch.object(remote, "ssh") as ssh:
            with self.assertRaisesRegex(store.FleetError, "identity differs"):
                remote.deploy("server", live=True, expected_machine_id=self.config["machine_id"])
        ssh.assert_not_called()

    def test_ci_only_host_does_not_require_uv_or_worker_credentials(self):
        store.update_host(workers_enabled=False)
        (store.data_dir() / "bin/tauceti").unlink()
        with patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")), \
                patch.object(update, "stage_worker", side_effect=AssertionError("No worker install needed")), \
                patch.object(host, "install_service"), \
                patch.object(update, "handover", return_value=dict(controller_release=str(self.release))):
            self.assertIsNone(self.install()["worker_revision"])

    def test_handover_uses_sighup_for_modern_controller_and_main_only_kill_for_legacy(self):
        for protocol, signal in ((1, "SIGHUP"), (None, "SIGKILL")):
            store.atomic(store.state_dir() / "status.json", dict(live_update_protocol=protocol, controller_pid=42))
            def command(argv, **kwargs):
                self.assertIn("--kill-whom=main", argv)
                self.assertIn("--signal=" + signal, argv)
                store.atomic(store.state_dir() / "status.json", dict(
                    observed_at=time.time() + 1, controller_release=str(self.release), controller_pid=42))
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(update, "service", return_value=dict(MainPID="42", Restart="on-failure")), \
                    patch.object(update, "run", side_effect=command):
                update.handover(self.release)
            admission = store.read(store.state_dir() / "admission.json")
            self.assertEqual(admission["workers"], ["paid-session"])
            self.assertGreater(admission["expires_at"], time.time() + 100)

    def test_hup_during_startup_recovers_an_inactive_controller(self):
        store.atomic(store.state_dir() / "status.json", dict(live_update_protocol=1, controller_pid=42))
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if "start" in argv:
                store.atomic(store.state_dir() / "status.json", dict(
                    observed_at=time.time() + 1, controller_release=str(self.release), controller_pid=43))
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(update, "service", side_effect=[dict(MainPID="42", Restart="on-failure"), dict(MainPID="0"), dict(MainPID="43")]), \
                patch.object(update, "run", side_effect=command), patch.object(update.time, "sleep"):
            update.handover(self.release)
        self.assertIn("--signal=SIGHUP", calls[0])
        self.assertEqual([v[2] for v in calls[1:]], ["reset-failed", "start"])

    def test_rollback_forces_kill_even_with_stale_modern_protocol_and_confirms_legacy(self):
        store.atomic(store.state_dir() / "status.json", dict(live_update_protocol=1, controller_pid=42))
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if "kill" in argv:
                for slot in range(5):
                    with update.vm.slot_file({"ci_slot": slot}, "vm.lock").open("a") as stream:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                store.atomic(store.state_dir() / "status.json", dict(observed_at=time.time() + 1, error=None))
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(update, "service", return_value=dict(MainPID="42", Restart="on-failure")), \
                patch.object(update, "run", side_effect=command):
            update.handover(self.previous, rollback=True)
        self.assertIn("reset-failed", calls[0])
        self.assertIn("--signal=SIGKILL", calls[1])
        # Locks cover the kill only, never the replacement controller's startup.
        with update.vm.slot_file({"ci_slot": 0}, "vm.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_unexpected_activation_errors_restore_release_without_signalling_healthy_controller(self):
        original = update.write_binary
        for error in (TypeError("unexpected"), KeyboardInterrupt()):
            calls = 0
            def write(*args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise error
                return original(*args)
            with patch.object(update, "service", return_value=dict(MainPID="42", Restart="on-failure")), \
                    patch.object(update, "stage_worker", return_value=self.home / "new-worker"), \
                    patch.object(update, "write_binary", side_effect=write), patch.object(update, "handover") as handover:
                with self.assertRaisesRegex(store.FleetError, "before handover; previous release restored"):
                    self.install()
            handover.assert_not_called()
            self.assertEqual((store.data_dir() / "current").resolve(), self.previous)
            self.assert_work_preserved()

    def test_uvx_interpreter_is_not_retained_in_the_installed_launcher(self):
        base = os.sys.executable
        with patch.object(update, "service", return_value=dict(MainPID="1", Restart="on-failure")), \
                patch.object(update, "stage_worker", return_value=self.home / "new-worker"), \
                patch.object(update, "handover", return_value=dict(controller_release=str(self.release))), \
                patch.object(update.sys, "executable", "/temporary/uvx/bin/python"), \
                patch.object(update.sys, "_base_executable", base):
            self.install()
        self.assertNotIn("/temporary/uvx", self.binary.read_text())
        self.assertIn(base, self.binary.read_text())
        self.assertIn("os.path.realpath", self.binary.read_text())

    def test_failed_signal_does_not_kill_the_healthy_controller_during_rollback(self):
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            raise store.FleetError("systemctl kill failed")
        with patch.object(update, "service", return_value=dict(MainPID="42", Restart="on-failure")), \
                patch.object(update, "stage_worker", return_value=self.home / "new-worker"), \
                patch.object(update, "run", side_effect=command):
            with self.assertRaisesRegex(store.FleetError, "before handover; previous release restored"):
                self.install()
        self.assertEqual(len(calls), 1)
        self.assertIn("kill", calls[0])
        self.assertEqual((store.data_dir() / "current").resolve(), self.previous)
        admission = store.state_dir() / "admission.json"
        self.assertEqual(store.read(admission)["workers"], ["paid-session"])
        self.protected.pop(admission)
        self.assert_work_preserved()

    def test_cached_healthy_status_cannot_confirm_a_handover(self):
        store.atomic(store.state_dir() / "status.json", dict(
            observed_at=time.time() - 60, controller_release=str(self.release), live_update_protocol=1))
        with patch.object(update, "service", return_value=dict(MainPID="42", Restart="on-failure")), \
                patch.object(update, "run"), patch.object(update.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaisesRegex(store.FleetError, "did not publish"):
                update.handover(self.release)

    def test_worker_install_failure_preserves_old_virtualenv_and_launcher(self):
        def command(argv, **kwargs):
            if "venv" in argv:
                self.assertNotIn("worker-venv", str(argv))
                Path(argv[-1]).mkdir(parents=True)
            else:
                raise store.FleetError("install failed")
        with patch.object(update, "executable", return_value="uv"), patch.object(update, "run", side_effect=command):
            with self.assertRaisesRegex(store.FleetError, "install failed"):
                update.stage_worker(self.revision)
        self.assertEqual((store.data_dir() / "bin/tauceti").resolve(), self.old_worker)
        self.assert_work_preserved()

    def test_update_local_works_without_an_operator_inventory(self):
        with patch.object(remote, "deploy", return_value=dict(installed=True)) as deploy, \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(cli.main(["update", "local", "--json"]), 0)
        deploy.assert_called_once_with("local", live=True, expected_machine_id=self.config["machine_id"])
        self.assertFalse((store.config_dir() / "inventory.json").exists())
        self.assert_work_preserved()

    def test_release_archive_ignores_extraction_timestamps(self):
        package = self.home / "package"
        package.mkdir()
        module = package / "module.py"
        module.write_text("print('release contents')\n")
        store.atomic(package / "release.json", dict(worker_revision=self.revision))
        with patch.object(remote, "__file__", str(package / "remote.py")):
            before = remote.archive()
            os.utime(module, (1000000000, 1000000000))
            self.assertEqual(remote.archive(), before)

    def test_bootstrap_staged_code_checks_identity_before_switching_current(self):
        raw = remote.archive()
        result = subprocess.run([os.sys.executable, "-c", remote.BOOTSTRAP, "live", "c" * 32],
                                input=raw, capture_output=True, env=os.environ)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertFalse(reply["installed"])
        self.assertIn("identity differs", reply["error"])
        self.assertEqual((store.data_dir() / "current").resolve(), self.previous)
        self.assert_work_preserved()

    def test_real_sighup_reexec_keeps_admission_and_releases_daemon_lock(self):
        # Exercise actual signal handlers, exec and CLOEXEC lock behavior in an
        # isolated process. External services are replaced with fixed observations.
        receiver = """
import os
from tauceti_fleet import store
with store.locked(store.state_dir() / 'daemon.lock'):
    store.atomic(store.state_dir() / 'reexec.json', dict(
        pid=os.getpid(), admission=store.read(store.state_dir() / 'admission.json')))
"""
        self.binary.write_text("#!/bin/sh\nexec " + shlex.quote(os.sys.executable) + " -c " + shlex.quote(receiver) + "\n")
        child = """
import os,signal
from tauceti_fleet import host, store
host.install_pressure_service=lambda: store.atomic(store.state_dir()/"pressure-installed.json", dict(installed=True))
host.resources=lambda: dict(memory_total=32*1024**3,memory_available=30*1024**3)
host.cpu_sample=lambda: (10,9)
host.collect_ci_results=lambda config: None
host.github.runners=lambda config: []
host.vm.slots_in_use=lambda: []
host.vm.outstanding=lambda: False
host.CISlot.step=lambda *a,**kw: {}
host.worker_config=lambda config: ['paid-session']
host.start_workers=lambda: None
host.worker_statuses=lambda: []
host.worker_memory=lambda: None
host.focus.policy=lambda *a: {}
def publish(path,value):
    store.atomic(path,value)
    if path.name=='status.json': os.kill(os.getpid(),signal.SIGHUP)
host.atomic=publish
host.daemon()
"""
        process = subprocess.Popen([os.sys.executable, "-c", child], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _, error = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            self.fail("Controller re-exec or lock acquisition hung")
        self.assertEqual(process.returncode, 0, error)
        result = store.read(store.state_dir() / "reexec.json")
        self.assertTrue(store.read(store.state_dir() / "pressure-installed.json")["installed"])
        self.assertEqual(result["pid"], process.pid)
        self.assertEqual(result["admission"]["workers"], ["paid-session"])
        self.assertNotEqual(result["admission"].get("reason"), "controller stopped")

class LatestWorker(unittest.TestCase):
    def test_resolve_main_once_to_exact_commit(self):
        revision = "d" * 40
        with patch.object(update, "run", return_value=subprocess.CompletedProcess([], 0, revision + "\trefs/heads/main\n")) as run:
            self.assertEqual(update.latest_worker_revision(), revision)
        self.assertEqual(run.call_args.args[0][-1], "refs/heads/main")
        self.assertEqual(run.call_count, 1)

    def test_missing_ambiguous_or_malformed_head_is_not_selected(self):
        for output in ("", "main refs/heads/main", "d" * 40 + " refs/heads/other", ("d" * 40 + " refs/heads/main\n") * 2):
            with patch.object(update, "run", return_value=subprocess.CompletedProcess([], 0, output)):
                with self.assertRaises(store.FleetError):
                    update.latest_worker_revision()


if __name__ == "__main__":
    unittest.main()
