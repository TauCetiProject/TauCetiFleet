"""Disk watchdog checks remain live while bounded background scans run."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tauceti_fleet import native_inside as inside
from tauceti_fleet import native_runner as native

GIB = 1024**3


class DiskMonitor(unittest.TestCase):
    def test_slow_scan_does_not_block_or_overlap_and_is_killed_at_deadline(self):
        child = Mock(pid=42)
        child.poll.return_value = None
        child.communicate.return_value = ('', '')
        monitor = inside.DiskUsage()
        with patch.object(inside.subprocess, 'Popen', return_value=child) as spawn, patch.object(inside.time, 'monotonic', return_value=0) as clock:
            monitor.poll()
            clock.return_value = 2
            monitor.poll()
            spawn.assert_called_once()
            child.communicate.assert_not_called()
            clock.return_value = 10
            monitor.poll()
            child.kill.assert_called_once()
            self.assertIsNone(monitor.snapshot()['disk_allocated'])
            self.assertIn('timed out', monitor.error)
            clock.return_value = 59
            monitor.poll()
            spawn.assert_called_once()
            clock.return_value = 60
            monitor.poll()
            self.assertEqual(spawn.call_count, 2)
            monitor.close()

    def test_scan_failure_is_unknown_and_retries_are_bounded(self):
        child = Mock(pid=42)
        child.poll.return_value = 1
        child.communicate.return_value = ('', 'permission denied')
        monitor = inside.DiskUsage()
        with patch.object(inside.subprocess, 'Popen', return_value=child), patch.object(inside.time, 'monotonic', return_value=0) as clock:
            for at in (0, 2, 60, 62, 120, 122):
                clock.return_value = at
                monitor.poll()
            self.assertEqual(monitor.failures, inside.DISK_SCAN_FAILURES)
            self.assertIsNone(monitor.allocated)
            self.assertIsNotNone(monitor.error)

    def test_success_reports_one_whole_instance_allocation_then_waits(self):
        child = Mock(pid=42)
        child.poll.return_value = 0
        child.communicate.return_value = ('12345\t/\n', '')
        monitor = inside.DiskUsage()
        with patch.object(inside.subprocess, 'Popen', return_value=child) as spawn, patch.object(inside.time, 'monotonic', return_value=0) as clock:
            monitor.poll()
            clock.return_value = 2
            monitor.poll()
            self.assertEqual(monitor.allocated, 12345)
            self.assertEqual(monitor.failures, 0)
            spawn.assert_called_once()
            command = spawn.call_args.args[0]
            self.assertEqual(command.count('/'), 1)
            self.assertNotIn(str(inside.WORK), command)

    def test_low_space_stops_job_even_while_scan_is_running(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(inside, 'LINK', Path(directory)), patch.object(inside, 'WORK', Path(directory)):
            stop = threading.Event()
            monitor = inside.DiskUsage()
            monitor.child = Mock()
            monitor.child.poll.return_value = None
            monitor.started = 0
            disk = SimpleNamespace(f_bavail=7, f_frsize=GIB, f_favail=10)
            with patch.object(stop, 'wait', return_value=False), patch.object(inside.os, 'statvfs', return_value=disk), patch.object(inside.time, 'monotonic', return_value=2):
                # Exit the loop once its cheap check has marked the local failure.
                stop.wait.side_effect = [False, True]
                inside.measure_progress(stop, monitor)
            self.assertTrue(stop.is_set())
            failure = json.loads((Path(directory)/'native-resource-error.json').read_text())
            self.assertEqual(failure['reason'], 'host disk headroom')
            monitor.child.communicate.assert_not_called()

    def test_budget_and_unavailable_monitor_stop_the_instance(self):
        for allocated, failures, reason in ((57*GIB, 0, 'native disk budget'), (None, 3, 'disk usage monitor unavailable')):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory, patch.object(inside, 'LINK', Path(directory)), patch.object(inside, 'WORK', Path(directory)):
                stop = threading.Event()
                monitor = inside.DiskUsage()
                monitor.allocated, monitor.failures = allocated, failures
                disk = SimpleNamespace(f_bavail=100, f_frsize=GIB, f_favail=100)
                with patch.object(stop, 'wait', side_effect=[False, True]), patch.object(monitor, 'poll'), patch.object(inside.os, 'statvfs', return_value=disk):
                    inside.measure_progress(stop, monitor)
                self.assertTrue(stop.is_set())
                self.assertEqual(json.loads((Path(directory)/'native-resource-error.json').read_text())['reason'], reason)

    def test_native_observation_discounts_only_clean_file_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(native, 'CGROUP_ROOT', Path(directory)):
            group = Path(directory)/'native'
            group.mkdir()
            (group/'memory.current').write_text(str(23*GIB))
            (group/'memory.stat').write_text(f'file {20*GIB}\nshmem {2*GIB}\nfile_dirty {GIB}\nfile_writeback {GIB}\n')
            result = Mock(stdout='ControlGroup=/native\nActiveState=active\nSubState=running\n')
            with patch.object(native, 'run', return_value=result):
                value, inactive = native.usage(dict(unit='native.service'))
            self.assertFalse(inactive)
            self.assertEqual(value['memory_bytes'], 23*GIB)
            self.assertEqual(value['memory_clean_file_bytes'], 16*GIB)
            self.assertEqual(value['memory_working_bytes'], 7*GIB)
