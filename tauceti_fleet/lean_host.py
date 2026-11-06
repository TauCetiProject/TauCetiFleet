"""Coordinate compiler starts over serial ports; no network API or guest commands."""

import json
import math
import os
import re
import signal
import socket
import time
from pathlib import Path

from . import ci_priority, lean_pressure, lean_statistics
from .store import FleetError, atomic, read, state_dir


def slice_cpu(name):
    try:
        relative = next(
            line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")
        )
        group = Path("/sys/fs/cgroup") / relative.lstrip("/")
        parent = next(p for p in (group, *group.parents) if p.name == "tauceti-fleet.slice")
        stats = dict(
            line.split() for line in (parent / ("tauceti-fleet-" + name + ".slice/cpu.stat")).read_text().splitlines()
        )
        return int(stats["usage_usec"])
    except (OSError, ValueError, StopIteration, KeyError):
        return None


def worker_cpu():
    """Low-priority workers yield to CI; their CPU use must not block CI starts."""
    return slice_cpu("workers")


class Publisher:
    def __init__(self, *, collect=True):
        self.clients = {}
        self.previous = lean_pressure.cpu_sample()
        self.previous_worker = worker_cpu()
        self.cpu_baseline = None
        self.pending = {}
        self.buffers = {}
        self.granted = {}
        self.granted_at = {}
        self.recent = []
        self.turn = 0
        try:
            saved = read(state_dir() / "lean-statistics.json", {})
            if not isinstance(saved, dict):
                saved = {}
        except FleetError:
            saved = {}
        now = time.time()

        def timestamp(value):
            return type(value) in (int, float) and math.isfinite(value) and 0 < value <= now + 5

        since = saved.get("admission_seen_since")
        self.admission_seen_since = since if timestamp(since) else now
        admissions = saved.get("last_admission_seen", {})
        admissions = admissions if isinstance(admissions, dict) else {}
        self.last_admission_seen = {
            k: admissions.get(k) if timestamp(admissions.get(k)) else None for k in ("workers", "ci")
        }
        self.statistics = None
        self.remote_statistics = (
            {p: v for p, v in saved.get("sources", {}).items() if isinstance(p, str) and lean_statistics.valid(v)}
            if isinstance(saved.get("sources", {}), dict)
            else {}
        )
        self.remote_statistics_times = (
            saved.get("source_times", {}) if isinstance(saved.get("source_times"), dict) else {}
        )
        self.last_workers = (
            saved.get("workers") if lean_statistics.valid(saved.get("workers")) else lean_statistics.empty()
        )
        if saved.get("restart_loss_accounting") != 2:
            # Earlier reports could not distinguish a cancelled wait from an
            # observation interrupted by our own restart. Preserve it as unknown.
            self.last_workers = dict(
                self.last_workers, lost=self.last_workers["lost"] + self.last_workers["abandoned"], abandoned=0
            )
        self.archived_statistics = (
            saved.get("archived") if lean_statistics.valid(saved.get("archived")) else lean_statistics.empty()
        )
        self.archived_keys = (
            [x for x in saved.get("archived_keys", [])[-1024:] if isinstance(x, str) and len(x) < 512]
            if isinstance(saved.get("archived_keys", []), list)
            else []
        )
        self.legacy = saved.get("legacy", {}) if isinstance(saved.get("legacy"), dict) else {}
        try:
            if collect:
                self.statistics = lean_statistics.Collector(
                    Path("/run/user") / str(os.getuid()) / "tauceti-lean-statistics.sock", initial=self.last_workers
                )
                self.statistics.listen()
        except OSError:
            self.statistics = None

    def close(self):
        if self.statistics:
            try:
                self.statistics.close()
            except (OSError, KeyError, ValueError):
                pass
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def step(self):
        if self.statistics:
            try:
                self.statistics.poll()
            except (OSError, KeyError, ValueError):
                try:
                    self.statistics.close()
                except (OSError, KeyError, ValueError):
                    pass
                self.statistics = None
        current = lean_pressure.cpu_sample()
        workers = worker_cpu()
        elapsed = max(1, current[0] - self.previous[0])
        worker_percent = (
            max(0, workers - self.previous_worker) * os.sysconf("SC_CLK_TCK") / 10000 / elapsed
            if workers is not None and self.previous_worker is not None
            else 0
        )
        total, available = lean_pressure.memory()
        value = dict(
            version=1,
            at=time.time(),
            cpu_percent=max(0, lean_pressure.cpu_percent(self.previous, current) - worker_percent),
            memory_total=total,
            memory_available=available,
            statistics=1,
        )
        self.previous = current
        self.previous_worker = workers
        paths = set((state_dir() / "vms").glob("*/pressure.sock"))
        try:
            status = read(state_dir() / "status.json", {})
            if not isinstance(status, dict):
                status = {"unavailable": True}
        except FleetError:
            status = {"unavailable": True}
        owned = (
            {str(Path(v["directory"]) / "pressure.sock") for v in status["ci_vms"] if v.get("directory")}
            if isinstance(status.get("ci_vms"), list)
            else None
        )
        staging_paths = {Path(v["directory"]) / "pressure.sock" for v in ci_priority.receipts(status)
                         if v.get("directory") and ci_priority.staging(v)}
        for path in list(self.remote_statistics):
            if Path(path) not in paths and (path not in owned if owned is not None else not status):
                self.archive_statistics(path)
        for path in set(self.clients) - paths:
            self.clients.pop(path).close()
            self.pending.pop(path, None)
            self.buffers.pop(path, None)
            self.granted.pop(path, None)
            self.granted_at.pop(path, None)
            if owned is not None and str(path) not in owned:
                self.archive_statistics(str(path))
        ordered = sorted(paths)
        if ordered:
            self.turn = (self.turn + 1) % len(ordered)
            ordered = ordered[self.turn :] + ordered[: self.turn]
        ordered.sort(key=lambda path: path not in staging_paths)
        now = time.monotonic()
        # A brief ramp allowance covers compilers which were just admitted but
        # have not allocated yet. It reserves no physical CPUs or guest RAM.
        self.recent = [at for at in self.recent if now - at < 0.25]
        for path in ordered:
            try:
                if path not in self.clients:
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.clients[path] = client
                    client.settimeout(0.001)
                    client.connect(str(path))
                try:
                    chunk = self.clients[path].recv(4096)
                    if not chunk:
                        raise OSError("guest disconnected")
                    buffer = self.buffers.get(path, b"") + chunk
                    if len(buffer) > 8192:
                        raise OSError("oversized admission request")
                    while b"\n" in buffer:
                        request, buffer = buffer.split(b"\n", 1)
                        if request.startswith(b"S"):
                            self.observe_statistics(str(path), request[1:])
                            continue
                        fields = request.split(b":")
                        if (
                            len(fields) not in (1, 2)
                            or any(not f.isdigit() for f in fields)
                            or len(fields[0]) > 18
                            or len(fields[-1]) > 18
                        ):
                            raise OSError("invalid admission request")
                        self.pending[path] = (int(fields[0]), min(256, int(fields[1])) if len(fields) == 2 else 1)
                    self.buffers[path] = buffer
                except TimeoutError:
                    pass
                request, waiting = self.pending.get(path, (0, 0))
                staging = path in staging_paths
                headroom = ci_priority.headroom(total, staging=staging)
                credits = max(0, math.ceil((available - headroom) / lean_pressure.WORST_FILE_RSS_BYTES) - len(self.recent))
                if not staging and value["cpu_percent"] > lean_pressure.CPU_LIMIT_PERCENT:
                    credits = 0
                unacknowledged = request != self.granted.get(path) or now - self.granted_at.get(path, 0) >= 2
                permit = request if credits and request and unacknowledged else 0
                remaining = sum(bool(self.pending.get(p, (0, 0))[0]) for p in ordered[ordered.index(path) :]
                                if (p in staging_paths) == staging)
                starts = min(waiting, math.ceil(credits / max(1, remaining))) if permit else 0
                frame = (json.dumps(dict(ci_priority.frame(value, staging=staging), permit=permit, starts=starts), allow_nan=False) + "\n").encode()
                self.clients[path].sendall(frame)
                if permit:
                    self.granted[path] = permit
                    self.granted_at[path] = now
                    self.recent.extend([now] * starts)
            except OSError:
                # A slow or hostile guest cannot stall the other guests. Close
                # after a partial frame too; the receiver discards that frame.
                client = self.clients.pop(path, None)
                if client:
                    client.close()
                self.buffers.pop(path, None)
                self.pending.pop(path, None)
                self.granted.pop(path, None)
                self.granted_at.pop(path, None)
        if time.time() - getattr(self, "reported_at", 0) >= 1:
            atomic(
                state_dir() / "lean-host-pressure.json",
                dict(value, guests=len(self.clients), waiting_guests=sum(bool(n) for n, _ in self.pending.values()),
                     staging_priority=dict(guests=sum(path in staging_paths for path in self.clients),
                                           cpu_sleeps=False, headroom_gib=ci_priority.headroom(total, staging=True) / 1024**3,
                                           normal_headroom_gib=ci_priority.headroom(total) / 1024**3)),
            )
            self.reported_at = time.time()
            workers = (
                self.statistics.snapshot()
                if self.statistics
                else dict(self.last_workers, waiting_now=0, running_now=0, oldest_wait=0.0)
            )
            self.observe_admissions("workers", self.last_workers, workers)
            self.last_workers = workers
            if time.time() - getattr(self, "legacy_at", 0) >= 5:
                previous_stats = self.legacy
                self.legacy = legacy_compilers(
                    {v["pid"] for v in self.statistics.clients.values()} if self.statistics else set()
                )
                previous = getattr(self, "legacy_waiters", {})
                self.legacy_waiters = {p: previous.get(p, time.monotonic()) for p in self.legacy.pop("_pids", [])}
                self.legacy["oldest_observed"] = max(
                    (time.monotonic() - t for t in self.legacy_waiters.values()), default=0.0
                )
                elapsed = time.time() - previous_stats.get("sampled_at", 0)
                elapsed = elapsed if 0 <= elapsed <= 10 else 0
                self.legacy["sampled_wait_seconds"] = (
                    previous_stats.get("sampled_wait_seconds", 0.0) + previous_stats.get("waiting", 0) * elapsed
                )
                self.legacy["sampled_run_seconds"] = (
                    previous_stats.get("sampled_run_seconds", 0.0) + previous_stats.get("running", 0) * elapsed
                )
                self.legacy["sampled_since"] = previous_stats.get("sampled_since", time.time())
                self.legacy["sampled_at"] = time.time()
                self.legacy_at = time.time()
            fresh = {
                path
                for path in self.remote_statistics
                if Path(path) in self.clients
                and type(self.remote_statistics_times.get(path)) in (int, float)
                and 0 <= time.time() - self.remote_statistics_times[path] <= 10
            }
            ci_rows = [
                v if p in fresh else dict(v, waiting_now=0, running_now=0, oldest_wait=0.0)
                for p, v in self.remote_statistics.items()
            ]
            value = dict(
                version=1,
                restart_loss_accounting=2,
                admission_seen_since=self.admission_seen_since,
                last_admission_seen=self.last_admission_seen,
                cpu_usage=self.cpu_usage(),
                at=time.time(),
                workers=workers,
                ci=lean_statistics.merge([self.archived_statistics, *ci_rows]),
                sources=self.remote_statistics,
                archived=self.archived_statistics,
                archived_keys=self.archived_keys,
                source_times=self.remote_statistics_times,
                telemetry_guests=len(fresh),
                connected_guests=len(self.clients),
                worker_collector_available=self.statistics is not None,
                wait_bucket_upper_seconds=[*lean_statistics.BINS, None],
                legacy=getattr(self, "legacy", {}),
            )
            atomic(state_dir() / "lean-statistics.json", value)
            report = {k: v for k, v in value.items() if k not in ("sources", "source_times", "archived", "archived_keys")}
            # Preserve VM identity without exposing the private serial-socket path.
            report["instances"] = {
                Path(path).parent.name: dict(
                    observed_at=self.remote_statistics_times.get(path),
                    **{key: stats[key] for key in ("waiting_now", "running_now", "completed", "oldest_wait", "wait_seconds", "run_seconds", "lost")},
                )
                for path, stats in self.remote_statistics.items()
                if re.fullmatch(r"[a-f0-9]{16}", Path(path).parent.name) and lean_statistics.valid(stats)
            }
            atomic(state_dir() / "lean-statistics-report.json", report)

    def cpu_usage(self):
        now = time.monotonic()
        counters = {name: slice_cpu(name) for name in ("workers", "ci")}
        previous = self.cpu_baseline
        self.cpu_baseline = (now, counters)
        result = dict(at=time.time())
        for name, value in counters.items():
            before = previous[1].get(name) if previous else None
            elapsed = now - previous[0] if previous else 0
            result[name] = ((value - before) / 1e6 / elapsed
                            if value is not None and before is not None and value >= before and 0 < elapsed <= 10
                            else None)
        return result

    def observe_admissions(self, kind, prior, sample):
        """Track confirmed counter increases, not permits or merged totals.

        Guest clocks are independent, so report the host observation time. A
        first snapshot from an older collector seeds a baseline, not activity.
        """
        same_session = prior.get("since") == sample["since"]
        previous = prior.get("admitted", 0) if same_session else 0
        if sample["admitted"] > previous and (same_session or sample["since"] >= self.admission_seen_since):
            self.last_admission_seen[kind] = time.time()

    def archive_statistics(self, path):
        previous = self.remote_statistics.pop(path, None)
        self.remote_statistics_times.pop(path, None)
        if previous:
            key = f"{path}@{float(previous['since']):.6f}"
            if key in self.archived_keys:
                return
            previous = dict(previous, waiting_now=0, running_now=0, oldest_wait=0.0)
            self.archived_statistics = lean_statistics.merge([self.archived_statistics, previous])
            self.archived_keys = [*self.archived_keys, key][-1024:]

    def observe_statistics(self, path, payload):
        # Telemetry is untrusted and observational. Bound JSON nesting before
        # decoding, and isolate every decoder/validation error from admission.
        try:
            if payload[:1] != b"{" or len(payload) > 4096 or payload.count(b"[") > 2 or payload.count(b"{") > 2:
                return
            sample = json.loads(payload)
            if not lean_statistics.valid(sample):
                return
            if f"{path}@{float(sample['since']):.6f}" in self.archived_keys:
                return
            prior = self.remote_statistics.get(path, {})
            self.observe_admissions("ci", prior, sample)
            if prior.get("since") != sample["since"]:
                self.archive_statistics(path)
            self.remote_statistics[path] = sample
            self.remote_statistics_times[path] = time.time()
        except Exception:
            pass


def legacy_compilers(known):
    counts = dict(waiting=0, running=0, _pids=[])
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) in known:
            continue
        try:
            if p.stat().st_uid != os.getuid():
                continue
            if "tauceti-fleet-workers.slice" not in (p / "cgroup").read_text():
                continue
            name = (p / "comm").read_text(errors="replace").strip()
            if name == "lean":
                with (p / "cmdline").open("rb") as stream:
                    args = stream.read(2048).split(b"\0")
                if b"--server" not in args:
                    counts["running"] += 1
                continue
            if not name.startswith("python"):
                continue
            with (p / "cmdline").open("rb") as stream:
                args = stream.read(2048).split(b"\0", 3)
            if (
                len(args) > 1
                and b"python" in args[0]
                and Path(os.fsdecode(args[1])).name in ("lean", "lean-real", "tauceti-lean")
                and (p / "wchan").read_text().strip() == "hrtimer_nanosleep"
            ):
                counts["waiting"] += 1
                counts["_pids"].append(int(p.name))
        except OSError:
            pass
    return counts


def main():
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    publisher = Publisher()
    try:
        while not stopping:
            time.sleep(0.1)
            try:
                publisher.step()
            except (OSError, ValueError, KeyError) as error:
                # Old snapshots expire at the receiver. Never keep advertising
                # spare capacity after host measurement fails.
                print(f"tauceti-host-pressure: {error}", flush=True)
    finally:
        publisher.close()


if __name__ == "__main__":
    main()
