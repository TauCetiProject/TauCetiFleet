"""Host-side CI classification. Only authenticated GitHub metadata is evidence.

The runner group restricts execution to the main-defined workflow. Guest messages
supply a lookup hint, never authority for cancellation or runner health.
"""

import re

from .report import timestamp

BUILD = "Build exact candidate under bwrap with workflow-pinned audits, offline"
REPORT = "Report build and bump-guard statuses to the PR head commit"
BORS_ID = 337246530
ACTIONS_ID = 41898282
INFRA_PREFIXES = (
    "Set up job",
    "Set up runner",
    "Raise vm.",
    "Resolve PR head",
    "Checkout ",
    "Install ",
    "Restore ",
    "Fetch ",
    "Decide ",
    "Configure the Lake artifact cache",
    "Save verified Lake archive",
    "Prepare trusted read-only Lean watchdog",
    "Build the trusted environment-lint driver",
)


def classify(job, *, run=None, statuses=None, host_health=None):
    """Return a bounded, non-secret decision; missing/conflicting facts fail closed."""

    def result(cooldown, reason, **evidence):
        return dict(cooldown=cooldown, reason=reason, evidence=evidence)

    steps = job.get("steps", [])
    if host_health == "fault":
        return result(True, "host_service_fault")
    if job.get("conclusion") in ("timed_out", "startup_failure"):
        return result(True, "job_" + job["conclusion"])
    for step in steps:
        if step.get("conclusion") in ("timed_out", "startup_failure") or (
            step.get("conclusion") == "failure" and step.get("name", "").startswith(INFRA_PREFIXES)
        ):
            return result(True, "infrastructure_step_failed", step_number=step.get("number"))
    if job.get("conclusion") != "cancelled":
        if job.get("conclusion") == "success":
            return result(False, "successful_job")
        failed = [s.get("name") for s in steps if s.get("conclusion") == "failure"]
        if job.get("conclusion") == "failure" and BUILD in failed and all(name in (BUILD, REPORT) for name in failed):
            return result(False, "candidate_build_failure")
        return result(True, "job_failure_inconclusive")

    unknown = result(True, "cancellation_evidence_inconclusive")
    if host_health != "healthy":
        return result(True, "cancellation_host_health_unknown")
    if not run or not isinstance(statuses, list):
        return unknown
    match = re.fullmatch(
        r"bors branch=main batch=([1-9][0-9]*) head=([0-9a-f]{40}) base=([0-9a-f]{40})",
        run.get("name", ""),
    )
    if not match:
        return result(True, "cancellation_run_name_mismatch")
    batch, candidate, base = match.groups()
    if (
        run.get("id") != job.get("run_id")
        or run.get("repository", {}).get("full_name") != "TauCetiProject/TauCeti"
        or run.get("path") != ".github/workflows/pr-build.yml"
        or run.get("event") != "repository_dispatch"
        or run.get("head_branch") != "main"
        or run.get("head_sha") != base
        or job.get("head_sha") != base
        or run.get("run_attempt") != 1
        or job.get("run_attempt") != 1
        or run.get("actor", {}).get("id") != BORS_ID
        or run.get("triggering_actor", {}).get("id") != BORS_ID
        or job.get("name") != "sandboxed-build"
    ):
        return result(True, "cancellation_run_identity_mismatch")
    # Duplicate names or partial/unknown step outcomes cannot establish health.
    by_name = {s.get("name"): s for s in steps}
    if len(by_name) != len(steps) or any(
        s.get("status") != "completed" or s.get("conclusion") not in ("success", "skipped", "cancelled", "failure")
        for s in steps
    ):
        return result(True, "cancellation_steps_incomplete")
    for name in ("Set up job", "Checkout workflow-pinned trusted build tooling", "Complete job"):
        if by_name.get(name, {}).get("conclusion") != "success":
            return result(True, "cancellation_runner_completion_missing")
    build = by_name.get(BUILD, {})
    if build.get("conclusion") != "cancelled":
        return result(True, "cancellation_not_build_interrupt")
    if any(s.get("conclusion") == "cancelled" and s.get("name") != BUILD for s in steps):
        return result(True, "cancellation_additional_step_cancelled")
    # Read newest-first status history from GitHub, not candidate logs/artifacts.
    bors = next((s for s in statuses if s.get("context") == "bors" and s.get("creator", {}).get("id") == BORS_ID), {})
    if (
        bors.get("state") != "failure"
        or bors.get("description") != "Canceled"
        or bors.get("target_url") != "https://bors.taucetiproject.org/batches/" + batch
    ):
        return result(True, "cancellation_status_mismatch")
    try:
        requested = timestamp(bors.get("created_at"))
        if not (
            timestamp(build.get("started_at"))
            <= requested
            <= timestamp(build.get("completed_at"))
            <= timestamp(job.get("completed_at"))
        ):
            return result(True, "cancellation_status_time_mismatch")
        for step in steps:
            if step.get("conclusion") != "failure":
                continue
            # This trusted reporting step deliberately exits 1 for a cancelled
            # build. Require BOTH successful API status writes to exclude its
            # network/publication fault path; no other failed step is exempt.
            if step.get("name") != REPORT or timestamp(step.get("started_at")) < requested:
                return result(True, "cancellation_unknown_step_failed", step_number=step.get("number"))
            for context in ("scope", "bump-guard"):
                status = next(
                    (
                        s
                        for s in statuses
                        if s.get("context") == context and s.get("creator", {}).get("id") == ACTIONS_ID
                    ),
                    {},
                )
                if (
                    status.get("state") != "success"
                    or status.get("target_url") != f"https://github.com/TauCetiProject/TauCeti/actions/runs/{run['id']}"
                    or not (
                        timestamp(step.get("started_at"))
                        <= timestamp(status.get("created_at"))
                        <= timestamp(step.get("completed_at"))
                    )
                ):
                    return result(True, "cancellation_report_evidence_missing")
    except (ValueError, TypeError, OverflowError):
        return result(True, "cancellation_timestamps_invalid")
    return result(
        False,
        "verified_bors_cancellation",
        batch_id=int(batch),
        candidate_sha=candidate,
        cancellation_status_id=bors.get("id"),
        host_service="healthy",
        requested_at=requested,
        run_attempt=1,
    )


def cancellation_metadata(job, config, request):
    """Two bounded reads; never follow candidate-provided URLs or paginate forever."""
    run = request(
        f"repos/TauCetiProject/TauCeti/actions/runs/{job['run_id']}", token_file=config.get("github_token_file")
    )
    match = re.fullmatch(
        r"bors branch=main batch=[1-9][0-9]* head=([0-9a-f]{40}) base=[0-9a-f]{40}", run.get("name", "")
    )
    if not match:
        return run, []
    statuses = request(
        f"repos/TauCetiProject/TauCeti/commits/{match[1]}/statuses?per_page=100",
        token_file=config.get("github_token_file"),
    )
    return run, statuses
