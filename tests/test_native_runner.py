"""Native runners retain the existing start, slot and recovery contracts."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_fleet import github, host, native_inside, store, vm
from tauceti_fleet import native_runner as native

GIB = 1024**3


class NativeRunner(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TAUCETI_FLEET_" + key: str(self.root / key)
                                               for key in ("STATE", "CONFIG", "DATA")})
        environment.start()
        self.addCleanup(environment.stop)
        mapping = patch.object(native, "mapping_ok", return_value=True)
        mapping.start()
        self.addCleanup(mapping.stop)
        (self.root / "control").mkdir()
        self.config = dict(ci_native_slot=1, ci_slot=1, ci_staging_slots=1,
                           ci_max_jobs=4, ci_kinds=["pr"], ci_enabled=True)

    def test_selection_never_claims_staging_or_another_slot(self):
        self.assertTrue(native.selected(self.config))
        for change in (dict(ci_slot=0), dict(ci_native_slot=0, ci_slot=0),
                       dict(ci_kinds=["staging"]), dict(ci_native_slot=None), dict(ci_native_slot=True)):
            self.assertFalse(native.selected(dict(self.config, **change)))

    def test_choose_falls_back_before_registration_and_has_no_wait(self):
        with patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]) as instances:
            self.assertTrue(native.choose(self.config, 52 * GIB))
            for available in (None, 51 * GIB):
                self.assertFalse(native.choose(self.config, available))
                self.assertEqual(store.read(store.state_dir() / "native-choice.json")["backend"], "vm")
            instances.return_value = [dict(runtime=native.RUNTIME)]
            self.assertFalse(native.choose(self.config, 100 * GIB))
            instances.return_value = []
            store.atomic(store.state_dir() / "native-backoff.json", dict(until=10**12))
            self.assertFalse(native.choose(self.config, 100 * GIB))

    def test_next_general_selection_excludes_reserved_staging_and_invalid_slots(self):
        config = dict(self.config, ci_native_slot="next-general")
        for index in (1, 2, 3):
            self.assertTrue(native.selected(dict(config, ci_slot=index)))
        for index in (0, -1, 4, True, "1"):
            self.assertFalse(native.selected(dict(config, ci_slot=index)))
        self.assertFalse(native.selected(dict(config, ci_slot=2, ci_kinds=["staging"])))

    def test_next_general_starts_one_native_across_slots_and_retains_cleanup_ownership(self):
        config = dict(self.config, ci_native_slot="next-general")
        receipts = []

        def launch(value):
            if value["_ci_backend"] == native.RUNTIME:
                receipts.append(dict(runtime=native.RUNTIME, unit="native.service", phase="cleanup"))
            return value

        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", side_effect=lambda: receipts),
              patch.object(native, "usage", return_value=(32 * GIB, False)),
              patch.object(vm, "start_memory_available", return_value=80 * GIB),
              patch.object(vm, "_start", side_effect=launch), patch.object(vm.time, "monotonic", return_value=100) as clock):
            self.assertEqual(vm.start(config)["_ci_backend"], native.RUNTIME)
            clock.return_value = 161
            self.assertEqual(vm.start(dict(config, ci_slot=2))["_ci_backend"], "vm")
            self.assertEqual(store.read(store.state_dir() / "native-choice.json")["reason"], "one native instance is already owned")
            receipts.clear()  # ordinary cleanup has released ownership
            clock.return_value = 222
            self.assertEqual(vm.start(dict(config, ci_slot=3))["_ci_backend"], native.RUNTIME)
            choice = store.read(store.state_dir() / "native-choice.json")
            self.assertEqual((choice["selection"], choice["slot"]), ("next-general", 3))

    def test_next_general_insufficient_memory_keeps_existing_vm_start_path(self):
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]),
              patch.object(vm, "start_memory_available", return_value=25 * GIB),
              patch.object(vm, "_start", side_effect=lambda config: config)):
            self.assertEqual(vm.start(dict(self.config, ci_native_slot="next-general", ci_slot=3))["_ci_backend"], "vm")

    def test_unsuitable_host_mapping_limit_falls_back_to_vm(self):
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]),
              patch.object(native, "mapping_ok", return_value=False)):
            self.assertFalse(native.choose(self.config, 100 * GIB))
            self.assertEqual(store.read(store.state_dir() / "native-choice.json")["reason"], "native host mapping limit is too small")

    def test_same_locked_start_decision_uses_one_memory_snapshot(self):
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]),
              patch.object(vm, "start_memory_available", return_value=52 * GIB) as memory,
              patch.object(vm, "_start", side_effect=lambda config: config) as launch):
            value = vm.start(self.config)
            self.assertEqual(value["_ci_backend"], native.RUNTIME)
            self.assertEqual(value["ci_kinds"], ["pr"])
            memory.assert_called_once()
            launch.assert_called_once()
            self.assertEqual(vm.start(dict(self.config, ci_slot=2)), {})
            launch.assert_called_once()

    def test_small_headroom_starts_ordinary_vm_without_a_native_queue(self):
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]),
              patch.object(vm, "start_memory_available", return_value=25 * GIB),
              patch.object(vm, "_start", side_effect=lambda config: config)):
            self.assertEqual(vm.start(self.config)["_ci_backend"], "vm")

    def test_pending_native_growth_affects_only_startup_and_is_charged_once(self):
        receipt = dict(runtime=native.RUNTIME, unit="native.service", expected_memory_bytes=32 * GIB)
        with patch.object(native, "instances", return_value=[receipt]), patch.object(native, "run", return_value=Mock(stdout=str(8 * GIB))):
            self.assertEqual(native.pending_growth(), 24 * GIB)
            self.assertFalse(vm.start_memory_ok(dict(_ci_backend="vm", _ci_start_available=43 * GIB)))
            self.assertTrue(vm.start_memory_ok(dict(_ci_backend="vm", _ci_start_available=44 * GIB)))
        # The legacy helper without a backend selection keeps its prior behavior.
        with patch.object(vm, "start_memory_available", return_value=20 * GIB), patch.object(native, "pending_growth", side_effect=AssertionError):
            self.assertTrue(vm.start_memory_ok())

    def test_dead_native_receipt_does_not_withhold_future_workspace_ram(self):
        value = dict(runtime=native.RUNTIME, unit="native.service", phase="ready")
        with (patch.object(native, "instances", return_value=[value]),
              patch.object(native, "run", return_value=Mock(stdout="ActiveState=failed\nMemoryCurrent=[not set]\n"))):
            self.assertEqual(native.pending_growth(), 0)

    def test_native_configuration_changes_only_next_backend(self):
        initial = dict(store.host_config(), ci_max_jobs=4, ci_staging_slots=1, ci_kinds=["pr", "staging"], workers_enabled=True)
        store.update_host(**initial)
        with patch.object(native, "ready", return_value=True):
            native.configure(3)
            after = store.host_config()
            self.assertEqual(after, dict(initial, ci_native_slot=3))
            with self.assertRaises(store.FleetError):
                native.configure(0)
            native.configure("next-general")
            self.assertEqual(store.host_config(), dict(initial, ci_native_slot="next-general"))
            native.configure(None)
            self.assertEqual(store.host_config(), dict(initial, ci_native_slot=None))

    def test_next_general_configuration_rejects_hosts_without_general_pr_slots(self):
        with patch.object(native, "ready", return_value=True):
            for change in (dict(ci_max_jobs=2, ci_staging_slots=2, ci_kinds=["pr", "staging"]),
                           dict(ci_max_jobs=2, ci_staging_slots=0, ci_kinds=["staging"])):
                store.update_host(**change)
                with self.assertRaises(store.FleetError):
                    native.configure("next-general")

    def test_lost_native_registration_reply_reuses_the_common_intent(self):
        config = dict(store.host_config(), **self.config, ci_broker_url="https://broker.invalid", _ci_backend=native.RUNTIME)
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        names = []
        def register(config, name):
            names.append(name)
            if len(names) == 1:
                raise store.FleetError("lost JIT reply")
            return 7, "private-jit"
        with (patch.object(github, "jit_config", side_effect=register),
              patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)),
              patch.object(native, "stage", return_value=dict(runtime=native.RUNTIME)) as stage):
            with self.assertRaises(store.FleetError):
                vm._start(config)
            intent = store.read(vm.slot_file(config, "ci-registration.json"))
            self.assertTrue(intent["instance"])
            value = vm._start(config)
        self.assertEqual(names[0], names[1])
        self.assertEqual(value["instance"], intent["instance"])
        self.assertEqual(value["runtime"], native.RUNTIME)
        stage.assert_called_once()
        self.assertIsNone(store.read(vm.slot_file(config, "ci-registration.json")))

    def test_idle_expiry_never_retires_running_or_newly_online_work(self):
        value = dict(runtime=native.RUNTIME, online_observed_at=100, busy=False)
        own = dict(status="online", busy=False)
        with patch.object(native.time, "time", return_value=401):
            self.assertTrue(native.idle_expired(value, own))
            self.assertFalse(native.idle_expired(dict(value, busy=True), own))
            self.assertFalse(native.idle_expired(dict(value, run_id=42), own))
            self.assertFalse(native.idle_expired(value, dict(own, busy=True)))
            self.assertFalse(native.idle_expired(dict(value, online_observed_at=200), own))

    def test_hook_reader_keeps_partial_records_and_bounds_history(self):
        path = self.root / "control/events.jsonl"
        complete = b'{"kind":"ci_started","run_id":"42"}\n'
        path.write_bytes(complete + b'{"kind":')
        lines, offset = native.hook_lines(self.root)
        self.assertEqual((lines, offset), ([complete], len(complete)))
        self.assertEqual(native.hook_lines(self.root, offset), ([], offset))
        path.write_bytes(b'x' * 5000)
        with self.assertRaises(store.FleetError):
            native.hook_lines(self.root)

    def test_completed_hook_is_durable_before_optional_telemetry(self):
        (self.root / "control/telemetry").mkdir()
        with patch.object(native_inside, "LINK", self.root / "control"), patch.dict(os.environ, GITHUB_RUN_ID="42", GITHUB_WORKSPACE="/missing"):
            native_inside.hook("ci_completed")
        self.assertEqual(json.loads((self.root / "control/events.jsonl").read_text())["run_id"], "42")

    def test_restart_recovers_run_id_before_dead_unit_retirement(self):
        directory = self.root / "instance"
        directory.mkdir()
        (directory / "control").mkdir()
        (directory / "control/events.jsonl").write_text('{"kind":"ci_started","run_id":"42","at":100}\n'
                                                '{"kind":"ci_completed","run_id":"42","at":200}\n')
        value = dict(runtime=native.RUNTIME, slot=1, instance="native", directory=str(directory), unit="native.service",
                     runner_id=12, started_at=10, phase="ready", ci_kinds=["pr"])
        store.atomic(vm.slot_file(self.config, "vm.json"), value)
        with (patch.object(host, "ci_service_state", return_value=(False, "healthy")),
              patch.object(vm, "retire", side_effect=lambda *_: vm.slot_file(self.config, "vm.json").unlink()),
              patch.object(vm, "start", return_value={})):
            host.CISlot(1).step(self.config, inventory=[], refresh=False, cooling=False)
        receipt = store.read(store.state_dir() / "pending-ci.json")[0]
        self.assertEqual((receipt["run_id"], receipt["runner_id"], receipt["native_completed_at"]), (42, 12, 200))

    def test_cleanup_requires_stopped_owned_unit_and_preserves_failure_evidence(self):
        directory = store.state_dir() / "vms" / "0123456789abcdef"
        directory.mkdir(parents=True)
        store.atomic(directory / "native-result.json", dict(exit_code=75, infrastructure_error=dict(reason="ENOSPC")))
        value = dict(directory=str(directory), unit="native.service")
        with patch.object(native, "run", return_value=Mock(returncode=0)), patch.object(native, "remove_root") as remove:
            with self.assertRaises(store.FleetError):
                native.cleanup(value)
            remove.assert_not_called()
        with patch.object(native, "run", return_value=Mock(returncode=1)), patch.object(native, "remove_root") as remove:
            with self.assertRaises(store.FleetError):
                native.cleanup(value)
            remove.assert_not_called()
        with patch.object(native, "run", return_value=Mock(returncode=3)), patch.object(native, "remove_root") as remove:
            native.cleanup(value)
            remove.assert_called_once_with(directory)
        self.assertTrue(store.read(store.state_dir() / "native-backoff.json")["until"])
        self.assertEqual(store.read(store.state_dir() / "native-results" / directory.name / "native-result.json")["exit_code"], 75)

    def test_stage_is_small_and_filesystem_clone_runs_in_the_unit(self):
        (self.root / "control").rmdir()
        with patch.object(native, "image", return_value=dict(sha256="a"*64)), patch.object(native, "run") as run:
            value = native.stage({}, self.root, "private-test-jit")
        run.assert_not_called()
        self.assertEqual(value["runtime"], native.RUNTIME)
        self.assertFalse((self.root / "rootfs").exists())
        self.assertEqual((self.root / "control/jit").stat().st_mode & 0o777, 0o600)

    def test_shared_records_cannot_read_host_symlinks_or_block_on_fifo(self):
        target = self.root / "control/native-progress.json"
        target.symlink_to("/etc/passwd")
        self.assertIsNone(native.shared_json(target))
        target.unlink()
        os.mkfifo(target)
        self.assertIsNone(native.shared_json(target))
        target.rename(self.root / "control/events.jsonl")
        self.assertEqual(native.hook_lines(self.root), ([], 0))

    def test_requested_idle_stop_does_not_create_native_cooldown(self):
        directory = store.state_dir() / "vms" / "0123456789abcdef"
        directory.mkdir(parents=True)
        store.atomic(directory / "native-stop.json", dict(reason="requested retirement"))
        with patch.object(native, "run", return_value=Mock(returncode=3)), patch.object(native, "remove_root"):
            native.cleanup(dict(directory=str(directory), unit="native.service"))
        self.assertFalse((store.state_dir() / "native-backoff.json").exists())

    def test_asynchronous_cleanup_keeps_slot_ownership_until_filesystem_is_gone(self):
        directory = store.state_dir() / "vms" / "0123456789abcdef"
        directory.mkdir(parents=True)
        value = dict(instance=directory.name, runtime=native.RUNTIME, slot=1, directory=str(directory), unit="native.service", runner_id=7)
        config = dict(store.host_config(), ci_slot=1)
        path = vm.slot_file(config, "vm.json")
        store.atomic(path, value)
        with (patch.object(native, "request_stop"), patch.object(native, "cleanup", return_value=False) as cleanup,
              patch.object(github, "retire_runner"), patch.object(vm, "run", return_value=Mock(returncode=0))):
            self.assertFalse(vm.retire(config, value))
            cleanup.assert_called_once_with(value, asynchronous=True)
        self.assertEqual(store.read(path)["phase"], "cleanup")
        with (patch.object(native, "request_stop"), patch.object(native, "cleanup", return_value=True),
              patch.object(github, "retire_runner"), patch.object(vm, "run", return_value=Mock(returncode=0))):
            self.assertTrue(vm.retire(config, value))
        self.assertFalse(path.exists())

    def test_pressure_relay_reconnects_without_connecting_to_a_job_path(self):
        import socket
        relay = native.PressureRelay(self.root)
        self.addCleanup(relay.close)
        host = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(host.close)
        host.connect(str(self.root / "pressure.sock"))
        relay.poll()
        host.sendall(b'{"version":1}\n')
        for _ in range(3):
            relay.poll()
        relay.child.settimeout(1)
        self.assertIn(b'{"version":1}', relay.child.recv(4096))
        relay.child.sendall(b'123:1\n')
        for _ in range(3):
            relay.poll()
        host.settimeout(1)
        self.assertEqual(host.recv(4096), b'123:1\n')
        host.close()
        relay.poll()
        newer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(newer.close)
        newer.connect(str(self.root / "pressure.sock"))
        relay.poll()
        relay.child.sendall(b'124:1\n')
        for _ in range(3):
            relay.poll()
        newer.settimeout(1)
        self.assertEqual(newer.recv(4096), b'124:1\n')

    def test_pressure_peer_shutdown_with_unread_data_is_a_normal_end(self):
        import socket
        relay = native.PressureRelay(self.root)
        self.addCleanup(relay.close)
        host = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(host.close)
        host.connect(str(self.root / "pressure.sock"))
        relay.poll()
        host.sendall(b'{"version":1}\n')
        for _ in range(3):
            relay.poll()
        relay.child.close()  # Peer exits with unread pressure data queued.
        for _ in range(3):
            relay.poll()
        self.assertTrue(relay.ended)

    def test_optional_diagnostics_cannot_block_on_fifo_or_symlink(self):
        source = self.root / "fifo"
        os.mkfifo(source)
        native.copy_diagnostic(source, self.root / "out")
        self.assertFalse((self.root / "out").exists())
        source.unlink()
        source.symlink_to("/etc/passwd")
        native.copy_diagnostic(source, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_supervisor_drains_hook_after_child_exit(self):
        (self.root / "control/events.jsonl").write_text('{"kind":"ci_completed","run_id":"42"}\n')
        child = Mock()
        child.poll.return_value = 0
        child.wait.return_value = 0
        with patch.object(native, "materialize"), patch.object(native, "publish_memory"), patch.object(subprocess, "Popen", return_value=child) as spawn, patch.object(native, "hook_lines", wraps=native.hook_lines) as drain:
            self.assertEqual(native.serve(self.root), 0)
            drain.assert_called_once()
            self.assertIn("tauceti_fleet.native_runner", spawn.call_args.args[0])
            self.assertNotIn("__main__", spawn.call_args.args[0])
        self.assertEqual(store.read(self.root / "native-result.json")["exit_code"], 0)

    def test_malformed_hook_does_not_kill_useful_work(self):
        (self.root / "control/events.jsonl").write_text('invalid\n{"kind":"ci_started","run_id":"42"}\n')
        child = Mock()
        child.poll.side_effect = [None, 0, 0]
        child.wait.return_value = 0
        with patch.object(native, "materialize"), patch.object(native, "publish_memory"), patch.object(subprocess, "Popen", return_value=child):
            self.assertEqual(native.serve(self.root), 0)
        child.terminate.assert_not_called()
