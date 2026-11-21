"""Tell operators when a worker host has stopped making progress.

A systemd timer on the operator's control host runs `tauceti-fleet fleet-alert` once a minute.
It reads every inventory host's status the way `tauceti-fleet status` does. A host whose workers
are enabled has a problem when almost all of its live workers are in backoff or blocked, when
no worker is alive although admission is open, or when its admitted workers complete no round
without failure in an hour. A problem lasting 15 minutes sends one alert; the host recovers after
15 minutes of positive evidence (a successful round in the last hour, an intentional pause, or
workers disabled) and sends one recovery message. Reports that are missing, cached or stale
neither open nor close an incident. Without a Zulip configuration the messages go only to the
unit's journal.
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
WARMUP = 60 * 60  # the hour-long rounds window must lie entirely in observed admitted time
GAP = 5 * 60  # a longer silence restarts pending periods: unobserved time proves nothing
STATUS_MAX_AGE = 5 * 60
ACTIVITY_MAX_AGE = 20 * 60
STUCK_SHARE = 0.8
MIN_WORKERS = 3  # the share rule needs a few workers; one worker's backoff is ordinary
# Admission decisions, not failures: the host's capacity, budget or quota is holding workers.
ADMISSION_WAITS = {"waiting-capacity", "waiting-budget", "waiting-quota"}
UNIT = "tauceti-fleet-alert"


def fresh(at, now, limit):
    return isinstance(at, (int, float)) and now - at <= limit


def assess(value, now):
    """This host's facts from one status report; None when the report is missing, cached or stale."""
    status = value.get("status")
    if (not isinstance(status, dict) or value.get("offline") or value.get("error") or status.get("error")
            or not fresh(status.get("observed_at"), now, STATUS_MAX_AGE)):
        return None
    alive = [w for w in status.get("workers") or [] if w.get("alive")]
    blocked = [w for w in alive if w.get("state") == "backoff" or (
        str(w.get("state")).startswith("waiting-") and w.get("state") not in ADMISSION_WAITS)]
    summary = (value.get("activity") or {}).get("summary") or {}
    hour = (summary.get("rounds") or {}).get("1h")
    known = fresh(summary.get("prepared_at"), now, ACTIVITY_MAX_AGE) and isinstance(hour, dict)
    details = collections.Counter(str(w.get("detail") or w.get("state")) for w in blocked)
    return dict(
        enabled=value.get("workers_enabled") is True,
        alive=len(alive),
        working=sum(w.get("state") not in host.IDLE_STATES for w in alive),
        blocked=len(blocked),
        waiting=sum(w.get("state") in ADMISSION_WAITS for w in alive),
        # Rounds that failed or timed out also count as finished.
        **{key: sum(r.get(key, 0) for r in hour.values()) if known else None for key in ("finished", "failed")},
        detail=details.most_common(1)[0][0] if details else None,
        admitted=(status.get("worker_admission") or {}).get("state") == "at_ceiling",
    )


def classify(health, record, now):
    """("bad", description), ("good", None), or None when this report is no evidence either way."""
    if not health["enabled"]:
        return "good", None
    alive = health["alive"]
    if alive >= MIN_WORKERS and health["blocked"] >= STUCK_SHARE * alive:
        return "bad", f"{health['blocked']} of {alive} workers in backoff or blocked"
    if not health["admitted"] or (alive and health["waiting"] >= STUCK_SHARE * alive):
        # A schedule, memory, budget or quota pause: no progress is expected.
        record.pop("admitted_since", None)
        return "good", None
    if not alive:
        return "bad", "no workers alive although admission is open"
    record.setdefault("admitted_since", now)
    if health["finished"] is None:
        return None
    if health["finished"] > health["failed"]:
        return "good", None
    if now - record["admitted_since"] >= WARMUP:
        return "bad", f"no rounds succeeded in the last hour with {alive} workers alive"
    return None


def step(record, health, now):
    """Advance one host's episode; returns ("alert", description) or ("recovery", None) when one is due."""
    if now - record.get("seen", now) > GAP:
        for key in ("clear_since", "admitted_since", *(() if record.get("alerted_at") else ("since",))):
            record.pop(key, None)
    verdict = classify(health, record, now)
    if verdict is None:
        return None
    record["seen"] = now
    kind, description = verdict
    if kind == "bad":
        record.pop("clear_since", None)
        record.setdefault("since", now)
        if not record.get("alerted_at") and now - record["since"] >= SUSTAIN:
            return "alert", description
    elif record.get("alerted_at"):
        record.setdefault("clear_since", now)
        if now - record["clear_since"] >= SUSTAIN or not health["enabled"]:
            return "recovery", None
    else:
        record.pop("since", None)
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


def zulip(config, text):
    """Post one message as the configured bot: to a stream topic, or privately to one user."""
    from .ci_overview import status_ssl_context

    site = urllib.parse.urlsplit(config["site"])
    if site.scheme != "https" or not site.netloc or site.path not in ("", "/"):
        raise ValueError("Zulip site must be an HTTPS origin")
    if config.get("stream") and config.get("topic"):
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
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=status_ssl_context()))
    with opener.open(request, timeout=10) as response:
        if json.loads(response.read(65536)).get("result") != "success":
            raise ValueError("Zulip request was not successful")


def tick(results, state, now, send, commit=lambda: None):
    """One pass over the hosts' reports. `send(text)` returns whether the message was delivered;
    `commit()` persists the state after each delivery, so a later failure cannot repeat it."""
    for name, (machine_id, value) in results.items():
        try:
            health = assess(value, now)
        except (AttributeError, TypeError, ValueError) as error:
            print(f"fleet alert: {name}: unreadable status ({type(error).__name__})", file=sys.stderr, flush=True)
            continue
        if health is None:
            continue
        record = state.setdefault(machine_id, {})
        record["host"] = name
        due = step(record, health, now)
        if not due:
            continue
        kind, description = due
        if kind == "alert" and send(alert_text(name, description, health, now - record["since"])):
            record["alerted_at"] = now
            commit()
        elif kind == "recovery" and send(recovery_text(name, health, now - record["since"])):
            for key in ("since", "alerted_at", "clear_since"):
                record.pop(key, None)
            commit()
    return state


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
            # A reply lost after delivery repeats the message next run; a message is never dropped.
            zulip(config, text)
            return True
        except (OSError, ValueError, KeyError) as error:
            print("fleet alert: Zulip delivery failed (" + type(error).__name__ + "); retrying next run",
                  file=sys.stderr, flush=True)
            return False

    from .cli import fetch_status

    entries = {socket.gethostname() if entry["target"] == "local" else name: entry
               for name, entry in remote.inventory().items()}
    if not entries:
        raise FleetError("No hosts enrolled")
    with ThreadPoolExecutor(len(entries)) as pool:
        values = dict(zip(entries, pool.map(fetch_status, entries.values()), strict=True))
    path = state_dir() / "fleet-alert.json"
    with locked(path.with_suffix(".lock")):
        state = read(path, {})
        tick({name: (entries[name]["machine_id"], value) for name, value in values.items()}, state, time.time(),
             send, lambda: atomic(path, state))
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
    )
    for key in ("PATH", "TAUCETI_FLEET_CONFIG", "TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA"):
        if key in os.environ:
            service += "Environment=" + unit_argument(key + "=" + os.environ[key]) + "\n"
    service += "TimeoutStartSec=300\nUMask=0077\nMemoryMax=256M\n"
    timer = "[Unit]\nDescription=Check worker host progress every minute\n\n[Timer]\nOnBootSec=2min\n"
    timer += "OnUnitActiveSec=1min\n\n[Install]\nWantedBy=timers.target\n"
    (units() / (UNIT + ".service")).write_text(service)
    (units() / (UNIT + ".timer")).write_text(timer)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", UNIT + ".timer"])
    return dict(installed=UNIT + ".timer", zulip=bool(zulip_config))
