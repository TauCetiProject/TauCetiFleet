import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tauceti_fleet import capabilities, capacity, ci, cli, github, host, hostos, store, vm

hostos.MACOS = False

GIB = 1024**3


class Overcommit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + k.upper(): str(self.root / k)
                                     for k in ("config", "state", "data")})
        env.start()
        self.addCleanup(env.stop)
        self.config = capacity.settings(store.host_config(), ci="both", maximum=12,
                                        vcpus=16, memory_gib=32, disk_gib=64)

    def checks(self, config, free=100 * GIB):
        with (
            patch.object(capabilities, "resources", return_value=dict(cpus=96, memory_total=125 * GIB, memory_available=30 * GIB)),
            patch.object(capabilities, "run", return_value=subprocess.CompletedProcess([], 0, stdout=str(40 * GIB))),
            patch.object(capabilities, "executable", return_value=None),
            patch.object(capabilities.os, "open", side_effect=PermissionError),
            patch.object(capabilities.shutil, "disk_usage", return_value=SimpleNamespace(free=free)),
        ):
            return {c["name"]: c for c in capabilities.doctor(config, probe=False)["checks"]}

    def test_explicit_budget_allows_cpu_and_guest_ram_overcommit(self):
        unbounded = self.checks(self.config)
        self.assertEqual(unbounded["ci-shape"]["status"], "ok")
        self.assertEqual(unbounded["ci-memory-overcommit"]["status"], "warning")
        config = capacity.settings(self.config, ci="both", memory_budget_gib=80)
        checks = self.checks(config)
        self.assertEqual(checks["ci-shape"]["status"], "ok")
        self.assertEqual(checks["available-memory-headroom"]["status"], "ok")
        self.assertEqual(checks["ci-memory-overcommit"]["status"], "warning")
        self.assertEqual(config["ci_vcpus"], 16)
        self.assertEqual(config["ci_memory_gib"], 32)
        self.assertEqual(capacity.memory_budget_gib(config), 80)
        config["ci_memory_budget_gib"] = 124
        self.assertEqual(self.checks(config)["ci-shape"]["status"], "blocked")

    def test_invalid_saved_and_requested_memory_budgets_fail(self):
        for value in (0, -1, True, 1.5, 1048577):
            with self.assertRaises(store.FleetError):
                capacity.settings(self.config, ci="both", memory_budget_gib=value)
            with self.assertRaises(store.FleetError):
                capacity.memory_budget_gib(dict(self.config, ci_memory_budget_gib=value))

    def test_shared_default_requires_startup_headroom_without_reserving_the_ceiling(self):
        for config in (dict(self.config, ci_max_jobs=1), dict(self.config, ci_max_jobs=1, ci_memory_budget_gib=None)):
            checks = self.checks(config)
            self.assertEqual(checks["ci-shape"]["status"], "ok")
            self.assertEqual(checks["available-memory-headroom"]["status"], "ok")

    def test_sparse_disks_require_startup_space_not_sum_of_virtual_sizes(self):
        self.assertEqual(self.checks(self.config)["storage"]["status"], "ok")
        self.assertEqual(self.checks(self.config, free=20 * GIB)["storage"]["status"], "blocked")

    def test_native_readiness_reports_existing_namespace_and_disk_requirements(self):
        config = dict(self.config, ci_kinds=["pr"], ci_max_jobs=4)
        with patch.object(host.native_runner, "prerequisites", return_value=None):
            checks = self.checks(config, free=48 * GIB)
            self.assertEqual(checks["native-pr-isolation"]["status"], "ok")
            self.assertEqual(checks["storage"]["status"], "ok")
            self.assertIn("capped at 54 GiB", checks["ci-shape"]["evidence"])
            self.assertEqual(self.checks(config, free=47 * GIB)["storage"]["status"], "blocked")
        with patch.object(host.native_runner, "prerequisites", side_effect=store.FleetError("namespaces unavailable")):
            self.assertEqual(self.checks(config)["native-pr-isolation"]["status"], "blocked")

    def test_native_caps_fitting_explicit_budget_do_not_reserve_that_budget(self):
        config = dict(self.config, ci_kinds=["pr"], ci_max_jobs=2, ci_memory_budget_gib=110)
        # Two 54 GiB caps fit the 110 GiB ceiling; the host currently has 30 GiB
        # available. Starting a job needs startup headroom, not 110 GiB free.
        with patch.object(host.native_runner, "prerequisites", return_value=None):
            checks = self.checks(config)
        self.assertEqual(checks["ci-shape"]["status"], "ok")
        self.assertNotIn("ci-memory-overcommit", checks)
        self.assertEqual(checks["available-memory-headroom"]["status"], "ok")

    def test_overlay_filesystem_is_independent_of_image_storage(self):
        overlay = store.state_dir() / "vms"
        overlay.mkdir(parents=True)
        self.assertEqual(capacity.disk_root(), overlay)
        self.assertNotEqual(capacity.disk_root(), store.data_dir())

    def test_authorized_overcommit_oom_does_not_pause_all_slots(self):
        for kind in ("pr", "staging"):
            job = dict(conclusion="failure", steps=[])
            self.assertTrue(ci.classify(job, host_health="oom", kind=kind)["cooldown"])
            decision = ci.classify(job, host_health="oom", kind=kind, memory_overcommit=True)
            self.assertFalse(decision["cooldown"])
            self.assertEqual(decision["reason"], "memory_budget_oom")
            self.assertTrue(ci.classify(job, host_health="fault", kind=kind, memory_overcommit=True)["cooldown"])

    def test_guests_return_free_pages_and_report_disk_full(self):
        with patch.object(vm, "executable", side_effect=lambda x: x):
            args = vm.qemu_args(self.config, self.root, self.root / "disk", self.root / "seed")
        self.assertIn("virtio-balloon-pci,free-page-reporting=on", args)
        self.assertTrue(any("werror=report" in a for a in args))

    def test_low_disk_space_prevents_new_registration(self):
        store.atomic(store.data_dir() / "images/prepared.json", {})
        with patch.object(vm, "start_memory_available", return_value=100 * GIB), patch.object(vm.shutil, "disk_usage", return_value=SimpleNamespace(free=GIB)), patch.object(github, "jit_config") as jit:
            with self.assertRaisesRegex(store.FleetError, "free space"):
                vm.start(self.config)
            jit.assert_not_called()

    def test_worker_ram_shares_host_capacity_without_subtracting_ci_budget(self):
        config = dict(self.config, ci_allowed=True, ci_memory_budget_gib=80)
        _, maximum = host.worker_memory_limits(config, dict(memory_total=125 * GIB, memory_available=30 * GIB))
        self.assertEqual(maximum, 111.5 * GIB)

    def test_slice_budget_cannot_shrink_below_live_ci_usage(self):
        config = dict(self.config, ci_allowed=True, ci_memory_budget_gib=80)
        with patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, stdout=str(81 * GIB))), patch.object(host, "units", return_value=self.root):
            with self.assertRaisesRegex(store.FleetError, "current CI usage"):
                host.write_slices(config, dict(memory_total=125 * GIB, memory_available=30 * GIB))
        self.assertFalse((self.root / "tauceti-fleet-ci.slice").exists())

    def test_cli_exposes_explicit_budget(self):
        args = cli.parser().parse_args(["enable", "local", "--ci", "both", "--max-jobs", "12", "--ci-memory-budget-gib", "80"])
        self.assertEqual(args.ci_memory_budget_gib, 80)

    def test_live_growth_persists_budget_without_replacing_existing_guests(self):
        store.update_host(**dict(self.config, ci_max_jobs=2, ci_enabled=True, workers_enabled=True, ci_staging_slots=1))
        store.atomic(store.data_dir() / "images/prepared.json", {})
        receipt = dict(slot=0, instance="existing", unit="existing.service", busy=True)
        store.atomic(vm.slot_file({"ci_slot": 0}, "vm.json"), receipt)
        with (
            patch.object(host, "doctor", return_value=dict(readiness=dict(ci="ready"))),
            patch.object(github, "capacity_policy", return_value=dict(max_jobs=12, kinds=["pr", "staging"])),
            patch.object(host, "write_slices"), patch.object(host, "resources", return_value={}),
            patch.object(host, "worker_config"), patch.object(vm, "start") as start,
            patch.object(host.native_runner, "prepare", return_value={}),
            patch.object(host, "install_service") as install, patch.object(vm, "retire") as retire,
        ):
            result = host.enable(ci="both", maximum=12, memory_budget_gib=80)
        self.assertEqual(result["ci_max_jobs"], 12)
        self.assertTrue(result["workers_enabled"])
        self.assertEqual(store.host_config()["ci_memory_budget_gib"], 80)
        self.assertEqual(store.host_config()["ci_staging_slots"], 1)
        self.assertEqual(store.read(vm.slot_file({"ci_slot": 0}, "vm.json")), receipt)
        start.assert_not_called()
        retire.assert_not_called()
        install.assert_called_once_with(restart=False)
