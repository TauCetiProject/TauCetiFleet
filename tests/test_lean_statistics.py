import contextlib
import io
import math
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import lean_host, lean_pressure, store
from tauceti_fleet import lean_statistics as stats


class CompilerStatistics(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.clock = 100.0
        self.collector = stats.Collector(self.root / "statistics.sock", now=lambda: self.clock)
        self.addCleanup(self.collector.close)

    def connection(self):
        a, b = socket.socketpair()
        self.addCleanup(b.close)
        self.collector.accept(a)
        return a, b, self.collector.clients[a]

    def phase(self, state, phase, at):
        self.clock = at
        self.collector.event(state, f"{phase}:{at}".encode())

    def test_waiting_and_runtime_are_weighted_elapsed_times_with_active_waits(self):
        a, b, s = self.connection()
        self.phase(s, "ram", 100)
        self.clock = 110
        row = self.collector.snapshot()
        self.assertEqual((row["waiting_now"], row["wait_seconds"], row["oldest_wait"]), (1, 10, 10))
        # Snapshot must not advance the actor's event clock: buffered phase
        # transitions can arrive after the most recent report was prepared.
        self.phase(s, "run", 105)
        self.clock = 115
        row = self.collector.snapshot()
        self.assertEqual((row["wait_seconds"], row["run_seconds"]), (5, 10))
        self.assertEqual(row["waited"], 1)
        self.assertEqual(sum(row["waits"]), 1)
        self.assertEqual(stats.percentile(row, 0.95), 5)
        self.collector.finish(a)
        self.assertEqual(self.collector.snapshot()["completed"], 1)

    def test_probe_is_separate_and_quantiles_only_include_admitted_waiters(self):
        a, b, s = self.connection()
        self.phase(s, "probe", 100)
        self.phase(s, "run", 100.5)
        self.clock = 102.5
        self.collector.finish(a)
        row = self.collector.snapshot()
        self.assertEqual(row["probe_seconds"], 0.5)
        self.assertEqual((row["waited"], row["wait_seconds"], row["run_seconds"]), (0, 0, 2))
        self.assertIsNone(stats.percentile(row, 0.5))

    def test_abandoned_wait_is_visible_without_becoming_a_completed_build(self):
        a, b, s = self.connection()
        self.phase(s, "signal", 100)
        self.clock = 130
        self.collector.finish(a)
        row = self.collector.snapshot()
        self.assertEqual((row["abandoned"], row["admitted"], row["completed"]), (1, 0, 0))
        self.assertEqual(row["reasons"]["signal"], 30)
        self.assertEqual(sum(row["waits"]), 0)

    def test_merge_preserves_histograms_and_uses_weighted_ratio(self):
        a = stats.empty()
        b = stats.empty()
        a.update(observed=1, admitted=1, waited=1, wait_seconds=90, run_seconds=10)
        b.update(observed=9, admitted=9, run_seconds=900)
        a["waits"][stats.BINS.index(120)] = 1
        row = stats.merge([a, b])
        self.assertEqual(row["observed"], 10)
        self.assertAlmostEqual(row["wait_seconds"] / (row["wait_seconds"] + row["run_seconds"]), 0.09)
        self.assertEqual(stats.percentile(row, 0.95), 120)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            stats.display(dict(workers=row, ci=stats.empty()))
        self.assertIn("9.0%", output.getvalue())

    def test_invalid_or_unbounded_snapshots_are_rejected(self):
        for row in (
            {},
            dict(stats.empty(), wait_seconds=math.nan),
            dict(stats.empty(), observed=True),
            dict(stats.empty(), waits=[0]),
            dict(stats.empty(), reasons={"ram": 0}),
        ):
            self.assertFalse(stats.valid(row))

    def test_lifetime_socket_survives_real_exec_watchdog_and_silent_compilation(self):
        self.collector.listen()
        helper = self.root / "compiler"
        helper.write_text("#!/bin/sh\nexec timeout 2 sleep 0.08\n")
        helper.chmod(0o755)
        # Inject only the socket path; every other operation is the production
        # Trace/pause/exec path. No Python monitor remains around the compiler.
        code = """import sys
from pathlib import Path
from tauceti_fleet import lean_pressure as p
p.HOST_REQUIRED=Path(sys.argv[1])
original=p.Path
def paths(path):
 if str(path)=='/usr/local/share/tauceti/lean-statistics.sock':return original(sys.argv[2])
 return original(path)
p.Path=paths
p._TRACE=p.Trace()
p.pause(.05,'ram')
p._TRACE.event('run')
p.exec_lean(Path(sys.argv[3]),[])
"""
        required = self.root / "required"
        required.touch()
        process = subprocess.Popen(
            ["python3", "-c", code, str(required), str(self.collector.path), str(helper)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Use real monotonic time for timestamps emitted by the child.
        self.collector.now = time.monotonic
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.collector.poll()
            if process.poll() is not None and self.collector.totals["completed"]:
                break
            time.sleep(0.005)
        out, err = process.communicate(timeout=1)
        self.assertEqual((process.returncode, out, err), (0, b"", b""))
        row = self.collector.snapshot()
        self.assertEqual((row["observed"], row["admitted"], row["completed"]), (1, 1, 1))
        self.assertGreaterEqual(row["wait_seconds"], 0.04)
        self.assertGreaterEqual(row["run_seconds"], 0.07)

    def test_observer_failure_does_not_change_compiler_exit_or_output(self):
        trace = lean_pressure.Trace()
        a, b = socket.socketpair()
        b.close()
        trace.client = a
        trace.event("run")
        self.assertIsNone(trace.client)

    def test_telemetry_clients_are_bounded_and_not_limited_to_select_fd_sets(self):
        import resource

        count = 1050 if resource.getrlimit(resource.RLIMIT_NOFILE)[0] > 2200 else 40
        for _ in range(count):
            self.connection()
        self.collector.poll()
        self.assertEqual(self.collector.snapshot()["observed"], count)

    def test_guest_cumulative_snapshots_are_replaced_then_archived_once(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)):
            with patch.object(lean_pressure, "cpu_sample", return_value=(100, 90)):
                publisher = lean_host.Publisher(collect=False)
            self.addCleanup(publisher.close)
            row = stats.empty()
            row["observed"] = 3
            publisher.remote_statistics["guest"] = row
            publisher.archive_statistics("guest")
            publisher.archive_statistics("guest")
            self.assertEqual(publisher.archived_statistics["observed"], 3)

    def test_restart_retains_totals_and_marks_unclosed_invocations_abandoned(self):
        row = stats.empty()
        row.update(observed=3, admitted=2, completed=1, waiting_now=1, running_now=1)
        collector = stats.Collector(self.root / "other", initial=row)
        self.addCleanup(collector.close)
        sample = collector.snapshot()
        self.assertEqual(sample["abandoned"], 2)
        self.assertEqual((sample["observed"], sample["completed"], sample["waiting_now"]), (3, 1, 0))

    def test_failed_timing_collector_cannot_stop_host_capacity_publication(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)):
            with (
                patch.object(lean_pressure, "cpu_sample", side_effect=[(100, 90), (200, 180)]),
                patch.object(lean_pressure, "memory", return_value=(112 * 1024**3, 50 * 1024**3)),
                patch.object(lean_host, "legacy_compilers", return_value=dict(waiting=0, running=0, _pids=[])),
            ):
                publisher = lean_host.Publisher(collect=False)
                self.addCleanup(publisher.close)
                from unittest.mock import Mock

                publisher.statistics = Mock()
                publisher.statistics.poll.side_effect = OSError("observer failed")
                publisher.step()
            self.assertIsNone(publisher.statistics)
            self.assertEqual(store.read(self.root / "lean-host-pressure.json")["memory_available"], 50 * 1024**3)
            self.assertFalse(store.read(self.root / "lean-statistics-report.json")["worker_collector_available"])


if __name__ == "__main__":
    unittest.main()
