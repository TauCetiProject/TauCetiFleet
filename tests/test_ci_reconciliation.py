"""Reclaim deregistered JIT guests without killing registered builds."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import github, host, store, vm


class Reconciliation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + key: str(Path(temp.name) / key)
                                     for key in ("STATE", "CONFIG", "DATA")})
        env.start()
        self.addCleanup(env.stop)
        self.config = dict(store.host_config(), ci_enabled=True)
        self.value = dict(slot=0, instance="guest", unit="guest.service", directory=temp.name,
                          runner_id=12, started_at=100, busy=True, phase="ready", run_id=42)
        self.path = vm.slot_file(self.config, "vm.json")
        store.atomic(self.path, self.value)
        self.slot = host.CISlot(0)

    def step(self, now, *, inventory=(), refresh=True):
        with (
            patch.object(host.time, "time", return_value=now),
            patch.object(host, "ci_service_state", return_value=(True, "unknown")),
            patch.object(host, "collect_ci_results"),
            patch.object(github, "ci_read", side_effect=AssertionError("no new metadata requests")),
            patch.object(vm, "retire", side_effect=lambda *_: self.path.unlink()) as retire,
            patch.object(host, "run") as stop,
            patch.object(vm, "start") as start,
        ):
            result = self.slot.step(self.config, inventory=inventory, refresh=refresh, cooling=False)
        start.assert_not_called()
        return result, stop, retire

    def test_busy_orphan_requires_fresh_confirmations_then_uses_existing_replacement_path(self):
        result, stop, _ = self.step(1000)
        self.assertTrue(result["busy"])
        stop.assert_not_called()
        _, stop, _ = self.step(1599)
        stop.assert_not_called()
        _, stop, _ = self.step(1601, refresh=False)
        stop.assert_not_called()
        result, stop, retire = self.step(1601)
        self.assertFalse(result)
        stop.assert_called_once_with(["systemctl", "--user", "stop", "guest.service"])
        retire.assert_called_once()
        receipt = store.read(store.state_dir() / "pending-ci.json")[0]
        self.assertEqual((receipt["run_id"], receipt["runner_id"]), (42, 12))
        self.assertEqual(receipt["host_health"], "unknown")
        with patch.object(host.time, "time", return_value=1607), patch.object(vm, "start", return_value={}) as start:
            self.slot.step(self.config, inventory=[], refresh=False, cooling=False)
        start.assert_called_once()

    def test_registered_runner_resets_grace_and_restart_keeps_absence_evidence(self):
        self.step(1000)
        self.step(1100, inventory=[dict(id=12, status="online", busy=True)])
        self.assertNotIn("runner_missing_since", store.read(self.path))
        self.step(1200)
        self.slot = host.CISlot(0)
        _, stop, _ = self.step(1799)
        stop.assert_not_called()
        _, stop, _ = self.step(1800)
        stop.assert_called_once()

    def test_old_registered_vms_and_recent_builds_are_never_killed_for_age(self):
        for status in ("online", "offline"):
            with self.subTest(status=status):
                self.value.update(job_started_at=1000000 - 60, runner_missing_since=1000)
                store.atomic(self.path, self.value)
                result, stop, retire = self.step(1000000, inventory=[dict(id=12, status=status, busy=True)])
                self.assertTrue(result["busy"])
                self.assertNotIn("runner_missing_since", result)
                stop.assert_not_called()
                retire.assert_not_called()

    def test_lost_start_notification_still_clears_a_sticky_busy_flag(self):
        del self.value["run_id"]
        store.atomic(self.path, self.value)
        self.step(1000)
        _, stop, retire = self.step(1600)
        stop.assert_called_once()
        retire.assert_called_once()
        self.assertIsNone(store.read(store.state_dir() / "pending-ci.json"))

    def test_startup_and_unavailable_inventory_do_not_authorize_stopping(self):
        self.value.update(started_at=900, phase="starting")
        store.atomic(self.path, self.value)
        _, stop, _ = self.step(1000)
        stop.assert_not_called()
        self.assertNotIn("runner_missing_since", store.read(self.path))
        self.step(1200)
        _, stop, _ = self.step(1000000, refresh=False)
        stop.assert_not_called()

    def test_stop_failure_preserves_guest_ownership_for_retry(self):
        self.step(1000)
        with (
            patch.object(host.time, "time", return_value=1600),
            patch.object(host, "ci_service_state", return_value=(True, "unknown")),
            patch.object(host, "run", side_effect=store.FleetError("stop failed")),
            patch.object(vm, "retire") as retire,
        ):
            with self.assertRaisesRegex(store.FleetError, "stop failed"):
                self.slot.step(self.config, inventory=[], refresh=True, cooling=False)
        self.assertTrue(self.path.exists())
        retire.assert_not_called()

    def test_complete_inventory_requires_stable_total_and_unique_runner_ids(self):
        with patch.object(github, "request", side_effect=[
            dict(total_count=2, runners=[dict(id=1)]), dict(total_count=2, runners=[dict(id=2)])
        ]):
            self.assertEqual(github.runners(self.config), [dict(id=1), dict(id=2)])
        for responses in (
            [dict(total_count=2, runners=[dict(id=1)]), dict(total_count=1, runners=[])],
            [dict(total_count=2, runners=[dict(id=1), dict(id=1)])],
            [dict(total_count=2, runners=[])],
            [dict(total_count=0, runners=[dict(id=1)])],
        ):
            with self.subTest(responses=responses), patch.object(github, "request", side_effect=responses):
                with self.assertRaisesRegex(store.FleetError, "incomplete"):
                    github.runners(self.config)


if __name__ == "__main__":
    unittest.main()
