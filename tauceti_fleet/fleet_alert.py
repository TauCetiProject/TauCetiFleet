"""Tell operators when a worker host has stopped making progress.

A systemd timer on the operator's control host runs `tauceti-fleet fleet-alert` once a minute.
It reads every inventory host's status the way `tauceti-fleet status` does, and opens an alert
for a host whose workers are enabled and which, for 15 minutes, either has almost all of its
live workers in backoff or blocked, or completes no round without failure in an hour while its
workers are alive and admitted. Each incident gets one alert and, after 15 healthy minutes, one recovery
message. Without a Zulip configuration the messages go only to the unit's journal.
"""

import argparse
import base64
import collections
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import host, remote
from .store import FleetError, atomic, locked, read, state_dir

SUSTAIN = 15 * 60  # a problem, or its absence, must last this long before a message
WARMUP = 60 * 60  # the hour-long rounds window must lie entirely in observed enabled time
STUCK_SHARE = 0.8
MIN_WORKERS = 3  # the share rule needs a few workers; one worker's backoff is ordinary
ACTIVITY_MAX_AGE = 20 * 60
# Admission decisions, not failures: the host's schedule, budget or quota is holding workers.
ADMISSION_WAITS = {"waiting-capacity", "waiting-budget", "waiting-quota"}
UNIT = "tauceti-fleet-alert"


def stuck(state):
    return state == "backoff" or (
        isinstance(state, str) and state.startswith("waiting-") and state not in ADMISSION_WAITS
    )


def assess(value, now):
    """This host's health from one status report; None when there is no fresh report to judge."""
    status = value.get("status")
    if not status or value.get("offline") or value.get("error"):
        return None
    alive = [w for w in status.get("workers") or [] if w.get("alive")]
    blocked = [w for w in alive if stuck(w.get("state"))]
    summary = (value.get("activity") or {}).get("summary") or {}
    hour = (summary.get("rounds") or {}).get("1h")
    fresh = isinstance(summary.get("prepared_at"), (int, float)) and now - summary["prepared_at"] <= ACTIVITY_MAX_AGE
    counts = {key: sum(r.get(key, 0) for r in hour.values()) if fresh and isinstance(hour, dict) else None
              for key in ("finished", "failed")}  # rounds that failed or timed out also count as finished
    details = collections.Counter(str(w.get("detail") or w.get("state")) for w in blocked)
    return dict(
        enabled=value.get("workers_enabled") is True,
        alive=len(alive),
        working=sum(w.get("state") not in host.IDLE_STATES for w in alive),
        blocked=len(blocked),
        **counts,
        detail=details.most_common(1)[0][0] if details else None,
        admitted=(status.get("worker_admission") or {}).get("state") == "at_ceiling",
    )


def problem(health, eligible_for):
    """The incident's one-line description, or None when the host is healthy or not expected to work."""
    if not health["enabled"]:
        return None
    if health["alive"] >= MIN_WORKERS and health["blocked"] >= STUCK_SHARE * health["alive"]:
        return f"{health['blocked']} of {health['alive']} workers in backoff or blocked"
    if (health["alive"] and health["admitted"] and health["finished"] is not None
            and health["finished"] - health["failed"] <= 0 and eligible_for >= WARMUP):
        return f"no rounds succeeded in the last hour with {health['alive']} workers alive"
    return None


def duration(seconds):
    minutes = int(seconds // 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes} min"


def snapshot(health):
    text = f"{health['working']} of {health['alive']} workers working"
    if health["finished"] is None:
        return text + "; rounds in the last hour unknown"
    return text + f"; {health['finished']} rounds finished in the last hour, {health['failed']} of them failed"


def alert_text(name, description, health, opened_for):
    text = f":warning: **{name}**: {description} for {duration(opened_for)}. Now {snapshot(health)}."
    if health["detail"]:
        text += " Most common worker detail: `" + health["detail"].replace("`", "'")[:160] + "`"
    return text


def recovery_text(name, health, opened_for):
    if not health["enabled"]:
        return f":check: **{name}**: workers disabled; closing the alert opened {duration(opened_for)} ago."
    return f":check: **{name}** recovered after {duration(opened_for)}. Now {snapshot(health)}."


def step(record, health, now):
    """Advance one host's episode; returns ("alert"|"recovery", text inputs) when a message is due."""
    if health["enabled"] and health["alive"]:
        record.setdefault("eligible_since", now)
    else:
        record.pop("eligible_since", None)
    description = problem(health, now - record.get("eligible_since", now))
    if description:
        record.pop("clear_since", None)
        record.setdefault("since", now)
        if not record.get("alerted_at") and now - record["since"] >= SUSTAIN:
            return "alert", description
    elif record.get("alerted_at"):
        record.setdefault("clear_since", now)
        if now - record["clear_since"] >= SUSTAIN:
            return "recovery", None
    else:
        record.pop("since", None)
    return None


def zulip(config, text):
    """Post one message as the configured bot: to a stream topic, or privately to one user."""
    site = urllib.parse.urlsplit(config["site"])
    if site.scheme != "https" or not site.netloc or site.path not in ("", "/"):
        raise ValueError("Zulip site must be an HTTPS origin")
    if config.get("stream"):
        params = dict(type="stream", to=config["stream"], topic=config["topic"], content=text)
    elif type(config.get("recipient_id")) is int and config["recipient_id"] > 0:
        params = dict(type="private", to=json.dumps([config["recipient_id"]]), content=text)
    else:
        raise ValueError("Zulip configuration needs stream and topic, or recipient_id")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    credentials = base64.b64encode(f"{config['email']}:{config['api_key']}".encode()).decode()
    request = urllib.request.Request(
        f"https://{site.netloc}/api/v1/messages",
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": "Basic " + credentials, "User-Agent": "TauCetiFleet/fleet-alert"},
        method="POST",
    )
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        if json.loads(response.read(65536)).get("result") != "success":
            raise ValueError("Zulip request was not successful")


def tick(results, state, now, send):
    """One pass over the hosts' reports; `send(text)` returns whether the message was delivered."""
    for name, (machine_id, value) in results.items():
        health = assess(value, now)
        if health is None:
            continue  # An unreachable or cached report neither opens nor closes an incident.
        record = state.setdefault(machine_id, {})
        record["host"] = name
        due = step(record, health, now)
        if not due:
            continue
        kind, description = due
        if kind == "alert" and send(alert_text(name, description, health, now - record["since"])):
            record["alerted_at"] = now
        elif kind == "recovery" and send(recovery_text(name, health, now - record["since"])):
            for key in ("since", "alerted_at", "clear_since"):
                record.pop(key, None)
    return state


def display_name(name, entry):
    return socket.gethostname() if entry["target"] == "local" else name


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tauceti-fleet fleet-alert", description=__doc__)
    parser.add_argument("--zulip-config", type=Path,
                        help="mode-0600 bot configuration; without it, messages go only to standard output")
    args = parser.parse_args(argv)
    config = None
    if args.zulip_config:
        if args.zulip_config.stat().st_mode & 0o077:
            parser.error("the Zulip configuration must be private (chmod 600)")
        config = read(args.zulip_config)

    def send(text):
        print(text, flush=True)
        if config is None:
            return True
        try:
            zulip(config, text)
            return True
        except (OSError, ValueError, KeyError) as error:
            print("fleet alert: Zulip delivery failed (" + type(error).__name__ + "); retrying next run",
                  file=sys.stderr, flush=True)
            return False

    from .cli import fetch_status

    entries = {display_name(name, entry): entry for name, entry in remote.inventory().items()}
    if not entries:
        raise FleetError("No hosts enrolled")
    with ThreadPoolExecutor(len(entries)) as pool:
        values = dict(zip(entries, pool.map(fetch_status, entries.values()), strict=True))
    path = state_dir() / "fleet-alert.json"
    with locked(path.with_suffix(".lock")):
        state = read(path, {})
        tick({name: (entries[name]["machine_id"], value) for name, value in values.items()}, state, time.time(), send)
        atomic(path, state)
    return 0


def install(zulip_config=None):
    """Write and start this account's once-a-minute alert timer; it runs the installed Fleet release."""
    from .host import unit_argument, units
    from .process import run

    args = [Path.home() / ".local/bin/tauceti-fleet", "fleet-alert"]
    if zulip_config:
        args += ["--zulip-config", Path(zulip_config).expanduser().resolve()]
    service = (
        "[Unit]\nDescription=Alert operators when a worker host stops making progress\n\n[Service]\nType=oneshot\n"
        "ExecStart=" + " ".join(unit_argument(a) for a in args) + "\n"
        "Environment=" + unit_argument("PATH=" + os.environ.get("PATH", "/usr/bin:/bin")) + "\n"
        "TimeoutStartSec=120\nUMask=0077\nMemoryMax=256M\n"
    )
    timer = "[Unit]\nDescription=Check worker host progress every minute\n\n[Timer]\nOnBootSec=2min\n"
    timer += "OnUnitActiveSec=1min\n\n[Install]\nWantedBy=timers.target\n"
    (units() / (UNIT + ".service")).write_text(service)
    (units() / (UNIT + ".timer")).write_text(timer)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", UNIT + ".timer"])
    return dict(installed=UNIT + ".timer", zulip=bool(zulip_config))
