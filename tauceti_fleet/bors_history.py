"""Small durable job-runtime facts, filled by bounded background reads only."""

import datetime
import json
import time

from .store import FleetError, atomic, read, state_dir

WINDOWS = (("1h", 3600), ("3h", 10800), ("24h", 86400), ("7d", 604800))
MAX_READS = 5


def percentile(values, fraction):
    """Linearly interpolate a percentile of sorted cached durations."""
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def job_facts(jobs, run_id, now):
    facts = []
    for job in jobs:
        if (not isinstance(job, dict) or str(job.get("run_id")) != str(run_id)
                or job.get("name") != "sandboxed-build" or job.get("status") != "completed"
                or type(job.get("runner_id")) is not int or job["runner_id"] <= 0):
            continue
        try:
            start = datetime.datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
            finish = datetime.datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
            if not start.tzinfo or not finish.tzinfo or not 0 < start.timestamp() <= finish.timestamp() <= now + 5:
                continue
            facts.append(dict(id=str(job["id"]), finished_at=finish.timestamp(),
                              seconds=(finish - start).total_seconds()))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return facts


def collect(config, run_ids, fetch, *, now=None, allow_reads=True):
    now = time.time() if now is None else now
    if not isinstance(run_ids, list) or len(run_ids) > 10000 or any(type(n) is not int or n <= 0 for n in run_ids):
        raise FleetError("Invalid Bors runtime source identities")
    ids = list(dict.fromkeys(run_ids))
    path = state_dir() / "bors-job-durations.json"
    value = read(path, {})
    if not isinstance(value, dict) or value.get("source") != "github_actions_jobs/v1" or not isinstance(value.get("runs", {}), dict):
        value = {}
    runs = {str(n): entry if isinstance(entry, dict) else {}
            for n in ids for entry in [value.get("runs", {}).get(str(n), {})]}
    due = value.get("next_attempt_at", 0)
    if allow_reads and (type(due) not in (int, float) or not now < due <= now + 600):
        # Persist the attempt boundary first, even if a provider request fails.
        value = dict(source="github_actions_jobs/v1", runs=runs, next_attempt_at=now + 300)
        atomic(path, value)
        token_file = config.get("github_token_file") or config.get("ci_token_file")
        if not token_file:
            raise FleetError("No direct read credential for staging job history")
        deadline = time.monotonic() + 8
        reads = 0
        for run_id in ids:
            entry = runs[str(run_id)]
            if entry.get("complete") or now - entry.get("checked_at", 0) < 900:
                continue
            if reads >= MAX_READS or time.monotonic() >= deadline:
                break
            reads += 1
            entry["checked_at"] = now
            atomic(path, value)
            try:
                result = fetch(f"https://api.github.com/repos/TauCetiProject/TauCeti/actions/runs/{run_id}/jobs?per_page=100",
                               token_file=token_file)
                jobs = result.get("jobs")
                if not isinstance(jobs, list) or result.get("total_count") != len(jobs):
                    value["error"] = "incomplete_job_history"
                    break
                if any(not isinstance(job, dict) for job in jobs):
                    raise FleetError("Malformed job history")
                entry["jobs"] = job_facts(jobs, run_id, now)
                entry["complete"] = bool(jobs) and all(j.get("status") == "completed" for j in jobs)
            except Exception as error:
                value["error"] = type(error).__name__
                break  # Respect an unavailable/throttled provider; never fan out on failure.
            finally:
                atomic(path, value)
    jobs = {job["id"]: job for entry in runs.values() for job in entry.get("jobs", [])
            if isinstance(job, dict) and isinstance(job.get("id"), str)
            and type(job.get("finished_at")) in (int, float) and type(job.get("seconds")) in (int, float)}
    windows = []
    for label, seconds in WINDOWS:
        selected = sorted(j["seconds"] for j in jobs.values() if now - seconds < j["finished_at"] <= now)
        windows.append(dict(window=label, samples=len(selected), mean_seconds=
                            sum(selected) / len(selected) if selected else None,
                            p50_seconds=percentile(selected, .5), p95_seconds=percentile(selected, .95)))
    return dict(windows=windows, runs_total=len(ids), runs_checked=sum(e.get("complete") is True for e in runs.values()))
