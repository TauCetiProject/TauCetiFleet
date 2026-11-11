"""Divide one machine between Fleet installs that run under different accounts."""

import json
import os
import stat
import time
from pathlib import Path

from .store import atomic

LEASE = 15


def problem(directory):
    missing = "shared_host_dir must name an existing directory by its absolute path"
    if not isinstance(directory, str) or not Path(directory).is_absolute():
        return missing
    try:
        if not stat.S_ISDIR(os.stat(directory).st_mode):
            return missing
    except FileNotFoundError:
        return missing
    except OSError as exc:
        return f"this account cannot reach {directory}: {type(exc).__name__}"
    if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        return f"this account cannot read and write {directory}"
    return None


def peers(directory, machine_id, now=None):
    """Unexpired claims of the other installs; each file is another account's input."""
    now = time.time() if now is None else now
    values = []
    for path in Path(directory).glob("*.json"):
        if path.stem == machine_id:
            continue
        try:
            # A FIFO or a link left in the directory must not hold the controller.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                value = json.loads(os.read(fd, 4096))
            finally:
                os.close(fd)
        except (OSError, ValueError, RecursionError):
            continue
        if (
            isinstance(value, dict)
            and value.get("version") == 1
            and type(value.get("workers")) is int
            and 0 <= value["workers"] <= 4096
            and type(value.get("ci_busy")) is bool
            and type(value.get("expires_at")) in (int, float)
            and now < value["expires_at"] <= now + 2 * LEASE
        ):
            values.append(value)
    return values


def step(directory, machine_id, *, workers, ci_busy, now=None):
    """Publish this install's claim and return its part of the machine."""
    now = time.time() if now is None else now
    error = problem(directory)
    others = []
    if error is None:
        try:
            atomic(
                Path(directory) / (machine_id + ".json"),
                dict(version=1, expires_at=now + LEASE, workers=workers, ci_busy=ci_busy),
                mode=0o644,
            )
        except OSError as exc:
            error = f"cannot publish in {directory}: {type(exc).__name__}"
        others = peers(directory, machine_id, now)
    peer_workers = sum(value["workers"] for value in others)
    return dict(
        directory=str(directory),
        workers=workers,
        peers=len(others),
        peer_workers=peer_workers,
        share=workers / (workers + peer_workers) if peer_workers else 1,
        ci_busy=any(value["ci_busy"] for value in others),
        error=error,
    )
