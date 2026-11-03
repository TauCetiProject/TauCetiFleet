import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capacity, cli, github, host, store, vm

GIB = 1024**3


class CIMemoryGuard(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + k.upper(): str(self.root / k)
                                     for k in ("config", "state", "data")})
        env.start()
        self.addCleanup(env.stop)
        self.config = capacity.settings(store.host_config(), ci="both", maximum=10, vcpus=28,
                                        memory_gib=48, disk_gib=100, memory_budget_gib=880)

    def test_guard_is_off_unless_configured(self):
        self.assertIsNone(capacity.memory_guard(self.config))
        self.assertEqual(host.ci_memory_guard(self.config, 870 * GIB, False), (False, None))

    def test_resume_defaults_below_pause(self):
        config = capacity.settings(self.config, ci="both", memory_pause_gib=760)
        self.assertEqual(capacity.memory_guard(config), (760, 646))

    def test_invalid_thresholds_fail(self):
        for pause, resume in ((760, 760), (760, 800), (760, 0), (880, 650), (900, 650), (760, True)):
            with self.assertRaises(store.FleetError):
                capacity.settings(self.config, ci="both", memory_pause_gib=pause, memory_resume_gib=resume)

    def test_zero_disables_a_configured_guard(self):
        config = capacity.settings(self.config, ci="both", memory_pause_gib=760, memory_resume_gib=650)
        config = capacity.settings(config, ci="both", memory_pause_gib=0)
        self.assertIsNone(capacity.memory_guard(config))

    def test_hysteresis_pauses_at_the_high_mark_and_resumes_at_the_low_mark(self):
        config = capacity.settings(self.config, ci="both", memory_pause_gib=760, memory_resume_gib=650)
        paused = False
        decisions = []
        for used in (500, 759, 760, 700, 651, 650, 700, 760):
            paused, detail = host.ci_memory_guard(config, used * GIB, paused)
            decisions.append(paused)
            self.assertEqual((detail["pause_gib"], detail["resume_gib"]), (760, 650))
        self.assertEqual(decisions, [False, False, True, True, True, False, False, True])

    def test_unknown_usage_keeps_the_previous_decision(self):
        config = capacity.settings(self.config, ci="both", memory_pause_gib=760, memory_resume_gib=650)
        self.assertEqual(host.ci_memory_guard(config, None, True)[0], True)
        self.assertEqual(host.ci_memory_guard(config, None, False)[0], False)
        self.assertIsNone(host.ci_memory_guard(config, None, True)[1]["current_gib"])

    def test_a_paused_slot_starts_no_guest_and_retires_an_idle_one(self):
        config = dict(self.config, ci_enabled=True)
        with patch.object(vm, "start", return_value={}) as start:
            host.CISlot(0).step(config, inventory=[], refresh=False, cooling=True)
            start.assert_not_called()
        idle = dict(slot=1, instance="idle", unit="idle.service", started_at=0, busy=False, runner_id=5,
                    directory=str(self.root / "vms/idle"))
        store.atomic(vm.slot_file({"ci_slot": 1}, "vm.json"), idle)
        with patch.object(host, "ci_service_state", return_value=(True, "healthy")), \
                patch.object(vm, "retire") as retire, patch.object(vm, "start") as start:
            # Idle guests are retired on the controller's GitHub inventory refresh.
            host.CISlot(1).step(config, inventory=[dict(id=5, status="online", busy=False)], refresh=True, cooling=True)
            retire.assert_called_once()
            start.assert_not_called()

    def test_cli_exposes_thresholds(self):
        args = cli.parser().parse_args(["enable", "local", "--ci", "both", "--ci-memory-pause-gib", "760",
                                        "--ci-memory-resume-gib", "650"])
        self.assertEqual((args.ci_memory_pause_gib, args.ci_memory_resume_gib), (760, 650))

    def test_live_enable_sets_thresholds_without_replacing_busy_guests(self):
        store.update_host(**dict(self.config, ci_enabled=True))
        store.atomic(store.data_dir() / "images/prepared.json", {})
        receipt = dict(slot=0, instance="busy", unit="busy.service", busy=True)
        store.atomic(vm.slot_file({"ci_slot": 0}, "vm.json"), receipt)
        with (
            patch.object(host, "doctor", return_value=dict(readiness=dict(ci="ready"))),
            patch.object(github, "capacity_policy", return_value=dict(max_jobs=12, kinds=["pr", "staging"])),
            patch.object(host, "write_slices"), patch.object(host, "resources", return_value={}),
            patch.object(host, "worker_config"), patch.object(vm, "start") as start,
            patch.object(host, "install_service"), patch.object(vm, "retire") as retire,
        ):
            result = host.enable(ci="both", memory_pause_gib=760, memory_resume_gib=650)
            self.assertEqual(result["ci_memory_guard"], dict(pause_gib=760, resume_gib=650))
            self.assertEqual(capacity.memory_guard(store.host_config()), (760, 650))
            result = host.enable(ci="both", memory_pause_gib=0)
            self.assertIsNone(result["ci_memory_guard"])
            self.assertIsNone(capacity.memory_guard(store.host_config()))
        self.assertEqual(store.read(vm.slot_file({"ci_slot": 0}, "vm.json")), receipt)
        start.assert_not_called()
        retire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
