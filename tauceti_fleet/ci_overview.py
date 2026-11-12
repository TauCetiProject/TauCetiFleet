"""Prepared CI observations. Foreground status reads this cache only."""

import concurrent.futures
import fcntl
import hashlib
import heapq
import json
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import bors_history as history_jobs
from .store import FleetError, atomic, read, state_dir

REPOSITORY = "TauCetiProject/TauCeti"
INTERVAL = 300
BACKLOG_SCHEMA = "tauceti.operations.backlog/v1"
BACKLOG_STATES = ("pending", "queued", "in_progress", "stuck", "unconfirmed")
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


def status_ssl_context():
    context = ssl.create_default_context()
    # Standalone Python may expect /etc/ssl/cert.pem, absent on NixOS. Its
    # existing certs directory need not contain OpenSSL's hashed issuer links.
    # Use the distribution's installed bundle; preserve explicit trust settings.
    if ssl.get_default_verify_paths().cafile is None and not any(
            key in os.environ for key in ("SSL_CERT_FILE", "SSL_CERT_DIR")):
        for path in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/certs/ca-bundle.crt"):
            if Path(path).is_file():
                context.load_verify_locations(cafile=path)
                break
    return context


def fetch(url, *, token_file=None, timeout=10):
    headers = {"Accept": "application/json", "User-Agent": "TauCetiFleetStatus/1.0"}
    if token_file:
        token = Path(token_file).expanduser().read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise FleetError("Invalid status credential")
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=status_ssl_context()))
    with opener.open(request, timeout=timeout) as response:
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


def valid_backlog(data):
    if data.get("schema") != BACKLOG_SCHEMA or type(data.get("partial")) is not bool:
        return False
    for category in ("pr_builds", "other_workflows"):
        row = data.get(category)
        if not isinstance(row, dict) or any(type(row.get(k)) is not int or row[k] < 0 for k in BACKLOG_STATES):
            return False
        oldest = row.get("oldest_waiting_at")
        if oldest is not None and (type(oldest) not in (int, float) or not math.isfinite(oldest)
                                   or not 0 < oldest <= time.time() + 5):
            return False
    return all(type(data.get(k)) is int and data[k] >= 0 for k in ("waiting_jobs", "running_jobs"))


def backlog(config):
    from . import pool_activity

    manifest = fetch(pool_activity.MANIFEST)
    jobs = fetch(pool_activity.jobs_url(manifest))
    source = jobs.get("backlog") or {}
    if (jobs.get("schema") != "tauceti.operations.jobs/v1"
            or source.get("schema") != BACKLOG_SCHEMA or source.get("coverage") not in ("observed", "partial")):
        raise FleetError("Shared CI backlog observation is not ready")
    row = source.get("repositories", {}).get(REPOSITORY)
    if not isinstance(row, dict):
        raise FleetError("Shared CI backlog has no repository observation")
    data = dict(row, schema=BACKLOG_SCHEMA, partial=source["coverage"] == "partial")
    if not valid_backlog(data):
        raise FleetError("Invalid shared CI backlog counts")
    observed = pool_activity.timestamp(jobs.get("observed_at"))
    if not math.isfinite(observed) or not 0 < observed <= time.time() + 5:
        raise FleetError("Invalid shared CI backlog timestamp")
    return data, observed


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


def pressure_rows(path, identity, account, policies):
    stat = path.stat()
    if stat.st_size > 8 * 1024 * 1024:
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
    # The gateway computes author_counts over the inventory before filtering
    # rows by roadmap. Only the exclusions need to match an all-roadmaps policy.
    author_counts = payload.get("author_counts")
    counts = {}
    if isinstance(author_counts, dict):
        counts.update((area, author_counts.get(account, 0)) for area in ("any", "auto"))
    rows = payload.get("data")
    if scope not in ("any", "auto") and isinstance(rows, list) and any(
        area == scope and excluded == skipped for area, excluded, _ in policies
    ):
        counts[scope] = sum(not row["pr"].get("isDraft") and row["pr"].get("author", {}).get("login") == account for row in rows)
    return [
        dict(scope=area, skip=",".join(skipped), count=count, limit=limit, blocked=count >= limit,
             observed_at=stat.st_mtime)
        for area, excluded, limit in policies
        if excluded == skipped and type(count := counts.get(area)) is int and count >= 0
    ]


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
    seen = set()
    deadline = time.monotonic() + 5

    def observe(path):
        if path in seen:
            return
        seen.add(path)
        try:
            for row in pressure_rows(path, identity, account, policies):
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
    if len(scopes) < len(policies) or any(scope in ("auto", "any") for scope, _, _ in policies):
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
        for stamp, path in sorted(newest, reverse=True):
            if time.monotonic() > deadline:
                break
            if len(scopes) == len(policies) and stamp <= min(row["observed_at"] for row in scopes.values()):
                break
            observe(Path(path))
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
            if not valid_backlog(data):
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


def display(value, *, section="all"):
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

    if section != "overview":
        data, note = source("bors")
        if data is None:
            print("Bors: unknown (background observation unavailable)")
        else:
            batches = sorted(data["batches"], key=lambda batch: batch["id"])
            parts = [str(b["size"]) for b in batches]
            held = data["held"] if data.get("held") is not None else "unknown"
            total = sum(b["size"] for b in batches)
            print("Bors: " + (f"{total}=" + "+".join(parts) if parts else "0") + f"; held {held} ({note})")
        if "queue" in value:
            from . import queue_status
            queue_status.display(value["queue"], timezone=value.get("queue_timezone", queue_status.DEFAULT_TIMEZONE))
        data, note = source("bors_history")
        if "queue_history" in value:
            from . import queue_history
            queue_history.display(value["queue_history"], timezone=value.get("queue_timezone", "Australia/Sydney"))
        elif data:
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
    if section == "bors":
        return
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
    else:
        if data["partial"]:
            note += "; partial observations"
        pr, other = data["pr_builds"], data["other_workflows"]
        oldest = pr.get("oldest_waiting_at")
        age = ""
        if oldest is not None:
            minutes = max(0, int((time.time() - oldest) / 60))
            hours, minutes = divmod(minutes, 60)
            age = f"; oldest PR wait {hours}h {minutes}m" if hours else f"; oldest PR wait {minutes}m"
        print(f"CI backlog: PR builds {pr['pending']} pending, {pr['queued']} queued; "
              f"other workflows {other['pending']} pending, {other['queued']} queued{age} ({note})")
        stuck = pr["stuck"] + other["stuck"]
        unconfirmed = pr["unconfirmed"] + other["unconfirmed"]
        print(f"CI workflow records: {stuck} verified stuck; {unconfirmed} without confirmed active jobs "
              "(separate from waiting work)")
    accounts = value.get("pressure_accounts", {})
    pressures = list(accounts.values()) if isinstance(accounts, dict) else []
    pressures = pressures or [sources.get("pressure", {})]
    if not any(source_value("pressure", row) is not None for row in pressures):
        print("Backpressure: unknown (no prepared worker survey)")
    for observation in pressures:
        data = source_value("pressure", observation)
        if data is None:
            continue
        rows = {}
        for row in data["scopes"]:
            if row["scope"] in ("any", "auto") and not row["skip"]:
                limit = row["limit"]
                if row["observed_at"] > rows.get(limit, {}).get("observed_at", -1):
                    rows[limit] = row
        newest = max((r["observed_at"] for r in rows.values()), default=observation["observed_at"])
        age = max(0, int(time.time() - newest))
        note = f"{age}s old" + ("; stale" if age > 600 or observation.get("error") else "")
        parts = []
        for limit, row in sorted(rows.items()):
            parts.append(f"{row['count']}/{limit}" + (" blocked" if row["blocked"] else ""))
            scope_age = max(0, int(time.time() - row["observed_at"]))
            if scope_age > 600 and row["observed_at"] < newest:
                parts[-1] += f" (stale {scope_age}s)"
        machine = data.get("machine_id")
        label = " on " + pool_names.get("fleet-" + machine[:12], machine[:12]) if isinstance(machine, str) else ""
        print(f"Backpressure {data['account']}{label}: all roadmaps: " + (", ".join(parts) or "unknown") + f" open non-draft roadmap PRs ({note})")
