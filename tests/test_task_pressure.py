import tempfile
import unittest
from pathlib import Path

from tauceti_fleet.capabilities import task_accounting, task_observation


class Tasks(unittest.TestCase):
    def test_kernel_counters_missing_and_unlimited_are_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(task_accounting(root)["current"])
            for name, value in {"pids.current": "80", "pids.max": "max", "pids.peak": "95", "pids.events.local": "max 17"}.items():
                (root / name).write_text(value)
            self.assertEqual(task_accounting(root), dict(current=80, maximum=None, peak=95, rejected=17))

    def test_new_rejections_are_deltas_not_historical_totals(self):
        first = task_observation(dict(rejected=100), None, 1000)
        self.assertIsNone(first["new_rejections"])
        next = task_observation(dict(rejected=105), first, 1010)
        self.assertEqual(next["new_rejections"], 5)
        self.assertEqual(next["sample_seconds"], 10)
        self.assertEqual(next["last_rejection_at"], 1010)
        quiet = task_observation(dict(rejected=105), next, 1020)
        self.assertEqual(quiet["new_rejections"], 0)
        self.assertEqual(quiet["last_rejection_at"], 1010)
        self.assertIsNone(task_observation(dict(rejected=0), quiet, 1030)["new_rejections"])
