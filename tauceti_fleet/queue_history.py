"""Current controller-ledger history, independent of the retired Bors feed."""

import math
import time
from datetime import UTC, datetime

from . import ci_overview
from .queue_status import clock
from .store import FleetError, atomic, config_dir, read

URL = "https://queue.taucetiproject.org/statistics"
COUNTS = (
    "landed_batches",
    "landed_prs",
    "ci_completed",
    "ci_passed",
    "ci_source_failed",
    "ci_infrastructure",
    "ci_superseded",
    "run_duration_samples",
)


def cache_path():
    return config_dir() / "status-cache" / "queue-history.json"


def cached():
    try:
        value = read(cache_path(), {})
        return value if isinstance(value, dict) else {}
    except FleetError:
        return dict(error="Current queue history cache is unreadable")


def parse(value):
    if (
        value.get("schema") != "tauceti-queue.statistics/v1"
        or value.get("repository") != ci_overview.REPOSITORY
        or value.get("canonical_branch") != "main"
        or value.get("coverage") != "controller_ledger"
    ):
        raise ValueError("Wrong queue statistics source")
    if type(value.get("observed_at")) not in (float, int) or not math.isfinite(value["observed_at"]):
        raise ValueError("Invalid statistics time")
    windows = value.get("windows")
    if (
        not isinstance(windows, list)
        or len(windows) != 4
        or {w.get("window") for w in windows} != {"1h", "3h", "24h", "7d"}
    ):
        raise ValueError("Missing history windows")
    for w in windows:
        if any(type(w.get(k)) is not int or w[k] < 0 for k in COUNTS):
            raise ValueError("Invalid history count")
        for k in ("run_p50_seconds", "run_p95_seconds"):
            if w.get(k) is not None and (type(w[k]) not in (int, float) or not math.isfinite(w[k]) or w[k] < 0):
                raise ValueError("Invalid CI run duration")
    return {key: value.get(key) for key in ("observed_at", "since", "windows", "coverage")}


def refresh():
    previous = cached()
    try:
        report = dict(value=parse(ci_overview.fetch(URL, timeout=4)), checked_at=time.time())
    except (OSError, FleetError, ValueError, KeyError, TypeError, AttributeError) as error:
        report = dict(
            previous,
            error="Current queue history refresh failed (" + type(error).__name__ + ")",
            checked_at=time.time(),
        )
    try:
        atomic(cache_path(), report)
    except (OSError, ValueError):
        report["error"] = "Current queue history cache could not be saved"
    return report


def display(report, *, timezone="Australia/Sydney", now=None):
    now = time.time() if now is None else now
    value = report.get("value") if isinstance(report, dict) else None
    if not value:
        print("Queue history: unavailable (current controller ledger; historical snapshot is not used)")
        return
    observed = value["observed_at"]
    when = datetime.fromtimestamp(observed, UTC).isoformat()
    notes = [clock(when, timezone), f"{max(0, int(now - observed))}s old"]
    since = value.get("since")
    if type(since) in (int, float) and since > now - 604800:
        start = datetime.fromtimestamp(since, UTC).isoformat()
        notes.append("7d limited to ledger since " + clock(start, timezone))
    if now - observed > 180 or report.get("error"):
        notes.append("STALE; cached" if report.get("error") else "STALE")
    print("Queue history (current controller ledger; " + "; ".join(notes) + "):")
    windows = {w["window"]: w for w in value["windows"]}
    periods = ("1h", "3h", "24h", "7d")
    rows = [
        (title, [str(windows[p][metric]) for p in periods])
        for title, metric in (
            ("Batches landed", "landed_batches"),
            ("PRs landed", "landed_prs"),
            ("CI attempts completed", "ci_completed"),
            ("CI passed", "ci_passed"),
            ("CI source failures", "ci_source_failed"),
            ("CI infrastructure failures", "ci_infrastructure"),
            ("CI superseded", "ci_superseded"),
        )
    ]
    for title, metric in (("CI run p50", "run_p50_seconds"), ("CI run p95", "run_p95_seconds")):
        rows.append(
            (
                title,
                [
                    "—"
                    if windows[p][metric] is None
                    else f"{int(windows[p][metric]) // 60}m {int(windows[p][metric]) % 60}s"
                    for p in periods
                ],
            )
        )
    rows.append(("CI run samples", [str(windows[p]["run_duration_samples"]) for p in periods]))
    width = max(len(title) for title, _ in rows)
    print("  " + " " * width + "".join(f"{p:>10}" for p in periods))
    for title, cells in rows:
        print(f"  {title:<{width}}" + "".join(f"{cell:>10}" for cell in cells))
    print("  CI run times include setup, reporting and publication; they are not build-only times.")
