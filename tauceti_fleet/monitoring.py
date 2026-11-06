"""Public operations bridge: persistent identity-checked prepared streams only.

Run independently of the host daemon. The private registry maps approved public
aliases to permanent identities and SSH targets; only the aliases and identities
leave this process. No collector, resource probe or admission mutation runs here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import http.client
import json
import math
import os
import re
import signal
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from . import status_follow


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def stamp(value):
    value = number(value)
    return dt.datetime.fromtimestamp(value, dt.UTC).isoformat().replace("+00:00", "Z") if value else None


def text(value, n=100):
    return re.sub(r"[\x00-\x1f\x7f]", "", value)[:n] if isinstance(value, str) else ""


def identity(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def project(entry, value, transport_at=None, connected=False):
    if value.get("machine_id") not in (None, entry["machine_id"]):
        raise ValueError("permanent host identity mismatch")
    state = value.get("status") or {}
    resources = state.get("resources") or {}
    pressure = value.get("lean_host_pressure") or {}
    workers = state.get("workers") or []
    slots = state.get("ci_vms") or []
    instances = (value.get("lean_statistics") or {}).get("instances") or {}
    summary = (value.get("activity") or {}).get("summary") or {}
    rounds = (summary.get("rounds") or {}).get("24h")

    def round_count(key):
        if not isinstance(rounds, dict):
            return None
        values = [number(row.get(key)) for row in rounds.values() if isinstance(row, dict)]
        return sum(values) if len(values) == len(rounds) and all(v is not None for v in values) else None

    def compiler(instance):
        row = instances.get(instance)
        if not isinstance(row, dict):
            return None
        return {
            "observed_at": stamp(row.get("observed_at")),
            **{
                k: number(row.get(k))
                for k in (
                    "waiting_now",
                    "running_now",
                    "completed",
                    "oldest_wait",
                    "wait_seconds",
                    "run_seconds",
                    "lost",
                )
            },
        }

    return {
        "machine_id": entry["machine_id"],
        "alias": entry["alias"],
        "observed_at": stamp(state.get("observed_at")),
        "transport_at": stamp(transport_at),
        "connected": connected,
        "coverage": "observed" if state.get("observed_at") else "unavailable",
        "source_revision": identity(Path(text(state.get("controller_release"), 2000)).name, r"[a-f0-9]{40,64}") or "",
        "controller_error": bool(state.get("error")),
        "ci_enabled": value.get("ci_enabled") if type(value.get("ci_enabled")) is bool else None,
        "workers_enabled": value.get("workers_enabled") if type(value.get("workers_enabled")) is bool else None,
        "ci_max_jobs": number(value.get("ci_max_jobs")),
        "worker_limit": number(state.get("active_worker_limit")),
        "admission": {
            "ci_kinds": value.get("ci_kinds")
            if value.get("ci_kinds") in (["pr"], ["staging"], ["pr", "staging"])
            else None,
            "staging_slots": number(value.get("ci_staging_slots")),
            "memory_budget_gib": number(value.get("ci_memory_budget_gib")),
            "reservation_error": bool(value.get("ci_staging_reservation_error")),
        },
        "activity": {
            "observed_at": stamp(summary.get("prepared_at")),
            "window_hours": 24,
            "finished_rounds": round_count("finished"),
            "failed_rounds": round_count("failed"),
            "unknown_rounds": round_count("unknown"),
        },
        "resources": {k: number(resources.get(k)) for k in ("cpus", "memory_total", "memory_available")},
        "cpu_idle_fraction": number(state.get("cpu_idle_fraction")),
        "memory_pressure": bool(state.get("memory_pressure")) if "memory_pressure" in state else None,
        "compiler": {
            "observed_at": stamp(pressure.get("at")),
            "waiting_guests": number(pressure.get("waiting_guests")),
            "guests": number(pressure.get("guests")),
            "healthy": pressure.get("healthy") is True,
        },
        "slots": [
            {
                "slot": number(v.get("slot")),
                "instance_id": identity(v.get("instance"), r"[a-f0-9]{16}"),
                "runner_id": str(v["runner_id"]) if str(v.get("runner_id", "")).isdigit() else None,
                "runner_name": text(v.get("runner_name")),
                "phase": text(v.get("phase"), 40),
                "busy": v.get("busy") is True,
                "started_at": stamp(v.get("started_at")),
                "vcpus": number(v.get("vcpus")),
                "memory_gib": number(v.get("memory_gib")),
                "compiler": compiler(v.get("instance")),
            }
            for v in slots[:128]
            if isinstance(v, dict)
        ],
        "workers": [
            {
                "id": text(w.get("worker") or w.get("id")),
                "state": text(w.get("state"), 40),
                "alive": w.get("alive") is True,
                "observed_at": stamp(w.get("heartbeat_at") or w.get("updated_at")),
            }
            for w in workers[:128]
            if isinstance(w, dict)
        ],
        "slots_truncated": len(slots) > 128,
        "workers_truncated": len(workers) > 128,
    }


def registry(path):
    entries = json.loads(Path(path).read_text())
    if not isinstance(entries, list) or not 0 < len(entries) <= 100:
        raise ValueError("registry must contain 1–100 explicitly approved hosts")
    seen = set()
    for entry in entries:
        if set(entry) != {"alias", "machine_id", "target"} or not re.fullmatch(r"[a-f0-9]{32}", entry["machine_id"]):
            raise ValueError("invalid private registry")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", entry["alias"]) or entry["machine_id"] in seen:
            raise ValueError("duplicate or invalid public alias/identity")
        seen.add(entry["machine_id"])
    return entries


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path)
    parser.add_argument(
        "--local-broker", action="store_true", help="read only this host and publish through its existing enrollment"
    )
    parser.add_argument("--alias", help="approved public alias for local broker publication")
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--publish-url")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--interval", type=float, default=15)
    args = parser.parse_args(argv)
    if not 10 <= args.interval <= 60 or (
        args.publish_url and (not args.token_file or not args.publish_url.startswith("https://"))
    ):
        parser.error("publication needs HTTPS and a token file; interval must be 10–60s")
    if args.local_broker:
        from .store import host_config

        config = host_config()
        if (args.alias and not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", args.alias)) or not config.get("ci_broker_url"):
            parser.error("local publication requires an enrolled host")
        entries = [{"machine_id": config["machine_id"], "target": "local", "alias": args.alias or "unconfigured"}]
    elif args.registry:
        entries = registry(args.registry)
    else:
        parser.error("choose --registry or --local-broker")
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(args.state_file.with_suffix(".lock"), "a+") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = (
            json.loads(args.state_file.read_text())
            if args.state_file.exists()
            else {
                "id": str(uuid.UUID(entries[0]["machine_id"])) if args.local_broker else str(uuid.uuid4()),
                "epoch": 0,
            }
        )
        source = {"id": str(uuid.UUID(saved["id"])), "epoch": max(saved["epoch"] + 1, time.time_ns() // 1_000_000)}
        fd = os.open(args.state_file.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(args.state_file.with_suffix(".tmp"), args.state_file)
        directory = os.open(args.state_file.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return run(entries, source, args)


def run(entries, source, args):
    stop, lock, process_lock = threading.Event(), threading.Lock(), threading.Lock()
    processes, threads, observations = set(), [], {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def accept(entry, frame):
        with lock:
            previous = observations.get(entry["machine_id"], {})
            value = previous.get("value", {})
            if frame.get("ok"):
                value = frame["result"]
                if value.get("machine_id") != entry["machine_id"]:
                    return
            observations[entry["machine_id"]] = {
                "value": value,
                "at": time.time(),
                "connected": bool(frame.get("ok") or frame.get("heartbeat")),
            }

    for entry in entries:
        thread = threading.Thread(
            target=status_follow.receive,
            args=(entry, lambda frame, e=entry: accept(e, frame), stop, processes, process_lock),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    opener, sequence = urllib.request.build_opener(NoRedirect), 0
    local_policy, policy_due = None, 0
    try:
        while not stop.is_set():
            if args.local_broker and time.monotonic() >= policy_due:
                from .broker import request as broker_request
                from .store import host_config

                try:
                    local_policy = broker_request(host_config(), "/v1/monitoring-policy")
                    alias = local_policy.get("alias")
                    if (
                        local_policy.get("enabled")
                        and isinstance(alias, str)
                        and re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", alias)
                    ):
                        entries[0]["alias"] = alias
                    else:
                        local_policy = None
                except (status_follow.FleetError, OSError, ValueError):
                    local_policy = None
                policy_due = time.monotonic() + 300
            if args.local_broker and local_policy is None:
                stop.wait(args.interval)
                continue
            sequence += 1
            with lock:
                hosts = [
                    project(
                        entry,
                        observations.get(entry["machine_id"], {}).get("value", {}),
                        observations.get(entry["machine_id"], {}).get("at"),
                        observations.get(entry["machine_id"], {}).get("connected", False),
                    )
                    for entry in entries
                ]
            value = {
                "schema": "tauceti.operations.hosts-update/v1",
                "source": dict(source, sequence=sequence),
                "observed_at": stamp(time.time()),
                "hosts": hosts,
            }
            data = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
            try:
                if len(data) > 256_000:
                    raise ValueError("host projection exceeds publication limit")
                if args.output:
                    args.output.with_suffix(".tmp").write_bytes(data)
                    os.replace(args.output.with_suffix(".tmp"), args.output)
                if args.local_broker:
                    # Host-bound enrollment token goes only to its own broker.
                    from .broker import request as broker_request
                    from .store import host_config

                    broker_request(host_config(), "/v1/monitoring", method="POST", data=value)
                if args.publish_url:
                    request = urllib.request.Request(
                        args.publish_url,
                        data=data,
                        headers={
                            "Authorization": "Bearer " + args.token_file.read_text().strip(),
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with opener.open(request, timeout=10) as response:
                        response.read(65536)
            except (OSError, ValueError, http.client.HTTPException, status_follow.FleetError) as error:
                print("fleet monitoring: " + type(error).__name__ + "; retrying", file=__import__("sys").stderr)
            stop.wait(args.interval)
    finally:
        stop.set()
        with process_lock:
            for process in processes:
                process.terminate()
        for thread in threads:
            thread.join(timeout=5)
    return 0


def install_local(alias=None):
    """Install the independent sidecar during ordinary enable, without restarting work."""
    from .host import command, unit_argument, units
    from .process import run as command_run
    from .store import host_config, state_dir

    config = host_config()
    if not config.get("ci_broker_url") or (alias is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", alias)):
        raise ValueError("monitoring needs an enrolled host and approved public alias")
    args = [
        *command(),
        "monitoring",
        "--local-broker",
        "--state-file",
        str(state_dir() / "monitoring-publisher.json"),
    ]
    if alias is not None:
        args += ["--alias", alias]
    body = "[Unit]\nDescription=Publish prepared public TauCeti host observations\n\n[Service]\n"
    body += "ExecStart=" + " ".join(unit_argument(a) for a in args) + "\n"
    for key in ("TAUCETI_FLEET_CONFIG", "TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA"):
        if key in os.environ:
            body += "Environment=" + unit_argument(key + "=" + os.environ[key]) + "\n"
    body += (
        "Restart=on-failure\nRestartSec=15\nNice=10\nUMask=0077\nMemoryMax=256M\n\n[Install]\nWantedBy=default.target\n"
    )
    (units() / "tauceti-fleet-monitoring.service").write_text(body)
    command_run(["systemctl", "--user", "daemon-reload"])
    command_run(["systemctl", "--user", "enable", "tauceti-fleet-monitoring.service"])
    command_run(["systemctl", "--user", "restart", "tauceti-fleet-monitoring.service"])
    return {"installed": True, "alias": alias, "admissions_changed": False}


if __name__ == "__main__":
    raise SystemExit(main())
