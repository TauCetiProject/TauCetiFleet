"""Read the public /ci pool summary once for the operator CLI, never scan GitHub."""

import re
import time
from datetime import datetime

from . import ci_overview
from .store import FleetError, atomic, config_dir, read

ORIGIN = "https://ci-data.taucetiproject.org"
MANIFEST = ORIGIN + "/v1/operations/manifest.json"
WINDOWS = (3600, 10800, 86400)
OUTCOMES = ("finished", "success", "failure", "cancelled", "skipped", "other")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing pool observation time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Pool observation time has no timezone")
    return parsed.timestamp()


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid pool activity count")
    return value


def job(value, clock):
    if value is None:
        return None
    if not isinstance(value, dict) or not str(value.get("id", "")).isdigit():
        raise ValueError("Invalid pool job identity")
    timestamp(value.get(clock))
    if not isinstance(value.get("conclusion"), (str, type(None))):
        raise ValueError("Invalid pool job outcome")
    if value.get("conclusion") and not value["conclusion"].isprintable():
        raise ValueError("Invalid pool job outcome")
    return {key: value.get(key) for key in ("id", "run_id", "run_attempt", "name", "url", "status", "conclusion",
                                           "created_at", "started_at", "completed_at")}


def parse(manifest, jobs):
    if (manifest.get("schema") != "tauceti.operations.manifest/v1"
            or jobs.get("schema") != "tauceti.operations.jobs/v1"):
        raise ValueError("Invalid public CI activity schema or scope")
    activity = jobs.get("pool_activity")
    if not isinstance(activity, dict) or not isinstance(activity.get("pools"), list):
        raise ValueError("Public CI pool activity is unavailable")
    if activity.get("coverage") not in ("retained_observations", "partial"):
        raise ValueError("Public CI pool activity coverage is unavailable")
    timestamp(activity.get("through"))
    pools = []
    names = set()
    for original in activity["pools"]:
        name = original.get("pool")
        if not isinstance(name, str) or not name or not name.isprintable() or len(name) > 80 or name in names:
            raise ValueError("Invalid pool name")
        names.add(name)
        row = dict(pool=name)
        for category in ("builds", "all"):
            source = original.get(category)
            if not isinstance(source, dict):
                raise ValueError("Missing pool activity category")
            windows = {}
            for window in source.get("windows", []):
                seconds = window.get("seconds")
                if seconds in WINDOWS:
                    if seconds in windows:
                        raise ValueError("Duplicate pool activity window")
                    windows[seconds] = {key: count(window.get(key)) for key in OUTCOMES}
            row[category] = dict(running=count(source.get("running")), waiting=count(source.get("waiting")),
                                 latest_completion=job(source.get("latest_completion"), "completed_at"),
                                 oldest_running=job(source.get("oldest_running"), "started_at"),
                                 oldest_waiting=job(source.get("oldest_waiting"), "created_at"),
                                 windows=[dict(seconds=seconds, **value) for seconds, value in windows.items()])
        pools.append(row)
    # Aggregate coverage is independent of the bounded job-detail pages.
    return dict(through=activity["through"], coverage=activity["coverage"],
                partial=activity["coverage"] != "retained_observations", pools=pools)


def cache_path():
    return config_dir() / "status-cache" / "pool-activity.json"


def cached():
    try:
        value = read(cache_path(), {})
    except FleetError:
        return dict(error="Cached public CI activity is unreadable")
    return value if isinstance(value, dict) else {}


def refresh():
    previous = cached()
    manifest = None
    try:
        manifest = ci_overview.fetch(MANIFEST, timeout=3)
        domain = manifest.get("domains", {}).get("jobs", {})
        path = domain.get("url", "")
        scope = manifest.get("scope", "")
        if not isinstance(scope, str) or not re.fullmatch(r"[a-f0-9]{64}", scope) or not isinstance(path, str) or not re.fullmatch(
                r"/v1/operations/objects/" + scope + r"/[a-f0-9]{64}\.json", path):
            raise ValueError("Invalid public CI jobs object URL")
        value = dict(value=parse(manifest, ci_overview.fetch(ORIGIN + path, timeout=3)), checked_at=time.time())
    except (FleetError, OSError, ValueError, TypeError, AttributeError) as error:
        value = dict(previous, error="Public CI activity refresh failed (" + type(error).__name__ + ")",
                     checked_at=time.time())
    from . import queue_status
    if isinstance(manifest, dict) and "queue" in manifest.get("domains", {}):
        value["queue"] = queue_status.refresh(manifest, previous.get("queue"))
    elif isinstance(manifest, dict):
        value["queue"] = dict(error="Public queue observation is unavailable", checked_at=time.time())
    else:
        saved_queue = queue_status.cached() or previous.get("queue", {})
        value["queue"] = dict(saved_queue, error="Public manifest unavailable; using cached queue", checked_at=time.time())
    if isinstance(manifest, dict) and "queue" in manifest.get("domains", {}):
        from . import queue_history
        queue_history.refresh()
    try:
        atomic(cache_path(), value)
    except (OSError, ValueError):
        value = dict(value, error="Public CI activity cache could not be saved")
    return value


def age(value, now):
    seconds = max(0, int(now - timestamp(value)))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02}m" if hours < 48 else f"{hours // 24}d{hours % 24:02}h"


def display(report, *, now=None, all_jobs=False):
    now = time.time() if now is None else now
    value = report.get("value")
    if not value:
        print("CI pool activity: unavailable" + ("; " + report["error"] if report.get("error") else " (not collected yet)"))
        return
    try:
        observed = timestamp(value["through"])
        stale = now - observed > 180 or observed - now > 5
        note = ["partial observations" if value.get("partial") else "retained observations",
                "through " + value["through"], "age " + age(value["through"], now)]
        if stale:
            note.append("STALE")
        if report.get("error"):
            note.extend(("cached", report["error"]))
        scope = "all jobs" if all_jobs else "builds"
        print("CI pool activity — " + scope + " (" + "; ".join(note) + ")")
        rows = [["Pool", "Run/Wait", "Latest completion", "Oldest run", "Oldest wait", "1h", "3h", "24h"]]
        absent = "unknown" if value.get("partial") else "—"
        for pool in value["pools"]:
            builds = pool["all" if all_jobs else "builds"]
            last = builds["latest_completion"]
            latest = age(last["completed_at"], now) + " " + (last["conclusion"] or "unknown") if last else absent
            windows = {window["seconds"]: window for window in builds["windows"]}
            completions = []
            for seconds in WINDOWS:
                window = windows.get(seconds)
                completions.append(f"{window['finished']} f{window['failure']} c{window['cancelled']}" if window else "unknown")
            oldest_run, oldest_wait = builds["oldest_running"], builds["oldest_waiting"]
            rows.append([pool["pool"], f"{builds['running']}/{builds['waiting']}", latest,
                         age(oldest_run["started_at"], now) if oldest_run else absent,
                         age(oldest_wait["created_at"], now) if oldest_wait else absent, *completions])
        widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
        for row in rows:
            print("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
        print("  Run/Wait = running/waiting. Windows: finished (incl skipped/other); f=failures, c=cancellations.")
        print("  — = none observed; unknown = incomplete coverage. Oldest columns show ages; --json includes job identities/URLs.")
        if not value["pools"]:
            print("  No pools observed; this does not establish zero activity.")
    except (ValueError, KeyError, TypeError, AttributeError):
        print("CI pool activity: unavailable (invalid cached observation)")
