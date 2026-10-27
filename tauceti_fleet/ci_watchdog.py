"""Bound CI guest lifetime using host time and authenticated job metadata."""

from .report import timestamp

# GitHub's default job timeout is six hours. Allow startup and teardown overhead,
# but never let a missing completion notification hold a slot indefinitely.
VM_MAX_SECONDS = 7 * 3600
COMPLETION_GRACE_SECONDS = 120
OBSERVATION_INTERVAL_SECONDS = 60


def completed_job(value, response, now):
    """A guest-supplied run ID is only a hint; require this host-owned runner ID.

    An earlier attempt, another runner, an incomplete page, or unavailable
    metadata cannot establish completion. The independent lifetime limit still
    applies when GitHub cannot supply evidence.
    """
    jobs = response.get("jobs", [])
    if response.get("total_count") != len(jobs):
        return False
    owned = [j for j in jobs if j.get("runner_id") == value["runner_id"]]
    if not owned or any(j.get("run_id") != value["run_id"] or j.get("status") != "completed" for j in owned):
        return False
    try:
        finished = max(timestamp(j["completed_at"]) for j in owned)
        return value["started_at"] <= finished <= now - COMPLETION_GRACE_SECONDS
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
