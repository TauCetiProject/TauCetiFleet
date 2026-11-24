import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tauceti_fleet import capabilities, capacity, ci, cli, github, host, store, vm

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

    def repair_slice(self, *, current, file, high="max"):
        cg = self.root / "cgroup/user.slice/tauceti.slice/tauceti-repair.slice"
        cg.mkdir(parents=True)
        (cg / "memory.current").write_text(f"{current}\n")
        (cg / "memory.stat").write_text(f"anon {current - file}\nfile {file}\nshmem 0\nfile_dirty 0\nfile_writeback 0\n")
        (cg / "memory.high").write_text(f"{high}\n")
        return cg

    def refresh_repair(self, cg, available):
        shown = subprocess.CompletedProcess([], 0, stdout="/user.slice/tauceti.slice/tauceti-repair.slice\n")
        cgroup_root = self.root / "cgroup"
        real = Path
        with (
            patch.object(host, "run", return_value=shown),
            patch.object(host.lean_pressure, "memory", return_value=(112 * GIB, available)) as memory,
            patch.object(host, "Path", side_effect=lambda p, *a: cgroup_root if p == "/sys/fs/cgroup" else real(p, *a)),
        ):
            value = host.refresh_repair_memory()
        memory.assert_called_once_with("/user.slice/tauceti.slice/tauceti-repair.slice")
        return value, int((cg / "memory.high").read_text())

    def test_repair_takes_what_the_vms_leave_under_the_shared_ceiling(self):
        # Quiet VMs: repair's limit sits far above its use, so it caps nothing.
        cg = self.repair_slice(current=30 * GIB, file=10 * GIB)
        value, high = self.refresh_repair(cg, 60 * GIB)
        self.assertEqual(high, 20 * GIB + 60 * GIB - host.REPAIR_HEADROOM)
        self.assertEqual(value["pinned"], 20 * GIB)
        # A growing staging build leaves less: the limit falls below repair's own use.
        _, high = self.refresh_repair(cg, 5 * GIB)
        self.assertEqual(high, 20 * GIB + 5 * GIB - host.REPAIR_HEADROOM)
        self.assertLess(high, 20 * GIB)
        # It never goes below 1 GiB, and small rises are not rewritten.
        _, high = self.refresh_repair(cg, 0)
        self.assertEqual(high, GIB)
        (cg / "memory.high").write_text(f"{60 * GIB}\n")
        _, high = self.refresh_repair(cg, host.REPAIR_HEADROOM + 40 * GIB + 100 * 1024**2)
        self.assertEqual(high, 60 * GIB)

    def test_live_update_moves_the_ceiling_onto_the_parent_slice(self):
        units = self.root / "units"
        units.mkdir()
        (units / "tauceti-fleet.slice").write_text(f"[Slice]\nMemoryHigh={100 * GIB}\nMemoryMax={100 * GIB}\nTasksMax=32768\n")
        caps = dict(memory_total=125 * GIB, memory_available=30 * GIB)
        with patch.object(host, "units", return_value=units):
            self.assertTrue(host.install_ceiling(caps))
            self.assertFalse(host.install_ceiling(caps))
        self.assertEqual((units / "tauceti.slice").read_text(), f"[Slice]\nMemoryMax={capacity.shared_memory_bytes(caps)}\n")
        self.assertNotIn("Memory", (units / "tauceti-fleet.slice").read_text())

    def test_live_update_raises_the_ci_task_limit_and_keeps_memory_settings(self):
        units = self.root / "units"
        units.mkdir()
        (units / "tauceti-fleet-ci.slice").write_text("[Slice]\nCPUWeight=1000\nMemoryMax=7\nTasksMax=16384\n")
        caps = dict(memory_total=125 * GIB, memory_available=30 * GIB)
        with patch.object(host, "units", return_value=units):
            self.assertTrue(host.install_ceiling(caps))
            self.assertFalse(host.install_ceiling(caps))
        self.assertEqual((units / "tauceti-fleet-ci.slice").read_text(),
                         f"[Slice]\nCPUWeight=1000\nMemoryMax=7\nTasksMax={host.CI_TASKS_MAX}\n")
        self.assertEqual((units / "tauceti-fleet.slice").read_text(), f"[Slice]\nTasksMax={host.FLEET_TASKS_MAX}\n")
        self.assertGreaterEqual(host.FLEET_TASKS_MAX, host.CI_TASKS_MAX + host.WORKER_TASKS_MAX)

    def test_hosts_without_a_repair_slice_are_untouched(self):
        with patch.object(host, "run", return_value=subprocess.CompletedProcess([], 1, stdout="")):
            self.assertIsNone(host.refresh_repair_memory())

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
