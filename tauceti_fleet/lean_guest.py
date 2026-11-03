"""Cache host serial measurements where the offline compiler sandbox can read them."""

import io
import json
import math
import os
import select
import secrets
import socket
import time
from pathlib import Path

TARGET = Path("/usr/local/share/tauceti/host-pressure.json")
PORT = Path("/dev/virtio-ports/org.tauceti.pressure")
SOCKET = Path("/usr/local/share/tauceti/admission.sock")
# Match the measured two-file headroom used by the compiler gate. Keep this
# standalone so cloud-init does not need to install a Python package.
HEADROOM = 2 * 9_431_461_888


def reclaim(value, last_reclaim):
    now = time.monotonic()
    limit = max(60, math.floor(100 - 100 * HEADROOM / value["memory_total"]) - 1)
    used = 100 * (1 - value["memory_available"] / value["memory_total"])
    if used > limit - 3 and now - last_reclaim >= 30:
        try:
            # Host QEMU RSS includes the guest's file cache. Without reclaim,
            # every compiler can wait on RAM held by caches in other guests.
            Path("/proc/sys/vm/drop_caches").write_text("1\n")
            return now
        except OSError:
            pass
    return last_reclaim


def receive(stream, target=TARGET, last_reclaim=-math.inf):
    while True:
        try:
            line = stream.readline(4097)
        except OSError:
            return last_reclaim
        if not line:
            return last_reclaim
        if len(line) > 4096 or not line.endswith(b"\n"):
            # Drop an incomplete frame after a host reconnect.
            continue
        try:
            value = json.loads(line)
            if value.get("version") != 1:
                continue
            cpu, total, available = (value[k] for k in ("cpu_percent", "memory_total", "memory_available"))
            if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (cpu, total, available))
                    or not 0 <= cpu <= 100 or not 0 <= available <= total or total <= 0):
                continue
            value["received_monotonic"] = time.monotonic()
            temporary = target.with_suffix(".next")
            temporary.write_text(json.dumps(value, allow_nan=False))
            temporary.chmod(0o644)
            os.replace(temporary, target)
            last_reclaim = reclaim(value, last_reclaim)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue


class Broker:
    def __init__(self):
        self.clients = []
        # A receiver restart cannot reuse a request the host already granted.
        self.request = 1 + secrets.randbits(48)

    def grant(self, value):
        age = time.time() - value.get('at', 0)
        if value.get('permit') != self.request:
            return
        # Consume even an expired token so the next request can recover without
        # disconnecting a healthy serial link after a guest pause.
        self.request += 1
        if not -2 <= age <= 10:
            return
        remaining = value.get('starts', 1)
        if type(remaining) is not int or not 1 <= remaining <= 256:
            return
        while self.clients:
            client = self.clients.pop(0)
            try:
                client.sendall(b'1')
                remaining -= 1
                if remaining <= 0:
                    return
            except OSError:
                pass
            finally:
                client.close()

    def run(self, port, listener):
        last_request = 0
        buffer = b''
        last_reclaim = -math.inf
        while True:
            ready, _, _ = select.select([port, listener, *self.clients], [], [], 0.05)
            if listener in ready:
                client, _ = listener.accept()
                client.settimeout(0.01)
                if len(self.clients) < 256:
                    self.clients.append(client)
                else:
                    client.close()
            for client in list(self.clients):
                if client in ready:
                    # Clients send no data. Readability means a disconnected
                    # compiler, or malformed input; either is safe to discard.
                    client.close()
                    self.clients.remove(client)
            if port in ready:
                chunk = os.read(port, 4096)
                if not chunk:
                    return
                buffer = (buffer + chunk)[-8192:]
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    last_reclaim = receive(io.BytesIO(line + b'\n'), last_reclaim=last_reclaim)
                    try:
                        self.grant(json.loads(line))
                    except (ValueError, TypeError, AttributeError):
                        pass
            if time.monotonic() - last_request >= 0.05:
                os.write(port, (str(self.request if self.clients else 0) + ':' + str(len(self.clients)) + '\n').encode())
                last_request = time.monotonic()


def main():
    SOCKET.unlink(missing_ok=True)
    broker = Broker()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(SOCKET))
        SOCKET.chmod(0o666)
        listener.listen(256)
        while True:
            try:
                with PORT.open('r+b', buffering=0) as port:
                    broker.run(port.fileno(), listener)
            except OSError:
                pass
            time.sleep(1)


if __name__ == "__main__":
    main()
