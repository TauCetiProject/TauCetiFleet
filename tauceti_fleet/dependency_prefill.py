"""Operator-armed, one-guest PR canary; never a prerequisite for CI admission."""

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .store import data_dir, event


# Called only inside vm.start's host-wide ci-start.lock. An attempt consumes the
# ticket even if registration/launch fails; operators explicitly re-arm a retry.
def record(kind, **fields):
    # Optional telemetry must not turn the optimization into an admission gate.
    with contextlib.suppress(OSError, sqlite3.Error):
        event(kind, **fields)


def claim(config, instance):
    if "pr" not in config.get("ci_kinds", []):
        return None
    root = data_dir() / "dependency-prefill"
    ticket = root / "next-pr.json"
    if not ticket.is_file():
        return None
    try:
        if ticket.stat().st_size > 4096:
            raise ValueError("oversized ticket")
        value = json.loads(ticket.read_text())
        digest = value["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("invalid digest")
        if not time.time() < value["expires_at"] <= time.time() + 3600:
            raise ValueError("expired or excessively long ticket")
        image = root / (digest + ".iso")
        if image.is_symlink() or not image.is_file() or image.stat().st_mode & 0o222:
            raise ValueError("payload must be an immutable regular file")
        if image.stat().st_size > 1024**3:
            raise ValueError("canary payload exceeds 1 GiB")
        with image.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("payload digest mismatch")
        # Valid instance IDs are established before this call in vm._start.
        if not re.fullmatch(r"[a-f0-9]{16}", instance):
            raise ValueError("invalid instance")
        os.replace(ticket, root / ("claimed-" + instance + ".json"))
        record("dependency_prefill_claimed", instance=instance, sha256=digest)
        return dict(path=str(image), sha256=digest)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Quarantine once, avoiding repeated noise from an expired/bad ticket.
        if re.fullmatch(r"[a-f0-9]{16}", instance):
            with contextlib.suppress(OSError):
                os.replace(ticket, root / ("rejected-" + instance + ".json"))
        record("dependency_prefill_ignored", reason=(str(exc) or type(exc).__name__)[:160])
        return None


def mount_commands():
    # This exposes trusted data only. Hydration/execution waits for the workflow's
    # exact integrated-config attestation; no candidate path is touched at boot.
    return [
        "mkdir -p /opt/tauceti-dependency-prefill; "
        "if ! timeout 5 mount -t iso9660 -o ro,nosuid,nodev,noexec "
        "/dev/disk/by-label/TAUCETI_PREFILL /opt/tauceti-dependency-prefill; then "
        "echo 'optional dependency prefill unavailable; using ordinary setup'; fi"
    ]
