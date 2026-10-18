"""Operator CLI and host-side JSON protocol."""

import argparse
import datetime as dt
import json
import sys
import time
from decimal import Decimal

from . import host, remote
from .report import WINDOWS, summarize
from .store import FleetError, atomic, config_dir, read


def parser():
    p = argparse.ArgumentParser(description="Deploy and inspect user-level TauCeti fleets")
    sub = p.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="install inactive fleet services over existing SSH access")
    add.add_argument("target", help="SSH alias, user@host, or local")
    add.add_argument("--profile", default="ai-for-science")
    add.add_argument("--no-ci", action="store_true")
    for name in ("doctor", "status", "report", "prepare", "update", "drain"):
        cmd = sub.add_parser(name)
        cmd.add_argument("host", nargs="?")
        cmd.add_argument("--json", action="store_true")
        if name == "doctor":
            cmd.add_argument(
                "--smoke", action="store_true", help="boot small disposable guests without production registration"
            )
        if name == "report":
            cmd.add_argument("--since", choices=list(WINDOWS))
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
    ramp = sub.add_parser("ramp", help="start, inspect or stop a persistent worker capacity experiment")
    ramp.add_argument("host")
    mode = ramp.add_mutually_exclusive_group()
    mode.add_argument("--start", action="store_true")
    mode.add_argument("--stop", action="store_true")
    ramp.add_argument("--interval-minutes", type=int, default=30)
    ramp.add_argument("--queue-limit", type=int, default=40)
    ramp.add_argument("--ceiling", type=int, default=64)
    ramp.add_argument("--json", action="store_true")
    for name in ("_host", "host-daemon", "run-workers", "ramp-daemon"):
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


def print_report(value, entries):
    names = {v["machine_id"]: k for k, v in entries.items()}
    for report in value["reports"]:
        machine = report["machine_id"]
        print(f"\n{names.get(machine, machine)} — {report['open_prs']} open originating PRs")
        if report["pr_metadata_unknown"]:
            print(f"PR metadata unavailable for {report['pr_metadata_unknown']} originating PR(s)")
        current = report.get("current", {})
        budget = current.get("budget")
        if budget:
            print(
                f"Budget: ${Decimal(budget['balance']):.2f} balance; ${Decimal(budget['pending']):.2f} pending estimates; ${Decimal(budget['grant_rate']):.2f}/hour replenishment"
            )
        if "pending_estimates_usd" in current:
            print(
                "Fleet pending estimates: "
                + (
                    "$" + current["pending_estimates_usd"]
                    if current["pending_estimates_usd"] is not None
                    else "unknown"
                )
            )
        status = current.get("status", {})
        if status:
            print(
                f"CI busy: {status.get('ci_busy', 'unknown')}; worker limit: {status.get('active_worker_limit', 'unknown')}; CPU idle: {status.get('cpu_idle_fraction', 0):.0%}"
            )
        print("Window   Opened  Landed  Assisted       Lean + / - / net    Settled API USD   CI attempts")
        for window, row in report["windows"].items():
            loc = row["lean_loc"]
            expenditure = f"{Decimal(row['settled_api_usd']):.2f}" if row["settled_api_usd"] is not None else "unknown"
            print(
                f"{window:6} {row['prs_originated']:7} {row['prs_landed']:7} {row['prs_assisted']:9}  {loc['added']:7} / {loc['deleted']:7} / {loc['net']:7}  {expenditure:>15}  {row['ci_attempts']:11}"
            )
            if row["loc_unknown_prs"]:
                print(f"       LOC unavailable for {row['loc_unknown_prs']} landed PR(s)")
            if not row["attribution_covers_window"]:
                print("       Machine attribution covers only the period since installation")
        if report.get("observed_at"):
            print(f"Last collection: {max(0, time.time() - report['observed_at']):.0f}s ago")
        for error in report["coverage_errors"]:
            print("Coverage: " + error)


def print_ramp(value):
    print(f"Ramp: {value.get('phase', 'unknown')}; worker ceiling: {value.get('maximum', 0)}")
    if value.get("phase") == "running" and value.get("next_increase_at"):
        print("Next increase: " + dt.datetime.fromtimestamp(value["next_increase_at"], dt.UTC).isoformat())
    if value.get("hold_reason"):
        print("Holding: " + value["hold_reason"])
    if value.get("limiting_factors"):
        print(
            f"Stopped at ceiling {value.get('maximum_at_stop', value.get('maximum', 0))}: "
            + ", ".join(value["limiting_factors"])
        )
    sample = value.get("last_sample", {})
    if sample:
        gib = 1024**3
        print(
            f"Queue: {sample['queue_prs']}/{value['queue_limit']} PRs; CPU idle: {sample['cpu_idle']:.0%}; CI busy: {sample['ci_busy']}"
        )
        print(
            f"Worker memory: {sample['worker_memory'] / gib:.1f}/{sample['worker_memory_high'] / gib:.1f} GiB; host available: {sample['memory_available'] / gib:.1f} GiB"
        )
        print(
            f"Budget: ${Decimal(sample['available']):.2f} available, ${sample['pending']} pending; ${sample['grant_rate']}/hour replenishment"
        )
        print("Worker states: " + json.dumps(sample["worker_states"]))
    print("\n" + value["spending_basis"])


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
        if args.command == "ramp-daemon":
            from .ramp import daemon

            daemon()
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
        results, snapshots = {}, []
        for name, entry in selected.items():
            action = "snapshot" if args.command == "report" else args.command
            payload = dict(action=action)
            if args.command == "doctor":
                payload["smoke"] = args.smoke
            if args.command == "budget":
                payload.update(grant=args.grant, grant_rate=args.grant_rate)
            if args.command == "enable":
                payload.update(ci=args.ci, workers=args.workers)
            if args.command == "workers":
                payload.update(maximum=args.maximum)
            if args.command == "ramp":
                payload.update(
                    start=args.start,
                    stop=args.stop,
                    interval_minutes=args.interval_minutes,
                    queue_limit=args.queue_limit,
                    ceiling=args.ceiling,
                )
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
            cache = config_dir() / "cache" / (entry["machine_id"] + ".json")
            try:
                value = remote.call(
                    entry["target"],
                    payload,
                    timeout=1800 if action == "prepare" else 300 if action == "snapshot" else 90,
                )
                results[name] = value
                if args.command == "report":
                    atomic(cache, value)
                    snapshots.append(value)
            except FleetError as exc:
                if args.command == "report" and cache.exists():
                    value = read(cache)
                    value["spending_complete"] = False
                    value["coverage_errors"].append("Host offline; showing cached observations: " + str(exc))
                    snapshots.append(value)
                else:
                    results[name] = dict(error=str(exc))
        if args.command == "report":
            windows = {args.since: WINDOWS[args.since]} if args.since else None
            value = summarize(snapshots, windows=windows)
            value["unavailable_hosts"] = {k: v for k, v in results.items() if "error" in v}
            if value["unavailable_hosts"]:
                fleet = value["reports"][-1]
                fleet["current"]["pending_estimates_usd"] = None
                for row in fleet["windows"].values():
                    row["settled_api_usd"] = None
                    row["attribution_covers_window"] = False
                fleet["coverage_errors"].append("Fleet totals omit hosts without cached observations")
            if args.json:
                print(json.dumps(value, indent=2))
            else:
                print_report(value, entries)
                for name, error in value["unavailable_hosts"].items():
                    print(name + ": " + error["error"] + "; no cached coverage")
        elif args.json:
            print(json.dumps(results, indent=2))
        else:
            for name, value in results.items():
                print(name + ":")
                if args.command == "doctor" and "readiness" in value:
                    print_doctor(value)
                elif args.command == "ramp" and "phase" in value:
                    print_ramp(value)
                else:
                    print(json.dumps(value, indent=2))
        return 1 if any("error" in value for value in results.values()) else 0
    except (FleetError, OSError, ValueError) as exc:
        print("tauceti-fleet: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
