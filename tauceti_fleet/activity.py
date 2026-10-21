"""Read prepared worker activity. No subprocess, database, or network access."""

import hashlib
import os
from pathlib import Path

from . import activity_summary
from .store import FleetError, read

WINDOWS = activity_summary.WINDOWS
# Compatibility for summaries prepared before the worker published its taxonomy.
# New summaries carry round_types, keeping future worker kinds out of fleet policy.
ROUND_TYPES = (
    "rebase",
    "review",
    "fix-ci",
    "fix",
    "bump",
    "progress",
    "roadmap",
    "lint-repair",
    "overlap-scan",
    "overlap-fix",
    "security-audit",
    "no-work",
)


def activity_root():
    return Path(os.environ.get("TAUCETI_ACTIVITY_DIR", Path.home() / ".local/state/tauceti/activity"))


def cached(machine):
    try:
        path = activity_root() / "groups" / (hashlib.sha256(machine.encode()).hexdigest() + ".json")
        value = read(path, None)
        if value is None:
            value = read(activity_root() / "summary.json", {})
            summary = value.get("groups", {}).get(machine)
        else:
            summary = value.get("all")
        errors = list(value.get("errors", []))
        failure = read(activity_root() / "preparation-error.json", {})
        if failure.get("at", 0) > value.get("prepared_at", 0):
            errors.append("Background activity preparation failed; retaining last-known data")
        return dict(
            available=summary is not None,
            summary=summary,
            errors=errors,
            reason="Activity not prepared for this machine; refresh/backfill with tauceti activity",
        )
    except FleetError:
        return dict(available=False, reason="Prepared activity is unreadable; run tauceti activity refresh")


def inventory(machine):
    value = read(activity_root() / "prs.json", {})
    return [p for p in value.get("prs", []) if p["scope"] == machine]


def combine_status(values):
    return activity_summary.combine(values)


def display(value):
    activity_summary.display(value, ROUND_TYPES)


def combine(snapshots, prs, owners, origin_times, now):
    known = [s["activity"]["summary"] for s in snapshots if s.get("activity", {}).get("available")]
    if not known:
        return dict(available=False, reason="No prepared activity available")
    errors = [e for snap in snapshots for e in snap.get("activity", {}).get("errors", [])]
    if len(known) != len(snapshots):
        errors.append("Fleet activity omits hosts without prepared summaries")
    report_windows = tuple(w for w in WINDOWS if all(w in s["rounds"] for s in known))
    rounds = {w: {} for w in report_windows}
    for s in known:
        for w, kinds in s["rounds"].items():
            if w not in report_windows:
                continue
            for kind, counts in kinds.items():
                row = rounds[w].setdefault(kind, dict(finished=0, failed=0, unknown=0))
                for key in row:
                    row[key] += counts.get(key, 0)
    # PR counts use the existing deduplicated repository/number identities.
    windows = {}
    for w in report_windows:
        seconds = activity_summary.WINDOW_SECONDS[w]
        since = float("-inf") if seconds is None else now - seconds
        windows[w] = dict(
            authored=sum(since <= at <= now for at in origin_times.values()),
            landed=sum(
                key in owners and p.get("landed_at") is not None and since <= p["landed_at"] <= now
                for key, p in prs.items()
            ),
        )
    return dict(
        available=True,
        errors=errors,
        summary=dict(
            prepared_at=min(s["prepared_at"] for s in known),
            history_since=min((s["history_since"] for s in known if s.get("history_since")), default=None),
            rounds=rounds,
            round_types=list(dict.fromkeys(kind for s in known for kind in s.get("round_types", ROUND_TYPES))),
            pr_windows=windows,
            running=sum(s["running"] for s in known)
            if all(s["running"] is not None for s in known) and len(known) == len(snapshots)
            else None,
            interrupted_unfinished=sum(s.get("interrupted_unfinished", 0) for s in known),
            open_prs=sum(key in owners and p["state"] == "open" and not p.get("landed_at") for key, p in prs.items()),
            unknown_prs=sum(key not in prs for key in owners),
            unknown_landings=sum(
                key in owners and p["state"] == "closed" and not p.get("landed_at") for key, p in prs.items()
            ),
            pr_observed_at=min((s["pr_observed_at"] for s in known if s.get("pr_observed_at")), default=None),
        ),
    )
