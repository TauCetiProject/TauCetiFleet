"""Operator CLI and host-side JSON protocol."""

import argparse
import json
import math
import sys
import time
from datetime import UTC, datetime

from . import activity, broker, host, remote
from .store import FleetError, atomic, config_dir, read


def parser():
    p = argparse.ArgumentParser(description="Deploy and inspect user-level TauCeti fleets")
    sub = p.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="install inactive fleet services over existing SSH access")
    add.add_argument("target", help="SSH alias, user@host, or local")
    add.add_argument("--profile", default="ai-for-science")
    add.add_argument("--no-ci", action="store_true")
    enroll = sub.add_parser("enroll", help="enroll one explicit machine for CI using a one-time code")
    enroll.add_argument("target", help="SSH alias, user@host, or local; the credential stays on this machine")
    enroll.add_argument("--broker", default=broker.DEFAULT_URL)
    enroll.add_argument("--token-file", help="read the one-time enrollment code from a file instead of prompting")
    enroll.add_argument("--replace-machine", action="store_true", help="explicitly enroll a replacement machine at a remembered SSH alias")
    for name in ("doctor", "status", "prepare", "update", "drain"):
        cmd = sub.add_parser(name)
        cmd.add_argument("host", nargs="?")
        cmd.add_argument("--json", action="store_true")
        if name == "status":
            cmd.add_argument("-f", "--follow", action="store_true", help="follow prepared reports in a live, scrollable view")
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
    enable.add_argument("--ci", choices=("pr", "staging", "both"), help="explicit CI job classes for this machine")
    enable.add_argument("--max-jobs", type=int, help="shared PR + staging concurrency limit")
    enable.add_argument("--ci-vcpus", type=int, help="vCPUs per CI job")
    enable.add_argument("--ci-memory-gib", type=int, help="GiB of guest RAM per CI job")
    enable.add_argument("--ci-memory-budget-gib", type=int, help="aggregate physical CI RAM ceiling; permits guest RAM overcommit and possible OOM failures")
    enable.add_argument("--ci-disk-gib", type=int, help="GiB of writable guest disk per CI job")
    enable.add_argument("--ci-memory-pause-gib", type=int,
                        help="pause new CI guests while the CI slice uses at least this many GiB (0 disables); applies live")
    enable.add_argument("--ci-memory-resume-gib", type=int,
                        help="resume CI admissions once usage falls to this many GiB (default 85%% of the pause threshold)")
    enable.add_argument("--workers", action="store_true")
    enable.add_argument("--json", action="store_true")
    reserve = sub.add_parser("reserve-staging", help="reserve CI slots for staging as current guests finish")
    reserve.add_argument("host")
    reserve.add_argument("--slots", dest="maximum", type=int, required=True)
    reserve.add_argument("--json", action="store_true")
    workers = sub.add_parser("workers", help="change the admission ceiling without interrupting active work")
    workers.add_argument("host")
    workers.add_argument("--max", dest="maximum", type=int, required=True)
    workers.add_argument("--json", action="store_true")
    focus = sub.add_parser("focus", help="steer a share of roadmap slots without interrupting work")
    focus.add_argument("host")
    focus.add_argument("--percent", type=int)
    selection = focus.add_mutually_exclusive_group()
    selection.add_argument("--preset", choices=["pde-symplectic"])
    selection.add_argument("--areas", help="comma-separated roadmap directory names")
    selection.add_argument("--clear", action="store_true")
    focus.add_argument("--json", action="store_true")
    for name in ("_host", "_watch-status", "host-daemon", "run-workers", "collect-status"):
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


def print_isolation(status):
    isolation = status.get("worker_isolation", {})
    if isolation.get("events"):
        print("\nRecent userspace OOM events (separate from kernel OOM counters):")
        for event in isolation["events"][-3:]:
            when = datetime.fromtimestamp(event["at"], UTC).strftime("%m-%d %H:%M:%S UTC")
            recovered = sum(event.get("replacement_alive", {}).values())
            outcome = (f"{recovered}/{len(event['workers'])} replacements alive"
                       if event["workers"] else event.get("role", "other host unit"))
            print(f"  {when}: {event['unit']}; ancestor {event['ancestor']}; {outcome}")
    if isolation.get("error"):
        print("Isolation diagnostics: " + isolation["error"])


def main(argv=None, *, _status_results=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "_watch-status":
            from . import status_follow

            raw = sys.stdin.buffer.readline(65537)
            if len(raw) > 65536:
                raise FleetError("Live status request exceeds limit")
            status_follow.serve(json.loads(raw))
            return 0
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
        if args.command == "collect-status":
            from . import ci_overview

            ci_overview.collect(host.host_config())
            return 0
        if args.command == "run-workers":
            host.run_workers()
            return 0
        if args.command in ("add", "enroll"):
            value = remote.enroll(args.target, url=args.broker, token_file=args.token_file, replace_machine=args.replace_machine) if args.command == "enroll" else remote.add(args.target, profile_name=args.profile, no_ci=args.no_ci)
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
        if args.command == "enable" and not args.ci and any(v is not None for v in (args.max_jobs, args.ci_vcpus, args.ci_memory_gib, args.ci_disk_gib, args.ci_memory_budget_gib,
                                                                                 args.ci_memory_pause_gib, args.ci_memory_resume_gib)):
            raise FleetError("CI capacity options require --ci pr, staging, or both")
        if args.command == "budget" and (args.grant is not None or args.grant_rate is not None) and not args.host:
            raise FleetError("Budget changes require one explicit host; queries may aggregate the fleet")
        if args.command == "focus" and args.host == "local":
            local = read(config_dir() / "host.json")
            if not local or not local.get("machine_id"):
                raise FleetError("This account has no Fleet host configuration; use add or enroll first")
            selected = {"local": dict(target="local", machine_id=local["machine_id"])}
        else:
            selected = {args.host: remote.resolve(args.host)} if args.host else entries
        if not selected:
            raise FleetError("No hosts enrolled; use tauceti-fleet add user@host")
        if args.command == "status" and args.follow:
            from . import status_follow

            command = ["status"] + ([args.host] if args.host else []) + (["--json"] if args.json else [])
            return status_follow.run(selected, lambda values: status_follow.render_with_cli(command, values),
                                     json_output=args.json)
        results = {}
        for name, entry in selected.items():
            if _status_results is not None:
                results[name] = _status_results.get(name, dict(error="Waiting for prepared status"))
                continue
            action = args.command
            payload = dict(action=action)
            if args.command == "doctor":
                payload["smoke"] = args.smoke
            if args.command == "budget":
                payload.update(grant=args.grant, grant_rate=args.grant_rate)
            if args.command == "enable":
                payload.update(ci=args.ci, workers=args.workers, maximum=args.max_jobs,
                               vcpus=args.ci_vcpus, memory_gib=args.ci_memory_gib, disk_gib=args.ci_disk_gib,
                               memory_budget_gib=args.ci_memory_budget_gib,
                               memory_pause_gib=args.ci_memory_pause_gib, memory_resume_gib=args.ci_memory_resume_gib)
            if args.command == "workers":
                payload.update(maximum=args.maximum)
            if args.command == "reserve-staging":
                payload.update(maximum=args.maximum)
            if args.command == "focus":
                if args.percent is not None:
                    payload["percent"] = args.percent
                if args.preset:
                    payload["preset"] = args.preset
                if args.areas is not None:
                    payload["areas"] = [a.strip() for a in args.areas.split(",") if a.strip()]
                if args.clear:
                    if args.percent is not None:
                        raise FleetError("--clear cannot be combined with --percent")
                    payload["clear"] = True
            if args.command == "update":
                status = remote.call(entry["target"], dict(action="status"), expected_machine_id=entry["machine_id"])
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
                remote.call(entry["target"], dict(action="prepare_update"), expected_machine_id=entry["machine_id"])
                results[name] = remote.add(
                    entry["target"], profile_name=entry["profile"], no_ci=not status["ci_allowed"],
                    preserve_profile=status.get("ci_enrolled", False) or status["profile"] == "ci-enrolled" or entry.get("ci_enrolled", False) or entry["profile"] == "ci-enrolled",
                    expected_machine_id=entry["machine_id"],
                )
                continue
            cache = config_dir() / "status-cache" / (entry["machine_id"] + ".json")
            try:
                value = remote.call(
                    entry["target"],
                    payload,
                    timeout=1800 if action == "prepare" else 3 if action == "status" else 90,
                    expected_machine_id=entry["machine_id"],
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
        if args.command == "status":
            from . import ci_overview

            pool_names = {"fleet-" + e["machine_id"][:12]: name[:1].upper() + name[1:] for name, e in entries.items()}
            overview = ci_overview.combine([v.get("ci_overview", {}) for v in results.values()], pool_names)
            bors_printed = False

            def show_bors():
                nonlocal bors_printed
                if not bors_printed:
                    print()
                    ci_overview.display(overview, section="bors")
                    print()
                    bors_printed = True

            if args.json:
                results["_ci"] = overview
            else:
                ci_overview.display(overview, section="overview")
                print()
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
                        if _status_results is not None:
                            print(value.get("error", "Live reports unavailable"))
                        print()
                    workers = status.get("workers")
                    if isinstance(workers, list):
                        running = sum(w.get("state") == "running" for w in workers)
                        surveying = sum(w.get("state") == "surveying" for w in workers)
                        waiting = sum(w.get("state") == "waiting-capacity" for w in workers)
                        waiting_for = "RAM" if status.get("memory_pressure") else "admission"
                        print(f"Worker rounds: {running} running, {surveying} surveying, {waiting} waiting for {waiting_for}")
                    else:
                        print("Worker rounds: unknown")
                    print(f"New-round admission limit: {status.get('active_worker_limit', 0)} / {value.get('max_workers', 0)}")
                    if "ci_max_jobs" in value:
                        print(f"CI capacity: {status.get('ci_jobs_busy', 'unknown')} / {value['ci_max_jobs']} shared jobs; accepts {', '.join(value['ci_kinds'])}")
                    if "ci_memory_budget_gib" in value:
                        print(f"CI aggregate physical RAM ceiling: {value['ci_memory_budget_gib']} GiB")
                    guard = status.get("ci_memory_guard")
                    if guard:
                        used = "unknown" if guard.get("current_gib") is None else f"{guard['current_gib']:g} GiB"
                        state = "PAUSED new CI guests" if guard.get("paused") else "admitting"
                        print(f"CI memory guard: {state}; using {used}, pause at {guard['pause_gib']} GiB, "
                              f"resume at {guard['resume_gib']} GiB")
                    if value.get("ci_staging_slots"):
                        print(f"Reserved for staging: {value['ci_staging_slots']} slots")
                    if value.get("ci_staging_reservation_error"):
                        print("Staging reservation invalid: " + value["ci_staging_reservation_error"])
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
                        if memory.get("swap"):
                            print(f"  Includes {memory['swap'] / gib:.1f} GiB swapped out")
                        print(
                            f"Worker limits: {memory['high'] / gib:.1f} GiB soft / {memory['maximum'] / gib:.1f} GiB hard"
                        )
                        if memory.get("allocation_error"):
                            print(f"Memory allocation error: {memory['allocation_error']}")
                        print("Worker memory reclamation waits (1-minute averages; informational)")
                        print(
                            f"  Time with at least one task waiting: {memory['stall_some']:.2f}%; all active tasks waiting: {memory['stall_full']:.2f}%"
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
                    if decision and decision.get("policy") == "opportunistic":
                        print(f"CPU idle: {status['cpu_idle_fraction']:.1%}; workers yield CPU to CI")
                    elif decision:
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
                    if status.get("roadmap_focus_error"):
                        print("Roadmap focus disabled: " + status["roadmap_focus_error"])
                    focus = status.get("roadmap_focus", {})
                    if focus.get("percent"):
                        print(
                            f"Roadmap focus: {focus['percent']}% ({len(focus.get('workers', []))} slots"
                            + (
                                f" + {focus['fractional_percent']}% of one slot"
                                if focus.get("fractional_percent")
                                else ""
                            )
                            + "); "
                            + ", ".join(focus["areas"])
                        )
                    if status.get("workers"):
                        print()
                    groups = {}
                    for worker in status.get("workers", []):
                        state = worker.get("state", "unknown")
                        if state == "waiting-capacity":
                            state = "waiting for RAM headroom" if status.get("memory_pressure") else "waiting for admission"
                        error = worker.get("isolation_error")
                        key = (state, worker.get("detail") or "", error, worker["id"] if error else None)
                        groups.setdefault(key, []).append(worker["id"])
                    for (state, detail, error, _), workers in groups.items():
                        label = workers[0] if len(workers) == 1 else f"{len(workers)} workers"
                        print(f"  {label}: {state} — {detail}")
                        if error:
                            print("    Isolation degraded: " + error)
                    print()
                    activity.display(value.get("activity", {}), storage=False,
                                     after_budget=show_bors if fleet is None else None)
                else:
                    print(json.dumps(value, indent=2))
            if fleet is not None:
                print("\nFleet totals:")
                activity.display(fleet, storage=False, recovery=False, after_budget=show_bors)
            if args.command == "status":
                show_bors()
                for name, value in results.items():
                    status = value.get("status", {})
                    isolation = status.get("worker_isolation", {})
                    summary = value.get("activity", {}).get("summary") or {}
                    storage = summary.get("storage_cleanup") or summary.get("expanded_cache", {}).get("workers")
                    if isolation.get("events") or isolation.get("error") or storage:
                        print(f"\n{name} diagnostics:")
                        print_isolation(status)
                        if storage:
                            print()
                            activity.display_storage(value.get("activity", {}))
        return 1 if any("error" in value for value in results.values()) else 0
    except (FleetError, OSError, ValueError) as exc:
        print("tauceti-fleet: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
