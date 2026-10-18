"""Persistent, measured worker admission experiment; never terminates paid work."""

import http.client
import json
import os
import signal
import time
import urllib.request
from collections import Counter
from decimal import Decimal
from pathlib import Path

from .capabilities import GIB
from .process import run
from .store import FleetError, atomic, event, host_config, locked, read, state_dir, update_host


def set_maximum(maximum, *, experiment=False):
    if type(maximum) is not int or not 0 <= maximum <= 4096:
        raise FleetError("Worker maximum must be an integer from 0 to 4096")
    if not experiment and read(state_dir() / "ramp.json", {}).get("phase") == "running":
        raise FleetError("Stop the active ramp before manually changing its worker ceiling")
    update_host(max_workers=maximum)
    if maximum == 0:
        atomic(
            state_dir() / "admission.json",
            dict(version=1, expires_at=time.time() + 15, workers=[], reason="new worker work paused"),
        )
    return dict(max_workers=maximum, detail="Admission ceiling; already admitted sessions finish")


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
    from . import host

    now = time.time()
    status = read(state_dir() / "status.json", {})
    if now - status.get("observed_at", 0) > 30 or status.get("error"):
        raise FleetError("Host controller observations are unavailable or stale")
    config = host_config()
    resources = status["resources"]
    result = run(
        [
            "systemctl",
            "--user",
            "show",
            "tauceti-fleet-workers.slice",
            "-p",
            "MemoryCurrent",
            "-p",
            "MemoryHigh",
            "-p",
            "ControlGroup",
        ]
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    cg = Path("/sys/fs/cgroup") / fields["ControlGroup"].lstrip("/")
    memory_events = dict(line.split() for line in (cg / "memory.events").read_text().splitlines())
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
        worker_memory=int(fields["MemoryCurrent"]),
        worker_memory_high=int(fields["MemoryHigh"]),
        worker_oom_kill=int(memory_events.get("oom_kill", 0)),
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
    reserve = max(4 * GIB, int(observation["memory_total"] * 0.1))
    oldest = history[0]
    span = now - oldest["at"]
    rate = max(Decimal(0), Decimal(observation["spent"]) - Decimal(oldest["spent"])) * 3600 / Decimal(str(max(1, span)))
    observation["settled_usd_per_hour"] = str(rate) if span >= 1800 else None
    conditions = dict(
        merge_queue=observation["queue_prs"] > value["queue_limit"],
        memory=(
            observation["memory_available"] < reserve
            or observation["worker_memory"] >= observation["worker_memory_high"]
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
            delay = 0 if name == "merge_queue" or observation["worker_oom_kill"] > value["baseline_oom_kill"] else 300
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
        return value, 0
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
            return value, 0
        value["maximum"] += 1
        value["stage_admitted_peak"] = 0
        value["next_increase_at"] = now + value["interval_seconds"]
        return value, value["maximum"]
    return value, None


def control(payload):
    from .host import unit_argument, units

    path = state_dir() / "ramp.json"
    with locked(state_dir() / "ramp.lock"):
        value = read(path, {})
        if payload.get("stop"):
            if value:
                value.update(phase="stopped", stopped_at=time.time(), limiting_factors=["operator"])
                atomic(path, value)
            set_maximum(0, experiment=True)
            return public(value)
        if not payload.get("start"):
            return public(value) if value else dict(phase="not_started")
        if value.get("phase") == "running":
            raise FleetError("A ramp is already running; inspect or stop it first")
        interval = payload.get("interval_minutes", 30)
        limit, ceiling = payload.get("queue_limit", 40), payload.get("ceiling", 64)
        if any(type(v) is not int or v <= 0 for v in (interval, limit, ceiling)) or ceiling > 4096:
            raise FleetError("Interval, queue limit and ceiling must be positive integers; ceiling at most 4096")
        if not host_config()["workers_enabled"]:
            raise FleetError("Enable workers with an admission maximum of zero before starting the ramp")
        baseline = sample()
        if baseline["queue_prs"] > limit:
            raise FleetError("Merge queue is already over the experiment threshold; workers remain paused")
        now = time.time()
        value = dict(
            version=1,
            phase="running",
            started_at=now,
            heartbeat_at=now,
            maximum=1,
            interval_seconds=interval * 60,
            next_increase_at=now + interval * 60,
            queue_limit=limit,
            ceiling=ceiling,
            baseline_oom_kill=baseline["worker_oom_kill"],
            baseline=baseline,
            history=[],
            condition_since={},
        )
        atomic(path, value)
        # The first admission is made by the supervisor only after it is running.
        target = units() / "tauceti-fleet-ramp.service"
        target.write_text(
            "[Unit]\nDescription=TauCeti worker capacity ramp\nAfter=tauceti-fleet.service\n"
            "[Service]\nType=simple\nExecStart="
            + unit_argument(Path.home() / ".local/bin/tauceti-fleet")
            + " ramp-daemon\nRestart=on-failure\nRestartSec=15\n"
            "[Install]\nWantedBy=default.target\n"
        )
        run(["systemctl", "--user", "daemon-reload"])
        try:
            run(["systemctl", "--user", "enable", "--now", target.name])
        except FleetError:
            value.update(phase="stopped", limiting_factors=["supervisor_start_failed"])
            atomic(path, value)
            raise
        return public(value)


def public(value):
    return {k: v for k, v in value.items() if k != "history"}


def daemon():
    path = state_dir() / "ramp.json"
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
        with locked(state_dir() / "ramp.lock"):
            value = read(path, {})
            if value.get("phase") != "running":
                return
            now = time.time()
            value["heartbeat_at"] = now
            if error:
                value.setdefault("observation_failure_since", now)
                value["condition_since"] = {}
                value.update(hold_reason="observations unavailable", observation_error=error)
                set_maximum(0, experiment=True)
                # Missing evidence cannot establish any resource bottleneck.
                if now - value["observation_failure_since"] >= 300:
                    value.update(phase="stopped", stopped_at=now, limiting_factors=["observation_unavailable"])
            else:
                if value.pop("observation_failure_since", None) is not None:
                    value["next_increase_at"] = now + value["interval_seconds"]
                value.pop("observation_error", None)
                value, maximum = advance(value, observation)
                set_maximum(value["maximum"] if maximum is None else maximum, experiment=True)
                sample_path = state_dir() / "ramp-samples.jsonl"
                fd = os.open(sample_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as stream:
                    stream.write(json.dumps(observation) + "\n")
            atomic(path, value)
            atomic(state_dir() / "ramp-report.json", public(value))
            if value.get("phase") != "running":
                event("worker_ramp_stopped", result=public(value))
                return
        for _ in range(60):
            if stopping:
                break
            time.sleep(1)
    # No unattended increases or paid admissions without a live supervisor.
    set_maximum(0, experiment=True)
