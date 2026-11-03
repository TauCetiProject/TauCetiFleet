"""Broadcast host pressure over guest serial ports; no network API or guest commands."""

import json
import signal
import socket
import time

from . import lean_pressure
from .store import atomic, state_dir


class Publisher:
    def __init__(self):
        self.clients = {}
        self.previous = lean_pressure.cpu_sample()

    def close(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def step(self):
        current = lean_pressure.cpu_sample()
        total, available = lean_pressure.memory()
        value = dict(version=1, at=time.time(), cpu_percent=lean_pressure.cpu_percent(self.previous, current),
                     memory_total=total, memory_available=available)
        self.previous = current
        frame = (json.dumps(value, allow_nan=False) + "\n").encode()
        paths = set((state_dir() / "vms").glob("*/pressure.sock"))
        for path in set(self.clients) - paths:
            self.clients.pop(path).close()
        for path in paths:
            try:
                if path not in self.clients:
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.clients[path] = client
                    client.settimeout(0.01)
                    client.connect(str(path))
                self.clients[path].sendall(frame)
            except OSError:
                # A slow or hostile guest cannot stall the other guests. Close
                # after a partial frame too; the receiver discards that frame.
                self.clients.pop(path).close()
        atomic(state_dir() / "lean-host-pressure.json", dict(value, guests=len(self.clients)))


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
            time.sleep(1)
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
