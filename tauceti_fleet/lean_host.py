"""Coordinate compiler starts over serial ports; no network API or guest commands."""

import json
import math
import os
import signal
import socket
import time
from pathlib import Path

from . import lean_pressure
from .store import atomic, state_dir


def worker_cpu():
    """Low-priority workers yield to CI; their CPU use must not block CI starts."""
    try:
        relative = next(l[3:] for l in Path('/proc/self/cgroup').read_text().splitlines() if l.startswith('0::'))
        group = Path('/sys/fs/cgroup') / relative.lstrip('/')
        parent = next(p for p in (group, *group.parents) if p.name == 'tauceti-fleet.slice')
        stats = dict(l.split() for l in (parent / 'tauceti-fleet-workers.slice/cpu.stat').read_text().splitlines())
        return int(stats['usage_usec'])
    except (OSError, ValueError, StopIteration, KeyError):
        return None


class Publisher:
    def __init__(self):
        self.clients = {}
        self.previous = lean_pressure.cpu_sample()
        self.previous_worker = worker_cpu()
        self.pending = {}
        self.buffers = {}
        self.granted = {}
        self.granted_at = {}
        self.recent = []
        self.turn = 0

    def close(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def step(self):
        current = lean_pressure.cpu_sample()
        workers = worker_cpu()
        elapsed = max(1, current[0] - self.previous[0])
        worker_percent = (max(0, workers - self.previous_worker) * os.sysconf('SC_CLK_TCK') / 10000 / elapsed
                          if workers is not None and self.previous_worker is not None else 0)
        total, available = lean_pressure.memory()
        value = dict(version=1, at=time.time(), cpu_percent=max(0, lean_pressure.cpu_percent(self.previous, current) - worker_percent),
                     memory_total=total, memory_available=available)
        self.previous = current
        self.previous_worker = workers
        paths = set((state_dir() / "vms").glob("*/pressure.sock"))
        for path in set(self.clients) - paths:
            self.clients.pop(path).close()
            self.pending.pop(path, None)
            self.buffers.pop(path, None)
            self.granted.pop(path, None)
            self.granted_at.pop(path, None)
        ordered = sorted(paths)
        if ordered:
            self.turn = (self.turn + 1) % len(ordered)
            ordered = ordered[self.turn:] + ordered[:self.turn]
        now = time.monotonic()
        # A brief ramp allowance covers compilers which were just admitted but
        # have not allocated yet. It reserves no physical CPUs or guest RAM.
        self.recent = [at for at in self.recent if now - at < 0.25]
        headroom = total * (1 - lean_pressure.memory_limit(total) / 100)
        credits = max(0, math.ceil((available - headroom) / lean_pressure.WORST_FILE_RSS_BYTES) - len(self.recent))
        if value['cpu_percent'] > lean_pressure.CPU_LIMIT_PERCENT:
            credits = 0
        for path in ordered:
            try:
                if path not in self.clients:
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.clients[path] = client
                    client.settimeout(0.001)
                    client.connect(str(path))
                try:
                    chunk = self.clients[path].recv(256)
                    if not chunk:
                        raise OSError('guest disconnected')
                    buffer = self.buffers.get(path, b'') + chunk
                    if len(buffer) > 512:
                        raise OSError('oversized admission request')
                    while b'\n' in buffer:
                        request, buffer = buffer.split(b'\n', 1)
                        fields = request.split(b':')
                        if (len(fields) not in (1, 2) or any(not f.isdigit() for f in fields)
                                or len(fields[0]) > 18 or len(fields[-1]) > 18):
                            raise OSError('invalid admission request')
                        self.pending[path] = (int(fields[0]), min(256, int(fields[1])) if len(fields) == 2 else 1)
                    self.buffers[path] = buffer
                except TimeoutError:
                    pass
                request, waiting = self.pending.get(path, (0, 0))
                unacknowledged = request != self.granted.get(path) or now - self.granted_at.get(path, 0) >= 2
                permit = request if credits and request and unacknowledged else 0
                remaining = sum(bool(self.pending.get(p, (0, 0))[0]) for p in ordered[ordered.index(path):])
                starts = min(waiting, math.ceil(credits / max(1, remaining))) if permit else 0
                frame = (json.dumps(dict(value, permit=permit, starts=starts), allow_nan=False) + '\n').encode()
                self.clients[path].sendall(frame)
                if permit:
                    self.granted[path] = permit
                    self.granted_at[path] = now
                    self.recent.extend([now] * starts)
                    credits -= starts
            except OSError:
                # A slow or hostile guest cannot stall the other guests. Close
                # after a partial frame too; the receiver discards that frame.
                self.clients.pop(path).close()
                self.buffers.pop(path, None)
                self.pending.pop(path, None)
                self.granted.pop(path, None)
                self.granted_at.pop(path, None)
        if time.time() - getattr(self, 'reported_at', 0) >= 1:
            atomic(state_dir() / "lean-host-pressure.json", dict(value, guests=len(self.clients),
                   waiting_guests=sum(bool(n) for n, _ in self.pending.values())))
            self.reported_at = time.time()


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
