import contextlib
import io
import json
import math
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import hostos, lean_host, lean_pressure, store
from tauceti_fleet import lean_statistics as stats

hostos.MACOS = False


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
        trace = lean_pressure.Trace.__new__(lean_pressure.Trace)
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

    def test_restart_retains_totals_and_marks_all_unclosed_observations_lost(self):
        row = stats.empty()
        row.update(observed=3, admitted=2, completed=1, waiting_now=1, running_now=1)
        collector = stats.Collector(self.root / "other", initial=row)
        self.addCleanup(collector.close)
        sample = collector.snapshot()
        self.assertEqual(sample["abandoned"], 0)
        self.assertEqual(sample["lost"], 2)
        self.assertEqual((sample["observed"], sample["completed"], sample["waiting_now"]), (3, 1, 0))

    def test_ambiguous_older_closed_waits_are_preserved_as_unknown(self):
        row = stats.empty()
        row.update(observed=3, abandoned=2)
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)):
            store.atomic(self.root / "lean-statistics.json", dict(workers=row))
            publisher = self.publisher()
            self.assertEqual(publisher.last_workers["abandoned"], 0)
            self.assertEqual(publisher.last_workers["lost"], 2)

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

    def publisher(self):
        with patch.object(lean_pressure, "cpu_sample", return_value=(100, 90)):
            publisher = lean_host.Publisher(collect=False)
        self.addCleanup(publisher.close)
        return publisher

    def test_hostile_nested_json_and_decoder_errors_cannot_affect_admission(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)):
            publisher = self.publisher()
            for payload in (b"[" * 2000, b'{"x":' + b"[" * 2000, b"{}" * 2000):
                publisher.observe_statistics("guest", payload)
            with patch.object(lean_host.json, "loads", side_effect=RecursionError("hostile nesting")):
                publisher.observe_statistics("guest", b'{"version":1}')
            row = stats.empty()
            row["observed"] = 1
            publisher.observe_statistics("guest", json.dumps(row).encode())
            self.assertEqual(publisher.remote_statistics["guest"]["observed"], 1)
            self.assertFalse(publisher.pending)

    def test_archived_session_reappearance_is_not_counted_twice(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)):
            publisher = self.publisher()
            row = stats.empty()
            row["observed"] = 3
            publisher.observe_statistics("guest", json.dumps(row).encode())
            publisher.archive_statistics("guest")
            publisher.observe_statistics("guest", json.dumps(row).encode())
            publisher.archive_statistics("guest")
            self.assertEqual(publisher.archived_statistics["observed"], 3)
            row.update(since=row["since"] + 1, observed=2)
            publisher.observe_statistics("guest", json.dumps(row).encode())
            publisher.archive_statistics("guest")
            self.assertEqual(publisher.archived_statistics["observed"], 5)

    def test_unavailable_socket_glob_does_not_archive_an_owned_guest(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)):
            publisher = self.publisher()
            path = str(self.root / "vms/guest/pressure.sock")
            row = stats.empty()
            row["observed"] = 3
            publisher.observe_statistics(path, json.dumps(row).encode())
            store.atomic(self.root / "status.json", dict(ci_vms=[dict(directory=str(self.root / "vms/guest"))]))
            with (
                patch.object(lean_pressure, "cpu_sample", return_value=(200, 180)),
                patch.object(lean_pressure, "memory", return_value=(112 * 1024**3, 50 * 1024**3)),
                patch.object(lean_host, "legacy_compilers", return_value=dict(waiting=0, running=0, _pids=[])),
            ):
                publisher.step()
            self.assertIn(path, publisher.remote_statistics)
            self.assertEqual(publisher.archived_statistics["observed"], 0)

    def test_emfile_backs_off_accepts_without_losing_existing_observations(self):
        import errno

        self.collector.listen()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(str(self.collector.path))
        with patch.object(socket.socket, "accept", side_effect=OSError(errno.EMFILE, "full")):
            self.collector.poll()
        self.assertFalse(self.collector.listener_registered)
        self.clock += 2
        self.collector.poll()
        self.assertTrue(self.collector.listener_registered)
        self.assertEqual(self.collector.snapshot()["observed"], 1)

    def test_telemetry_reserves_descriptors_when_hard_limit_cannot_grow(self):
        import resource

        with patch.object(resource, "getrlimit", return_value=(1024, 1024)):
            collector = stats.Collector(self.root / "limited")
        self.addCleanup(collector.close)
        self.assertLessEqual(collector.max_clients, 384)

    def test_guest_admission_handles_descriptors_above_select_limit(self):
        import fcntl

        from tauceti_fleet import lean_guest

        try:
            a, b = socket.socketpair()
            self.addCleanup(a.close)
            self.addCleanup(b.close)
            high = fcntl.fcntl(a.fileno(), fcntl.F_DUPFD, 1100)
        except OSError:
            self.skipTest("hard descriptor limit below test fixture")
        client = socket.socket(fileno=high)
        self.addCleanup(client.close)
        port, peer = socket.socketpair()
        self.addCleanup(port.close)
        self.addCleanup(peer.close)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.root / "admission"))
        listener.listen()
        broker = lean_guest.Broker()
        broker.clients.append(client)
        peer.sendall(b"{}\n")
        peer.shutdown(socket.SHUT_WR)
        broker.run(port.fileno(), listener)

    def test_missing_permissions_on_optional_observer_and_lsp_are_harmless(self):
        from unittest.mock import Mock

        required = Mock()
        required.exists.side_effect = PermissionError("not accessible")
        with patch.object(lean_pressure, "HOST_REQUIRED", required):
            self.assertIsNone(lean_pressure.Trace().client)
        with (
            patch.object(lean_pressure, "real_lean", return_value=Path("/real/lean")),
            patch.object(lean_pressure, "Trace") as trace,
            patch.object(lean_pressure, "wait_for_capacity") as wait,
            patch.object(lean_pressure, "exec_lean", side_effect=SystemExit(0)),
        ):
            with self.assertRaises(SystemExit):
                lean_pressure.main(["--server"])
            trace.assert_not_called()
            wait.assert_called_once()

    def test_last_admission_tracks_source_deltas_and_ignores_replays_and_archives(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)), patch.object(lean_host.time, "time", return_value=1000) as clock:
            publisher = self.publisher()
            row = stats.empty()
            row.update(since=900, observed=10, admitted=5)
            publisher.observe_statistics("guest", json.dumps(row).encode())
            self.assertIsNone(publisher.last_admission_seen["ci"])
            row.update(admitted=6)
            clock.return_value = 1010
            publisher.observe_statistics("guest", json.dumps(row).encode())
            self.assertEqual(publisher.last_admission_seen["ci"], 1010)
            clock.return_value = 1020
            publisher.observe_statistics("guest", json.dumps(row).encode())
            publisher.archive_statistics("guest")
            publisher.observe_statistics("guest", json.dumps(row).encode())
            self.assertEqual(publisher.last_admission_seen["ci"], 1010)

    def test_new_collectors_and_worker_admissions_are_observed_without_guest_changes(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)), patch.object(lean_host.time, "time", return_value=1000) as clock:
            publisher = self.publisher()
            row = stats.empty()
            row.update(since=1001, observed=1, admitted=1)
            clock.return_value = 1002
            publisher.observe_statistics("guest", json.dumps(row).encode())
            self.assertEqual(publisher.last_admission_seen["ci"], 1002)
            publisher.observe_admissions("workers", dict(row, admitted=0), row)
            self.assertEqual(publisher.last_admission_seen["workers"], 1002)

    def test_last_admission_persists_across_observer_restart_without_fake_activity(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)), patch.object(lean_host.time, "time", return_value=1000):
            row = stats.empty()
            row.update(since=800, observed=10, admitted=5)
            store.atomic(self.root / "lean-statistics.json", dict(admission_seen_since=800,
                last_admission_seen=dict(workers=950, ci=970), sources={"guest":row}, workers=row))
            publisher = self.publisher()
            publisher.observe_statistics("guest", json.dumps(row).encode())
            publisher.observe_admissions("workers", publisher.last_workers, row)
            self.assertEqual(publisher.last_admission_seen, dict(workers=950, ci=970))
            self.assertEqual(publisher.admission_seen_since, 800)

    def test_report_shows_last_admission_after_counts_and_marks_unknown_history(self):
        row = stats.empty()
        row.update(observed=1)
        value = dict(at=1000, workers=row, ci=row, admission_seen_since=880,
                     last_admission_seen=dict(workers=985, ci=None))
        output = io.StringIO()
        with patch.object(stats.time, "time", return_value=1000), contextlib.redirect_stdout(output):
            stats.display(value)
        lines = output.getvalue().splitlines()
        position = next(i for i, line in enumerate(lines) if "Waiting / running now" in line)
        self.assertIn("Last admission seen", lines[position + 1])
        self.assertIn("15s ago", lines[position + 1])
        self.assertIn("none in 2m", lines[position + 1])
        output = io.StringIO()
        with patch.object(stats.time, "time", return_value=1020), contextlib.redirect_stdout(output):
            stats.display(value)
        last = next(line for line in output.getvalue().splitlines() if "Last admission seen" in line)
        self.assertEqual(last.count("unknown"), 2)

    def test_cpu_report_counts_native_work_even_when_instrumented_lean_is_idle(self):
        with patch.dict(os.environ,TAUCETI_FLEET_STATE=str(self.root)):
            publisher=self.publisher()
            with patch.object(lean_host.time,'monotonic',side_effect=[100,103,104]), patch.object(lean_host,'slice_cpu',side_effect=[10_000_000,20_000_000,37_000_000,242_000_000,36_000_000,None]):
                self.assertIsNone(publisher.cpu_usage()['ci'])
                usage=publisher.cpu_usage()
                self.assertEqual(usage['workers'],9)
                self.assertEqual(usage['ci'],74)
                reset=publisher.cpu_usage()
                self.assertIsNone(reset['workers'])
                self.assertIsNone(reset['ci'])
            row=stats.empty();row.update(observed=1,admitted=1,completed=1)
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                stats.display(dict(at=time.time(),workers=row,ci=row,cpu_usage=usage))
            line=next(line for line in output.getvalue().splitlines() if 'CPU active (cores)' in line)
            self.assertIn('9.0',line);self.assertIn('74.0',line)
            self.assertIn('CPU includes all worker/guest processes',output.getvalue())

    def test_stale_cpu_samples_are_not_reported_as_current_activity(self):
        row=stats.empty();row.update(observed=1)
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            stats.display(dict(at=time.time(),workers=row,ci=row,cpu_usage=dict(at=time.time()-30,workers=10,ci=74)))
        line=next(line for line in output.getvalue().splitlines() if 'CPU active (cores)' in line)
        self.assertEqual(line.count('unknown'),2)

    def test_stale_reports_do_not_present_live_counts_as_current(self):
        row = stats.empty()
        row.update(observed=1, waiting_now=1, waited=1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stats.display(dict(at=time.time() - 30, workers=row, ci=stats.empty()))
        self.assertIn("live counts unavailable", out.getvalue())
        self.assertIn("unknown", out.getvalue())
        self.assertEqual(stats.duration(86400), "1d")


if __name__ == "__main__":
    unittest.main()
