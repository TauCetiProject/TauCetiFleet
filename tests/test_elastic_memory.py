import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import host, capacity, lean_pressure
from tauceti_fleet.capabilities import GIB, memory_pressure


class ElasticMemory(unittest.TestCase):
    config = dict(ci_allowed=True, ci_memory_gib=64)

    def test_low_startup_availability_does_not_cap_future_recovery(self):
        caps = dict(memory_total=125 * GIB, memory_available=16 * GIB)
        initial, maximum = host.worker_memory_limits(self.config, caps, 12 * GIB)
        recovered, same_maximum = host.worker_memory_limits(
            self.config, dict(caps, memory_available=70 * GIB), 12 * GIB
        )
        self.assertEqual(maximum, same_maximum)
        self.assertLess(initial, 20 * GIB)
        self.assertGreater(recovered, 40 * GIB)
        self.assertLessEqual(maximum + GIB + int(125 * GIB * .1), 125 * GIB)

    def test_ci_growth_reclaims_cache_and_release_restores_headroom(self):
        caps = dict(memory_total=125 * GIB, memory_available=70 * GIB)
        full, maximum = host.worker_memory_limits(self.config, caps, 12 * GIB)
        busy, busy_maximum = host.worker_memory_limits(self.config, dict(caps, memory_available=8 * GIB), 12 * GIB)
        self.assertEqual(busy, 13 * GIB)
        self.assertLess(busy, full)
        self.assertEqual(maximum, busy_maximum)
        self.assertEqual(host.worker_memory_limits(self.config, caps, 12 * GIB)[0], full)

    def test_refresh_lowers_only_high_and_opens_nonblocking(self):
        with tempfile.TemporaryDirectory() as directory:
            cg = Path(directory)
            (cg / 'memory.high').write_text(str(43 * GIB))
            (cg / 'memory.max').write_text(str(46 * GIB))
            memory = dict(cgroup=str(cg), pinned=12 * GIB, maximum=46 * GIB)
            real_open = os.open
            with patch.object(host.os, 'open', wraps=real_open) as opened:
                host.refresh_worker_memory(self.config, dict(memory_total=125 * GIB, memory_available=8 * GIB), memory)
            self.assertEqual(memory['high'], 13 * GIB)
            self.assertEqual((cg / 'memory.max').read_text(), str(46 * GIB))
            self.assertTrue(opened.call_args.args[1] & os.O_NONBLOCK)

    def test_workers_yield_before_active_ci_loses_compiler_headroom(self):
        caps = dict(memory_total=125 * GIB, memory_available=18 * GIB)
        ci_total = capacity.shared_memory_bytes(caps)
        ci_floor = ci_total * (1 - lean_pressure.memory_limit(ci_total) / 100)
        active = host.worker_headroom(caps, ci_busy=True)
        self.assertGreater(active, ci_floor)
        self.assertGreater(active, host.worker_headroom(caps, ci_busy=False))
        memory = dict(working_set=2 * GIB, maximum=100 * GIB)
        self.assertEqual(memory_pressure(memory, caps), [])
        self.assertEqual(memory_pressure(memory, caps, reserve=active), ["host memory headroom low"])
        self.assertEqual(memory_pressure(memory, dict(caps, memory_available=30 * GIB), reserve=active), [])

    def test_ci_priority_changes_only_the_opportunistic_worker_soft_budget(self):
        caps = dict(memory_total=125 * GIB, memory_available=30 * GIB)
        idle_high, maximum = host.worker_memory_limits(self.config, caps, 4 * GIB)
        busy_high, busy_maximum = host.worker_memory_limits(self.config, caps, 4 * GIB, ci_busy=True)
        self.assertLess(busy_high, idle_high)
        self.assertEqual(busy_maximum, maximum)
        self.assertGreaterEqual(busy_high, 5 * GIB)
        self.assertEqual(host.worker_memory_limits(self.config, caps, 4 * GIB)[0], idle_high)

    def test_missing_host_measurement_retains_the_live_limit(self):
        memory = dict(high=40 * GIB)
        with patch.object(host.os, 'open', side_effect=AssertionError('must not change live limits')):
            host.refresh_worker_memory(self.config, dict(memory_total=0, memory_available=0), memory)
        self.assertEqual(memory['high'], 40 * GIB)
        self.assertIn('measurement unavailable', memory['allocation_error'])


if __name__ == '__main__':
    unittest.main()
