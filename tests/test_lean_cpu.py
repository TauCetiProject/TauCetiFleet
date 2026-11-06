import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import lean_pressure as gate


class LeanCPU(unittest.TestCase):
    def test_spare_capacity_never_sleeps_even_with_cold_cpu_history(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, XDG_CACHE_HOME=directory),
            patch.object(gate, "cpu_sample", return_value=(100, 20)),
            patch.object(gate, "memory", return_value=(100 * 1024**3, 90 * 1024**3)),
            patch.object(gate.time, "sleep", side_effect=AssertionError("unconditional sleep")),
        ):
            gate.wait_for_capacity()
            gate.wait_for_capacity()

    def test_cpu_history_measures_across_invocations_and_reuses_recent_samples(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, XDG_CACHE_HOME=directory),
            patch.object(gate.time, "monotonic", side_effect=[100, 100.5, 100.6, 101, 120]),
            patch.object(gate, "cpu_sample", side_effect=[(100, 20), (200, 25), (210, 30), (300, 100), (400, 200)]),
            patch.object(gate.time, "sleep", side_effect=AssertionError("CPU sampling slept")),
        ):
            self.assertEqual(gate.cpu_usage(), 0)  # Cold start seeds counters.
            self.assertEqual(gate.cpu_usage(), 95)
            self.assertEqual(gate.cpu_usage(), 95)  # No noisy near-zero interval.
            self.assertEqual(gate.cpu_usage(), 25)
            self.assertEqual(gate.cpu_usage(), 0)  # Stale baseline starts afresh.

    def test_busy_cached_cpu_waits_then_resumes_after_pressure_clears(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, XDG_CACHE_HOME=directory),
            patch.object(gate.time, "monotonic", side_effect=[100, 100.5, 103]),
            patch.object(gate, "cpu_sample", side_effect=[(100, 20), (200, 25), (700, 525)]),
            patch.object(gate, "memory", return_value=(100 * 1024**3, 90 * 1024**3)),
            patch.object(gate.random, "uniform", return_value=2.5),
            patch.object(gate.time, "sleep") as sleep,
        ):
            gate.cpu_usage()
            gate.wait_for_capacity()
            sleep.assert_called_once_with(2.5)

    def test_bad_cpu_history_does_not_delay_or_fail_compilation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, XDG_CACHE_HOME=directory),
            patch.object(gate, "cpu_sample", return_value=(100, 20)),
        ):
            cache = Path(directory) / "lean-throttle/cpu.json"
            cache.parent.mkdir()
            for text in ("", "{", "null", "[]", '{"at": "bad"}', '{"at": 0, "sample": null, "percent": 95}'):
                cache.write_text(text)
                self.assertEqual(gate.cpu_usage(), 0)
