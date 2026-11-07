import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import ci_images, cli, host, prefill_guest, store, vm


class Images(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            TAUCETI_FLEET_DATA=str(self.directory / "data"),
            TAUCETI_FLEET_STATE=str(self.directory / "state"),
            TAUCETI_FLEET_CONFIG=str(self.directory / "config"),
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.payload = dict(
            schema=1,
            producer="TauCetiFleet/ci-prefill-v1",
            producer_revision="a" * 40,
            platform="Linux-x86_64",
            project_path=str(prefill_guest.PROJECT),
            uid=1001,
            toolchain="leanprover/lean4:v4.36.0-rc2",
            payload_sha256="b" * 64,
            payload_bytes=512,
            config_sha256={name: "c" * 64 for name in prefill_guest.CONFIG},
        )
        self.config = dict(ci_kinds=["pr"], ci_prefill_enabled=True, ci_disk_gib=100)
        self.base = dict(sha256="d" * 64, image_revision=vm.IMAGE_REVISION, ubuntu_sha256=vm.UBUNTU_SHA256)
        store.atomic(store.data_dir() / "images/prepared.json", self.base)
        self.validation = dict(
            native_executable_reused=True,
            native_cache_get_exit_status=0,
            native_executable_sha256="e" * 64,
            consumer=dict(hit=True),
        )

    def publish(self, contents):
        image = self.directory / "candidate.qcow2"
        image.write_bytes(contents)
        with patch.object(
            ci_images,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({"format": "qcow2", "virtual-size": 20 * 1024**3})
            ),
        ):
            return ci_images.publish(
                image, self.payload, base=self.base, seconds=12, validation=self.validation, consumer="f" * 64
            )

    def test_versioned_publication_and_future_selection_do_not_replace_readers(self):
        old = self.publish(b"first image")
        selected = ci_images.select(self.config)
        self.assertEqual(selected["sha256"], old["sha256"])
        store.atomic(vm.slot_file({}, "vm.json"), dict(image_sha256=old["sha256"]))
        new = self.publish(b"second image")
        self.assertNotEqual(old["sha256"], new["sha256"])
        self.assertEqual(Path(selected["path"]).read_bytes(), b"first image")
        self.assertEqual(ci_images.select(self.config)["sha256"], new["sha256"])
        self.assertEqual(ci_images.retire_unused(), [])
        vm.slot_file({}, "vm.json").unlink()
        self.assertEqual(ci_images.retire_unused(), [old["sha256"]])
        self.assertTrue(Path(ci_images.select(self.config)["path"]).is_file())

    def test_missing_corrupt_or_unsupported_optional_images_keep_ordinary_admission(self):
        self.assertIsNone(ci_images.select(self.config))
        value = self.publish(b"complete")
        for config in (
            {},
            {**self.config, "ci_prefill_enabled": False},
            {**self.config, "ci_kinds": ["staging"]},
            {**self.config, "ci_kinds": ["pr", "staging"]},
        ):
            self.assertIsNone(ci_images.select(config))
        image = ci_images.root() / (value["sha256"] + ".qcow2")
        image.chmod(0o600)
        image.write_bytes(b"corrupt!")
        image.chmod(0o400)
        self.assertIsNone(ci_images.select(self.config))

    def test_partial_publication_preserves_previous_usable_pointer(self):
        old = self.publish(b"previous usable image")
        atomic = ci_images.atomic

        def interrupted(path, value):
            if Path(path).name == "current.json":
                raise OSError("simulated interrupted publication")
            return atomic(path, value)

        with patch.object(ci_images, "atomic", side_effect=interrupted), self.assertRaises(OSError):
            self.publish(b"next image")
        self.assertEqual(ci_images.select(self.config)["sha256"], old["sha256"])

    def test_corrupt_manifest_shapes_always_choose_the_ordinary_base(self):
        original = self.publish(b"complete")
        path = ci_images.root() / (original["sha256"] + ".json")
        variants = [
            [],
            {**original, "validation": []},
            {**original, "validation": {"consumer": []}},
            {**original, "producer_seconds": None},
        ]
        wrong = copy.deepcopy(original)
        wrong["payload"]["config_sha256"] = list(prefill_guest.CONFIG)
        variants.append(wrong)
        for value in variants:
            with self.subTest(value=type(value).__name__):
                path.chmod(0o600)
                path.write_text(json.dumps(value))
                path.chmod(0o400)
                self.assertIsNone(ci_images.select(self.config))

    def test_selection_checks_disk_size_base_refresh_and_producer_changes(self):
        self.publish(b"complete")
        self.assertIsNotNone(ci_images.select({**self.config, "ci_disk_gib": 20}))
        self.assertIsNone(ci_images.select({**self.config, "ci_disk_gib": 19}))
        with patch.object(ci_images, "producer_identity", return_value="f" * 64):
            self.assertIsNone(ci_images.select(self.config))
        store.atomic(store.data_dir() / "images/prepared.json", {**self.base, "sha256": "f" * 64})
        self.assertIsNone(ci_images.select(self.config))

    def test_prepare_rejects_a_stale_base_abi_without_building_or_claiming_new_provenance(self):
        store.atomic(store.data_dir() / "images/prepared.json", {**self.base, "image_revision": -1})
        with (
            patch.object(ci_images, "trusted_main", return_value=("a" * 40, {}, "f" * 64)),
            patch.object(vm, "prepare_tools") as build,
            self.assertRaisesRegex(ValueError, "different image revision"),
        ):
            ci_images.prepare(self.config)
        build.assert_not_called()

    def test_gc_collects_orphans_and_a_partial_manifest_can_be_repaired(self):
        value = self.publish(b"complete")
        path = ci_images.root() / (value["sha256"] + ".json")
        path.chmod(0o600)
        self.assertIsNone(ci_images.select(self.config))
        self.publish(b"complete")
        self.assertIsNotNone(ci_images.select(self.config))
        orphan = ci_images.root() / ("f" * 64 + ".qcow2")
        orphan.write_bytes(b"interrupted image publication")
        orphan.chmod(0o400)
        self.assertEqual(ci_images.retire_unused(), ["f" * 64])
        self.assertFalse(orphan.exists())

    def test_gc_defers_if_reference_files_change_during_the_unlocked_scan(self):
        old = self.publish(b"old")
        self.publish(b"current")
        with patch.object(ci_images, "reference_files", side_effect=[{}, {Path("new reference"): (1, 2)}]):
            self.assertEqual(ci_images.retire_unused(), [])
        self.assertTrue((ci_images.root() / (old["sha256"] + ".qcow2")).exists())

    def test_background_owner_is_opt_in_and_never_blocks_the_controller(self):
        owner = ci_images.Owner()
        with patch.object(ci_images.threading, "Thread") as thread:
            owner.step(dict(ci_enabled=True), now=1)
            thread.assert_not_called()
            owner.step(dict(ci_enabled=True, ci_prefill_enabled=True, ci_kinds=["pr"]), now=1)
            thread.return_value.start.assert_called_once()
            owner.step(dict(ci_enabled=True, ci_prefill_enabled=True, ci_kinds=["pr"]), now=2)
            self.assertEqual(thread.call_count, 1)
        owner = ci_images.Owner()
        with patch.object(ci_images.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("no thread available")
            owner.step(dict(ci_enabled=True, ci_prefill_enabled=True, ci_kinds=["pr"]), now=1)
            self.assertEqual(owner.next_refresh, 901)

    def test_publication_validates_written_manifest_before_switching_pointer(self):
        old = self.publish(b"old complete")
        with patch.object(ci_images, "manifest", side_effect=ValueError("unreadable metadata")):
            with self.assertRaisesRegex(ValueError, "unreadable metadata"):
                self.publish(b"new complete")
        self.assertEqual(ci_images.current()["sha256"], old["sha256"])

    def test_gc_does_no_subprocess_work_inside_an_admission(self):
        self.publish(b"complete")
        with store.locked(store.state_dir() / "ci-start.lock"), patch.object(ci_images, "run") as run:
            self.assertEqual(ci_images.retire_unused(), [])
            run.assert_not_called()

    def test_failed_build_discards_disks_but_keeps_diagnostics(self):
        staging = self.directory / "staging"
        staging.mkdir()
        for name in ("disk.qcow2", "scratch.qcow2", "complete.qcow2", "seed.iso", "console.log"):
            (staging / name).write_text("owned")
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with ci_images.discard_build_disks(staging):
                raise RuntimeError("failure")
        self.assertEqual([p.name for p in staging.iterdir()], ["console.log"])

    def test_builder_preserves_ordinary_memory_and_disk_headroom(self):
        gib = 1024**3
        with patch.object(vm, "start_memory_available", return_value=29 * gib):
            with self.assertRaisesRegex(store.FleetError, "ordinary CI"):
                ci_images.builder_resources(self.directory)
        with (
            patch.object(vm, "start_memory_available", return_value=30 * gib),
            patch.object(ci_images.shutil, "disk_usage") as usage,
        ):
            usage.return_value.free = 63 * gib
            with self.assertRaisesRegex(store.FleetError, "64 GiB"):
                ci_images.builder_resources(self.directory)
            usage.return_value.free = 64 * gib
            ci_images.builder_resources(self.directory)

    def test_base_refresh_reports_busy_image_owner_without_waiting(self):
        with (
            store.locked(store.data_dir() / "images/prepare.lock"),
            patch.object(vm, "prepare_tools"),
            self.assertRaisesRegex(store.FleetError, "already in progress"),
        ):
            vm.prepare_image(self.config)

    def test_owner_backs_off_same_failure_and_retries_changed_configuration(self):
        class InlineThread:
            def __init__(self, *, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

            def is_alive(self):
                return False

        owner = ci_images.Owner()
        config = dict(ci_enabled=True, ci_prefill_enabled=True, ci_kinds=["pr"])
        trusted = ("a" * 40, {"pin": "one"}, "f" * 64)
        with (
            patch.object(ci_images.threading, "Thread", InlineThread),
            patch.object(ci_images, "trusted_main", return_value=trusted) as capture,
            patch.object(ci_images, "prepare", side_effect=store.FleetError("bad pins")) as build,
            patch.object(ci_images.time, "monotonic", return_value=100),
        ):
            owner.step(config, now=100)
            self.assertEqual(owner.retry_after, 1000)
            owner.step(config, now=1000)
            self.assertEqual(build.call_count, 1)
            capture.return_value = (trusted[0], {"pin": "two"}, trusted[2])
            owner.step(config, now=1900)
            self.assertEqual(build.call_count, 2)
            self.assertEqual(owner.failures, 1)
            with patch.object(ci_images.time, "monotonic", return_value=1001):
                owner.step(config, now=2800)
            self.assertEqual(owner.failures, 2)
            self.assertEqual(owner.retry_after, 2801)

    def test_disabling_owner_cancels_its_builder_without_changing_admissions(self):
        owner = ci_images.Owner()
        owner.unit = "tauceti-fleet-prefill-" + "a" * 16 + ".scope"
        from unittest.mock import Mock

        owner.thread = Mock()
        owner.thread.is_alive.return_value = True
        config = dict(ci_enabled=True, ci_prefill_enabled=False, ci_kinds=["pr"])
        with patch.object(ci_images, "run") as stop:
            owner.step(config)
            self.assertTrue(owner.cancelled.is_set())
            self.assertEqual(stop.call_args.args[0][-1], owner.unit)
        self.assertTrue(config["ci_enabled"])

    def test_corrupt_pointer_and_incomplete_reference_observations_do_not_retire(self):
        old = self.publish(b"first")
        new = self.publish(b"second")
        disk = store.state_dir() / "vms/aaaaaaaaaaaaaaaa/disk.qcow2"
        disk.parent.mkdir(parents=True)
        disk.touch()
        with patch.object(ci_images, "run", side_effect=store.FleetError("unknown backing")):
            self.assertEqual(ci_images.retire_unused(), [])
        (ci_images.root() / "current.json").write_text('{"sha256":"invalid"}')
        self.assertEqual(ci_images.retire_unused(), [])
        for value in (old, new):
            self.assertTrue((ci_images.root() / (value["sha256"] + ".qcow2")).exists())

    def test_gc_retains_recoverable_overlay_without_slot_state(self):
        old = self.publish(b"first")
        self.publish(b"second")
        disk = store.state_dir() / "vms/aaaaaaaaaaaaaaaa/disk.qcow2"
        disk.parent.mkdir(parents=True)
        disk.touch()
        info = dict(format="qcow2", **{"full-backing-filename": str(ci_images.root() / (old["sha256"] + ".qcow2"))})
        with patch.object(ci_images, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(info))):
            self.assertEqual(ci_images.retire_unused(), [])
        disk.unlink()
        self.assertEqual(ci_images.retire_unused(), [old["sha256"]])

    def test_disabling_changes_only_subsequent_selection_and_keeps_admissions(self):
        value = self.publish(b"first")
        store.update_host(ci_enabled=True, workers_enabled=True, ci_prefill_enabled=True)
        self.assertFalse(host.rpc(dict(action="ci-prefill", enabled=False))["enabled"])
        config = store.host_config()
        self.assertTrue(config["ci_enabled"])
        self.assertTrue(config["workers_enabled"])
        self.assertTrue((ci_images.root() / (value["sha256"] + ".qcow2")).exists())

    def test_builder_rpc_is_live_and_cli_requires_explicit_host(self):
        store.update_host(ci_enabled=True, workers_enabled=True)
        with patch.object(ci_images, "prepare", return_value=dict(ready=True)) as prepare:
            self.assertEqual(host.rpc(dict(action="prepare-ci-prefill")), dict(ready=True))
            prepare.assert_called_once_with()
        self.assertEqual(cli.parser().parse_args(["ci-prefill", "local", "--disable"]).enabled, False)
        self.assertEqual(cli.parser().parse_args(["prepare-ci-prefill", "local"]).host, "local")

    def test_vm_start_records_immutable_backing_and_restart_keeps_that_version(self):
        old = self.publish(b"first guest image")
        store.atomic(store.data_dir() / "images/prepared.json", self.base)
        config = dict(store.host_config(), **self.config)
        with (
            patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
            patch.object(vm, "executable", side_effect=lambda name: name),
            patch.object(vm.github, "jit_config", return_value=(12, "fixture")),
            patch.object(vm, "cloud_seed", return_value=self.directory / "seed.iso"),
        ):
            value = vm._start(config)
        create = next(call.args[0] for call in run.call_args_list if call.args[0][0] == "qemu-img")
        self.assertEqual(create[create.index("-b") + 1], str(ci_images.root() / (old["sha256"] + ".qcow2")))
        self.assertEqual(value["image_sha256"], old["sha256"])
        self.publish(b"new future image")
        with (
            patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)),
            patch.object(vm.github, "jit_config") as registration,
        ):
            self.assertEqual(vm._start(config), value)
            registration.assert_not_called()

    def test_builder_shape_is_bounded_and_discard_does_not_change_normal_launch(self):
        with patch.object(vm, "executable", return_value="qemu"):
            ordinary = vm.qemu_args({}, self.directory, "disk", "seed", small=True)
            builder = vm.qemu_args(
                {},
                self.directory,
                "disk",
                "seed",
                small=True,
                shape=dict(vcpus=4, memory_gib=8),
                discard=True,
                events=False,
            )
        self.assertEqual(builder[builder.index("-smp") + 1], "4")
        self.assertEqual(builder[builder.index("-m") + 1], "8192")
        self.assertTrue(any("discard=unmap" in arg for arg in builder))
        self.assertFalse(any("discard=unmap" in arg for arg in ordinary))
        self.assertFalse(any("org.tauceti.events" in arg for arg in builder))

    @unittest.skipUnless(shutil.which("qemu-img") and shutil.which("qemu-io"), "QEMU image tools required")
    def test_actual_private_overlays_isolate_writes_and_survive_pointer_turnover(self):
        image = self.directory / "real.qcow2"
        subprocess.run(["qemu-img", "create", "-f", "qcow2", str(image), "64M"], check=True, capture_output=True)
        value = ci_images.publish(image, self.payload, base=self.base, seconds=0, validation=self.validation)
        parent = Path(ci_images.select(self.config)["path"])
        before = vm.digest(parent)
        disks = []
        for name in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"):
            disk = store.state_dir() / "vms" / name / "disk.qcow2"
            disk.parent.mkdir(parents=True)
            subprocess.run(
                ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(parent), str(disk)],
                check=True,
                capture_output=True,
            )
            disks.append(disk)
        subprocess.run(
            ["qemu-io", "-f", "qcow2", "-c", "write -P 0x41 0 4096", str(disks[0])], check=True, capture_output=True
        )
        subprocess.run(
            ["qemu-io", "-f", "qcow2", "-c", "read -P 0 0 4096", str(disks[1])], check=True, capture_output=True
        )
        self.assertEqual(vm.digest(parent), before)
        self.publish(b"next version")
        self.assertEqual(ci_images.retire_unused(), [])
        for disk in disks:
            disk.unlink()
        self.assertEqual(ci_images.retire_unused(), [value["sha256"]])


if __name__ == "__main__":
    unittest.main()
