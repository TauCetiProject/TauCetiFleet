"""Operator CLI and host-side JSON protocol."""

import argparse
import json
import math
import sys
import time
from datetime import UTC, datetime

from . import activity, host, remote
from .store import FleetError, atomic, config_dir, read


def parser():
    p = argparse.ArgumentParser(description="Deploy and inspect user-level TauCeti fleets")
    sub = p.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="install inactive fleet services over existing SSH access")
    add.add_argument("target", help="SSH alias, user@host, or local")
    add.add_argument("--profile", default="ai-for-science")
    add.add_argument("--no-ci", action="store_true")
    for name in ("doctor", "status", "prepare", "update", "drain"):
        cmd = sub.add_parser(name)
        cmd.add_argument("host", nargs="?")
        cmd.add_argument("--json", action="store_true")
        if name == "doctor":
            cmd.add_argument(
                "--smoke", action="store_true", help="boot small disposable guests without production registration"
            )
    budget = sub.add_parser("budget", help="inspect or explicitly fund local host grant pools")
    budget.add_argument("host", nargs="?")
    budget.add_argument("--grant")
    budget.add_argument("--grant-rate")
    budget.add_argument("--json", action="store_true")
    enable = sub.add_parser("enable", help="explicitly activate a prepared role")
    enable.add_argument("host")
    enable.add_argument("--ci", action="store_true")
    enable.add_argument("--workers", action="store_true")
    enable.add_argument("--json", action="store_true")
    workers = sub.add_parser("workers", help="change the admission ceiling without interrupting active work")
    workers.add_argument("host")
    workers.add_argument("--max", dest="maximum", type=int, required=True)
    workers.add_argument("--json", action="store_true")
    for name in ("_host", "host-daemon", "run-workers"):
        sub.add_parser(name, help=argparse.SUPPRESS)
    return p


def print_doctor(value):
    print(f"Host {value['machine_id']} — workers: {value['readiness']['workers']}; CI: {value['readiness']['ci']}")
    for check in value["checks"]:
        marker = {"ok": "OK", "warning": "NOTE", "blocked": "BLOCKED"}[check["status"]]
        print(f"  {marker:7} {check['name']}: {check['evidence']}")
        if check["status"] != "ok":
            if check["remedy"]:
                print("          Next step: " + check["remedy"])
            if check["administrator_request"]:
                print("          Ask administrator: " + check["administrator_request"])


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "_host":
            try:
                raw = sys.stdin.buffer.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise FleetError("Host request too large")
                value = host.rpc(json.loads(raw))
                print(json.dumps(dict(ok=True, result=value)))
            except (FleetError, OSError, ValueError, KeyError) as exc:
                print(json.dumps(dict(ok=False, error=str(exc))))
            return 0
        if args.command == "host-daemon":
            host.daemon()
            return 0
        if args.command == "run-workers":
            host.run_workers()
            return 0
        if args.command == "add":
            value = remote.add(args.target, profile_name=args.profile, no_ci=args.no_ci)
            print("Installed " + value["host"] + "; existing workload activation and budget settings were preserved.")
            print_doctor(value["doctor"])
            print(
                "Next: tauceti-fleet prepare "
                + value["host"]
                + " (no paid workers or production routing are activated)"
            )
            return 0
        entries = remote.inventory()
        if args.command == "enable" and not (args.ci or args.workers):
            raise FleetError("Select --ci and/or --workers explicitly")
        if args.command == "budget" and (args.grant is not None or args.grant_rate is not None) and not args.host:
            raise FleetError("Budget changes require one explicit host; queries may aggregate the fleet")
        selected = {args.host: remote.resolve(args.host)} if args.host else entries
        if not selected:
            raise FleetError("No hosts enrolled; use tauceti-fleet add user@host")
        results = {}
        for name, entry in selected.items():
            action = args.command
            payload = dict(action=action)
            if args.command == "doctor":
                payload["smoke"] = args.smoke
            if args.command == "budget":
                payload.update(grant=args.grant, grant_rate=args.grant_rate)
            if args.command == "enable":
                payload.update(ci=args.ci, workers=args.workers)
            if args.command == "workers":
                payload.update(maximum=args.maximum)
            if args.command == "update":
                status = remote.call(entry["target"], dict(action="status"))
                if status["ci_enabled"] or status["workers_enabled"]:
                    raise FleetError("Drain this host before updating its installed software")
                waiting = {
                    "idle",
                    "waiting-capacity",
                    "waiting-budget",
                    "waiting-quota",
                    "backoff",
                    "stopped",
                    "exited",
                }
                if status["status"].get("ci_busy") or any(
                    w.get("alive") and w.get("state") not in waiting for w in status["status"].get("workers", [])
                ):
                    raise FleetError(
                        "Host is draining active work; wait for its round/CI build to finish before update"
                    )
                remote.call(entry["target"], dict(action="prepare_update"))
                results[name] = remote.add(
                    entry["target"], profile_name=entry["profile"], no_ci=not status["ci_allowed"]
                )
                continue
            cache = config_dir() / "status-cache" / (entry["machine_id"] + ".json")
            try:
                value = remote.call(
                    entry["target"],
                    payload,
                    timeout=1800 if action == "prepare" else 3 if action == "status" else 90,
                )
                results[name] = value
                if args.command == "status":
                    atomic(cache, dict(collected_at=time.time(), value=value))
            except FleetError as exc:
                if args.command == "status" and cache.exists():
                    saved = read(cache)
                    results[name] = dict(
                        saved["value"], error=str(exc), offline=True, collected_at=saved["collected_at"]
                    )
                else:
                    results[name] = dict(error=str(exc))
        fleet = None
        if args.command == "status" and not args.host:
            distinct = {}
            for name, value in results.items():
                distinct.setdefault(value.get("machine_id", entries[name]["machine_id"]), value)
            if len(distinct) > 1:
                fleet = activity.combine_status([v.get("activity", {}) for v in distinct.values()])
        if args.json:
            if fleet is not None:
                results["_fleet"] = dict(activity=fleet)
            print(json.dumps(results, indent=2))
        else:
            for index, (name, value) in enumerate(results.items()):
                if index:
                    print()
                print(name + ":")
                if args.command == "doctor" and "readiness" in value:
                    print_doctor(value)
                elif args.command == "status" and "status" in value:
                    status = value["status"]
                    if value.get("offline"):
                        print(
                            f"Host unreachable; cached status from {max(0, int(time.time() - value['collected_at']))}s ago"
                        )
                        print()
                    print(
                        f"Workers: {status.get('active_worker_limit', 0)} admitted / {value.get('max_workers', 0)} maximum; CI busy: {status.get('ci_busy', 'unknown')}"
                    )
                    memory = status.get("worker_memory")
                    if memory:
                        print()
                        gib = 1024**3
                        print(
                            f"Worker RAM: {memory['charged'] / gib:.1f} GiB counted toward limits (includes {memory['file'] / gib:.1f} GiB cached file data)"
                        )
                        print(
                            f"Estimated working memory: {memory['working_set'] / gib:.1f} GiB after subtracting likely reclaimable file cache"
                        )
                        print(
                            f"Worker limits: {memory['high'] / gib:.1f} GiB soft / {memory['maximum'] / gib:.1f} GiB hard"
                        )
                        stalls_high = memory["stall_some"] >= 5 or memory["stall_full"] >= 1
                        print(
                            f"Memory pressure from waiting for RAM: {'high' if stalls_high else 'low'} (1-minute averages)"
                        )
                        print(
                            f"  Time with at least one task waiting: {memory['stall_some']:.2f}%; all active tasks waiting: {memory['stall_full']:.2f}% (pause new work at 5% / 1%)"
                        )
                    print()
                    reason = str(status.get("scaling_reason", status.get("error", "unknown")))
                    # Explain older hosts' generic policy messages as well.
                    reason = {
                        "workers use available capacity": "CI idle; increases require spare CPU and memory",
                        "CI active; workers admitted only against measured spare capacity": "CI running; increases require spare CPU and memory",
                    }.get(reason, reason)
                    allowance = status.get("active_worker_limit", 0)
                    permission = f"up to {allowance} workers allowed" if allowance else "paused"
                    print(f"Starting new work: {permission}; {reason}")
                    decision = status.get("worker_admission", {})
                    if decision:
                        print(
                            f"CPU idle: {status['cpu_idle_fraction']:.1%}; required for an increase: {decision['cpu_idle_required']:.0%}"
                        )
                        next_increase = decision.get("next_increase_at")
                        if next_increase is not None:
                            remaining = math.ceil(next_increase - time.time())
                            if remaining > 0:
                                when = datetime.fromtimestamp(next_increase, UTC).strftime("%H:%M:%S UTC")
                                print(
                                    f"Next increase: {decision['next_worker_limit']} workers in {remaining}s ({when}), if CPU and memory checks pass"
                                )
                            else:
                                print("Next increase: eligible now, awaiting a fresh controller check")
                    if status.get("workers"):
                        print()
                    for worker in status.get("workers", []):
                        print(f"  {worker['id']}: {worker.get('state', 'unknown')} — {worker.get('detail') or ''}")
                    print()
                    activity.display(value.get("activity", {}))
                else:
                    print(json.dumps(value, indent=2))
            if fleet is not None:
                print("\nFleet totals:")
                activity.display(fleet)
        return 1 if any("error" in value for value in results.values()) else 0
    except (FleetError, OSError, ValueError) as exc:
        print("tauceti-fleet: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
