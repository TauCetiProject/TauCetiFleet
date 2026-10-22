"""Host-side CI classification. Only authenticated GitHub metadata is evidence.

The runner group restricts execution to the main-defined workflow. Guest messages
supply a lookup hint, never authority for cancellation or runner health.
Quota classification uses a dedicated trusted-workflow step, not log text or
candidate artifacts, and requires the host service to have exited cleanly.
"""

import re

from .report import timestamp

BUILD = "Build exact candidate under bwrap with workflow-pinned audits, offline"
REPORT = "Report build and bump-guard statuses to the PR head commit"
BORS_ID = 337246530
QUOTA = "GitHub API quota unavailable"
# Exact names from main's trusted workflow: never exempt arbitrary setup failures
# just because an unrelated API request also exhausted its quota.
QUOTA_STEPS = (
    "Resolve PR head",
    "Scope guard — allow TauCeti sources and validated pins",
    "Diff size guard — additions, deletions, and changed files each ≤ 2000 (trusted GitHub PR totals; Lake-pin bumps exempt from the line caps)",
    "New-file length guard — newly added .lean files must be ≤ 1000 lines",
    "Decide the environment-lint scope",
    REPORT,
)
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
        if step.get("conclusion") in ("timed_out", "startup_failure"):
            return result(True, "infrastructure_step_failed", step_number=step.get("number"))
    # The helper records a quota response in private runner-temp state outside
    # the candidate sandbox. A fixed main-defined finalizer fails ONLY when that
    # record exists. Its conclusion is available in the already-fetched jobs
    # metadata: no log download or additional quota-consuming read is needed.
    by_name = {s.get("name"): s for s in steps}
    failed = [s for s in steps if s.get("conclusion") == "failure"]
    if (
        host_health == "healthy"
        and job.get("name") == "sandboxed-build"
        and job.get("status") == "completed"
        and job.get("conclusion") == "failure"
        and len(by_name) == len(steps)
        and all(
            s.get("status") == "completed" and s.get("conclusion") in ("success", "skipped", "failure") for s in steps
        )
        and all(
            by_name.get(name, {}).get("conclusion") == "success"
            for name in ("Set up job", "Checkout workflow-pinned trusted build tooling", "Complete job")
        )
        and by_name.get(QUOTA, {}).get("conclusion") == "failure"
        and all(s.get("name") in (*QUOTA_STEPS, QUOTA) for s in failed)
    ):
        return result(
            False,
            "github_api_quota_unavailable",
            marker_step_number=by_name[QUOTA].get("number"),
            failed_api_steps=[s.get("name") for s in failed if s.get("name") != QUOTA],
            host_service="healthy",
        )
    for step in steps:
        if step.get("conclusion") == "failure" and step.get("name", "").startswith(INFRA_PREFIXES):
            return result(True, "infrastructure_step_failed", step_number=step.get("number"))
    if job.get("conclusion") != "cancelled":
        if job.get("conclusion") == "success":
            return result(False, "successful_job")
        # Preserve the existing treatment of source/policy guard failures.
        # Tightened evidence requirements apply to the cancellation exemption.
        if job.get("conclusion") == "failure":
            return result(False, "candidate_result")
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
    # Bors can cancel during setup, cache restoration, building or finalizers.
    # A clean host-service exit establishes runner health independently of
    # which workflow step happened to receive the cancellation.
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
            timestamp(job.get("started_at")) <= requested <= timestamp(job.get("completed_at"))
        ):
            return result(True, "cancellation_status_time_mismatch")
        for step in steps:
            if step.get("conclusion") != "failure":
                continue
            # This finalizer deliberately exits 1 for cancellation, including
            # before build/guard outputs exist. Its status writes do not prove
            # host health; the clean host receipt already does. Actual failed
            # infrastructure steps and host faults were handled above.
            if step.get("name") != REPORT or timestamp(step.get("started_at")) < requested:
                return result(True, "cancellation_unknown_step_failed", step_number=step.get("number"))
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
