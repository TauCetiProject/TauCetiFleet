"""Merge-queue activity from the prepared public snapshot; no GitHub or worker calls."""

import copy
import hashlib
import re
import socket
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ci_overview
from .store import FleetError, atomic, config_dir, read

ORIGIN = "https://ci-data.taucetiproject.org"
DEFAULT_TIMEZONE = "Australia/Sydney"
MAX_BATCHES = 16
# TauCetiTools pr-build runs each sandboxed phase as its own step; older runs had one step,
# "Build exact candidate under bwrap with workflow-pinned audits, offline", for them all.
SANDBOX_STEPS = (
    ("build exact candidate under bwrap with", "building and auditing"),
    ("build exact candidate under bwrap", "building"),
    ("audit the candidate under bwrap", "auditing duplicates and axioms"),
    ("check the module system under bwrap", "checking the module system"),
    ("environment lint under bwrap", "environment lint"),
    ("style lint under bwrap", "style lint"),
    ("record declaration names and stage cache outputs under bwrap", "recording declaration names"),
    ("build exact candidate", "building and auditing"),
)


# The live build reporter's phases (TauCetiTools ci_live_progress.py), as status names them.
REPORTED_PHASES = {
    "cache-unpack": "cache unpack", "build": "build", "audit-setup": "audit setup",
    "audit-import": "audit import", "audit-duplicates": "duplicate audit", "audit-axioms": "axiom audit",
    "audit-module-system": "module system", "lint-env": "environment lint", "lint-style": "style lint",
    "declaration-names": "record declaration names", "stage-cache": "stage cache", "end": "finished",
}
REPORTED = "untrusted_counters_trusted_reporter"
ESTIMATE_SOURCES = {"staging_build": "last staging run", "pr_build": "last PR build"}
MAX_JOB_PAGES = 5


def sandbox_step(name):
    """The status label of a sandboxed build step, or None for any other step."""
    low = name.lower()
    return next((label for key, label in SANDBOX_STEPS if key in low), None)


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("Missing queue observation time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Queue observation time has no timezone")
    return parsed.timestamp()


def clock(value, timezone=DEFAULT_TIMEZONE):
    return datetime.fromtimestamp(timestamp(value), ZoneInfo(timezone)).strftime("%d %b %H:%M %Z")


def text(value, maximum=200):
    if not isinstance(value, str) or len(value) > maximum or (value and not value.isprintable()):
        raise ValueError("Invalid public queue text")
    return value


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,20}", value):
        raise ValueError("Invalid queue identity")
    return value


def count(value):
    if type(value) is not int or value < 0:
        raise ValueError("Invalid queue count")
    return value


def object_url(manifest, reference):
    scope = manifest.get("scope")
    path = reference.get("url") if isinstance(reference, dict) else None
    if (not isinstance(scope, str) or not re.fullmatch(r"[a-f0-9]{64}", scope)
            or not isinstance(path, str)
            or not re.fullmatch(r"/v1/operations/objects/" + scope + r"/[a-f0-9]{64}\.json", path)):
        raise ValueError("Invalid public queue object URL")
    return ORIGIN + path


def parse(manifest, queue):
    if (manifest.get("schema") != "tauceti.operations.manifest/v1"
            or queue.get("schema") != "tauceti.operations.queue/v1"
            or not isinstance(queue.get("scope"), dict)
            or ci_overview.REPOSITORY not in queue["scope"].get("repositories", [])):
        raise ValueError("Invalid queue schema or scope")
    timestamp(queue.get("observed_at"))
    batches = queue.get("batches")
    if not isinstance(batches, list):
        raise ValueError("Queue batches are unavailable")
    seen = set()
    active, landed = [], []
    for original in batches:
        if original.get("repository") != ci_overview.REPOSITORY:
            continue
        number = identity(original.get("id"))
        if number in seen:
            raise ValueError("Duplicate queue batch")
        seen.add(number)
        state = original.get("state")
        if state not in ("head", "open", "landed", "removed"):
            raise ValueError("Invalid queue batch state")
        count(original.get("size"))
        count(original.get("version"))
        if original.get("sealed") is not None and type(original["sealed"]) is not bool:
            raise ValueError("Invalid membership seal")
        for key in ("sealed_at", "landed_at"):
            if original.get(key) is not None:
                timestamp(original[key])
        row = {key: original.get(key) for key in
               ("id", "size", "version", "state", "sealed", "sealed_at", "landed_at", "prepared_tip")}
        row["active_tasks"] = original.get("active_tasks") or []
        row["latest_attempt"] = original.get("latest_attempt")
        if not isinstance(row["active_tasks"], list):
            raise ValueError("Invalid queue tasks")
        # Keep only public, bounded task fields and numeric progress. Never cache source prompts.
        row["active_tasks"] = [task(t, number) for t in row["active_tasks"][:16]]
        # Insertion can advance membership before the controller refreshes an open task.
        # Its earlier version is useful progress, but a future version is inconsistent.
        if any(t["batch_version"] > row["version"] for t in row["active_tasks"]):
            raise ValueError("Future queue task version")
        if row["latest_attempt"] is not None:
            attempt = row["latest_attempt"]
            if not isinstance(attempt, dict) or attempt.get("batch_id") != number:
                raise ValueError("Mismatched queue attempt")
            row["latest_attempt"] = {key: attempt.get(key) for key in
                                     ("status", "outcome", "conclusion", "started_at", "completed_at", "verdicts")}
            if attempt.get("run_id") is not None:
                row["latest_attempt"]["run_id"] = identity(attempt["run_id"])
                row["latest_attempt"]["run_attempt"] = count(attempt.get("run_attempt", 1))
            row["latest_attempt"]["phases"] = []
            for phase in (attempt.get("phases") or [])[:100]:
                row["latest_attempt"]["phases"].append(dict(name=text(phase["name"], 500),
                                                            status=text(phase["status"], 40),
                                                            started_at=phase.get("started_at"),
                                                            completed_at=phase.get("completed_at")))
        if state in ("head", "open"):
            active.append(row)
        elif state == "landed" and row["landed_at"]:
            landed.append(row)
    if len(active) > MAX_BATCHES or sum(b["state"] == "head" for b in active) > 1:
        raise ValueError("Queue batch bounds exceeded")
    active.sort(key=lambda b: (b["state"] != "head", int(b["id"])))
    claims = []
    for c in queue.get("claims", [])[:100]:
        claims.append({key: c.get(key) for key in ("batch_id", "task_id", "worker", "expires_at", "created_at")})
    workers = []
    for w in queue.get("workers", [])[:32]:
        workers.append(dict(id=text(w["id"], 80), state=text(w.get("state", "unknown"), 80),
                            observed_at=w.get("observed_at")))
    received = queue.get("received_at")
    if received is not None:
        timestamp(received)
    return dict(observed_at=queue["observed_at"], status_observed_at=queue.get("status_observed_at"),
                received_at=received,
                partial=queue.get("coverage") != "observed" or bool(queue.get("truncated")),
                batches=active, waiting=count(queue.get("counts", {}).get("waiting")),
                workers=workers, claims=claims, stuck_heads=stuck_heads(queue.get("conditions")),
                progress_alerts=progress_alerts(queue.get("conditions")),
                last_landing=max(landed, key=lambda b: timestamp(b["landed_at"])) if landed else None)


def stuck_heads(conditions):
    """The CI recovery pass's stuck heads, as the queue publishes them: one `stuck_head` condition per
    listed head (since its last push) after `stuck_heads_observed`. None when the queue reported none."""
    if not isinstance(conditions, list):
        return None
    kinds = [c.get("kind") for c in conditions if isinstance(c, dict)]
    if "stuck_heads_observed" not in kinds:
        return None
    pushes = [timestamp(c["since"]) for c in conditions if isinstance(c, dict) and c.get("kind") == "stuck_head"]
    return dict(count=len(pushes), truncated="stuck_heads_truncated" in kinds,
                partial="stuck_heads_partial" in kinds, oldest=min(pushes) if pushes else None)


PROGRESS_KINDS = {"repair_stalled": "repair stalled", "head_no_progress": "head made no progress"}


def progress_alerts(conditions):
    """The queue's progress alerts: a head repair that keeps retaining the same tip with the same
    findings (`repair_stalled`) and a head without progress (`head_no_progress`), each with its batch
    and start. The findings and the agent's conclusion stay in the queue's own status."""
    if not isinstance(conditions, list):
        return []
    found = []
    for condition in conditions[:100]:
        kind = condition.get("kind") if isinstance(condition, dict) else None
        if isinstance(kind, str) and kind in PROGRESS_KINDS:
            timestamp(condition.get("since"))
            found.append(dict(kind=kind, batch_id=identity(condition.get("batch_id")), since=condition["since"]))
    return found


def task(original, batch_id):
    if not isinstance(original, dict) or original.get("batch_id") != batch_id:
        raise ValueError("Mismatched queue task")
    result = {key: original.get(key) for key in
              ("id", "batch_id", "batch_version", "kind", "state", "resolution", "created_at", "resolved_at")}
    for key in ("created_at", "resolved_at"):
        if result[key] is not None:
            timestamp(result[key])
    identity(result["id"])
    count(result["batch_version"])
    for key in ("kind", "state", "resolution"):
        text(result[key], 40)
    progress = original.get("progress") or {}
    result["progress"] = {key: progress.get(key) for key in ("coverage", "state", "observed_at")}
    if progress.get("observed_at") is not None:
        timestamp(progress["observed_at"])
    if progress.get("state") is not None:
        text(progress["state"], 40)
    build = progress.get("build")
    if isinstance(build, dict):
        if build.get("stage") not in ("preparation", "verification", "streaming"):
            raise ValueError("Invalid build stage")
        for key in ("completed_tasks", "total_tasks"):
            if build.get(key) is not None:
                count(build[key])
        for key in ("sampled_at", "last_progress_at", "log_updated_at"):
            if build.get(key) is not None:
                timestamp(build[key])
        result["progress"]["build"] = {key: build.get(key) for key in
                                      ("stage", "completed_tasks", "total_tasks", "sampled_at", "last_progress_at", "log_updated_at")}
    return result


def reported_progress(value):
    """The bounded numeric fields of a public job's live build report, or None without one."""
    if not isinstance(value, dict) or value.get("coverage") != REPORTED:
        return None
    timestamp(value.get("observed_at"))
    phase = value.get("phase")
    result = dict(observed_at=value["observed_at"], phase=phase if phase in REPORTED_PHASES else None,
                  completed_tasks=None, total_tasks=None, tasks_per_minute=None, completed_modules=None,
                  estimated_total_tasks=None, estimate_source=None)
    modules = value.get("completed_modules")
    if type(modules) is int and 0 <= modules <= 10_000_000:
        result["completed_modules"] = modules
    done, total = value.get("completed_tasks"), value.get("total_tasks")
    if done is not None and total is not None and count(done) <= count(total) and total > 0:
        result.update(completed_tasks=done, total_tasks=total)
    rate = value.get("tasks_per_minute")
    if type(rate) in (int, float) and 0 <= rate <= 10_000_000:
        result["tasks_per_minute"] = float(rate)
    # The receiver's estimate of the final count: Lake's own total grows as it discovers jobs.
    estimate, source = value.get("estimated_total_tasks"), value.get("estimate_source")
    if type(estimate) is int and 0 < estimate <= 10_000_000:
        result["estimated_total_tasks"] = estimate
        kind = source.get("job_class") if isinstance(source, dict) else None
        result["estimate_source"] = ESTIMATE_SOURCES.get(kind, "last build")
    return result


def build_progress(manifest, attempt):
    """The head attempt's live build report from the first public jobs pages that hold it."""
    from . import pool_activity

    root = ci_overview.fetch(pool_activity.jobs_url(manifest), timeout=3)
    found = []
    for reference in (root.get("job_pages") or [])[:MAX_JOB_PAGES]:
        records = ci_overview.fetch(object_url(manifest, reference), timeout=3).get("records") or []
        for job in records:
            if (isinstance(job, dict) and str(job.get("run_id")) == attempt["run_id"]
                    and job.get("run_attempt", 1) == attempt.get("run_attempt", 1)):
                progress = reported_progress(job.get("progress"))
                if progress:
                    found.append(progress)
        if found:
            break
    return max(found, key=lambda p: timestamp(p["observed_at"])) if found else None


COUNT_FIELDS = ("completed_tasks", "total_tasks", "tasks_per_minute", "estimated_total_tasks",
                "estimate_source", "completed_modules")


def carried_progress(progress, earlier, attempt):
    """The head run's latest report, keeping the latest known counts when it has none or older ones.

    A lookup can miss the run (a failed fetch, a job page published without its report) and a
    report can lack counts; then the newest counts already seen for the same run attempt (in any
    earlier snapshot) carry forward with the time they were reported, so status says how old they
    are instead of dropping the line or going back to older counts."""
    run = (attempt.get("run_id"), attempt.get("run_attempt", 1))

    def counted_at(value):
        if (not isinstance(value, dict) or (value.get("run_id"), value.get("run_attempt", 1)) != run
                or value.get("completed_tasks") is None):
            return None
        try:
            return timestamp(value.get("counts_observed_at"))
        except (ValueError, TypeError):
            return None

    def reported_at(value):
        if not isinstance(value, dict) or (value.get("run_id"), value.get("run_attempt", 1)) != run:
            return None
        try:
            return timestamp(value.get("observed_at"))
        except (ValueError, TypeError):
            return None

    if progress and progress.get("run_id") is None:
        progress = dict(progress, run_id=run[0], run_attempt=run[1])
        if progress.get("completed_tasks") is not None:
            progress["counts_observed_at"] = progress["observed_at"]
    values = [progress, *(earlier or [])]
    counted = [(counted_at(v), v) for v in values if counted_at(v) is not None]
    reports = [(reported_at(v), v) for v in values if reported_at(v) is not None]
    if not reports:
        return progress
    # The newest report's phase and time, with the newest counts seen for this run attempt.
    latest = max(reports, key=lambda item: item[0])[1]
    if not counted:
        return dict(latest)
    newest = max(counted, key=lambda item: item[0])[1]
    return dict(latest, **{key: newest.get(key) for key in COUNT_FIELDS},
                counts_observed_at=newest["counts_observed_at"])


def retain_progress(value, candidates, fresh=None):
    """Each running head's build progress: its fresh report (when looked up) merged with the
    newest counts the candidate snapshots hold for the same batch and run attempt."""
    for batch in value.get("batches") or []:
        attempt = batch.get("latest_attempt") or {}
        if batch.get("state") != "head" or attempt.get("status") != "in_progress" or not attempt.get("run_id"):
            continue
        earlier = [b.get("build_progress") for c in candidates for b in (c.get("value") or {}).get("batches") or []
                   if isinstance(b, dict) and b.get("id") == batch["id"]]
        own = fresh(batch, attempt) if fresh else batch.get("build_progress")
        progress = carried_progress(own, earlier, attempt)
        if progress:
            batch["build_progress"] = progress
        else:
            batch.pop("build_progress", None)
    return value


def progress_text(progress, timezone=DEFAULT_TIMEZONE, now=None):
    """"build: phase build, 44,180 jobs done, ~54% of ~81.8k jobs (from last PR build), 905/min,
    ETA ~19 Nov 20:10 AEDT". Lake's own total grows as it discovers jobs, so the percentage and ETA
    use the receiver's estimate (the latest finished build's final count) and are marked as estimates;
    without one only the done count and rate are shown."""
    now = time.time() if now is None else now
    parts = []
    if progress.get("phase"):
        parts.append("phase " + REPORTED_PHASES[progress["phase"]])
    done, total = progress.get("completed_tasks"), progress.get("total_tasks")
    counted = progress.get("counts_observed_at") or progress["observed_at"]
    if done is not None and total:
        parts.append(f"{done:,} jobs done")
        if now - timestamp(counted) > 120:
            parts[-1] += f" as of {int(now - timestamp(counted)) // 60} min ago"
        building = progress.get("phase") == "build"
        estimate = progress.get("estimated_total_tasks")
        # An estimate Lake's own count has reached is no longer an estimate of the final count.
        remaining = estimate - done if building and estimate and estimate > total else 0
        if remaining > 0:
            parts.append(f"~{100 * done / estimate:.0f}% of ~{estimate / 1000:.1f}k jobs "
                         f"(from {progress.get('estimate_source') or 'last build'})")
        rate = progress.get("tasks_per_minute")
        if rate is not None and building:
            parts.append(f"{rate:,.0f}/min")
            if remaining > 0:
                try:
                    eta = timestamp(counted) + remaining * 60 / rate
                    parts.append("ETA ~" + datetime.fromtimestamp(eta, ZoneInfo(timezone)).strftime("%d %b %H:%M %Z"))
                except (ZeroDivisionError, OverflowError, OSError, ValueError):
                    pass  # No usable ETA from this rate; the counters stand alone.
    elif progress.get("completed_modules") and progress.get("phase") == "build":
        # Reporters before Lake's job counter count only built TauCeti modules, with no total.
        parts.append(f"{progress['completed_modules']:,} TauCeti modules built")
    if not parts:
        return None
    age = now - timestamp(progress["observed_at"])
    # Counts already say their age; the report's own age matters when it is newer than they are.
    if age > 120 and not (done is not None and total and counted == progress["observed_at"]):
        parts.append(f"reported {int(age) // 60}m ago")
    return "build: " + ", ".join(parts)


def cache_path():
    return config_dir() / "status-cache" / "merge-queue.json"


def cached():
    try:
        value = read(cache_path(), {})
        return value if isinstance(value, dict) else {}
    except FleetError:
        return dict(error="Merge queue cache is unreadable")


def refresh(manifest, previous=None):
    # Older status clients still rewrite pool-activity.json without queue fields. Keep this
    # observation separately so an old follow process cannot erase a newer client's facts.
    saved = cached()
    # Either snapshot may hold the head's latest counts, whichever was saved last.
    candidates = [snapshot for snapshot in (previous, saved) if isinstance(snapshot, dict)]
    if saved.get("value") and (not previous or saved.get("checked_at", 0) >= previous.get("checked_at", 0)):
        previous = saved
    report = _refresh(manifest, previous, candidates)
    try:
        atomic(cache_path(), report)
    except (OSError, ValueError):
        report["error"] = "Merge queue cache could not be saved"
    return report


def _refresh(manifest, previous=None, candidates=None):
    previous = previous or {}
    if previous.get("scope") != manifest.get("scope"):
        previous = {}
    candidates = [c for c in (candidates if candidates is not None else [previous])
                  if c.get("scope") == manifest.get("scope")]
    try:
        root = ci_overview.fetch(object_url(manifest, manifest.get("domains", {}).get("queue")), timeout=3)
        value = parse(manifest, root)
        originals = {b["id"]: b for b in root["batches"]}
        # The root already contains active tasks and the latest CI attempt. Read just the last
        # task page for an otherwise idle open batch to distinguish prior preparation from none.
        for batch in value["batches"]:
            if batch["state"] != "open" or batch["active_tasks"]:
                continue
            try:
                detail = ci_overview.fetch(object_url(manifest, originals[batch["id"]].get("detail")), timeout=3)
                if detail.get("batch", {}).get("id") != batch["id"]:
                    raise ValueError("Mismatched batch detail")
                pages = detail.get("task_pages") or []
                if pages:
                    page = ci_overview.fetch(object_url(manifest, pages[-1]), timeout=3)
                    records = [task(t, batch["id"]) for t in page.get("records", [])[:100]]
                    settled = [t for t in records if t["kind"] == "prepare" and t["state"] == "submitted"
                               and t["resolution"] in ("adopted", "prepared") and t["resolved_at"]]
                    if settled:
                        batch["previous_preparation"] = max(settled, key=lambda t: int(t["id"]))
            except (FleetError, OSError, ValueError, TypeError, KeyError, AttributeError):
                batch["history_unavailable"] = True
        def fresh(_batch, attempt):
            # Live progress is optional: without it the head shows its running step alone.
            try:
                return build_progress(manifest, attempt)
            except (FleetError, OSError, ValueError, TypeError, KeyError, AttributeError):
                return None

        retain_progress(value, candidates, fresh)
        return dict(value=value, scope=manifest.get("scope"), checked_at=time.time())
    except (FleetError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        report = dict(previous, error="Queue refresh failed (" + type(error).__name__ + ")", checked_at=time.time())
        if isinstance(previous.get("value"), dict):
            # The fallback snapshot keeps the newest counts any candidate snapshot holds.
            report["value"] = retain_progress(copy.deepcopy(previous["value"]), candidates)
        return report


def activity(batch, claims, names, now, timezone):
    tasks = batch.get("active_tasks") or []
    if tasks:
        current = tasks[0]
        claim = next((c for c in claims if c.get("task_id") == current["id"] and c.get("expires_at")
                      and timestamp(c["expires_at"]) > now), None)
        kind = {"prepare": "preparation", "pre_rebase": "pre-rebase", "repair": "repair",
                "rebase_conflict": "rebase repair"}.get(current["kind"], current["kind"])
        earlier_membership = current["batch_version"] < batch["version"]
        if not claim:
            if earlier_membership:
                return "membership changed; awaiting task refresh"
            return kind + " queued"
        label = names.get(claim.get("worker"), "worker") + ": " + kind
        if earlier_membership:
            label += " (earlier membership)"
        progress = current.get("progress") or {}
        if (progress.get("observed_at") and claim.get("created_at")
                and timestamp(progress["observed_at"]) < timestamp(claim["created_at"])):
            return label + "; awaiting fresh worker progress"
        build = progress.get("build")
        if build:
            completed, total = build.get("completed_tasks"), build.get("total_tasks")
            if completed is not None:
                label += f"; Lake {completed:,}" + (f"/{total:,}" if total is not None else "") + " tasks"
            if build.get("last_progress_at"):
                label += "; advanced " + clock(build["last_progress_at"], timezone)
        elif progress.get("state"):
            phase = progress["state"]
            label += "; " + {"freezing_cache_outputs":"freezing cache outputs", "audit_import":"import audit", "audit_duplicates":"duplicate audit", "audit_axioms":"axiom audit", "environment_lint":"environment lint"}.get(phase,phase.replace("_"," "))
            if progress.get("observed_at"):
                checkpoint = phase in ("artifacts_ready","sources_ready","agent_running","verifying")
                label += ("; checkpoint " if checkpoint else " since ") + clock(progress["observed_at"], timezone)
        return label
    attempt = batch.get("latest_attempt")
    if batch["state"] == "head":
        if not attempt:
            return "awaiting CI dispatch"
        if attempt.get("outcome") == "pass":
            return "CI passed; awaiting landing"
        phases = attempt.get("phases") or []
        running = [p for p in phases if p.get("status") in ("in_progress", "running")]
        if running:
            phase = text(running[-1].get("name", "CI"), 500)
            low = phase.lower()
            sandbox = sandbox_step(phase)
            label = (sandbox if sandbox else
                     "publishing cache" if "publish" in low or "staged" in low else
                     "reporting results" if any(k in low for k in ("report", "terminal", "finaliz")) else
                     phase[:100])
            result = "CI " + label
            if running[-1].get("started_at"):
                result += " since " + clock(running[-1]["started_at"], timezone)
            builds = [p for p in phases if sandbox_step(p.get("name", "")) and p.get("completed_at")]
            # While a sandboxed step runs its label says which; afterwards, when the last one finished.
            if builds and not sandbox:
                verdict = (attempt.get("verdicts") or {}).get("build") or {}
                passed = verdict.get("bound") is True and verdict.get("state") == "success"
                result += "; " + ("build/audits passed " if passed else "build phase finished ") + clock(builds[-1]["completed_at"], timezone)
            return result
        status = attempt.get("status") or "pending"
        return "CI " + text(status, 40).replace("_", " ")
    label = "membership fixed; waiting" if batch.get("sealed") else "receiving PRs"
    previous = batch.get("previous_preparation")
    if previous:
        label += "; prepared " + clock(previous["resolved_at"], timezone)
        # Adoption may advance the batch by one itself; only a larger advance proves inflow.
        if batch["version"] > previous["batch_version"] + 1:
            label += ", then changed"
    if batch.get("history_unavailable"):
        label += "; preparation history unavailable"
    return label


def worker_names(workers, inventory=None):
    """Resolve public fingerprints using this operator's existing host inventory."""
    from . import remote

    inventory = remote.inventory() if inventory is None else inventory
    names = {w["id"]: f"worker {i + 1}" for i, w in enumerate(workers)}
    for name, entry in inventory.items():
        hosts = {name, *entry.get("aliases", [])}
        target = entry.get("target", "")
        hosts.add(socket.gethostname().split(".")[0] if target == "local" else target.rsplit("@",1)[-1].split(".")[0])
        for host in hosts:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", host):
                continue
            for number in range(1,33):
                identity = "repair" if number == 1 else f"repair-{number}"
                alias = "queue-worker-" + hashlib.sha256(f"{identity}@{host}".encode()).hexdigest()[:12]
                if alias in names:
                    names[alias] = f"{identity}@{host}"
    return names


PUBLICATION_STALE_SECONDS = 300
OBSERVATION_STALE_SECONDS = 420
ROLES = ("head", "next head", "sealed, waiting", "unsealed")


def processing_order(batches):
    """(id, batch, role) in the order the queue will land them: head, next head, sealed, unsealed."""
    sealed_waiting = sorted((b for b in batches if b["state"] != "head" and b.get("sealed")),
                            key=lambda b: int(b["id"]))
    next_head = sealed_waiting[0]["id"] if sealed_waiting else None
    rows = []
    for batch in batches:
        role = ("head" if batch["state"] == "head" else
                "next head" if batch["id"] == next_head else
                "sealed, waiting" if batch.get("sealed") else "unsealed")
        rows.append((batch["id"], batch, role))
    return sorted(rows, key=lambda row: (ROLES.index(row[2]), int(row[0])))


def display(report, *, now=None, timezone=DEFAULT_TIMEZONE):
    now = time.time() if now is None else now
    value = report.get("value") if isinstance(report, dict) else None
    if not value:
        print("Merge queue detail: unavailable" + ("; " + report["error"] if report and report.get("error") else ""))
        return
    try:
        observed = timestamp(value["observed_at"])
        notes = [clock(value["observed_at"], timezone), f"{max(0, int(now - observed))}s old"]
        if value.get("partial"):
            notes.append("partial")
        # The publisher takes about two minutes per cycle, so observations arrive about 90s old.
        # Staleness is a publication that stopped arriving, not the observation's own age.
        received = timestamp(value["received_at"]) if value.get("received_at") else None
        if (now - received > PUBLICATION_STALE_SECONDS if received is not None
                else now - observed > OBSERVATION_STALE_SECONDS) or observed > now + 5:
            notes.append("STALE")
        if report.get("error"):
            notes.extend(("cached", report["error"]))
        print("Merge queue (" + "; ".join(notes) + "):")
        workers = value.get("workers") or []
        names = worker_names(workers)
        rows = [(number, batch["size"], role, activity(batch, value["claims"], names, now, timezone))
                for number, batch, role in processing_order(value["batches"])]
        print("  Batch   PRs  State             Activity")
        for number, size, role, doing in rows:
            print(f"  {number:>5} {size:>5}  {role:<17} {doing}")
            batch = next(b for b in value["batches"] if b["id"] == number)
            progress = batch.get("build_progress")
            attempt = batch.get("latest_attempt") or {}
            if progress and attempt.get("status") == "in_progress" and attempt.get("outcome") != "pass":
                line = progress_text(progress, timezone, now)
                if line:
                    print(" " * 33 + line)
        if not rows:
            print("  No active batches observed" + (" (partial coverage)" if value.get("partial") else ""))
        if not any(b["state"] == "head" for b in value["batches"]):
            successor = next((b for b in value["batches"] if b.get("sealed")), None)
            if successor and successor["active_tasks"]:
                print("  Next head: batch " + successor["id"] + " after its active preparation releases.")
        sizes = [size for _, size, _, _ in rows]
        placed = f"{sum(sizes)} PRs in batches" + (f" ({'+'.join(map(str, sizes))})" if len(sizes) > 1 else "")
        print(f"  {placed}; {value['waiting']} waiting for placement.")
        stuck = value.get("stuck_heads")
        if stuck:
            line = f"  Stuck heads: {stuck['count']}{'+' if stuck['truncated'] or stuck.get('partial') else ''}"
            if stuck["oldest"] is not None:
                line += f", oldest {max(0, int(now - stuck['oldest'])) // 3600}h"
            line += " (no final state and, 3h after the last push, no CI or only interrupted builds"
            print(line + ("; partial pass)." if stuck.get("partial") else ")."))
        for alert in value.get("progress_alerts") or []:
            print(f"  Alert: batch {alert['batch_id']} {PROGRESS_KINDS[alert['kind']]} since "
                  + clock(alert["since"], timezone) + " (see the queue's status alerts).")
        visible = [w for w in workers if w.get("state") not in ("stopped","unknown") and w.get("coverage") != "unavailable"]
        if visible:
            print("  Workers: " + "; ".join(f"{names[w['id']]} {w['state']}" for w in visible))
        last = value.get("last_landing")
        if last:
            print(f"  Last landing: batch {last['id']}, {last['size']} PRs at " + clock(last["landed_at"], timezone) + ".")
    except (ValueError, TypeError, KeyError, AttributeError):
        print("Merge queue detail: incompatible cached observation")
