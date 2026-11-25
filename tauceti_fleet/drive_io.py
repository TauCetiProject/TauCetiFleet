"""Per-drive write rate and latency, once a minute, from `/proc/diskstats` (no root needed).

Latency is the change in milliseconds spent writing over the change in writes completed:
the mean time one write request took over the interval, queueing included. An md array
reports its own figure (the member drives' plus md's), and its rates count each write once.
"""

import json
import os
import re
import time
from pathlib import Path

from .store import state_dir

INTERVAL = 60
# About 0.7 kB a minute for three devices: halving at 16 MiB keeps at least a week.
HISTORY_BYTES = 16 * 1024**2
# Whole drives and arrays; partitions, loop, ram and zram devices are omitted.
DEVICE = re.compile(r"(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|md\d+|dm-\d+)")


def snapshot(path="/proc/diskstats"):
    rows = {}
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if len(fields) < 14 or not DEVICE.fullmatch(fields[2]):
            continue
        reads, read_sectors, read_ms = int(fields[3]), int(fields[5]), int(fields[6])
        writes, write_sectors, write_ms = int(fields[7]), int(fields[9]), int(fields[10])
        rows[fields[2]] = dict(reads=reads, read_sectors=read_sectors, read_ms=read_ms,
                               writes=writes, write_sectors=write_sectors, write_ms=write_ms,
                               in_flight=int(fields[11]), busy_ms=int(fields[12]))
    return rows


def rates(previous, current, seconds):
    result = []
    for name, now in sorted(current.items()):
        before = previous.get(name)
        if before is None or seconds <= 0 or any(now[key] < before[key] for key in now if key != "in_flight"):
            continue  # New device or a counter reset.
        writes = now["writes"] - before["writes"]
        reads = now["reads"] - before["reads"]
        result.append(dict(
            device=name,
            writes_per_second=round(writes / seconds, 1),
            write_mib_per_second=round((now["write_sectors"] - before["write_sectors"]) * 512 / seconds / 2**20, 1),
            write_latency_ms=None if not writes else round((now["write_ms"] - before["write_ms"]) / writes, 3),
            reads_per_second=round(reads / seconds, 1),
            read_mib_per_second=round((now["read_sectors"] - before["read_sectors"]) * 512 / seconds / 2**20, 1),
            read_latency_ms=None if not reads else round((now["read_ms"] - before["read_ms"]) / reads, 3),
            utilization=round(min(1.0, (now["busy_ms"] - before["busy_ms"]) / 1000 / seconds), 3),
            in_flight=now["in_flight"]))
    return result


class DriveIO:
    """Sampled by the controller loop; publishes the latest minute and keeps a bounded history."""

    def __init__(self, path="/proc/diskstats", history=None):
        self.path = path
        self.history = Path(history) if history else state_dir() / "drive-io.jsonl"
        self.previous = None
        self.previous_at = None
        self.latest = None

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        if self.previous_at is not None and now - self.previous_at < INTERVAL:
            return self.latest
        try:
            current = snapshot(self.path)
        except (OSError, ValueError):
            return self.latest
        if self.previous is not None:
            seconds = now - self.previous_at
            self.latest = dict(at=time.time(), interval_seconds=round(seconds, 1),
                               drives=rates(self.previous, current, seconds))
            self.record(self.latest)
        self.previous, self.previous_at = current, now
        return self.latest

    def record(self, value):
        try:
            self.history.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.history.exists() and self.history.stat().st_size > HISTORY_BYTES:
                # Keep the newer half.
                lines = self.history.read_bytes().splitlines(keepends=True)
                temporary = self.history.with_suffix(".next")
                temporary.write_bytes(b"".join(lines[len(lines) // 2:]))
                os.replace(temporary, self.history)
            with self.history.open("a") as stream:
                stream.write(json.dumps(value, sort_keys=True) + "\n")
        except OSError:
            pass  # Telemetry never stops the controller.
