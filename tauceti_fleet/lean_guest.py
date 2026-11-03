"""Cache host serial measurements where the offline compiler sandbox can read them."""

import json
import math
import os
import time
from pathlib import Path

TARGET = Path("/usr/local/share/tauceti/host-pressure.json")
PORT = Path("/dev/virtio-ports/org.tauceti.pressure")
# Match the measured two-file headroom used by the compiler gate. Keep this
# standalone so cloud-init does not need to install a Python package.
HEADROOM = 2 * 9_431_461_888


def reclaim(value, last_reclaim):
    now = time.monotonic()
    limit = max(60, math.floor(100 - 100 * HEADROOM / value["memory_total"]) - 1)
    used = 100 * (1 - value["memory_available"] / value["memory_total"])
    if used > limit and now - last_reclaim >= 30:
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


def main():
    last_reclaim = -math.inf
    while True:
        try:
            with PORT.open("rb", buffering=0) as stream:
                last_reclaim = receive(stream, last_reclaim=last_reclaim)
        except OSError:
            pass
        time.sleep(1)


if __name__ == "__main__":
    main()
