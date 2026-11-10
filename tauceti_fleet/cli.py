"""Operator CLI and host-side JSON protocol."""

import argparse
import json
import math
import sys
import time
from datetime import UTC, datetime

from . import activity, broker, host, pool_activity, remote
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
            cmd.add_argument("--timezone", default="Australia/Sydney", help="merge-queue timestamp timezone (default: Australia/Sydney)")
            cmd.add_argument("--all-jobs", action="store_true", help="include CI control jobs in the pool activity table")
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
    enable.add_argument("--ci-vcpus", type=int, help="vCPUs per VM job (staging/mixed registrations)")
    enable.add_argument("--ci-memory-gib", type=int, help="GiB of VM RAM per staging/mixed job")
    enable.add_argument("--ci-memory-budget-gib", type=int, help="aggregate physical CI RAM ceiling; permits guest RAM overcommit and possible OOM failures")
    enable.add_argument("--ci-disk-gib", type=int, help="GiB of writable virtual disk per VM job")
    enable.add_argument("--workers", action="store_true")
    enable.add_argument("--json", action="store_true")
    native = sub.add_parser("native-runner", help="inspect or refresh the standard disk-backed PR runner image")
    native.add_argument("host")
    native.add_argument("--prepare", action="store_true")
    native.add_argument("--json", action="store_true")
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
    monitor = sub.add_parser("monitoring", help="publish prepared observations independently of host work")
    monitor.add_argument("arguments", nargs=argparse.REMAINDER)
    install_monitor = sub.add_parser("install-monitoring", help="enable only the local telemetry sidecar")
    install_monitor.add_argument("--alias", required=True)
    for name in ("_host", "_watch-status", "host-daemon", "run-workers", "collect-status"):
        sub.add_parser(name, help=argparse.SUPPRESS)
    return p


def focused_rounds(workers):
    """Describe focus among rounds in progress; slot assignment only affects the next round start."""
    active = [w for w in workers or [] if w.get("state") in ("running", "surveying")]
    reporting = [w for w in active if "roadmap_focus" in w]
    unreported = len(active) - len(reporting)
    if active and not reporting:
        return f"{len(active)} rounds in progress, focus not reported by this worker release"
    # During a worker rollout, rounds on the older release are neither focused nor unfocused.
    text = f"{sum(bool(w['roadmap_focus']) for w in reporting)} of {len(reporting)} rounds in progress focused"
    return text + (f", {unreported} more on a worker release without focus reporting" if unreported else "")


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
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw[:1] == ["monitoring"]:
        from .monitoring import main as monitoring_main
        return monitoring_main(raw[1:])
    if raw[:1] == ["install-monitoring"]:
        from .monitoring import install_local
        args = parser().parse_args(raw)
        print(json.dumps(install_local(args.alias)))
        return 0
    args = parser().parse_args(argv)
    try:
        if args.command == "status":
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(args.timezone)
            except (ValueError, ZoneInfoNotFoundError):
                raise FleetError("Unknown timezone: " + args.timezone) from None
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
        if args.command == "enable" and not args.ci and any(v is not None for v in (args.max_jobs, args.ci_vcpus, args.ci_memory_gib, args.ci_disk_gib, args.ci_memory_budget_gib)):
            raise FleetError("CI capacity options require --ci pr, staging, or both")
        if args.command == "budget" and (args.grant is not None or args.grant_rate is not None) and not args.host:
            raise FleetError("Budget changes require one explicit host; queries may aggregate the fleet")
        if args.host == "local":
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

            command = (["status"] + ([args.host] if args.host else []) + (["--json"] if args.json else [])
                       + (["--all-jobs"] if args.all_jobs else []) + (["--timezone", args.timezone] if args.timezone != "Australia/Sydney" else []))
            return status_follow.run(selected, lambda values: status_follow.render_with_cli(command, values),
                                     json_output=args.json, refresh=pool_activity.refresh)
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
                               memory_budget_gib=args.ci_memory_budget_gib)
            if args.command == "native-runner":
                if args.prepare:
                    payload["prepare"] = True
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
                options = dict(live=True, expected_machine_id=entry["machine_id"])
                results[name] = remote.deploy(entry["target"], **options)
                continue
            cache = config_dir() / "status-cache" / (entry["machine_id"] + ".json")
            try:
                value = remote.call(
                    entry["target"],
                    payload,
                    timeout=4500 if action == "prepare" else 2400 if action in ("enable", "reserve-staging") or (action == "native-runner" and args.prepare) else 3 if action == "status" else 90,
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
            overview["pool_activity"] = pool_activity.cached() if _status_results is not None else pool_activity.refresh()
            from . import queue_history, queue_status
            overview["queue"] = overview["pool_activity"].get("queue") or queue_status.cached()
            overview["queue_history"] = queue_history.cached()
            overview["queue_timezone"] = args.timezone
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
                pool_activity.display(overview["pool_activity"], all_jobs=args.all_jobs)
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
                    pressure = value.get("lean_host_pressure")
                    if pressure is not None:
                        health = "healthy" if pressure.get("healthy") else "STALE or unavailable; new CI compilers wait"
                        print(f"Lean host admission: {health}; {pressure.get('guests', 0)} guests connected; "
                              f"{pressure.get('waiting_guests', 0)} guests requesting starts")
                        priority = pressure.get("staging_priority")
                        if priority:
                            print(f"Staging priority: CPU sleeps off; {priority['headroom_gib']:.1f} GiB RAM headroom "
                                  f"vs {priority['normal_headroom_gib']:.1f} GiB for other CI; first compiler permits")
                    if value.get('lean_statistics'):
                        from .lean_statistics import display as show_lean_statistics

                        show_lean_statistics(value['lean_statistics'])
                    if "ci_max_jobs" in value:
                        print(f"CI capacity: {status.get('ci_jobs_busy', 'unknown')} / {value['ci_max_jobs']} shared jobs; accepts {', '.join(value['ci_kinds'])}")
                    if value.get("ci_autoscale"):
                        auto = value["ci_autoscale"]
                        age = max(0, time.time() - auto.get("at", 0))
                        message = auto.get("error") or auto.get("reason", "")
                        print(f"CI autoscale: target {auto.get('target', '?')} / {auto.get('ceiling', '?')}; {message}"
                              + (f" (stale {age:.0f}s)" if age > 90 else ""))
                    if "ci_memory_budget_gib" in value:
                        print(f"CI aggregate physical RAM ceiling: {value['ci_memory_budget_gib']} GiB")
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
                    shared = status.get("shared_host")
                    if shared:
                        print(f"Shared machine: {shared['peers']} other installs with {shared['peer_workers']} workers; "
                              f"this pool may take {shared['share']:.0%} of spare RAM"
                              + ("; CI is building in another install" if shared["ci_busy"] else "")
                              + ("; " + shared["error"] if shared["error"] else ""))
                    if status.get("roadmap_focus_error"):
                        print("Roadmap focus disabled: " + status["roadmap_focus_error"])
                    focus = status.get("roadmap_focus", {})
                    if focus.get("percent"):
                        print(
                            f"Roadmap focus: {focus['percent']}%; "
                            + focused_rounds(status.get("workers"))
                            + f" ({len(focus.get('workers', []))} slots"
                            + (
                                f" + {focus['fractional_percent']}% of one slot"
                                if focus.get("fractional_percent")
                                else ""
                            )
                            + " at the current admission limit); "
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
