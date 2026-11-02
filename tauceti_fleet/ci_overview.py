"""Prepared CI observations. Foreground status reads this cache only."""

import concurrent.futures
import datetime
import fcntl
import hashlib
import heapq
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import github, bors_history as history_jobs
from .store import FleetError, atomic, read, state_dir

REPOSITORY = "TauCetiProject/TauCeti"
INTERVAL = 300
# Operator-confirmed uncancellable GitHub runs from 2026-10-07. Keep this a
# fixed cohort boundary: a rolling window could hide genuinely delayed new work.
QUEUED_RUNS_CREATED_SINCE = "2026-10-08T00:00:00Z"
POOL_NAMES = {
    "github": "GitHub", "namespace": "Namespace",
    "fleet-4fab2fed9a51": "Chris", "fleet-0fb9966f7c40": "Brett",
    "fleet-11e2756fc110": "Tauchonki", "fleet-a26dbb028408": "Yaël",
}


def unit_files(command, environment=None):
    service = ("[Unit]\nDescription=Prepare TauCeti CI status observations\n\n[Service]\n"
               "Type=oneshot\nExecStart=" + command + "\nTimeoutStartSec=60\n"
               "Nice=10\nUMask=0077\nMemoryMax=256M\n")
    service += "".join("Environment=" + json.dumps((k + "=" + str(v)).replace("%", "%%")) + "\n"
                       for k, v in (environment or {}).items())
    timer = ("[Unit]\nDescription=Refresh cached TauCeti CI status\n\n[Timer]\n"
             "OnBootSec=45\nOnUnitInactiveSec=300\nRandomizedDelaySec=30\n"
             "Unit=tauceti-fleet-observe.service\n\n[Install]\nWantedBy=timers.target\n")
    return {"tauceti-fleet-observe.service": service, "tauceti-fleet-observe.timer": timer}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, *, token_file=None):
    headers = {"Accept": "application/json", "User-Agent": "TauCetiFleetStatus/1.0"}
    if token_file:
        token = Path(token_file).expanduser().read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise FleetError("Invalid status credential")
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise FleetError("Status response exceeds limit")
    return json.loads(raw)


def bors(config):
    value = fetch("https://bors.taucetiproject.org/repositories/1/active-batches?base=main")
    if (value.get("schema") != "tauceti-bors.observation/v1" or value.get("repo") != REPOSITORY
            or value.get("base") != "main" or not isinstance(value.get("batches"), list)):
        raise FleetError("Invalid Bors observation")
    batches = []
    for batch in value["batches"]:
        if (type(batch.get("id")) is not int or not isinstance(batch.get("state"), str)
                or not re.fullmatch(r"[a-z_]{1,32}", batch["state"]) or not isinstance(batch.get("members"), list)):
            raise FleetError("Invalid Bors batch")
        batches.append(dict(id=batch["id"], state=batch["state"], size=len(batch["members"])))
    held = value.get("held")
    return dict(batches=batches, held=len(held) if isinstance(held, list) else None), time.time()


def bors_history(config):
    value = fetch("https://bors.taucetiproject.org/repositories/1/statistics?base=main")
    if (value.get("schema") != "tauceti-bors.statistics/v1" or value.get("repo") != REPOSITORY
            or value.get("base") != "main" or value.get("queue_authority") is not False):
        raise FleetError("Invalid Bors history observation")
    data = dict(windows=value.get("windows"), durations=[])
    observed = value.get("observed_at")
    if source_value("bors_history", dict(value=data, observed_at=observed)) is None:
        raise FleetError("Invalid Bors history metrics")
    try:
        runtime = history_jobs.collect(config, value.get("staging_run_ids", []), fetch,
                                       allow_reads=bool(config.get("ci_enabled") or config.get("workers_enabled")))
        data["durations"] = runtime["windows"]
        data["runtime_runs_total"] = runtime["runs_total"]
        data["runtime_runs_checked"] = runtime["runs_checked"]
    except Exception as error:
        data["durations_error"] = str(error)[:200] if isinstance(error, FleetError) else type(error).__name__
    observed = value.get("observed_at")
    if source_value("bors_history", dict(value=data, observed_at=observed)) is None:
        raise FleetError("Invalid Bors history metrics")
    return data, observed


def pools(config):
    base = config.get("github_state_url", "https://github-state.taucetiproject.org").rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise FleetError("Status source requires HTTPS")
    # A distinct path fails cheaply on old servers; an ignored query parameter
    # on the discovery endpoint could otherwise accidentally trigger a scan.
    value = fetch(base + "/v1/runner-capacity/cached?" + urllib.parse.urlencode(dict(repository=REPOSITORY)),
                  token_file=config.get("github_state_token_file"))
    counts = value.get("running_jobs")
    if value.get("read_only") is not True or value.get("complete") is not True or not isinstance(counts, dict):
        raise FleetError("Prepared running-job inventory unavailable")
    if any(type(n) is not int or n < 0 for n in counts.values()):
        raise FleetError("Invalid running-job counts")
    observed = value.get("observed_at")
    if type(observed) not in (int, float) or observed <= 0 or observed > time.time() * 1000 + 5000:
        raise FleetError("Invalid running-job timestamp")
    return dict(counts=counts, scope="organization_all_workflow_jobs"), observed / 1000


def backlog(config):
    token_file = config.get("github_token_file") or config.get("ci_token_file")
    if not token_file:
        raise FleetError("No direct read credential for queued-run counts")
    counts = {}
    query = "?status=queued&per_page=1&created=" + urllib.parse.quote(
        ">=" + QUEUED_RUNS_CREATED_SINCE, safe="")
    for name, path in (("pr_builds", "actions/workflows/pr-build.yml/runs"), ("all_workflows", "actions/runs")):
        value = github.request("repos/" + REPOSITORY + "/" + path + query, token_file=token_file)
        count = value.get("total_count")
        if type(count) is not int or count < 0:
            raise FleetError("Invalid queued-run count")
        counts[name] = count
        if name == "all_workflows" and count:
            # GitHub returns newest first and caps filtered results at 1,000.
            # Fetch just the final entry, never walk the queue or scan job APIs.
            oldest = value
            if count > 1000:
                counts["oldest_unavailable"] = "GitHub's 1,000-run result limit"
                continue
            if count > 1:
                oldest = github.request("repos/" + REPOSITORY + "/" + path
                                        + query + f"&page={count}", token_file=token_file)
            if oldest.get("total_count") != count:
                counts["oldest_unavailable"] = "queue changed during observation"
                continue
            try:
                created = oldest["workflow_runs"][0]["created_at"]
                stamp = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
                if stamp.tzinfo is None or not 0 < stamp.timestamp() <= time.time() + 5:
                    raise ValueError("Invalid creation timestamp")
                counts["oldest_created_at"] = stamp.timestamp()
            except (KeyError, IndexError, TypeError, ValueError):
                counts["oldest_unavailable"] = "queued-run creation time unavailable"
    counts["created_since"] = QUEUED_RUNS_CREATED_SINCE
    return counts, time.time()


def worker_policies(config):
    if not config.get("workers_enabled"):
        return []
    account = config.get("github_account", "tauceti-ai-for-science")
    fallback_limit = config.get("max_open_prs", 128 if config.get("profile") == "ai-for-science" else 8)
    policies = set()
    prefix = "fleet-" + config["machine_id"][:8] + "-"
    for path in (state_dir() / "workers").glob("*.json"):
        value = read(path, {})
        if not str(value.get("id", "")).startswith(prefix) or not value.get("alive"):
            continue
        spec = value.get("applied_spec") or {}
        if value.get("github_account", spec.get("github_account", account)) != account:
            continue
        scope = spec.get("roadmap_only")
        scope = "auto" if scope is None else scope or "any"
        skip = spec.get("roadmap_skip") or []
        skip = skip.split(",") if isinstance(skip, str) else skip
        limit = value.get("max_open_prs", spec.get("max_open_prs", fallback_limit))
        if type(limit) is int and limit > 0:
            policies.add((scope, tuple(sorted(set(skip))), limit))
    if not policies:
        raise FleetError("No live Fleet worker policy available")
    focus = config.get("roadmap_focus") or {}
    if focus.get("percent", 0) > 0:
        for scope, skip, limit in list(policies):
            if scope in ("auto", "any"):
                policies.update((area, skip, limit) for area in focus.get("areas", []))
    return sorted(policies)


def pressure_row(path, identity, account, policies):
    if path.stat().st_size > 8 * 1024 * 1024:
        return []
    cached = read(path, {})
    query, payload = cached.get("query", {}), cached.get("payload", {})
    args = query.get("args", {})
    if (query.get("identity") != identity or query.get("repository") != REPOSITORY
            or query.get("operation") != "survey" or args.get("numbers")
            or payload.get("status") != "ok" or payload.get("complete") is not True or payload.get("cursor")):
        return []
    scope, skip = args.get("roadmap", "any") or "any", args.get("skip", "")
    skipped = tuple(sorted(set(filter(None, skip.split(",")))))
    limits = {limit for area, excluded, limit in policies if area == scope and excluded == skipped}
    if not limits:
        return []
    if scope in ("any", "auto"):
        counts = payload.get("author_counts")
        if not isinstance(counts, dict):
            return []
        count = counts.get(account, 0)
    else:
        rows = payload.get("data")
        if not isinstance(rows, list):
            return []
        count = sum(not row["pr"].get("isDraft") and row["pr"].get("author", {}).get("login") == account for row in rows)
    if type(count) is not int or count < 0:
        return []
    return [dict(scope=scope, skip=skip, count=count, limit=limit, blocked=count >= limit,
                 observed_at=path.stat().st_mtime) for limit in limits]


def pressure(config):
    """Reuse complete survey responses for this machine's live Fleet workers."""
    policies = worker_policies(config)
    if not policies:
        raise FleetError("No Fleet worker role configured")
    account = config.get("github_account", "tauceti-ai-for-science")
    base = config.get("github_state_url", "https://github-state.taucetiproject.org").rstrip("/")
    token_file = config.get("github_state_token_file")
    token = Path(token_file).expanduser().read_text().strip() if token_file else ""
    identity = hashlib.sha256((base + "\0" + token).encode()).hexdigest()
    root = Path(os.environ.get("TAUCETI_GITHUB_SHARED_DIR", Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "tauceti/github-shared"))
    directory = root / "gateway" / identity / "responses"
    scopes = {}
    deadline = time.monotonic() + 5

    def observe(path):
        try:
            for row in pressure_row(Path(path), identity, account, policies):
                key = (row["scope"], row["skip"], row["limit"])
                if row["observed_at"] > scopes.get(key, {}).get("observed_at", -1):
                    scopes[key] = row
        except (OSError, FleetError, KeyError, TypeError, ValueError):
            pass

    # Current automatic survey shape is a lookup hint, not a policy dependency:
    # an older/newer worker's stage list is covered by the bounded fallback scan.
    stages = "bump,fix,fix-ci,lint-repair,overlap-fix,overlap-scan,progress,rebase,review"
    for scope, skip, _ in policies:
        args = dict(roadmap=scope, skip=",".join(skip), numbers="", stages=stages)
        key = hashlib.sha256(json.dumps([REPOSITORY, "survey", args, False], sort_keys=True).encode()).hexdigest()
        observe(directory / (key + ".json"))
    if len(scopes) < len(policies):
        newest = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if time.monotonic() > deadline:
                    break
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    if stat.st_size <= 8 * 1024 * 1024:
                        heapq.heappush(newest, (stat.st_mtime, entry.path))
                        if len(newest) > 256:
                            heapq.heappop(newest)
        for _, path in sorted(newest, reverse=True):
            if time.monotonic() > deadline:
                break
            observe(path)
    if not scopes:
        raise FleetError("No complete cached survey for this machine's Fleet worker scopes")
    rows = list(scopes.values())
    return dict(account=account, machine_id=config["machine_id"], scopes=rows), max(r["observed_at"] for r in rows)


def cached(config=None):
    """Small, bounded file read; never starts a collector or makes API calls."""
    try:
        path = state_dir() / "ci-overview.json"
        if path.stat().st_size > 256 * 1024:
            raise FleetError("Oversized status cache")
        value = read(path, {})
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("repository") != REPOSITORY or not isinstance(value.get("sources"), dict):
            raise FleetError("Invalid status cache")
        if any(not isinstance(source, dict) for source in value["sources"].values()):
            raise FleetError("Invalid status source")
        return value
    except (OSError, FleetError, ValueError, TypeError):
        return dict(version=1, repository=REPOSITORY, sources={}, available=False)


def collect(config):
    """At most one attempt per five minutes, even after failures or restarts."""
    state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state_dir() / "ci-overview.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return cached(config)
        attempt = state_dir() / "ci-overview-attempt.json"
        now = time.time()
        try:
            retry = read(attempt, {}).get("next_attempt_at", 0)
        except (FleetError, AttributeError):
            retry = 0
        if type(retry) in (int, float) and math.isfinite(retry) and now < retry <= now + INTERVAL:
            return cached(config)
        atomic(attempt, dict(at=now, next_attempt_at=now + INTERVAL))
        value = cached(config)
        sources = dict(value.get("sources", {}))
        def save():
            snapshot = dict(version=1, repository=REPOSITORY, sources=dict(sources), prepared_at=time.time(), next_attempt_at=now + INTERVAL)
            atomic(state_dir() / "ci-overview.json", snapshot)
            return snapshot

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        try:
            futures = {executor.submit(fn, config): name for name, fn in (("bors", bors), ("bors_history", bors_history), ("pools", pools), ("backlog", backlog), ("pressure", pressure))}
            for future in concurrent.futures.as_completed(futures, timeout=40):
                name = futures[future]
                try:
                    data, observed = future.result()
                    sources[name] = dict(value=data, observed_at=observed)
                except Exception as error:
                    reason = str(error) if isinstance(error, FleetError) else type(error).__name__
                    sources[name] = dict(sources.get(name, {}), error=reason[:200], failed_at=time.time())
                save()
        except concurrent.futures.TimeoutError:
            for future, name in futures.items():
                if not future.done():
                    sources[name] = dict(sources.get(name, {}), error="Background observation deadline", failed_at=time.time())
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return save()


def source_value(name, source):
    """Mixed-version or corrupt observations must not break foreground status."""
    def integer(value):
        return type(value) is int and value >= 0

    def timestamp(value):
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= time.time() + 5

    try:
        if not isinstance(source, dict) or not timestamp(source.get("observed_at")):
            return None
        data = source.get("value")
        if not isinstance(data, dict):
            return None
        if name == "bors":
            if not isinstance(data.get("batches"), list) or not (data.get("held") is None or integer(data["held"])):
                return None
            if any(not isinstance(b, dict) or not integer(b.get("id")) or not integer(b.get("size")) or not isinstance(b.get("state"), str) or not re.fullmatch(r"[a-z_]{1,32}", b["state"]) for b in data["batches"]):
                return None
        elif name == "bors_history":
            windows = data.get("windows")
            if not isinstance(windows, list) or {r.get("window") for r in windows if isinstance(r, dict)} != {"1h", "3h", "24h", "7d"} or len(windows) != 4:
                return None
            counts = ("completed_batches", "successful_batches", "failed_batches", "canceled_batches", "landed_prs", "landed_batches_without_receipts", "staging_pr_runs", "staging_unique_prs", "batches_with_unknown_membership")
            for row in windows:
                if not all(integer(row.get(key)) for key in counts):
                    return None
                if sum(row[k] for k in ("successful_batches", "failed_batches", "canceled_batches")) != row["completed_batches"]:
                    return None
                mean = row.get("mean_staging_runs_per_pr")
                if mean is not None and (type(mean) not in (int, float) or not math.isfinite(mean) or mean < 1):
                    return None
            if not isinstance(data.get("durations"), list):
                return None
            for row in data["durations"]:
                if not isinstance(row, dict) or row.get("window") not in {"1h", "3h", "24h", "7d"} or not integer(row.get("samples")):
                    return None
                for metric in ("mean_seconds", "p50_seconds", "p95_seconds"):
                    number = row.get(metric)
                    if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or number < 0):
                        return None
        elif name == "pools":
            if not isinstance(data.get("counts"), dict) or any(not isinstance(k, str) or not re.fullmatch(r"github|namespace|self-hosted|fleet-(?:[a-f0-9]{12}|[a-f0-9]{32})", k) or not integer(v) for k, v in data["counts"].items()):
                return None
        elif name == "backlog":
            if not all(integer(data.get(k)) for k in ("pr_builds", "all_workflows")):
                return None
        elif name == "pressure":
            if not isinstance(data.get("account"), str) or not re.fullmatch(r"[A-Za-z0-9-]{1,100}", data["account"]) or not isinstance(data.get("scopes"), list):
                return None
            if any(not isinstance(r, dict) or not integer(r.get("count")) or not integer(r.get("limit")) or r["limit"] == 0
                   or type(r.get("blocked")) is not bool or r["blocked"] != (r["count"] >= r["limit"])
                   or not timestamp(r.get("observed_at")) or not isinstance(r.get("scope"), str) or not r["scope"].isprintable()
                   or not isinstance(r.get("skip"), str) for r in data["scopes"]):
                return None
        else:
            return None
        return data
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def combine(values, pool_names=None):
    sources = {}
    accounts = {}

    def history_rank(observation):
        # An inactive status host has fewer local duration facts. Within the
        # normal freshness window, keep the host whose backfill is furthest on.
        observed = observation.get("observed_at", 0)
        fresh = observed >= time.time() - 600 and not observation.get("error")
        checked = observation.get("value", {}).get("runtime_runs_checked", 0)
        checked = checked if type(checked) is int and checked >= 0 else 0
        return fresh, checked if fresh else 0, observed

    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("sources"), dict):
            continue
        for name, source in value.get("sources", {}).items():
            if source_value(name, source) is None:
                continue
            preferred = (history_rank(source) > history_rank(sources.get(name, {}))
                         if name == "bors_history" else
                         source.get("observed_at", 0) > sources.get(name, {}).get("observed_at", -1))
            if preferred:
                sources[name] = source
            if name == "pressure" and isinstance(source.get("value"), dict):
                account = source["value"]["account"]
                machine = source["value"].get("machine_id")
                key = account + ("@" + machine if isinstance(machine, str) else "")
                if source.get("observed_at", 0) > accounts.get(key, {}).get("observed_at", -1):
                    accounts[key] = source
    return dict(version=1, repository=REPOSITORY, sources=sources, pressure_accounts=accounts, pool_names=pool_names or {})


def display(value):
    sources = value.get("sources", {}) if isinstance(value.get("sources"), dict) else {}
    names = value.get("pool_names", {})
    pool_names = {**POOL_NAMES, **(names if isinstance(names, dict) else {})}

    def source(name):
        row = sources.get(name, {})
        data = source_value(name, row)
        if data is None:
            return None, "incompatible or unavailable observation"
        age = max(0, int(time.time() - row.get("observed_at", time.time())))
        note = f"{age}s old" + ("; stale" if age > 600 or row.get("error") else "")
        return data, note

    data, note = source("bors")
    if data is None:
        print("Bors: unknown (background observation unavailable)")
    else:
        batches = data["batches"]
        parts = [str(b["size"]) for b in batches]
        held = data["held"] if data.get("held") is not None else "unknown"
        total = sum(b["size"] for b in batches)
        print("Bors: " + (f"{total}=" + "+".join(parts) if parts else "0") + f"; held {held} ({note})")
    data, note = source("bors_history")
    if data:
        print(f"Bors history ({note}):")
        durations = {r["window"]: r for r in data["durations"]}
        windows = {r["window"]: r for r in data["windows"]}
        periods = ("1h", "3h", "24h", "7d")
        rows = [("Completed batches", [str(windows[p]["completed_batches"]) for p in periods])]
        success = []
        for period in periods:
            row = windows[period]
            decided = row["successful_batches"] + row["failed_batches"]
            percent = round(100 * row["successful_batches"] / decided, 1) if decided else None
            success.append(f"{percent:.1f}%" if percent is not None else "—")
        rows.extend([("Succeeded", success),
                     ("Canceled", [str(windows[p]["canceled_batches"]) for p in periods]),
                     ("PRs landed", [str(windows[p]["landed_prs"]) for p in periods]),
                     ("Staging runs / PR", [f"{windows[p]['mean_staging_runs_per_pr']:.2f}"
                                            if windows[p]["mean_staging_runs_per_pr"] is not None else "—"
                                            for p in periods])])
        for title, metric in (("Build p50", "p50_seconds"), ("Build p95", "p95_seconds")):
            builds = []
            for period in periods:
                runtime = durations.get(period, {}).get(metric)
                builds.append(f"{int(runtime) // 60}m {int(runtime) % 60}s" if runtime is not None else "—")
            rows.append((title, builds))
        rows.append(("Build samples", [str(durations[p]["samples"]) if p in durations else "—" for p in periods]))
        if any(windows[p]["landed_batches_without_receipts"] for p in periods):
            rows.append(("Batches missing landing receipts", [str(windows[p]["landed_batches_without_receipts"]) for p in periods]))
        width = max(len(title) for title, _ in rows)
        print("  " + " " * width + "".join(f"{p:>10}" for p in periods))
        for title, cells in rows:
            print(f"  {title:<{width}}" + "".join(f"{cell:>10}" for cell in cells))
        checked, total = data.get("runtime_runs_checked", 0), data.get("runtime_runs_total", 0)
        print(f"  Build-time history: {checked}/{total} runs checked.")
    else:
        print("Bors history: unknown (background observation unavailable)")
    data, note = source("pools")
    if data is None:
        print("CI jobs running: unknown (prepared shared inventory unavailable)")
    else:
        counts = {name: 0 for name in POOL_NAMES.values()}
        for key, count in data["counts"].items():
            key = key[:18] if key.startswith("fleet-") else key
            name = pool_names.get(key, key)
            counts[name] = counts.get(name, 0) + count
        print("CI jobs running: " + ", ".join(f"{name} {count}" for name, count in counts.items()) + f" ({note}; organization-wide, all workflows)")
    data, note = source("backlog")
    if data is None:
        print("CI backlog: unknown (background observation unavailable)")
    elif data.get("created_since") != QUEUED_RUNS_CREATED_SINCE:
        # Older caches include the ignored cohort; do not reuse its counts or age.
        print(f"CI backlog: refreshing (excluding stuck runs through 2026-10-07; {note})")
    else:
        stamp = data.get("oldest_created_at")
        if type(stamp) in (int, float) and math.isfinite(stamp) and 0 < stamp <= time.time() + 5:
            minutes = max(0, int((time.time() - stamp) / 60))
            hours, minutes = divmod(minutes, 60)
            oldest = f"{hours}h {minutes}m old" if hours else f"{minutes}m old"
        else:
            oldest = "none" if data["all_workflows"] == 0 else "unknown"
        print(f"CI backlog: {data['pr_builds']} PR-build runs queued; {data['all_workflows']} across all workflows; oldest queued run {oldest} ({note}; excluding stuck runs through 2026-10-07)")
    accounts = value.get("pressure_accounts", {})
    pressures = list(accounts.values()) if isinstance(accounts, dict) else []
    pressures = pressures or [sources.get("pressure", {})]
    if not any(source_value("pressure", row) is not None for row in pressures):
        print("Backpressure: unknown (no prepared worker survey)")
    for observation in pressures:
        data = source_value("pressure", observation)
        if data is None:
            continue
        age = max(0, int(time.time() - observation.get("observed_at", time.time())))
        note = f"{age}s old" + ("; stale" if age > 600 or observation.get("error") else "")
        rows = sorted((r for r in data["scopes"] if r["scope"] in ("any", "auto") and not r["skip"]),
                      key=lambda r: -r["observed_at"])
        parts = []
        for row in rows[:1]:
            scope = "all roadmaps" if row["scope"] in ("any", "auto") else row["scope"]
            if row["skip"]:
                scope += " (with exclusions)"
            parts.append(f"{scope}: {row['count']}/{row['limit']}" + (" blocked" if row["blocked"] else ""))
            scope_age = max(0, int(time.time() - row["observed_at"]))
            if scope_age > 600:
                parts[-1] += f" (stale {scope_age}s)"
        machine = data.get("machine_id")
        label = " on " + pool_names.get("fleet-" + machine[:12], machine[:12]) if isinstance(machine, str) else ""
        print(f"Backpressure {data['account']}{label}: " + ("; ".join(parts) or "all roadmaps: unknown") + f" open non-draft roadmap PRs ({note})")
