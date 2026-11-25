"""Native runners retain the existing start, slot and recovery contracts."""

import json
import os
import shutil
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
        self.config = dict(ci_slot=1, ci_staging_slots=1,
                           ci_max_jobs=4, ci_kinds=["pr"], ci_enabled=True)

    def test_default_selection_preserves_staging_and_mixed_registrations(self):
        self.assertTrue(native.selected(self.config))
        self.assertTrue(native.selected(dict(ci_kinds=["pr"])))
        for change in (dict(ci_slot=0), dict(ci_slot=True), dict(ci_slot=-1),
                       dict(ci_kinds=["staging"]), dict(ci_staging_slots=0, ci_kinds=["pr", "staging"])):
            self.assertFalse(native.selected(dict(self.config, **change)))

    def test_selected_native_fault_is_explicit_before_registration(self):
        with patch.object(native, "ready", return_value=False):
            with self.assertRaisesRegex(store.FleetError, "image not prepared"):
                native.choose(self.config, 25 * GIB)
        self.assertEqual(store.read(store.state_dir() / "native-choice.json")["backend"], native.RUNTIME)
    def test_default_selection_excludes_reserved_staging_and_invalid_slots(self):
        config = dict(self.config)
        for index in (1, 2, 3):
            self.assertTrue(native.selected(dict(config, ci_slot=index)))
        for index in (0, -1, 4, True, "1"):
            self.assertFalse(native.selected(dict(config, ci_slot=index)))
        self.assertFalse(native.selected(dict(config, ci_slot=2, ci_kinds=["staging"])))

    def test_default_uses_total_slot_controls_for_concurrent_native_jobs(self):
        config = dict(self.config, ci_kinds=["pr", "staging"])

        def launch(value):
            if value["_ci_backend"] == native.RUNTIME:
                store.atomic(vm.slot_file(value, "vm.json"), dict(runtime=native.RUNTIME, unit="native.service", phase="cleanup"))
            return value

        with (patch.object(native, "ready", return_value=True),
              patch.object(vm, "start_memory_available", return_value=80 * GIB),
              patch.object(vm, "_start", side_effect=launch), patch.object(vm.time, "monotonic", return_value=100) as clock):
            self.assertEqual(vm.start(config)["_ci_backend"], native.RUNTIME)
            clock.return_value = 161
            self.assertEqual(vm.start(dict(config, ci_slot=2))["_ci_backend"], native.RUNTIME)
            clock.return_value = 222
            reserved = vm.start(dict(config, ci_slot=0))
            self.assertEqual((reserved["_ci_backend"], reserved["ci_kinds"]), ("vm", ["staging"]))
            vm.slot_file(config, "vm.json").unlink()  # ordinary cleanup has released ownership
            clock.return_value = 283
            self.assertEqual(vm.start(dict(config, ci_slot=3))["_ci_backend"], native.RUNTIME)
            choice = store.read(store.state_dir() / "native-choice.json")
            self.assertEqual((choice["backend"], choice["slot"]), (native.RUNTIME, 3))

    def test_default_uses_ordinary_startup_headroom(self):
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]),
              patch.object(vm, "start_memory_available", return_value=25 * GIB),
              patch.object(vm, "_start", side_effect=lambda config: config)):
            self.assertEqual(vm.start(dict(self.config, ci_slot=3))["_ci_backend"], native.RUNTIME)

    def test_default_keeps_mixed_idle_pool_spare_on_vm_and_recovers_pr_on_native(self):
        config = dict(self.config, ci_staging_slots=0, ci_kinds=["pr", "staging"])
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]),
              patch.object(vm, "start_memory_available", return_value=80 * GIB),
              patch.object(vm, "_start", side_effect=lambda value: value)):
            value = vm.start(dict(config, ci_idle_pool=True))
            self.assertEqual((value["_ci_backend"], value["ci_kinds"]), ("vm", ["pr", "staging"]))
            self.assertTrue(native.choose(dict(config, ci_recover_instance="0123456789abcdef", ci_kinds=["pr"]), 80 * GIB))

    def test_default_replays_existing_registration_classes_on_vm(self):
        config = dict(self.config, ci_kinds=["pr", "staging"], ci_broker_url="https://broker.invalid")
        intent = dict(instance="0123456789abcdef", ci_kinds=["pr", "staging"])
        store.atomic(vm.slot_file(config, "ci-registration.json"), intent)
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]),
              patch.object(vm, "start_memory_available", return_value=80 * GIB),
              patch.object(vm, "_start", side_effect=lambda value: value)):
            value = vm.start(config)
            self.assertEqual((value["_ci_backend"], value["ci_kinds"]), ("vm", intent["ci_kinds"]))
            self.assertEqual(store.read(vm.slot_file(config, "ci-registration.json")), intent)

    def test_converted_infra_slot_stays_native_despite_a_pending_mixed_registration(self):
        config = dict(self.config, ci_kinds=["infra"], ci_broker_url="https://broker.invalid")
        store.atomic(vm.slot_file(config, "ci-registration.json"),
                     dict(instance="0123456789abcdef", ci_kinds=["pr", "staging"]))
        with patch.object(native, "ready", return_value=True):
            self.assertTrue(native.choose(config, 80 * GIB))
        self.assertEqual(store.read(store.state_dir() / "native-choice.json")["backend"], native.RUNTIME)

    def test_infra_start_refuses_the_vm_backend(self):
        (store.data_dir() / "images").mkdir(parents=True)
        store.atomic(store.data_dir() / "images/prepared.json", {})
        with patch.object(vm, "remove_warm_disk") as warm, patch.object(github, "jit_config") as jit:
            with self.assertRaisesRegex(store.FleetError, "only on the native backend"):
                vm._start(dict(self.config, ci_kinds=["infra"], _ci_backend="vm"))
            warm.assert_not_called()
            jit.assert_not_called()

    def test_unsuitable_host_mapping_limit_is_an_explicit_native_fault(self):
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[]),
              patch.object(native, "mapping_ok", return_value=False)):
            with self.assertRaisesRegex(store.FleetError, "mapping limit"):
                native.choose(self.config, 100 * GIB)
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

    def test_small_headroom_starts_selected_native_without_a_workspace_deduction(self):
        with (patch.object(native, "ready", return_value=True), patch.object(native, "instances", return_value=[dict(runtime=native.RUNTIME)]),
              patch.object(vm, "start_memory_available", return_value=25 * GIB),
              patch.object(vm, "_start", side_effect=lambda config: config)):
            self.assertEqual(vm.start(self.config)["_ci_backend"], native.RUNTIME)

    def test_memory_start_threshold_is_shared_between_backends(self):
        for backend in ("vm", native.RUNTIME):
            for available, expected in ((19, False), (20, True), (25, True)):
                with self.subTest(backend=backend, available=available):
                    self.assertEqual(vm.start_memory_ok(dict(_ci_backend=backend, _ci_start_available=available * GIB)), expected)

    def test_staging_and_unsplit_mixed_hosts_do_not_prepare_native_images(self):
        with patch.object(native, "prepare") as prepare:
            for config in (dict(ci_kinds=["staging"]), dict(ci_kinds=["pr", "staging"]),
                           dict(ci_kinds=["pr", "staging"], ci_staging_slots=2, ci_max_jobs=2)):
                self.assertIsNone(native.prepare_for_ci(config))
        prepare.assert_not_called()
        # Protected infra slots run native-disk on any host, so they need the image.
        infra = dict(ci_kinds=["staging"], ci_max_jobs=3, ci_protected_slots={"2": dict(kind="infra", runner_group_id=8)})
        with patch.object(native, "prepare", return_value="image") as prepare:
            self.assertEqual(native.prepare_for_ci(infra), "image")
        prepare.assert_called_once_with(infra, infra=True)
        # A PR host with infra slots prepares both images.
        both = dict(infra, ci_kinds=["pr"])
        with patch.object(native, "prepare", side_effect=lambda config, infra=False: "infra" if infra else "pr") as prepare:
            self.assertEqual(native.prepare_for_ci(both), "pr")
        self.assertEqual([c.kwargs for c in prepare.call_args_list], [dict(infra=True), {}])

    def test_infra_image_adds_packages_and_refuses_to_export_without_them(self):
        commands = {}
        def seed(directory, values, name):
            commands[infra] = values
            raise store.FleetError("stop after seeding")
        base = dict(sha256="a" * 64, runner_version="v1")
        store.state_dir().mkdir(parents=True, exist_ok=True)
        (store.data_dir() / "images").mkdir(parents=True, exist_ok=True)
        for infra in (False, True):
            with (patch.object(native, "prerequisites"), patch.object(vm, "prepare_tools"),
                  patch.object(native, "read", return_value=base), patch.object(vm, "digest", return_value="a" * 64),
                  patch.object(native, "ready", return_value=False), patch.object(native, "run"),
                  patch.object(vm, "cloud_seed", side_effect=seed), self.assertRaises(store.FleetError)):
                native.prepare(dict(ci_kinds=["pr"]), infra=infra)
        joined = {k: " ; ".join(v) for k, v in commands.items()}
        for line in native.INFRA_PACKAGES:
            self.assertIn(line, joined[True])
            self.assertTrue(line == "apt-get update" or line not in joined[False])
        self.assertIn("command -v node && git --version", joined[True])
        self.assertNotIn("command -v node", joined[False])
        # Both images carry the system C compiler Lake's buildO/compileO call, and
        # neither exports without one.
        for infra in (False, True):
            self.assertIn("apt-get install -y bubblewrap " + native.C_COMPILER_PACKAGES, joined[infra])
            self.assertIn("cc --version && ", joined[infra])

    def test_manifests_are_keyed_by_revision_so_older_controllers_keep_theirs(self):
        # A live update exports before handover; the serving controller still
        # reads the revision-1 manifests under their original names.
        images = store.data_dir() / "images"
        store.atomic(images / "native.json", dict(revision=1, sha256="c" * 64))
        store.atomic(images / "native-r2.json", dict(revision=2, sha256="d" * 64))
        with patch.object(native, "IMAGE_REVISION", 2), patch.object(native, "INFRA_IMAGE_REVISION", 2):
            self.assertEqual(native.manifest_name(), "native-r2.json")
            self.assertEqual(native.manifest_name(True), "native-infra-r2.json")
            self.assertEqual(native.image()["sha256"], "d" * 64)
            self.assertEqual(native.image(True), {})
        with patch.object(native, "IMAGE_REVISION", 1):
            self.assertEqual(native.image()["sha256"], "c" * 64)

    def test_export_publishes_under_the_revision_name_and_keeps_older_manifests(self):
        images = store.data_dir() / "images"
        images.mkdir(parents=True, exist_ok=True)
        store.state_dir().mkdir(parents=True, exist_ok=True)
        old = dict(revision=1, sha256="c" * 64)
        store.atomic(images / "native.json", old)
        store.atomic(images / "native-infra.json", old)
        base = dict(sha256="a" * 64, runner_version="v1")

        def fake_run(argv, **kwargs):
            argv = [str(a) for a in argv]
            if argv[0].endswith("qemu-img"):
                Path(argv[-2]).touch()
            elif "-xpf" in argv:
                target = Path(argv[argv.index("-C") + 1])
                (target / "opt/actions-runner/bin").mkdir(parents=True)
                (target / "opt/actions-runner/bin/Runner.Listener").touch()
            elif "systemd-run" in " ".join(argv) or any("qemu-system" in a for a in argv):
                for staging in store.state_dir().glob("native-image-*"):
                    (staging / "console.log").write_text("FLEET_NATIVE_IMAGE_READY\n")
                    (staging / "rootfs.tar.gz").write_bytes(b"x")
            return subprocess.CompletedProcess(argv, 0, "", "")

        for infra in (False, True):
            with (patch.object(native, "prerequisites"), patch.object(vm, "prepare_tools"),
                  patch.object(native, "read", side_effect=lambda path, default=None: base
                               if Path(path).name == "prepared.json" else store.read(path, default)),
                  patch.object(vm, "digest", side_effect=lambda path, infra=infra: "a" * 64
                               if Path(path).name == "prepared.qcow2" else ("f" if infra else "e") * 64),
                  patch.object(vm, "cloud_seed", return_value="seed.iso"),
                  patch.object(vm, "qemu_args", return_value=["qemu-system-x86_64"]),
                  patch.object(vm, "scope_args", side_effect=lambda unit, args, small: ["systemd-run", *args]),
                  patch.object(native, "executable", side_effect=lambda name: name),
                  patch.object(native, "run", side_effect=fake_run)):
                value = native.prepare(dict(ci_kinds=["pr"]), infra=infra)
            self.assertEqual(store.read(images / native.manifest_name(infra)), value)
            self.assertNotIn(native.manifest_name(infra), ("native.json", "native-infra.json"))
        self.assertEqual(store.read(images / "native.json"), old)
        self.assertEqual(store.read(images / "native-infra.json"), old)

    def test_prepared_native_image_is_reused_without_booting_an_export_vm(self):
        config = dict(ci_kinds=["pr"])
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        image = dict(sha256="b" * 64, vm_sha256="a" * 64)
        with (patch.object(native, "prerequisites"), patch.object(vm, "prepare_tools"),
              patch.object(vm, "digest", return_value="a" * 64),
              patch.object(native, "ready", return_value=True), patch.object(native, "image", return_value=image),
              patch.object(native, "run") as run):
            self.assertEqual(native.prepare_for_ci(config), image)
        run.assert_not_called()

    def test_standard_enable_prepares_and_launches_native_without_host_opt_in(self):
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        operations = []

        def prepare(config):
            self.assertFalse(store.host_config()["ci_enabled"])
            operations.append("image")
            return {"sha256": "b" * 64}

        def launch(config):
            self.assertEqual(config["_ci_backend"], native.RUNTIME)
            self.assertEqual(config["ci_kinds"], ["pr"])
            operations.append("launch")
            return dict(instance="a" * 16, runtime=native.RUNTIME)

        with (patch.object(host, "doctor", return_value=dict(readiness=dict(ci="ready"))),
              patch.object(github, "capacity_policy", return_value=dict(kinds=["pr"])),
              patch.object(host, "write_slices"), patch.object(host, "resources", return_value={}),
              patch.object(host, "worker_config"), patch.object(host, "install_service"),
              patch.object(native, "prepare", side_effect=prepare), patch.object(native, "ready", return_value=True),
              patch.object(vm, "start_memory_available", return_value=25 * GIB),
              patch.object(vm.time, "monotonic", return_value=100) as clock,
              patch.object(vm, "_start", side_effect=launch)):
            result = host.enable(ci="pr", maximum=4)
            self.assertEqual(operations, ["image", "launch"])
            # A replacement controller reads the persisted role and starts its
            # next slot through the same native path, without provisioning again.
            clock.return_value = 161
            host.CISlot(1).step(store.host_config(), inventory=[], refresh=False, cooling=False)
        self.assertTrue(result["ci_enabled"])
        self.assertEqual(operations, ["image", "launch", "launch"])
        self.assertNotIn("ci_native_slot", store.host_config())

    def test_image_preparation_failure_does_not_enable_or_register_ci(self):
        initial = store.host_config()
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        with (patch.object(host, "doctor", return_value=dict(readiness=dict(ci="ready"))),
              patch.object(github, "capacity_policy", return_value=dict(kinds=["pr"])),
              patch.object(native, "prepare", side_effect=store.FleetError("image export failed")),
              patch.object(vm, "start") as start):
            with self.assertRaisesRegex(store.FleetError, "image export failed"):
                host.enable(ci="pr")
        start.assert_not_called()
        self.assertEqual(store.host_config(), initial)

    def test_lost_native_registration_reply_reuses_the_common_intent(self):
        config = dict(store.host_config(), **self.config, ci_broker_url="https://broker.invalid", _ci_backend=native.RUNTIME)
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        names = []
        def register(config, name):
            names.append(name)
            if len(names) == 1:
                raise store.FleetError("lost JIT reply")
            return 7, "private-jit"
        with (patch.object(vm.shutil, "disk_usage", return_value=Mock(free=48 * GIB)),
              patch.object(github, "jit_config", side_effect=register),
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

    def test_disk_shortfall_is_rejected_before_jit_registration(self):
        config = dict(store.host_config(), **self.config, _ci_backend=native.RUNTIME)
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        with (patch.object(vm.shutil, "disk_usage", return_value=Mock(free=47 * GIB)),
              patch.object(github, "jit_config") as register):
            with self.assertRaisesRegex(store.FleetError, "free space"):
                vm._start(config)
            register.assert_not_called()
        self.assertIsNone(store.read(vm.slot_file(config, "ci-registration.json")))

    def test_idle_expiry_never_retires_running_or_newly_online_work(self):
        value = dict(runtime=native.RUNTIME, online_observed_at=100, busy=False)
        own = dict(status="online", busy=False)
        with patch.object(native.time, "time", return_value=401):
            self.assertTrue(native.idle_expired(value, own))
            self.assertFalse(native.idle_expired(dict(value, busy=True), own))
            self.assertFalse(native.idle_expired(dict(value, run_id=42), own))
            infra = dict(value, ci_kinds=["infra"], native_image_sha256="b" * 64)
            with patch.object(native, "image", return_value=dict(sha256="b" * 64)):
                self.assertFalse(native.idle_expired(infra, own))
            # An idle infra runner on an older infra image retires at once to restart on the new one.
            with patch.object(native, "image", return_value=dict(sha256="c" * 64)):
                self.assertTrue(native.idle_expired(dict(infra, online_observed_at=400), own))
                self.assertFalse(native.idle_expired(dict(infra, busy=True), own))
            with patch.object(native, "image", return_value={}):
                self.assertFalse(native.idle_expired(infra, own))
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
        self.assertEqual(store.read(store.state_dir() / "native-results" / directory.name / "native-result.json")["exit_code"], 75)

    def test_stage_is_small_and_filesystem_clone_runs_in_the_unit(self):
        (self.root / "control").rmdir()
        with patch.object(native, "image", return_value=dict(sha256="a"*64)), patch.object(native, "run") as run:
            value = native.stage(self.root, "private-test-jit")
        run.assert_not_called()
        self.assertEqual(value["runtime"], native.RUNTIME)
        self.assertFalse((self.root / "rootfs").exists())
        self.assertEqual((self.root / "control/jit").stat().st_mode & 0o777, 0o600)

    def test_infra_slots_omit_the_lean_gate_environment(self):
        image = store.data_dir() / "images/native" / ("a" * 64) / "rootfs"
        (image / "usr/local/bin").mkdir(parents=True)
        (image / "opt").mkdir()
        for infra in (False, True):
            with self.subTest(infra=infra):
                directory = self.root / ("infra" if infra else "lean")
                directory.mkdir()
                with patch.object(native, "image", return_value=dict(sha256="a" * 64)) as image:
                    native.stage(directory, "jit", infra=infra)
                image.assert_called_once_with(infra)
                def copy(command, **_):
                    shutil.copytree(command[-2], command[-1], symlinks=True)
                with patch.object(native, "run", side_effect=copy):
                    native.materialize(directory)
                self.assertEqual((directory / "rootfs/opt/tauceti-lean-env").exists(), not infra)

    def test_shared_records_cannot_read_host_symlinks_or_block_on_fifo(self):
        target = self.root / "control/native-progress.json"
        target.symlink_to("/etc/passwd")
        self.assertIsNone(native.shared_json(target))
        target.unlink()
        os.mkfifo(target)
        self.assertIsNone(native.shared_json(target))
        target.rename(self.root / "control/events.jsonl")
        self.assertEqual(native.hook_lines(self.root), ([], 0))

    def test_asynchronous_cleanup_keeps_slot_ownership_until_filesystem_is_gone(self):
        directory = store.state_dir() / "vms" / "0123456789abcdef"
        directory.mkdir(parents=True)
        value = dict(instance=directory.name, runtime=native.RUNTIME, slot=1, directory=str(directory), unit="native.service", runner_id=7)
        config = dict(store.host_config(), ci_slot=1)
        path = vm.slot_file(config, "vm.json")
        store.atomic(path, value)
        with (patch.object(native, "cleanup", return_value=False) as cleanup,
              patch.object(github, "retire_runner"), patch.object(vm, "run", return_value=Mock(returncode=0))):
            self.assertFalse(vm.retire(config, value))
            cleanup.assert_called_once_with(value, asynchronous=True)
        self.assertEqual(store.read(path)["phase"], "cleanup")
        with (patch.object(native, "cleanup", return_value=True),
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

    def test_job_io_reports_the_filesystem_device_once(self):
        group = self.root / "cgroup"
        group.mkdir()
        device = os.stat(self.root).st_dev
        name = f"{os.major(device)}:{os.minor(device)}"
        (group / "io.stat").write_text(f"259:0 rbytes=1 wbytes=2 rios=3 wios=4 dbytes=0 dios=0\n"
                                       f"{name} rbytes=10 wbytes=20 rios=30 wios=40 dbytes=0 dios=0\n")
        value = native.job_io(group, self.root)
        self.assertEqual(value["device"], name)
        self.assertEqual(value["filesystem"]["wbytes"], 20)
        self.assertEqual(value["filesystem"]["wios"], 40)
        self.assertEqual(value["devices"]["259:0"]["wbytes"], 2)
        self.assertIsNone(native.job_io(None, self.root))
        self.assertIsNone(native.job_io(self.root / "missing", self.root))

    def test_file_count_accepts_partial_du_totals_and_bounds_failures(self):
        answers = [subprocess.CompletedProcess([], 0, "1234\t/x\n", ""), subprocess.CompletedProcess([], 1, "99\t/x\n", ""),
                   subprocess.CompletedProcess([], 2, "", ""), native.FleetError("du timed out")]
        def fake(args, **kwargs):
            self.assertEqual(args[-3:], ["-sx", "--inodes", self.root])
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        with patch.object(native, "run", side_effect=fake):
            self.assertEqual(native.count_files(self.root)["inodes"], 1234)
            self.assertEqual(native.count_files(self.root)["inodes"], 99)
            self.assertTrue(native.count_files(self.root)["partial"] is False)
            self.assertIsNone(native.count_files(self.root)["inodes"])

    def test_result_records_io_files_and_removal(self):
        (self.root / "control/events.jsonl").write_text('{"kind":"ci_completed","run_id":"42"}\n')
        (self.root / "rootfs").mkdir()
        child = Mock()
        child.poll.return_value = 0
        child.wait.return_value = 0
        ios = iter([dict(filesystem=dict(wbytes=5)), dict(filesystem=dict(wbytes=7))])
        def removed(root):
            Path(root).rmdir()
        with patch.object(native, "materialize"), patch.object(native, "publish_memory"), \
                patch.object(subprocess, "Popen", return_value=child), \
                patch.object(native, "job_io", side_effect=lambda *_: next(ios)), \
                patch.object(native, "count_files", return_value=dict(inodes=1200000, partial=False, seconds=4.0)), \
                patch.object(native, "remove_root", side_effect=removed):
            self.assertEqual(native.serve(self.root), 0)
        result = store.read(self.root / "native-result.json")
        self.assertEqual(result["io"]["filesystem"]["wbytes"], 5)
        self.assertEqual(result["io_after_removal"]["filesystem"]["wbytes"], 7)
        self.assertEqual(result["files"]["inodes"], 1200000)
        self.assertIn("removal_seconds", result)

    def test_requested_stop_skips_the_file_count(self):
        (self.root / "rootfs").mkdir()
        child = Mock()
        child.poll.return_value = None
        def stop(*_args, **_kwargs):
            os.kill(os.getpid(), __import__("signal").SIGTERM)
            return child
        with patch.object(native, "materialize"), patch.object(native, "publish_memory"), \
                patch.object(subprocess, "Popen", side_effect=stop), \
                patch.object(native, "count_files") as count, patch.object(native, "remove_root") as remove:
            self.assertEqual(native.serve(self.root), 143)
        count.assert_not_called()
        remove.assert_not_called()
        self.assertNotIn("files", store.read(self.root / "native-result.json"))

    def test_malformed_hook_does_not_kill_useful_work(self):
        (self.root / "control/events.jsonl").write_text('invalid\n{"kind":"ci_started","run_id":"42"}\n')
        child = Mock()
        child.poll.side_effect = [None, 0, 0]
        child.wait.return_value = 0
        with patch.object(native, "materialize"), patch.object(native, "publish_memory"), patch.object(subprocess, "Popen", return_value=child):
            self.assertEqual(native.serve(self.root), 0)
        child.terminate.assert_not_called()
