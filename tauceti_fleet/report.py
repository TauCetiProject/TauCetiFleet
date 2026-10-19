"""Machine attribution and settled spending; incomplete observations stay explicit."""

import datetime as dt
import gzip
import json
import sqlite3
import time
import urllib.request
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import activity, github
from .store import FleetError, database, export_events, host_config, state_dir

WINDOWS = {"3h": 3 * 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 86400}


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def budget_path():
    import os

    return Path(os.environ.get("TAUCETI_BUDGET_DIR", Path.home() / ".local/state/tauceti/budget"))


def settled_costs(path=None):
    """Read, never repair or rewrite, the existing local ledger.

    Reconciliations restate the original settlement's cost. Spending is recognized
    at settlement, not falsely prorated over the lifetime of a session.
    """
    path = path or budget_path() / "events.jsonl"
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    if raw and not raw.endswith(b"\n"):
        raise FleetError("Budget ledger has a partial final record; expenditure coverage is incomplete")
    sessions = {}
    for seq, line in enumerate(raw.splitlines(), 1):
        try:
            record = json.loads(line)
            if record["seq"] != seq or record["version"] != 1:
                raise ValueError("sequence")
            for value in record["events"]:
                kind = value["type"]
                if kind == "admit":
                    sessions[value["id"]] = dict(worker=value["worker"], model=value["model"])
                elif kind in ("settle", "correction"):
                    session = sessions[value["id"]]
                    session.setdefault("at", record["timestamp"])
                    cost = Decimal(value["cost"])
                    if not cost.is_finite() or cost < 0:
                        raise ValueError("cost")
                    session["usd"] = str(cost)
        except (ValueError, KeyError, TypeError, InvalidOperation):
            raise FleetError(f"Budget ledger record {seq} is invalid; expenditure coverage is incomplete") from None
    return [dict(id=key, **value) for key, value in sessions.items() if "usd" in value]


def refresh_prs(config, events):
    keys = sorted({(e["repository"], e["pr"]) for e in events if "repository" in e and "pr" in e})
    errors = []
    with database() as db:
        for repo, number in keys:
            prior = db.execute("SELECT data FROM prs WHERE repository=? AND number=?", (repo, number)).fetchone()
            old = json.loads(prior[0]) if prior else {}
            if time.time() - old.get("observed_at", 0) < (86400 if old.get("landed_at") else 900):
                continue
            try:
                data = github.request(f"repos/{repo}/pulls/{number}", token_file=config.get("github_token_file"))
                value = dict(
                    repository=repo,
                    pr=number,
                    state=data["state"],
                    head_sha=data["head"]["sha"],
                    landed_at=timestamp(data.get("merged_at")),
                    observed_at=time.time(),
                    lean_loc=None,
                    landing_source="github-merged" if data.get("merged_at") else None,
                )
                # LOC comes from the observed final PR diff, never from the
                # number of local edits or lines in build logs.
                additions = deletions = count = 0
                for page in range(1, 31):
                    files = github.request(
                        f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}",
                        token_file=config.get("github_token_file"),
                    )
                    count += len(files)
                    for file in files:
                        if file["filename"].endswith(".lean") and (
                            file["filename"].startswith("TauCeti/") or file["filename"] == "TauCeti.lean"
                        ):
                            additions += file["additions"]
                            deletions += file["deletions"]
                    if len(files) < 100:
                        break
                if count == data["changed_files"]:
                    value["lean_loc"] = dict(added=additions, deleted=deletions, net=additions - deletions)
                db.execute("INSERT OR REPLACE INTO prs VALUES (?,?,?)", (repo, number, json.dumps(value)))
            except (FleetError, KeyError, TypeError):
                errors.append(f"PR metadata unavailable for {repo}#{number}")
    return errors


def bors_landings(database_path):
    """Reuse TauCetiCI's verified batch-to-main evidence, not closed PR titles."""
    result = {}
    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT b.repo,b.members_json,l.landed_at FROM merge_builds b JOIN main_landings l ON l.repo=b.repo AND l.head_sha=b.head_sha WHERE b.landed=1"
        )
        for repository, members, landed_at in rows:
            repo = repository if "/" in repository else "TauCetiProject/" + repository
            for member in json.loads(members or "[]"):
                number = member.get("pr")
                head = member.get("head_sha")
                if isinstance(number, int) and head:
                    key = (repo, number, head)
                    result[key] = min(result.get(key, float("inf")), timestamp(landed_at))
    return result


def refresh_bors():
    """Existing public TauCetiCI archive, cached locally; no new service."""
    target = state_dir() / "tauceti-ci.sqlite"
    if target.exists() and time.time() - target.stat().st_mtime < 3600:
        return target
    compressed = target.with_suffix(".gz.tmp")
    expanded = target.with_suffix(".tmp")
    try:
        url = "https://github.com/TauCetiProject/TauCetiCI/releases/download/db/ci.sqlite.gz"
        with urllib.request.urlopen(url, timeout=30) as response, compressed.open("wb") as out:
            copied = 0
            while block := response.read(1024 * 1024):
                copied += len(block)
                if copied > 256 * 1024**2:
                    raise FleetError("TauCetiCI archive exceeds the reporting download limit")
                out.write(block)
        with gzip.open(compressed, "rb") as source, expanded.open("wb") as out:
            copied = 0
            while block := source.read(1024 * 1024):
                copied += len(block)
                if copied > 1024**3:
                    raise FleetError("Expanded TauCetiCI archive exceeds the reporting limit")
                out.write(block)
        bors_landings(expanded)  # Check schema before replacing the last good copy.
        expanded.replace(target)
        return target
    finally:
        compressed.unlink(missing_ok=True)
        expanded.unlink(missing_ok=True)


def snapshot(config=None, *, refresh=False):
    config = config or host_config()
    events = export_events()
    errors = refresh_prs(config, events) if refresh and events else []
    with database() as db:
        prs = [json.loads(row[0]) for row in db.execute("SELECT data FROM prs")]
    # Prepared facts replace on-demand GitHub refreshes, including Bors landings.
    try:
        prepared = activity.inventory(config["machine_id"])
        known = {(p["repository"], p["pr"]): p for p in prs}
        if activity.cached(config["machine_id"]).get("available"):
            # The prepared inventory owns the reporting period. Legacy creation
            # journals and metadata caches must not restore discarded PR activity.
            tracked = {(p["repo"], p["number"]) for p in prepared}
            known = {key: value for key, value in known.items() if key in tracked}
            events = [e for e in events if e["kind"] != "pr_created"] + [
                dict(
                    id=f"activity-pr:{p['repo']}:{p['number']}",
                    kind="pr_created",
                    repository=p["repo"],
                    pr=p["number"],
                    at=p["created"],
                )
                for p in prepared
            ]
        for p in prepared:
            if p.get("state") is not None:
                key = (p["repo"], p["number"])
                value = dict(
                    repository=p["repo"],
                    pr=p["number"],
                    state="open" if p["state"] == "OPEN" else "closed",
                    head_sha=p["head"],
                    landed_at=p["landed"],
                    observed_at=p["observed"],
                    landing_source=p["source"],
                    created_at=p["created"],
                    lean_loc=known.get(key, {}).get("lean_loc"),
                )
                if value["observed_at"] is None or value["observed_at"] >= known.get(key, {}).get("observed_at", 0):
                    known[key] = value
        prs = list(known.values())
    except (FleetError, KeyError, TypeError):
        errors.append("Prepared PR activity unavailable")
    if any(p["state"] == "closed" and not p.get("landed_at") for p in prs):
        try:
            path = refresh_bors() if refresh else state_dir() / "tauceti-ci.sqlite"
            known = bors_landings(path)
            for pr in prs:
                when = known.get((pr["repository"], pr["pr"], pr["head_sha"]))
                if when and not pr.get("landed_at"):
                    pr.update(landed_at=when, landing_source="tauceti-ci-main-evidence")
        except (OSError, ValueError, FleetError, sqlite3.Error):
            errors.append("Bors landing evidence unavailable; closed PRs are not counted as landed")
    spending_complete = True
    try:
        costs = settled_costs()
    except FleetError as exc:
        costs = []
        spending_complete = False
        errors.append(str(exc))
    budget = None
    from .process import executable, run

    binary = executable("tauceti")
    if binary:
        try:
            budget = json.loads(run([binary, "budget", "--json"]).stdout)
        except (FleetError, ValueError):
            errors.append("Current grant balance and pending estimates unavailable")
    return dict(
        version=1,
        machine_id=config["machine_id"],
        observed_at=time.time(),
        instrumentation_started_at=config["created_at"],
        events=events,
        prs=prs,
        costs=costs,
        coverage_errors=errors,
        budget=budget,
        spending_complete=spending_complete,
    )


def summarize(snapshots, *, now=None, windows=None):
    now = time.time() if now is None else now
    windows = WINDOWS if windows is None else windows
    events, prs, costs, machines = {}, {}, {}, {}
    for snap in snapshots:
        machine = snap["machine_id"]
        machines[machine] = snap
        for e in snap["events"]:
            events[(machine, e["id"])] = dict(e, machine_id=machine)
        for pr in snap["prs"]:
            key = (pr["repository"], pr["pr"])
            if pr.get("observed_at", 0) >= prs.get(key, {}).get("observed_at", 0):
                prs[key] = pr
        for cost in snap["costs"]:
            costs[(machine, cost["id"])] = dict(cost, machine_id=machine)
    owners, origin_times = {}, {}
    for e in sorted(events.values(), key=lambda e: (e["at"], e["id"])):
        if e["kind"] == "pr_created":
            key = (e["repository"], e["pr"])
            owners.setdefault(key, e["machine_id"])
            origin_times.setdefault(key, e["at"])
    for key in origin_times:
        if prs.get(key, {}).get("created_at") is not None:
            origin_times[key] = prs[key]["created_at"]
    reports = []
    for machine in [*sorted(machines), "fleet"]:
        covered = list(machines.values()) if machine == "fleet" else [machines[machine]]
        spending_complete = bool(covered) and all(s.get("spending_complete", True) for s in covered)
        rows = {}
        for label, seconds in windows.items():
            since = now - seconds
            landed = {
                key
                for key, pr in prs.items()
                if pr.get("landed_at")
                and since <= pr["landed_at"] <= now
                and key in owners
                and (machine == "fleet" or owners[key] == machine)
            }
            assisted = {
                (e["repository"], e["pr"])
                for e in events.values()
                if e["kind"] == "pr_activity"
                and since <= e["at"] <= now
                and owners.get((e["repository"], e["pr"])) != e["machine_id"]
                and (machine == "fleet" or e["machine_id"] == machine)
            }
            known = [prs[key]["lean_loc"] for key in landed if prs[key].get("lean_loc") is not None]
            spending = sum(
                (
                    Decimal(c["usd"])
                    for c in costs.values()
                    if since <= c["at"] <= now and (machine == "fleet" or c["machine_id"] == machine)
                ),
                Decimal(0),
            )
            ci = [
                e
                for e in events.values()
                if e["kind"] == "ci_result"
                and since <= e["at"] <= now
                and (machine == "fleet" or e["machine_id"] == machine)
            ]
            rows[label] = dict(
                prs_originated=sum(
                    since <= origin_times[key] <= now and (machine == "fleet" or owner == machine)
                    for key, owner in owners.items()
                ),
                attribution_covers_window=bool(covered)
                and all(s.get("instrumentation_started_at", now) <= since for s in covered),
                prs_landed=len(landed),
                prs_assisted=len(assisted),
                lean_loc={name: sum(v[name] for v in known) for name in ("added", "deleted", "net")},
                loc_unknown_prs=len(landed) - len(known),
                settled_api_usd=str(spending) if spending_complete else None,
                observed_settled_api_usd=str(spending),
                ci_attempts=len(ci),
                ci_successes=sum(e.get("conclusion") == "success" for e in ci),
                ci_failures=sum(e.get("conclusion") == "failure" for e in ci),
                ci_wait_seconds=sum(e.get("wait_seconds") or 0 for e in ci),
                ci_execution_seconds=sum(e.get("run_seconds", 0) for e in ci),
            )
        open_prs = sum(
            key in owners and (machine == "fleet" or owners[key] == machine) and pr["state"] == "open"
            for key, pr in prs.items()
        )
        reports.append(
            dict(
                machine_id=machine,
                activity=activity.combine(list(machines.values()), prs, owners, origin_times, now)
                if machine == "fleet"
                else machines[machine].get("activity", {}),
                open_prs=open_prs,
                pr_metadata_unknown=sum(
                    key not in prs and (machine == "fleet" or owner == machine) for key, owner in owners.items()
                ),
                windows=rows,
                current={"budget": machines[machine].get("budget"), "status": machines[machine].get("status", {})}
                if machine != "fleet"
                else {
                    "pending_estimates_usd": str(
                        sum(
                            (
                                Decimal(s.get("budget", {}).get("pending", "0"))
                                for s in machines.values()
                                if s.get("budget")
                            ),
                            Decimal(0),
                        )
                    )
                    if covered and all(s.get("budget") is not None for s in covered)
                    else None
                },
                observed_at=min(s["observed_at"] for s in snapshots)
                if machine == "fleet" and snapshots
                else machines.get(machine, {}).get("observed_at"),
                coverage_errors=[e for s in covered for e in s.get("coverage_errors", [])],
            )
        )
    return dict(
        version=1,
        as_of=now,
        reports=reports,
        spending_basis="settled API receipts; active estimates and infrastructure charges excluded",
    )
