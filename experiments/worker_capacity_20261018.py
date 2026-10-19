"""One-off 2026-10-18 capacity experiment; deliberately excluded from the fleet package."""

import datetime as dt
import http.client
import json
import os
import signal
import time
import urllib.request
from collections import Counter
from decimal import Decimal
from pathlib import Path

from tauceti_fleet.capabilities import memory_pressure
from tauceti_fleet.store import FleetError, atomic, event, host_config, locked, read, state_dir

EXPERIMENT_DIR = Path.home() / ".local/state/tauceti-fleet-experiments/worker-capacity-20261018"


def experiment_dir():
    return Path(os.environ.get("TAUCETI_CAPACITY_EXPERIMENT_DIR", EXPERIMENT_DIR))


def set_maximum(maximum, *, experiment=False):
    from tauceti_fleet.host import set_worker_maximum

    return set_worker_maximum(maximum, lease_seconds=120 if experiment else None)


def queue_depth():
    req = urllib.request.Request(
        "https://bors.taucetiproject.org/repositories/1/active-batches?base=main",
        headers={"Cache-Control": "no-cache", "User-Agent": "TauCetiFleetRamp/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise FleetError("Bors observation exceeds the bound")
    data = json.loads(raw)
    if (
        data.get("schema") != "tauceti-bors.observation/v1"
        or data.get("repo") != "TauCetiProject/TauCeti"
        or data.get("base") != "main"
        or not isinstance(data.get("batches"), list)
    ):
        raise FleetError("Bors observation scope is invalid")
    return len({m["pr"] for b in data["batches"] if b["state"] in ("waiting", "running") for m in b["members"]})


def sample():
    from tauceti_fleet import host

    now = time.time()
    status = read(state_dir() / "status.json", {})
    if now - status.get("observed_at", 0) > 30 or status.get("error"):
        raise FleetError("Host controller observations are unavailable or stale")
    config = host_config()
    resources = status["resources"]
    memory = status["worker_memory"]
    workers = host.worker_statuses()
    budget = host.budget({})
    if budget.get("ledger_repair_required") or budget.get("unresolved"):
        raise FleetError("Budget ledger requires reconciliation")
    return dict(
        at=now,
        maximum=config["max_workers"],
        admitted=status["active_worker_limit"],
        workers_enabled=config["workers_enabled"],
        ci_busy=status["ci_busy"],
        cpu_idle=status["cpu_idle_fraction"],
        cpus=resources["cpus"],
        memory_available=resources["memory_available"],
        memory_total=resources["memory_total"],
        worker_memory=memory["charged"],
        memory=memory,
        worker_memory_high=memory["high"],
        worker_oom_kill=memory["oom_kill"],
        worker_states=dict(Counter(w.get("state", "unknown") for w in workers if w.get("alive"))),
        workers_alive=sum(bool(w.get("alive")) for w in workers),
        queue_prs=queue_depth(),
        spent=budget["spent"],
        pending=budget["pending"],
        available=budget["available"],
        grant_rate=budget["grant_rate"],
        waiting_budget=len(budget.get("waiting", [])),
    )


def advance(value, observation):
    """Decide from timestamped observations; schedules never catch up in bursts."""
    now = observation["at"]
    previous = value.get("last_sample", {}).get("at")
    if previous is not None and now - previous > 120:
        value["condition_since"] = {}
        value["next_increase_at"] = now + value["interval_seconds"]
    value["last_sample"] = observation
    if value["phase"] != "running":
        return value, None
    history = value.setdefault("history", [])
    history.append(observation)
    history[:] = [s for s in history if s["at"] >= now - 1860]
    elapsed = now - value["started_at"]
    value["stage_admitted_peak"] = max(value.get("stage_admitted_peak", 0), observation["admitted"])
    oldest = history[0]
    span = now - oldest["at"]
    rate = max(Decimal(0), Decimal(observation["spent"]) - Decimal(oldest["spent"])) * 3600 / Decimal(str(max(1, span)))
    observation["settled_usd_per_hour"] = str(rate) if span >= 1800 else None
    conditions = dict(
        merge_queue=observation["queue_prs"] > value["queue_limit"],
        memory=(
            bool(
                memory_pressure(
                    observation.get(
                        "memory",
                        dict(
                            working_set=observation["worker_memory"],
                            high=observation["worker_memory_high"],
                            stall_some=0,
                            stall_full=0,
                        ),
                    ),
                    observation,
                )
            )
            or observation["worker_oom_kill"] > value["baseline_oom_kill"]
        ),
        cpu=observation["cpu_idle"] < (0.35 if observation["ci_busy"] else 0.25),
        budget=(
            elapsed >= 1800
            and (
                observation["waiting_budget"] > 0
                or observation["worker_states"].get("waiting-budget", 0) > 0
                or (span >= 1800 and rate >= Decimal(observation["grant_rate"]))
            )
        ),
    )
    since = value.setdefault("condition_since", {})
    reached = []
    for name, active in conditions.items():
        if active:
            since.setdefault(name, now)
            urgent = name == "merge_queue" or (
                name == "memory" and observation["worker_oom_kill"] > value["baseline_oom_kill"]
            )
            delay = 0 if urgent else 300
            if now - since[name] >= delay:
                reached.append(name)
        else:
            since.pop(name, None)
    if not observation["workers_enabled"]:
        reached.append("workers_disabled")
    if reached:
        value.update(
            phase="stopped",
            stopped_at=now,
            limiting_factors=reached,
            maximum_at_stop=value["maximum"],
            last_sample=observation,
        )
        # Resource thresholds stop the experiment's increases. The ordinary
        # controller still governs memory/CPU admissions and the budget ledger
        # governs spending. Only urgent protective stops pause all new work.
        pause = (
            "merge_queue" in reached
            or "workers_disabled" in reached
            or observation["worker_oom_kill"] > value["baseline_oom_kill"]
        )
        return value, 0 if pause else value["maximum"]
    # Do not increase during even a transient limit, or until the existing
    # configured workers have actually launched. Non-resource failures are explicit.
    ready = (
        observation["workers_alive"] >= value["maximum"]
        and value["stage_admitted_peak"] >= value["maximum"]
        and not any(observation["worker_states"].get(s, 0) for s in ("failed", "waiting-quota", "waiting-github"))
    )
    value["hold_reason"] = ", ".join(k for k, v in conditions.items() if v) or (None if ready else "workers not ready")
    if now >= value["next_increase_at"] and not any(conditions.values()) and ready:
        if value["maximum"] >= value["ceiling"]:
            value.update(
                phase="stopped",
                stopped_at=now,
                limiting_factors=["experiment_ceiling"],
                maximum_at_stop=value["maximum"],
            )
            return value, value["maximum"]
        value["maximum"] += 1
        value["stage_admitted_peak"] = 0
        value["next_increase_at"] = now + value["interval_seconds"]
        return value, value["maximum"]
    return value, None


def public(value):
    return {k: v for k, v in value.items() if k != "history"}


def daemon():
    path = experiment_dir() / "state.json"
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        try:
            observation, error = sample(), None
        except (FleetError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException) as exc:
            observation, error = None, type(exc).__name__ + ": " + str(exc)
        with locked(experiment_dir() / "state.lock"):
            value = read(path, {})
            if value.get("phase") != "running":
                return
            if value.get("lease_established") and not (state_dir() / "worker-admission-lease.json").exists():
                value.update(phase="stopped", stopped_at=time.time(), limiting_factors=["manual_worker_control"])
                atomic(path, value)
                atomic(experiment_dir() / "report.json", public(value))
                return
            now = time.time()
            value["heartbeat_at"] = now
            if error:
                value.setdefault("observation_failure_since", now)
                value["condition_since"] = {}
                value.update(hold_reason="observations unavailable", observation_error=error)
                # Missing evidence cannot establish any resource bottleneck.
                if now - value["observation_failure_since"] >= 300:
                    value.update(phase="stopped", stopped_at=now, limiting_factors=["observation_unavailable"])
                set_maximum(0, experiment=value["phase"] == "running")
            else:
                if value.pop("observation_failure_since", None) is not None:
                    value["next_increase_at"] = now + value["interval_seconds"]
                value.pop("observation_error", None)
                value, maximum = advance(value, observation)
                set_maximum(value["maximum"] if maximum is None else maximum, experiment=value["phase"] == "running")
                sample_path = experiment_dir() / "samples.jsonl"
                fd = os.open(sample_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as stream:
                    stream.write(json.dumps(observation) + "\n")
            value["lease_established"] = True
            atomic(path, value)
            atomic(experiment_dir() / "report.json", public(value))
            if value.get("phase") != "running":
                event("worker_ramp_stopped", result=public(value))
                return
        for _ in range(60):
            if stopping:
                break
            time.sleep(1)
    # Stopping the experiment ends its increases and releases its lease. Leave
    # the current ceiling under ordinary controller admission/resource checks.
    set_maximum(host_config()["max_workers"])


def print_status(value):
    print(f"Experiment: {value.get('phase', 'unknown')}; worker ceiling: {value.get('maximum', 0)}")
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
        memory = sample.get("memory")
        if memory:
            print(
                f"Worker memory: {memory['charged'] / gib:.1f} GiB charged; {memory['working_set'] / gib:.1f} GiB estimated working set; {memory['file'] / gib:.1f} GiB file cache"
            )
            print(f"Memory stalls: {memory['stall_some']:.2f}% partial / {memory['stall_full']:.2f}% full (60s)")
        else:
            print(f"Historical charged worker memory: {sample['worker_memory'] / gib:.1f} GiB")
        print(
            f"Budget: ${Decimal(sample['available']):.2f} available, ${sample['pending']} pending; ${sample['grant_rate']}/hour replenishment"
        )
        print("Worker states: " + json.dumps(sample["worker_states"]))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="One-off worker capacity experiment started 2026-10-18")
    parser.add_argument("action", choices=["run", "status", "stop"], default="status", nargs="?")
    args = parser.parse_args()
    if args.action == "run":
        daemon()
    elif args.action == "stop":
        with locked(experiment_dir() / "state.lock"):
            value = read(experiment_dir() / "state.json", {})
            value.update(phase="stopped", stopped_at=time.time(), limiting_factors=["operator"])
            atomic(experiment_dir() / "state.json", value)
            set_maximum(host_config()["max_workers"])
        print_status(public(value))
    else:
        print_status(public(read(experiment_dir() / "state.json", {})))
