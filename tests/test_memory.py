"""Distinguish cold cache from actual working-set and reclamation pressure."""

import unittest

from tauceti_fleet.capabilities import GIB, memory_accounting, memory_pressure


class MemoryTests(unittest.TestCase):
    def measured(self, **changes):
        stats = dict(
            anon=2 * GIB, file=33 * GIB, kernel=2 * GIB, inactive_file=27 * GIB, file_dirty=GIB, file_writeback=0
        )
        stats.update(changes)
        return memory_accounting(37 * GIB, 37 * GIB, 47 * GIB, stats, dict(some=0.1, full=0), dict(oom_kill=0))

    def test_cache_at_soft_limit_does_not_pause_workers(self):
        memory = self.measured()
        self.assertEqual(memory["working_set"], 11 * GIB)
        self.assertEqual(memory_pressure(memory, dict(memory_available=80 * GIB, memory_total=125 * GIB)), [])

    def test_dirty_data_is_retained_in_working_set_estimate(self):
        memory = self.measured(file_dirty=20 * GIB, file_writeback=10 * GIB)
        self.assertEqual(memory["cold_clean"], 0)
        self.assertEqual(memory["working_set"], memory["charged"])

    def test_active_cache_is_not_subtracted(self):
        memory = self.measured(inactive_file=0)
        self.assertEqual(memory["working_set"], 37 * GIB)

    def test_shared_memory_is_not_assumed_to_be_cold_file_cache(self):
        memory = self.measured(shmem=30 * GIB)
        self.assertEqual(memory["cold_clean"], 2 * GIB)

    def test_reclaim_stalls_do_not_pause_network_bound_work(self):
        memory = self.measured()
        memory.update(stall_full=60, stall_some=90)
        reasons = memory_pressure(memory, dict(memory_available=80 * GIB, memory_total=125 * GIB))
        self.assertEqual(reasons, [])

    def test_actual_working_set_near_hard_limit_still_pauses(self):
        memory = self.measured()
        memory["working_set"] = 45 * GIB
        self.assertEqual(
            memory_pressure(memory, dict(memory_available=80 * GIB, memory_total=125 * GIB)),
            ["worker working set near hard memory limit"],
        )

    def test_host_headroom_is_an_independent_limit(self):
        memory = self.measured()
        reasons = memory_pressure(memory, dict(memory_available=5 * GIB, memory_total=125 * GIB))
        self.assertEqual(reasons, ["host memory headroom low"])

    def test_real_memory_pressure_has_hysteresis_without_stall_gating(self):
        memory = self.measured()
        caps = dict(memory_available=80 * GIB, memory_total=125 * GIB)
        memory["working_set"] = 41 * GIB  # Between resume and pause thresholds.
        self.assertEqual(memory_pressure(memory, caps), [])
        self.assertEqual(memory_pressure(memory, caps, recovering=True), ["worker working set near hard memory limit"])
        memory["working_set"] = 39 * GIB
        self.assertEqual(memory_pressure(memory, caps, recovering=True), [])

    def test_swapped_memory_is_counted_in_working_set(self):
        original = self.measured()
        stats = dict(
            anon=2 * GIB, file=33 * GIB, kernel=2 * GIB, inactive_file=27 * GIB, file_dirty=GIB, file_writeback=0
        )
        swapped = memory_accounting(37 * GIB, 44 * GIB, 47 * GIB, stats, dict(some=0, full=0), dict(oom_kill=0), GIB)
        self.assertEqual(swapped["working_set"], original["working_set"] + GIB)
        self.assertEqual(swapped["swap"], GIB)
