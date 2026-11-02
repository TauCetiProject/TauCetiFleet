"""Prepared CI observations. Foreground status reads this cache only."""

import concurrent.futures
import fcntl
import hashlib
import heapq
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import github
from .store import FleetError, atomic, read, state_dir

REPOSITORY = "TauCetiProject/TauCeti"
INTERVAL = 300
POOL_NAMES = {
    "github": "GitHub", "namespace": "Namespace",
    "fleet-4fab2fed9a51": "Chris", "fleet-0fb9966f7c40": "Brett",
    "fleet-11e2756fc110": "Tauchonki", "fleet-a26dbb028408": "Yaël",
}


def unit_files(command, environment=None):
    service = ("[Unit]\nDescription=Prepare TauCeti CI status observations\n\n[Service]\n"
               "Type=oneshot\nExecStart=" + command + "\nTimeoutStartSec=60\n"
               "Nice=10\nUMask=0077\nMemoryMax=128M\n")
    service += "".join("Environment=" + json.dumps((k + "=" + str(v)).replace("%", "%%")) + "\n"
                       for k, v in (environment or {}).items())
    timer = ("[Unit]\nDescription=Refresh cached TauCeti CI status\n\n[Timer]\n"
             "OnBootSec=45\nOnUnitActiveSec=300\nRandomizedDelaySec=30\n"
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
    if value.get("repo") != REPOSITORY or not isinstance(value.get("batches"), list):
        raise FleetError("Invalid Bors observation")
    batches = []
    for batch in value["batches"]:
        if type(batch.get("id")) is not int or batch.get("state") not in ("waiting", "running") or not isinstance(batch.get("members"), list):
            raise FleetError("Invalid Bors batch")
        batches.append(dict(id=batch["id"], state=batch["state"], size=len(batch["members"])))
    return dict(batches=batches, held=len(value.get("held", []))), time.time()


def pools(config):
    base = config.get("github_state_url", "https://github-state.taucetiproject.org").rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise FleetError("Status source requires HTTPS")
    value = fetch(base + "/v1/runner-capacity?" + urllib.parse.urlencode(dict(repository=REPOSITORY, cached_only="true")),
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
    for name, path in (("pr_builds", "actions/workflows/pr-build.yml/runs"), ("all_workflows", "actions/runs")):
        value = github.request("repos/" + REPOSITORY + "/" + path + "?status=queued&per_page=1", token_file=token_file)
        count = value.get("total_count")
        if type(count) is not int or count < 0:
            raise FleetError("Invalid queued-run count")
        counts[name] = count
    return counts, time.time()


def pressure(config):
    """Reuse complete survey responses the workers already prepared locally."""
    account = config.get("github_account", "tauceti-ai-for-science")
    limit = config.get("max_open_prs", 128 if config.get("profile") == "ai-for-science" else 8)
    base = config.get("github_state_url", "https://github-state.taucetiproject.org").rstrip("/")
    token_file = config.get("github_state_token_file")
    token = Path(token_file).expanduser().read_text().strip() if token_file else ""
    identity = hashlib.sha256((base + "\0" + token).encode()).hexdigest()
    root = Path(os.environ.get("TAUCETI_GITHUB_SHARED_DIR", Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "tauceti/github-shared"))
    directory = root / "gateway" / identity / "responses"
    newest = []
    deadline = time.monotonic() + 5
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= 10000 or time.monotonic() > deadline:
                break
            if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                if stat.st_size <= 8 * 1024 * 1024:
                    heapq.heappush(newest, (stat.st_mtime, entry.path))
                    if len(newest) > 256:
                        heapq.heappop(newest)
    scopes = {}
    for validated, path in sorted(newest, reverse=True):
        if time.monotonic() > deadline:
            break
        try:
            cached = read(path, {})
            query, payload = cached.get("query", {}), cached.get("payload", {})
            args = query.get("args", {})
            if (query.get("identity") != identity or query.get("repository") != REPOSITORY
                    or query.get("operation") != "survey" or args.get("numbers")
                    or payload.get("status") != "ok" or payload.get("complete") is not True or payload.get("cursor")):
                continue
            scope, skip = args.get("roadmap", "any"), args.get("skip", "")
            key = (scope, skip)
            if key in scopes:
                continue
            if scope in ("", "any", "auto"):
                counts = payload.get("author_counts")
                if not isinstance(counts, dict):
                    continue
                count = counts.get(account, 0)
            else:
                rows = payload.get("data")
                if not isinstance(rows, list):
                    continue
                count = sum(not row["pr"].get("isDraft") and row["pr"].get("author", {}).get("login") == account for row in rows)
            if type(count) is not int or count < 0:
                continue
            scopes[key] = dict(scope=scope or "any", skip=skip, count=count, limit=limit,
                               blocked=count >= limit, observed_at=validated)
        except (FleetError, KeyError, TypeError, ValueError):
            continue
    if not scopes:
        raise FleetError("No complete cached worker survey")
    rows = list(scopes.values())
    return dict(account=account, scopes=rows), max(r["observed_at"] for r in rows)


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
        if read(attempt, {}).get("next_attempt_at", 0) > now:
            return cached(config)
        atomic(attempt, dict(at=now, next_attempt_at=now + INTERVAL))
        value = cached(config)
        sources = dict(value.get("sources", {}))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fn, config): name for name, fn in (("bors", bors), ("pools", pools), ("backlog", backlog), ("pressure", pressure))}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                try:
                    data, observed = future.result()
                    sources[name] = dict(value=data, observed_at=observed)
                except Exception as error:
                    reason = str(error) if isinstance(error, FleetError) else type(error).__name__
                    sources[name] = dict(sources.get(name, {}), error=reason[:200], failed_at=time.time())
        value = dict(version=1, repository=REPOSITORY, sources=sources, prepared_at=time.time(), next_attempt_at=now + INTERVAL)
        atomic(state_dir() / "ci-overview.json", value)
        return value


def combine(values):
    sources = {}
    accounts = {}
    for value in values:
        for name, source in value.get("sources", {}).items():
            if source.get("observed_at", 0) > sources.get(name, {}).get("observed_at", -1):
                sources[name] = source
            if name == "pressure" and isinstance(source.get("value"), dict):
                account = source["value"].get("account")
                if account and source.get("observed_at", 0) > accounts.get(account, {}).get("observed_at", -1):
                    accounts[account] = source
    return dict(version=1, repository=REPOSITORY, sources=sources, pressure_accounts=accounts)


def display(value):
    sources = value.get("sources", {})

    def source(name):
        row = sources.get(name, {})
        age = max(0, int(time.time() - row.get("observed_at", time.time())))
        note = f"{age}s old" + ("; stale" if age > 600 or row.get("error") else "")
        return row.get("value"), note

    data, note = source("bors")
    if data is None:
        print("Bors: unknown (background observation unavailable)")
    else:
        batches = data["batches"]
        parts = [f"#{b['id']} {b['size']} PRs {b['state']}" for b in batches]
        print("Bors: " + ("; ".join(parts) or "empty") + f"; held {data['held']} ({note})")
    data, note = source("pools")
    if data is None:
        print("CI jobs running: unknown (prepared shared inventory unavailable)")
    else:
        counts = {name: 0 for name in POOL_NAMES.values()}
        for key, count in data["counts"].items():
            name = POOL_NAMES.get(key, POOL_NAMES.get(key[:18], key))
            counts[name] = counts.get(name, 0) + count
        print("CI jobs running: " + ", ".join(f"{name} {count}" for name, count in counts.items()) + f" ({note}; organization-wide, all workflows)")
    data, note = source("backlog")
    print(f"CI backlog: {data['pr_builds']} PR-build runs queued; {data['all_workflows']} across all workflows ({note})" if data else "CI backlog: unknown (background observation unavailable)")
    pressures = list(value.get("pressure_accounts", {}).values()) or [sources.get("pressure", {})]
    if not any(row.get("value") for row in pressures):
        print("Backpressure: unknown (no prepared worker survey)")
    for observation in pressures:
        data = observation.get("value")
        if data is None:
            continue
        age = max(0, int(time.time() - observation.get("observed_at", time.time())))
        note = f"{age}s old" + ("; stale" if age > 600 or observation.get("error") else "")
        rows = sorted(data["scopes"], key=lambda r: (r["scope"] not in ("any", "auto"), -r["observed_at"]))
        parts = []
        for row in rows[:4]:
            scope = "all roadmaps" if row["scope"] in ("any", "auto") else row["scope"]
            if row["skip"]:
                scope += " (with exclusions)"
            parts.append(f"{scope}: {row['count']}/{row['limit']}" + (" blocked" if row["blocked"] else ""))
            scope_age = max(0, int(time.time() - row["observed_at"]))
            if scope_age > 600:
                parts[-1] += f" (stale {scope_age}s)"
        print(f"Backpressure {data['account']}: " + "; ".join(parts) + f" open non-draft roadmap PRs ({note})")
