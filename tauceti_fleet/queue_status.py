"""Merge-queue activity from the prepared public snapshot; no GitHub or worker calls."""

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ci_overview
from .store import FleetError

ORIGIN = "https://ci-data.taucetiproject.org"
DEFAULT_TIMEZONE = "Australia/Sydney"
MAX_BATCHES = 16


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
        if any(t["batch_version"] != row["version"] for t in row["active_tasks"]):
            raise ValueError("Stale queue task version")
        if row["latest_attempt"] is not None:
            attempt = row["latest_attempt"]
            if not isinstance(attempt, dict) or attempt.get("batch_id") != number:
                raise ValueError("Mismatched queue attempt")
            row["latest_attempt"] = {key: attempt.get(key) for key in
                                     ("status", "outcome", "conclusion", "started_at", "completed_at", "verdicts")}
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
    return dict(observed_at=queue["observed_at"], status_observed_at=queue.get("status_observed_at"),
                partial=queue.get("coverage") != "observed" or bool(queue.get("truncated")),
                batches=active, waiting=count(queue.get("counts", {}).get("waiting")),
                workers=workers, claims=claims,
                last_landing=max(landed, key=lambda b: timestamp(b["landed_at"])) if landed else None)


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


def refresh(manifest, previous=None):
    previous = previous or {}
    if previous.get("scope") != manifest.get("scope"):
        previous = {}
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
        return dict(value=value, scope=manifest.get("scope"), checked_at=time.time())
    except (FleetError, OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        return dict(previous, error="Queue refresh failed (" + type(error).__name__ + ")", checked_at=time.time())


def activity(batch, claims, names, now, timezone):
    tasks = batch.get("active_tasks") or []
    if tasks:
        current = tasks[0]
        claim = next((c for c in claims if c.get("task_id") == current["id"] and c.get("expires_at")
                      and timestamp(c["expires_at"]) > now), None)
        kind = {"prepare": "preparation", "pre_rebase": "pre-rebase", "repair": "repair",
                "rebase_conflict": "rebase repair"}.get(current["kind"], current["kind"])
        if not claim:
            return kind + " queued"
        label = names.get(claim.get("worker"), "worker") + ": " + kind
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
            label += "; " + progress["state"].replace("_", " ")
            if progress.get("observed_at"):
                label += " since " + clock(progress["observed_at"], timezone)
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
            label = ("publishing cache" if "publish" in low or "staged" in low else
                     "reporting results" if any(k in low for k in ("report", "terminal", "finaliz")) else
                     "building and auditing" if "build exact candidate" in low else phase[:100])
            result = "CI " + label
            if running[-1].get("started_at"):
                result += " since " + clock(running[-1]["started_at"], timezone)
            builds = [p for p in phases if "build exact candidate" in p.get("name", "").lower()
                      and p.get("completed_at")]
            if builds:
                verdict = (attempt.get("verdicts") or {}).get("build") or {}
                passed = verdict.get("bound") is True and verdict.get("state") == "success"
                result += "; " + ("build/audits passed " if passed else "build phase finished ") + clock(builds[-1]["completed_at"], timezone)
            return result
        status = attempt.get("status") or "pending"
        return "CI " + text(status, 40).replace("_", " ")
    label = "receiving PRs"
    previous = batch.get("previous_preparation")
    if previous:
        label += "; prepared " + clock(previous["resolved_at"], timezone)
        # Adoption may advance the batch by one itself; only a larger advance proves inflow.
        if batch["version"] > previous["batch_version"] + 1:
            label += ", then changed"
    if batch.get("history_unavailable"):
        label += "; preparation history unavailable"
    return label


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
        if now - observed > 180 or observed > now + 5:
            notes.append("STALE")
        if report.get("error"):
            notes.extend(("cached", report["error"]))
        print("Merge queue (" + "; ".join(notes) + "):")
        workers = value.get("workers") or []
        names = {w["id"]: f"worker {i + 1}" for i, w in enumerate(workers)}
        rows = []
        for batch in value["batches"]:
            role = "head" if batch["state"] == "head" else "sealed successor" if batch.get("sealed") else "unsealed"
            rows.append((batch["id"], str(batch["size"]), role,
                         activity(batch, value["claims"], names, now, timezone)))
        print("  Batch   PRs  State             Activity")
        for number, size, role, doing in rows:
            print(f"  {number:>5} {size:>5}  {role:<17} {doing}")
        if not rows:
            print("  No active batches observed" + (" (partial coverage)" if value.get("partial") else ""))
        if not any(b["state"] == "head" for b in value["batches"]):
            successor = next((b for b in value["batches"] if b.get("sealed")), None)
            if successor and successor["active_tasks"]:
                print("  Next head: batch " + successor["id"] + " after its active preparation releases.")
        print(f"  Waiting for placement: {value['waiting']} PRs.")
        if workers:
            print("  Workers: " + "; ".join(f"worker {i + 1} {w['state']}" for i, w in enumerate(workers)))
        last = value.get("last_landing")
        if last:
            print(f"  Last landing: batch {last['id']}, {last['size']} PRs at " + clock(last["landed_at"], timezone) + ".")
    except (ValueError, TypeError, KeyError, AttributeError):
        print("Merge queue detail: incompatible cached observation")
