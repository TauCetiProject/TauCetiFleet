"""Local user-service controller. All admission and resource decisions are local."""

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

from . import ci, github, vm
from .capabilities import GIB, doctor, resources
from .process import executable, run
from .report import snapshot
from .store import (
    FleetError,
    atomic,
    config_dir,
    data_dir,
    database,
    event,
    host_config,
    locked,
    read,
    state_dir,
    update_host,
)


def command():
    return [sys.executable, "-m", "tauceti_fleet"]


def unit_argument(value):
    return json.dumps(str(value).replace("%", "%%"))


def units():
    path = Path.home() / ".config/systemd/user"
    path.mkdir(parents=True, exist_ok=True)
    return path


def install_service():
    args = " ".join(unit_argument(a) for a in [Path.home() / ".local/bin/tauceti-fleet", "host-daemon"])
    environment = dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"))
    for name in ("TAUCETI_FLEET_CONFIG", "TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA"):
        if name in os.environ:
            environment[name] = os.environ[name]
    text = "[Unit]\nDescription=TauCeti fleet host controller\n\n[Service]\nType=simple\n"
    text += "ExecStart=" + args + "\n"
    text += "".join("Environment=" + unit_argument(k + "=" + v) + "\n" for k, v in environment.items())
    text += "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=15\n\n[Install]\nWantedBy=default.target\n"
    target = units() / "tauceti-fleet.service"
    target.write_text(text)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", target.name])
    run(["systemctl", "--user", "restart", target.name])


def configure(payload):
    allowed = {
        "profile",
        "ci_allowed",
        "max_workers",
        "max_open_prs",
        "ci_vcpus",
        "ci_memory_gib",
        "ci_disk_gib",
        "github_account",
        "author_model",
        "author_effort",
        "runner_group_id",
        "github_state_url",
    }
    updates = {k: v for k, v in payload.items() if k in allowed}
    for name in ("max_workers", "max_open_prs", "runner_group_id", "ci_vcpus", "ci_memory_gib", "ci_disk_gib"):
        if name in updates and (type(updates[name]) is not int or not 1 <= updates[name] <= 4096):
            raise FleetError(f"{name} must be a positive integer at most 4096")
    if "ci_allowed" in updates and type(updates["ci_allowed"]) is not bool:
        raise FleetError("ci_allowed must be a boolean")
    credentials = payload.get("credentials", {})
    if not isinstance(credentials, dict):
        raise FleetError("credentials must be a JSON object")
    current = host_config()
    if current["ci_enabled"] or current["workers_enabled"]:
        if any(current.get(k) != v for k, v in updates.items()):
            raise FleetError("Drain active roles before changing host profile or resource configuration")
    for key, secret in credentials.items():
        if key not in ("anthropic_api_key_file", "github_token_file", "ci_token_file", "github_state_token_file"):
            raise FleetError("Unknown credential kind")
        if not isinstance(secret, str) or not secret.strip() or len(secret) > 65536:
            raise FleetError("Invalid credential value")
    for key, secret in credentials.items():
        path = config_dir() / "secrets" / key
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Private atomic secret installation. Never return credential values.
        # Private temp file and fsync before rename; secrets are not argv/env
        # assignments in unit files or plaintext entries in fleet configuration.
        import tempfile

        fd, temporary = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            stream.write(secret.strip() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        updates[key] = str(path)
    value = update_host(**updates)
    if not value["ci_allowed"]:
        value = update_host(ci_enabled=False)
    # Inactive installation never adopts or starts the existing worker fleet.
    service_error = None
    try:
        install_service()
    except FleetError as exc:
        service_error = str(exc)
    return dict(
        machine_id=value["machine_id"],
        profile=value["profile"],
        ci_allowed=value["ci_allowed"],
        ci_enabled=value["ci_enabled"],
        workers_enabled=value["workers_enabled"],
        service_error=service_error,
    )


def prepare(config=None):
    config = config or host_config()
    if config["ci_enabled"] or config["workers_enabled"] or read(state_dir() / "vm.json", {}):
        raise FleetError("Drain this host and wait for its CI guest to exit before preparing runtimes")
    worker_revision = read(data_dir() / "release.json", {}).get("worker_revision")
    pinned = read(data_dir() / "worker-runtime.json", {}).get("revision") == worker_revision
    if not pinned:
        if not executable("uv") or not worker_revision:
            raise FleetError(
                "Worker runtime unavailable: install uv, then deploy a release with its pinned worker_revision"
            )
        venv = data_dir() / "worker-venv"
        if not (venv / "bin/python").exists():
            run([executable("uv"), "venv", venv], timeout=120)
        run(
            [
                executable("uv"),
                "pip",
                "install",
                "--python",
                venv / "bin/python",
                "git+https://github.com/TauCetiProject/TauCetiWorker.git@" + worker_revision,
            ],
            timeout=600,
        )
        (data_dir() / "bin").mkdir(parents=True, exist_ok=True)
        link = data_dir() / "bin/tauceti"
        link.unlink(missing_ok=True)
        link.symlink_to(venv / "bin/tauceti")
        atomic(data_dir() / "worker-runtime.json", dict(revision=worker_revision))
    manifest = vm.prepare_image(config) if config["ci_allowed"] else None
    return dict(worker=executable("tauceti"), image=manifest, smoke=vm.smoke(config) if manifest else None)


def write_slices(config, caps):
    reserve = max(4 * GIB, int(caps["memory_total"] * 0.1))
    ci = (config["ci_memory_gib"] + 2) * GIB if config["ci_allowed"] else 0

    def current(name):
        result = run(
            ["systemctl", "--user", "show", f"tauceti-fleet-{name}.slice", "-p", "MemoryCurrent", "--value"],
            check=False,
        )
        try:
            amount = int(result.stdout.strip())
            return amount if 0 <= amount <= caps["memory_total"] else 0
        except ValueError:
            return 0

    outstanding_ci = max(0, ci - current("ci"))
    worker = min(
        caps["memory_total"] - ci - reserve, caps["memory_available"] - outstanding_ci - reserve + current("workers")
    )
    if worker < GIB:
        raise FleetError(
            "Insufficient memory for bounded workers after CI and OS reservations; configure a smaller CI shape or --no-ci"
        )
    for name, weight, maximum in (("ci", 1000, ci or caps["memory_total"] - reserve), ("workers", 10, worker)):
        (units() / f"tauceti-fleet-{name}.slice").write_text(
            f"[Slice]\nCPUWeight={weight}\nMemoryHigh={int(maximum * (1 if name == 'ci' else 0.8))}\nMemoryMax={int(maximum)}\nTasksMax=16384\n"
        )
    run(["systemctl", "--user", "daemon-reload"])
    return worker


def worker_config(config):
    # Existing worker definitions and the operator's manager are never rewritten.
    fields = dict(
        agent="claude",
        max_open_prs=config.get("max_open_prs", 128 if config.get("profile") == "ai-for-science" else 8),
        claude_billing="api",
        budget=True,
        github_account=config.get("github_account", "tauceti-ai-for-science"),
        anthropic_api_key_file=config.get("anthropic_api_key_file"),
    )
    for key in ("author_model", "author_effort", "github_state_url", "github_state_token_file"):
        if config.get(key):
            fields[key] = config[key]
    lines = ["version = 1"]
    ids = []
    # Keep existing definitions when reducing admissions: removing a definition
    # makes the manager terminate its worker, including an admitted paid session.
    import tomllib

    prior = config_dir() / "workers.toml"
    preserve = config["workers_enabled"] or any(w.get("alive") for w in worker_statuses())
    old_count = len(tomllib.loads(prior.read_text()).get("workers", [])) if preserve and prior.exists() else 0
    count = max(1, config["max_workers"], old_count)
    for n in range(count):
        wid = "fleet-" + config["machine_id"][:8] + f"-{n + 1}"
        ids.append(wid)
        lines += ["", "[[workers]]", "id = " + json.dumps(wid)]
        for key, value in fields.items():
            if value is not None:
                lines.append(key + " = " + ("true" if value is True else json.dumps(value)))
    path = config_dir() / "workers.toml"
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return ids


def start_workers():
    root = str(Path(__file__).resolve().parent.parent)
    argv = " ".join(unit_argument(a) for a in [*command(), "run-workers"])
    target = units() / "tauceti-fleet-workers.service"
    target.write_text(
        "[Unit]\nDescription=TauCeti fleet worker manager\n[Service]\nType=simple\n"
        "Slice=tauceti-fleet-workers.slice\nExecStart="
        + argv
        + "\nEnvironment="
        + unit_argument("PYTHONPATH=" + root)
        + "\nLimitNOFILE=65536\nRestart=on-failure\nRestartSec=10\n"
    )
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "start", target.name])


def run_workers():
    config = host_config()
    os.environ["PATH"] = str(data_dir() / "bin") + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")
    os.environ.update(
        TAUCETI_FLEET_ADMISSION_FILE=str(state_dir() / "admission.json"),
        TAUCETI_FLEET_MACHINE_ID=config["machine_id"],
        TAUCETI_FLEET_EVENTS=str(state_dir() / "worker-events.jsonl"),
        TAUCETI_WORKERS_STATE_DIR=str(state_dir() / "workers"),
        TAUCETI_RUNTIME_DIR=str(state_dir() / "worker-runtime"),
        TAUCETI_DATA_HOME=str(data_dir() / "worker-data"),
    )
    os.environ["GH_TOKEN"] = Path(config["github_token_file"]).read_text().strip()
    gh_config = state_dir() / "gh"
    gh_config.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.environ["GH_CONFIG_DIR"] = str(gh_config)
    # Keep the pre-existing per-user budget location even though fleet workers
    # have separate checkout/state/runtime directories.
    os.environ.setdefault("TAUCETI_BUDGET_DIR", str(Path.home() / ".local/state/tauceti/budget"))
    binary = executable("tauceti")
    if not binary:
        raise FleetError("Pinned worker runtime is unavailable; run prepare")
    os.execv(binary, [binary, "workers", "--config", str(config_dir() / "workers.toml"), "manager"])


def enable(*, ci=False, workers=False):
    config = host_config()
    if ci and not config["ci_allowed"]:
        raise FleetError("This host was deployed with --no-ci; reconfigure its role first")
    result = doctor(config)
    for role, requested in (("ci", ci), ("workers", workers)):
        if requested and result["readiness"][role] == "blocked":
            reasons = [c["name"] for c in result["checks"] if role in c["roles"] and c["status"] == "blocked"]
            raise FleetError(f"Cannot enable {role}: {', '.join(reasons)}. Run doctor for remedies")
    if ci:
        github.runner_policy(config)
        if not (data_dir() / "images/prepared.json").exists():
            raise FleetError("CI image missing; run prepare before enable --ci")
    write_slices(config, resources())
    worker_config(config)
    if ci:
        vm.start(config)  # Check actual registration permission before claiming activation succeeded.
    value = update_host(
        ci_enabled=config["ci_enabled"] or ci, workers_enabled=config["workers_enabled"] or workers, draining=False
    )
    install_service()
    return dict(ci_enabled=value["ci_enabled"], workers_enabled=value["workers_enabled"])


def drain():
    update_host(ci_enabled=False, workers_enabled=False, draining=True)
    atomic(
        state_dir() / "admission.json", dict(version=1, expires_at=time.time() + 15, workers=[], reason="host draining")
    )
    return dict(state="draining", detail="Active work finishes; no new worker rounds or CI guests are admitted")


def cpu_sample():
    numbers = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(numbers), numbers[3] + numbers[4]


def worker_pool_pressure():
    result = run(
        ["systemctl", "--user", "show", "tauceti-fleet-workers.slice", "-p", "MemoryCurrent", "-p", "MemoryHigh"],
        check=False,
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    try:
        return int(fields["MemoryCurrent"]) >= int(fields["MemoryHigh"])
    except (ValueError, KeyError):
        return False


def worker_statuses():
    values = []
    for path in (state_dir() / "workers").glob("*.json"):
        value = read(path, {})
        if value.get("id"):
            values.append(
                {k: value.get(k) for k in ("id", "state", "alive", "heartbeat_at", "phase", "target", "detail")}
            )
    return values


def ci_infrastructure_failure(job):
    return ci.classify(job)["cooldown"]


def ci_service_state(unit):
    result = run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "Result",
            "-p",
            "ExecMainStatus",
        ],
        check=False,
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields.get("LoadState") == "not-found":
        return False, "unknown"
    if result.returncode or not all(fields.get(k) for k in ("ActiveState", "SubState", "Result", "ExecMainStatus")):
        return None, "unknown"
    running = fields["SubState"] in ("running", "start", "start-pre", "start-post")
    if fields["Result"] != "success" or fields["ExecMainStatus"] != "0":
        return running, "fault"
    healthy_exit = (
        fields.get("LoadState") == "loaded" and fields["ActiveState"] == "active" and fields["SubState"] == "exited"
    )
    return running, "healthy" if healthy_exit else "unknown"


def collect_ci_results(config):
    path = state_dir() / "pending-ci.json"
    remaining = []
    for value in read(path, []):
        if time.time() - value.get("started_at", time.time()) > 7 * 86400:
            event("ci_result_unavailable", run_id=value["run_id"], runner_id=value["runner_id"])
            continue
        try:
            data = github.request(
                f"repos/TauCetiProject/TauCeti/actions/runs/{value['run_id']}/jobs?filter=all&per_page=100",
                token_file=config.get("github_token_file"),
            )
            jobs = [j for j in data["jobs"] if j.get("runner_id") == value["runner_id"]]
            job = jobs[0] if len(jobs) == 1 else None
            if not job or job["status"] != "completed":
                remaining.append(value)
                continue
            from .report import timestamp

            identity = "ci-job:" + str(job["id"])
            with database() as db:
                if db.execute("SELECT 1 FROM events WHERE id=?", (identity,)).fetchone():
                    continue
            started, completed = timestamp(job["started_at"]), timestamp(job["completed_at"])
            created = timestamp(job.get("created_at"))
            decision = ci.classify(job, host_health=value.get("host_health"))
            if job.get("conclusion") == "cancelled" and decision["reason"] == "cancellation_evidence_inconclusive":
                try:
                    run_data, statuses = ci.cancellation_metadata(job, config, github.request)
                    decision = ci.classify(job, run=run_data, statuses=statuses, host_health=value.get("host_health"))
                except (FleetError, ValueError, KeyError, TypeError, OSError):
                    decision = dict(cooldown=True, reason="cancellation_evidence_unavailable", evidence={})
            circuit_path = state_dir() / "ci-backoff.json"
            if decision["cooldown"]:
                # Anchor to the actual failure, so replay/crash recovery never
                # renews the same cooldown or shortens a newer fault's cooldown.
                circuit = read(circuit_path, {})
                if completed + 900 > circuit.get("until", 0):
                    atomic(circuit_path, dict(until=completed + 900, job_id=job["id"], reason=decision["reason"]))
            event(
                "ci_result",
                identity=identity,
                at=completed,
                run_id=value["run_id"],
                job_id=job["id"],
                conclusion=job["conclusion"],
                runner_id=value["runner_id"],
                classification=decision,
                run_seconds=max(0, completed - started),
                wait_seconds=max(0, started - created) if created else None,
            )
        except (FleetError, ValueError, KeyError, TypeError):
            remaining.append(value)
    atomic(path, remaining)


class Scaler:
    def __init__(self):
        self.target = 0
        self.last_added = 0
        self.was_ci_busy = False

    def step(self, *, now, enabled, ci_busy, available, reserve, idle, maximum):
        if not enabled or available < reserve:
            self.target = 0
            return 0, "host draining or memory headroom reserved"
        if ci_busy and not self.was_ci_busy:
            self.target = min(self.target, 1)
            self.last_added = now
        self.was_ci_busy = ci_busy
        if idle >= (0.35 if ci_busy else 0.25) and now - self.last_added >= 60:
            self.target = min(maximum, self.target + 1)
            self.last_added = now
        self.target = min(self.target, maximum)
        return (
            self.target,
            "CI active; workers admitted only against measured spare capacity"
            if ci_busy
            else "workers use available capacity",
        )


def daemon():
    state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(state_dir() / "daemon.lock"):
        stopping = False

        def stop(*_):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        scaler = Scaler()
        previous = cpu_sample()
        client = None
        buffer = b""
        last_github = 0
        inventory = []
        error = None
        next_vm = 0
        ids = []
        last_results = 0
        while not stopping:
            config = host_config()
            try:
                caps = resources()
                if time.time() - last_results >= 60:
                    collect_ci_results(config)
                    last_results = time.time()
                current = cpu_sample()
                idle = (current[1] - previous[1]) / max(1, current[0] - previous[0])
                previous = current
                value = read(state_dir() / "vm.json", {})
                if value:
                    active, service_health = ci_service_state(value["unit"])
                    if active is False and not (
                        value.get("phase") == "starting" and time.time() - value["started_at"] < 60
                    ):
                        # Preserve immutable run identity for deferred result collection.
                        if value.get("run_id"):
                            value["host_health"] = service_health
                            pending = read(state_dir() / "pending-ci.json", [])
                            pending.append(value)
                            atomic(state_dir() / "pending-ci.json", pending)
                            collect_ci_results(config)
                        vm.retire(config, value)
                        value = {}
                        if client:
                            client.close()
                            client = None
                        buffer = b""
                        next_vm = time.time() + 5
                if value and client is None:
                    try:
                        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        client.connect(str(Path(value["directory"]) / "events.sock"))
                        client.setblocking(False)
                    except OSError:
                        if client:
                            client.close()
                        client = None
                if client:
                    try:
                        chunk = client.recv(16384)
                        if not chunk:
                            client.close()
                            client = None
                        buffer = (buffer + chunk)[-65536:]
                    except BlockingIOError:
                        pass
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        try:
                            notice = json.loads(line)
                            if notice.get("kind") == "ci_started" and str(notice.get("run_id", "")).isdigit():
                                value.update(busy=True, run_id=int(notice["run_id"]), job_started_at=time.time())
                                atomic(state_dir() / "vm.json", value)
                        except (ValueError, AttributeError):
                            pass
                busy = bool(value.get("busy"))
                circuit = read(state_dir() / "ci-backoff.json", {})
                cooling = circuit.get("until", 0) > time.time()
                if (config["ci_enabled"] or value) and time.time() - last_github >= 30:
                    last_github = time.time()
                    inventory = github.runners(config)
                    own = next((r for r in inventory if r["id"] == value.get("runner_id")), {})
                    if value and own.get("status") == "online" and not value.get("online_observed_at"):
                        value["online_observed_at"] = time.time()
                        atomic(state_dir() / "vm.json", value)
                        retired = read(state_dir() / "ci-last-retired.json", {})
                        event(
                            "ci_runner_online",
                            runner_id=value["runner_id"],
                            previous_runner_id=retired.get("runner_id"),
                            retirement_to_online_seconds=max(0, value["online_observed_at"] - retired["at"])
                            if retired
                            else None,
                            observation_interval_seconds=30,
                        )
                    busy = busy or own.get("busy", False)
                    if (
                        value
                        and not busy
                        and (
                            not config["ci_enabled"]
                            or cooling
                            or (time.time() - value["started_at"] > 180 and own.get("status") != "online")
                        )
                        and not (value.get("phase") == "starting" and time.time() - value["started_at"] < 60)
                    ):
                        vm.retire(config, value)
                        value = {}
                        next_vm = time.time() + 60
                if config["ci_enabled"] and not cooling and not value and time.time() >= next_vm:
                    value = vm.start(config)
                    next_vm = time.time() + 60
                if config["workers_enabled"]:
                    if not ids or len(ids) < config["max_workers"]:
                        ids = worker_config(config)
                        start_workers()
                reserve = max(4 * GIB, int(caps["memory_total"] * 0.1))
                maximum = config["max_workers"]
                ramp = read(state_dir() / "ramp.json", {})
                if ramp.get("phase") == "running" and time.time() - ramp.get("heartbeat_at", 0) > 120:
                    maximum = 0
                target, reason = scaler.step(
                    now=time.time(),
                    enabled=config["workers_enabled"] and not worker_pool_pressure(),
                    ci_busy=busy,
                    available=caps["memory_available"],
                    reserve=reserve,
                    idle=idle,
                    maximum=maximum,
                )
                atomic(
                    state_dir() / "admission.json",
                    dict(version=1, expires_at=time.time() + 15, workers=ids[:target], reason=reason),
                )
                atomic(
                    state_dir() / "status.json",
                    dict(
                        version=1,
                        machine_id=config["machine_id"],
                        observed_at=time.time(),
                        ci_enabled=config["ci_enabled"],
                        workers_enabled=config["workers_enabled"],
                        ci_busy=busy,
                        ci_cooldown=circuit if cooling else None,
                        active_worker_limit=target,
                        scaling_reason=reason,
                        resources=caps,
                        cpu_idle_fraction=idle,
                        workers=worker_statuses(),
                        error=None,
                        ci_vm=value or None,
                    ),
                )
                error = None
            except (FleetError, OSError, ValueError, KeyError) as exc:
                message = str(exc)
                if message != error:
                    print("tauceti-fleet: " + message, file=sys.stderr, flush=True)
                    error = message
                atomic(
                    state_dir() / "admission.json",
                    dict(version=1, expires_at=time.time() + 15, workers=[], reason="controller error; run doctor"),
                )
                atomic(
                    state_dir() / "status.json",
                    dict(machine_id=config["machine_id"], observed_at=time.time(), error=message),
                )
                next_vm = time.time() + 60
            for _ in range(10):
                if stopping:
                    break
                time.sleep(0.5)
        atomic(
            state_dir() / "admission.json",
            dict(version=1, expires_at=time.time() + 15, workers=[], reason="controller stopped"),
        )
        if client:
            client.close()


def budget(payload):
    binary = executable("tauceti")
    if not binary:
        raise FleetError("TauCetiWorker is missing; run prepare")
    args = [binary, "budget", "--json"]
    from decimal import Decimal, InvalidOperation

    for name in ("grant", "grant_rate"):
        if payload.get(name) is not None:
            try:
                amount = Decimal(str(payload[name]))
                if not amount.is_finite() or amount < 0:
                    raise ValueError("amount")
            except (InvalidOperation, ValueError):
                raise FleetError("Budget amounts must be finite and nonnegative") from None
            args += ["--" + name.replace("_", "-"), str(amount)]
    return json.loads(run(args, timeout=30).stdout)


def rpc(payload):
    if not isinstance(payload, dict):
        raise FleetError("Host request must be a JSON object")
    action = payload.get("action")
    if action == "configure":
        return configure(payload)
    if action == "prepare_update":
        config = host_config()
        waiting = {"idle", "waiting-capacity", "waiting-budget", "waiting-quota", "backoff", "stopped", "exited"}
        if config["workers_enabled"] or config["ci_enabled"]:
            raise FleetError("Drain this host before updating")
        if read(state_dir() / "vm.json", {}) or any(
            w.get("alive") and w.get("state") not in waiting for w in worker_statuses()
        ):
            raise FleetError("Active work is still draining; wait before updating")
        run(["systemctl", "--user", "stop", "tauceti-fleet-workers.service"], check=False)
        return dict(quiescent=True)
    if action == "doctor":
        result = doctor()
        if payload.get("smoke"):
            result["smoke"] = vm.smoke()
        return result
    if action == "prepare":
        return prepare()
    if action == "enable":
        return enable(ci=bool(payload.get("ci")), workers=bool(payload.get("workers")))
    if action == "workers":
        from .ramp import set_maximum

        return set_maximum(payload["maximum"])
    if action == "ramp":
        from .ramp import control

        return control(payload)
    if action == "drain":
        return drain()
    if action == "budget":
        return budget(payload)
    if action == "snapshot":
        result = snapshot(refresh=True)
        result["status"] = read(state_dir() / "status.json", {})
        return result
    if action == "status":
        config = host_config()
        return dict(
            machine_id=config["machine_id"],
            profile=config["profile"],
            ci_allowed=config["ci_allowed"],
            ci_enabled=config["ci_enabled"],
            workers_enabled=config["workers_enabled"],
            max_workers=config["max_workers"],
            status=read(state_dir() / "status.json", {}),
        )
    raise FleetError("Unknown host operation")
