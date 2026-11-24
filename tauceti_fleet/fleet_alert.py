"""Tell operators when a worker host has stopped making progress.

A systemd timer on the operator's control host runs `tauceti-fleet fleet-alert` once a minute.
It reads every inventory host's status the way `tauceti-fleet status` does. A host whose workers
are enabled and admitted has a problem when almost all of its live workers are in backoff or
blocked, when no worker is alive, or when its workers finish no round without failure in an
hour. A problem lasting 15 minutes sends one alert; the host recovers after 15 minutes of positive
evidence (a round finished without failure in the last hour, or an intentional pause), or at once
when its workers are disabled, with one recovery message. Reports that are missing, cached or
stale neither open nor close an incident. Without a Zulip configuration the messages go only to the
unit's journal.

Once an hour it also checks TauCeti's API docs build: when the newest finished api-docs run did
not succeed and the last success is more than a day old, it sends one alert, and one recovery
message after the next success. It reads two public workflow-run listings without credentials.
"""

import argparse
import base64
import datetime
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
ACTIVITY_MAX_AGE = 5 * 60  # summaries are prepared about once a minute
STUCK_SHARE = 0.8
MIN_WORKERS = 3  # the share rule needs a few workers; one worker's backoff is ordinary
# Admission decisions, not failures: the host's capacity, budget or quota is holding workers.
ADMISSION_WAITS = {"waiting-capacity", "waiting-budget", "waiting-quota"}
UNIT = "tauceti-fleet-alert"
DOCS_STATE = "api-docs"  # fleet-alert.json key; host records are keyed by 32-hex machine ids
DOCS_REPO = "TauCetiProject/TauCeti"
DOCS_WORKFLOW = "api-docs.yml"
DOCS_STALE = 24 * 60 * 60  # the docs may lag this long before the failing build is an incident
DOCS_INTERVAL = 60 * 60  # two unauthenticated reads an hour, of the 20 newest runs each


def fresh(at, now, limit):
    return isinstance(at, (int, float)) and now - at <= limit


def counters(hour):
    """The last hour's round totals, or None unless every row carries all three counters."""
    keys = ("finished", "failed", "unknown")
    if not isinstance(hour, dict) or not all(
            isinstance(r, dict) and all(type(r.get(k)) is int for k in keys) for r in hour.values()):
        return None
    return {k: sum(r[k] for r in hour.values()) for k in keys}


def assess(value, now):
    """This host's facts from one status report; None when the report is missing, cached, stale or partial."""
    status = value.get("status")
    if (not isinstance(status, dict) or value.get("offline") or value.get("error") or status.get("error")
            or not fresh(status.get("observed_at"), now, STATUS_MAX_AGE)
            or type(value.get("workers_enabled")) is not bool or not isinstance(status.get("workers"), list)):
        return None
    # As in `tauceti-fleet status`, a worker counts as alive only when its report says so.
    alive = [w for w in status["workers"] if isinstance(w, dict) and w.get("alive") is True]
    blocked = [w for w in alive if w.get("state") == "backoff" or (
        str(w.get("state")).startswith("waiting-") and w.get("state") not in ADMISSION_WAITS)]
    summary = (value.get("activity") or {}).get("summary") or {}
    rounds = counters((summary.get("rounds") or {}).get("1h"))
    if rounds is not None and fresh(summary.get("prepared_at"), now, ACTIVITY_MAX_AGE):
        rounds["at"] = summary["prepared_at"]
    else:
        rounds = None
    details = collections.Counter(str(w.get("detail") or w.get("state")) for w in blocked)
    return dict(
        enabled=value["workers_enabled"],
        alive=len(alive),
        working=sum(w.get("state") not in host.IDLE_STATES for w in alive),
        blocked=len(blocked),
        waiting=sum(w.get("state") in ADMISSION_WAITS for w in alive),
        # Finished rounds include those that failed, timed out or have an unconfirmed outcome.
        rounds=rounds,
        detail=details.most_common(1)[0][0] if details else None,
        admission=(status.get("worker_admission") or {}).get("state"),
    )


def classify(health, record, now):
    """("bad", description), ("good", None), or None when this report is no evidence either way."""
    alive, rounds = health["alive"], health["rounds"]
    if health["enabled"] and health["admission"] not in ("paused", "at_ceiling"):
        return None
    if (not health["enabled"] or health["admission"] == "paused"
            or (alive and health["waiting"] >= STUCK_SHARE * alive)):
        # Workers disabled, an admission pause (schedule, memory), or workers holding back for capacity
        # (open PR backpressure), budget or quota: no progress is expected.
        record.pop("admitted_since", None)
        return "good", None
    if alive >= MIN_WORKERS and health["blocked"] >= STUCK_SHARE * alive:
        return "bad", f"{health['blocked']} of {alive} workers in backoff or blocked"
    if not alive:
        return "bad", "no workers alive although admission is open"
    if now - record.get("admitted_seen", now) > GAP:
        record.pop("admitted_since", None)
    record["admitted_seen"] = now
    record.setdefault("admitted_since", now)
    if rounds is None:
        return None
    if rounds["finished"] > rounds["failed"] + rounds["unknown"]:
        return "good", None
    # No-progress rounds count as finished without failure: they are not crashes.
    if rounds["at"] - record["admitted_since"] >= WARMUP:
        return "bad", f"no round finished without failure in the last hour with {alive} workers alive"
    return None


def step(record, health, now):
    """Advance one host's episode; returns ("alert", description) or ("recovery", None) when one is due."""
    verdict = classify(health, record, now)
    if verdict is None:
        return None
    if now - record.get("seen", now) > GAP:
        # Time without a verdict proves nothing: restart pending periods; an open alert stays open.
        for key in ("clear_since", *(() if record.get("alerted_at") else ("since",))):
            record.pop(key, None)
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
    text, rounds = f"{health['working']} of {health['alive']} workers working", health["rounds"]
    if rounds is None:
        return text + "; rounds in the last hour unknown"
    return text + f"; {rounds['finished']} rounds finished in the last hour, {rounds['failed']} of them failed"


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


def parse_time(value):
    """Seconds since the epoch for GitHub's `2026-11-24T14:43:14Z`, or None."""
    try:
        moment = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")  # rejects out-of-range fields
        return moment.replace(tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


CONCLUSIONS = {"success", "failure", "cancelled", "timed_out", "startup_failure", "action_required",
               "neutral", "skipped", "stale"}


def checked_run(value, conclusion=None):
    """A run from GitHub's list, validated; raises ValueError on anything unexpected, including a
    run whose conclusion is not `conclusion` when one is required."""
    if not isinstance(value, dict) or value.get("conclusion") not in CONCLUSIONS:
        raise ValueError("workflow run without a known conclusion")
    if conclusion is not None and value["conclusion"] != conclusion:
        raise ValueError(f"workflow run listed as {conclusion} has another conclusion")
    if parse_time(value.get("updated_at")) is None:
        raise ValueError("workflow run without a readable time")
    return value


def newest_run(listing, conclusion=None):
    """The most recently finished run of a workflow-run listing, None for an empty history;
    ValueError when unreadable. GitHub lists runs by creation, so a rerun of an older run can
    finish after a newer one: pick by `updated_at`, not by position."""
    if not isinstance(listing, dict) or not isinstance(listing.get("workflow_runs"), list):
        raise ValueError("unreadable workflow run listing")
    runs = listing["workflow_runs"]
    if not runs and listing.get("total_count") != 0:
        raise ValueError("empty workflow run listing that does not say it is empty")
    runs = [checked_run(run, conclusion) for run in runs]
    return max(runs, key=lambda run: parse_time(run["updated_at"])) if runs else None


def docs_verdict(last_success, last_finished, now):
    """("bad", description), ("good", None), or None from the newest successful and finished runs."""
    if last_finished is None:
        return None
    if last_finished["conclusion"] == "success":
        return "good", None
    succeeded = None if last_success is None else parse_time(last_success["updated_at"])
    if succeeded is not None and now - succeeded <= DOCS_STALE:
        return None  # failing, but the published docs are recent enough
    since = "no successful run on record" if succeeded is None else f"last success {duration(now - succeeded)} ago"
    url = last_finished.get("html_url") or ""
    return "bad", f"TauCeti's API docs build keeps failing ({since}; latest run {last_finished['conclusion']}: {url})"


def fetch_docs_runs(request=None):
    """The newest successful and the newest finished api-docs runs on main; ValueError or FleetError
    when GitHub's answer is unavailable or unreadable."""
    if request is None:
        from .github import request
    base = f"repos/{DOCS_REPO}/actions/workflows/{DOCS_WORKFLOW}/runs?branch=main&exclude_pull_requests=true&per_page=20"
    return (newest_run(request(base + "&status=success"), "success"),
            newest_run(request(base + "&status=completed")))


def docs_due(state, now):
    return now - (state.get(DOCS_STATE) or {}).get("checked_at", 0) >= DOCS_INTERVAL


def docs_tick(state, now, send, fetch=fetch_docs_runs, commit=lambda: None):
    """At most hourly: alert once when the API docs build has failed for over a day, recover once.
    `fetch` may raise; an unreadable answer neither opens nor closes the alert."""
    if not docs_due(state, now):
        return state
    record = state.setdefault(DOCS_STATE, {})
    record["checked_at"] = now
    try:
        last_success, last_finished = fetch()
        verdict = docs_verdict(last_success, last_finished, now)
    except (FleetError, AttributeError, KeyError, TypeError, ValueError, IndexError) as error:
        print(f"fleet alert: API docs runs unreadable ({type(error).__name__})", file=sys.stderr, flush=True)
        return state
    success_at = None if last_success is None else parse_time(last_success["updated_at"])
    if record.get("alerted_at"):
        # Any success after the one the alert was about ends the incident, even if a later run
        # failed again before this check: that failure starts a new day-long wait.
        if success_at is not None and success_at > record.get("success_at", 0):
            if send(f":check: TauCeti's API docs build succeeded again; closing the alert opened "
                    f"{duration(now - record['alerted_at'])} ago."):
                record.pop("alerted_at", None)
                record.pop("success_at", None)
                commit()
        return state
    if verdict is not None and verdict[0] == "bad" and send(":warning: " + verdict[1] + "."):
        record["alerted_at"] = now
        record["success_at"] = success_at or 0
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
    # Read GitHub before taking the lock, so slow requests never delay other runs; the check
    # itself is re-decided under the lock.
    docs = None
    if docs_due(read(path, {}), time.time()):
        try:
            docs = fetch_docs_runs()
        except (FleetError, ValueError) as error:
            docs = error

    def docs_fetch():
        if docs is None:
            raise ValueError("API docs runs were not read before the lock")
        if isinstance(docs, Exception):
            raise docs
        return docs

    with locked(path.with_suffix(".lock")):
        state = read(path, {})
        tick({name: (entries[name]["machine_id"], value) for name, value in values.items()}, state, time.time(),
             send, lambda: atomic(path, state))
        docs_tick(state, time.time(), send, fetch=docs_fetch, commit=lambda: atomic(path, state))
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
