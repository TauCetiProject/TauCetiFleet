"""Local user-service controller. All admission and resource decisions are local."""

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

from . import activity, capacity, ci, ci_overview, ci_pool, ci_priority, focus, github, mathlib_store, native_runner, vm
from .capabilities import GIB, doctor, memory_pressure, resources, worker_memory
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

# Resolve once: switching the current symlink must not make an old controller
# report that it is already running the newly installed release.
RUNNING_RELEASE = str(Path(__file__).resolve().parent.parent)


def command():
    return [sys.executable, "-m", "tauceti_fleet"]


def unit_argument(value):
    return json.dumps(str(value).replace("%", "%%"))


def units():
    path = Path.home() / ".config/systemd/user"
    path.mkdir(parents=True, exist_ok=True)
    return path


def install_service(*, restart=True):
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
    observation_command = " ".join(unit_argument(a) for a in [Path.home() / ".local/bin/tauceti-fleet", "collect-status"])
    for name, content in ci_overview.unit_files(observation_command, environment).items():
        (units() / name).write_text(content)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", target.name])
    run(["systemctl", "--user", "enable", "--now", "tauceti-fleet-observe.timer"])
    install_pressure_service()
    if restart:
        run(["systemctl", "--user", "restart", target.name])


def install_pressure_service():
    """Provision the publisher on both first install and live controller handover."""
    environment = dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"))
    for name in ("TAUCETI_FLEET_CONFIG", "TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA"):
        if name in os.environ:
            environment[name] = os.environ[name]
    pressure_command = " ".join(unit_argument(a) for a in [sys.executable, "-m", "tauceti_fleet.lean_host"])
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    pressure = ("[Unit]\nDescription=Host pressure for CI Lean admission\nStartLimitIntervalSec=0\n\n[Service]\n"
                "ExecStart=" + pressure_command
                + "\nSlice=tauceti-fleet-ci.slice\nRestart=always\nRestartSec=5\n"
                + "".join("Environment=" + unit_argument(k + "=" + v) + "\n" for k, v in environment.items())
                + "\n[Install]\nWantedBy=default.target\n")
    (units() / "tauceti-fleet-pressure.service").write_text(pressure)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", "tauceti-fleet-pressure.service"])
    run(["systemctl", "--user", "try-restart", "tauceti-fleet-pressure.service"])


def configure(payload):
    allowed = {
        "profile",
        "ci_allowed",
        "max_workers",
        "max_open_prs",
        "ci_vcpus",
        "ci_memory_gib",
        "ci_disk_gib",
        "ci_max_jobs",
        "ci_kinds",
        "github_account",
        "author_model",
        "author_effort",
        "runner_group_id",
        "github_state_url",
        "ci_broker_url",
    }
    updates = {k: v for k, v in payload.items() if k in allowed}
    if "ci_broker_url" in updates:
        from .broker import validate_url

        validate_url(updates["ci_broker_url"])
    for name in ("max_workers", "max_open_prs", "runner_group_id", "ci_vcpus", "ci_memory_gib", "ci_disk_gib"):
        if name in updates and (type(updates[name]) is not int or not 1 <= updates[name] <= 4096):
            raise FleetError(f"{name} must be a positive integer at most 4096")
    if "ci_allowed" in updates and type(updates["ci_allowed"]) is not bool:
        raise FleetError("ci_allowed must be a boolean")
    if "ci_max_jobs" in updates:
        capacity.jobs(updates)
    if "ci_kinds" in updates and updates["ci_kinds"] not in capacity.KINDS.values():
        raise FleetError("ci_kinds must explicitly select pr, staging, or both")
    credentials = payload.get("credentials", {})
    if not isinstance(credentials, dict):
        raise FleetError("credentials must be a JSON object")
    current = host_config()
    capacity.staging_slots(dict(current, **updates))
    if current["ci_enabled"] or current["workers_enabled"] or vm.outstanding():
        if any(current.get(k) != v for k, v in updates.items()):
            raise FleetError("Drain active roles before changing host profile or resource configuration")
    for key, secret in credentials.items():
        if key not in ("anthropic_api_key_file", "github_token_file", "ci_token_file", "github_state_token_file", "ci_broker_token_file"):
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
    value = update_host(validate=capacity.staging_slots, **updates)
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
        ci_enrolled=bool(value.get("ci_broker_url")),
        workers_enabled=value["workers_enabled"],
        service_error=service_error,
    )


def prepare(config=None):
    config = config or host_config()
    if config["ci_enabled"] or config["workers_enabled"] or vm.outstanding():
        raise FleetError("Drain this host and wait for its CI guest to exit before preparing runtimes")
    from .update import latest_worker_revision, stage_worker, symlink
    with locked(state_dir() / "worker-runtime-upgrade.lock"):
        worker_revision = latest_worker_revision()
        venv = stage_worker(worker_revision)
        symlink(data_dir() / "bin/tauceti", venv / "bin/tauceti")
        atomic(data_dir() / "worker-runtime.json", dict(revision=worker_revision))
    manifest = vm.prepare_image(config) if config["ci_allowed"] else None
    native_image = native_runner.prepare_for_ci(config) if manifest else None
    return dict(worker=executable("tauceti"), worker_revision=worker_revision, image=manifest,
                native_image=native_image, smoke=vm.smoke(config) if manifest else None)


def worker_headroom(caps, *, ci_busy=False):
    reserve = max(4 * GIB, int(caps["memory_total"] * 0.1))
    if ci_busy:
        from . import lean_pressure

        total = capacity.shared_memory_bytes(caps)
        # CI cannot start Lean below its two-file gate. Let workers yield
        # before that gate closes, including a small admission cushion.
        reserve = max(reserve, int(total * (1 - lean_pressure.memory_limit(total) / 100)) + GIB)
    return reserve


def worker_memory_limits(config, caps, pinned=0, *, ci_busy=False):
    """Use presently spare RAM; retain a fixed host/swap emergency bound.

    Workers and CI share the parent slice's hard ceiling, without a fixed RAM
    split. The soft bound tracks actual headroom. As CI or other host
    processes grow, MemAvailable falls and the existing controller lowers high.
    Only high moves during work: lowering max could kill an active paid round.
    """
    reserve = worker_headroom(caps, ci_busy=ci_busy)
    maximum = capacity.shared_memory_bytes(caps)
    # MemAvailable already includes reclaimable worker cache. Add only pinned
    # memory, and don't force pointless reclaim below that plus a small cushion.
    high = min(int(maximum * 0.95), max(pinned + GIB, pinned + caps["memory_available"] - reserve))
    return high, maximum


def refresh_worker_memory(config, caps, memory, *, ci_busy=False):
    """Adjust the running slice without restarting work or blocking on reclaim."""
    if not caps["memory_total"]:
        memory["allocation_error"] = "Host memory measurement unavailable; retaining the last worker limit"
        return
    cg = Path(memory["cgroup"])
    high, _ = worker_memory_limits(config, caps, memory["pinned"], ci_busy=ci_busy)
    maximum = memory["maximum"]
    high = min(high, int(maximum * 0.95))
    # Ignore sub-256 MiB movement. Opening nonblocking prevents synchronous
    # reclaim from stalling this controller and expiring worker admission leases.
    current = int((cg / "memory.high").read_text())
    if high < current or high - current >= 256 * 1024**2:
        fd = os.open(cg / "memory.high", os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, str(high).encode())
        finally:
            os.close(fd)
    memory["high"] = int((cg / "memory.high").read_text())


def write_slices(config, caps):
    reserve = max(4 * GIB, int(caps["memory_total"] * 0.1))
    shared = capacity.shared_memory_bytes(caps)
    ci = capacity.memory_budget_gib(config, caps) * GIB if config["ci_allowed"] else shared

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

    current_workers = current("workers")
    if ci and current("ci") > ci:
        raise FleetError("Requested CI memory budget is below current CI usage; retain the running guest limit")
    _, worker = worker_memory_limits(config, caps)
    live_workers = any(w.get("alive") for w in worker_statuses())
    if live_workers and worker < max(GIB, current_workers):
        raise FleetError("Requested CI capacity leaves insufficient memory for the running workers")
    if worker < GIB and config["workers_enabled"]:
        raise FleetError(
            "Insufficient memory for bounded workers after CI and OS reservations; configure a smaller CI shape or --no-ci"
        )
    worker = max(16 * 1024**2, worker)
    # The existing hyphenated child slice names already belong to this parent.
    # Both may use free RAM; their combined usage still leaves OS/swap headroom.
    (units() / "tauceti-fleet.slice").write_text(
        f"[Slice]\nMemoryHigh={int(shared)}\nMemoryMax={int(shared)}\nTasksMax=32768\n"
    )
    for name, weight, maximum in (("ci", 1000, ci or caps["memory_total"] - reserve), ("workers", 10, worker)):
        (units() / f"tauceti-fleet-{name}.slice").write_text(
            f"[Slice]\nCPUWeight={weight}\nMemoryHigh={int(maximum * (1 if name == 'ci' else 0.95))}\nMemoryMax={int(maximum)}\n"
            + (f"MemorySwapMax={GIB}\n" if name == "workers" else "")
            + "TasksMax=16384\n"
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


WORKER_OOM_SCORE_ADJ = 500


def start_workers():
    root = RUNNING_RELEASE
    argv = " ".join(unit_argument(a) for a in [*command(), "run-workers"])
    target = units() / "tauceti-fleet-workers.service"
    target.write_text(
        "[Unit]\nDescription=TauCeti fleet worker manager\n[Service]\nType=simple\n"
        "Slice=tauceti-fleet-workers.slice\nExecStart="
        + argv
        + "\nEnvironment="
        + unit_argument("PYTHONPATH=" + root)
        + "\nLimitNOFILE=65536\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=30\nOOMPolicy=continue\nOOMScoreAdjust=100\n"
    )
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "start", target.name])


def start_mathlib_store():
    argv = " ".join(unit_argument(a) for a in [*command(), "warm-mathlib-store"])
    environment = dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"), PYTHONPATH=RUNNING_RELEASE)
    for name in ("TAUCETI_FLEET_CONFIG", "TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA"):
        if name in os.environ:
            environment[name] = os.environ[name]
    # A sibling of the CI and worker slices: it yields CPU to CI, and what it
    # unpacks is not charged to the workers' memory limit. The umask keeps
    # entries readable inside the containers that mount them.
    (units() / mathlib_store.UNIT).write_text(
        "[Unit]\nDescription=TauCeti fleet Mathlib store\n\n[Service]\nType=oneshot\nExecStart=" + argv
        + "\nSlice=tauceti-fleet.slice\nCPUWeight=10\nNice=10\nUMask=0022\nTimeoutStartSec=8h\n"
        + "".join("Environment=" + unit_argument(k + "=" + v) + "\n" for k, v in environment.items())
    )
    run(["systemctl", "--user", "daemon-reload"])
    # A pass still running absorbs this start.
    run(["systemctl", "--user", "start", "--no-block", mathlib_store.UNIT])


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
        TAUCETI_WORKER_SLICE="tauceti-fleet-workers.slice",
        # The manager runs at the user manager's floor (100). Each worker
        # wrapper raises itself before starting rounds, so workers stay the
        # preferred victims ahead of CI guests (200) without the supervisor.
        TAUCETI_WORKER_OOM_SCORE_ADJ=str(WORKER_OOM_SCORE_ADJ),
    )
    # Historical PRs can belong to workers no longer in workers.toml. Give the
    # manager the host's client context as well as each current worker's spec.
    for field in ("github_state_url", "github_state_token_file"):
        if config.get(field):
            os.environ["TAUCETI_" + field.upper()] = config[field]
    if mathlib_store.enabled(config):
        os.environ["TAUCETI_MATHLIB_STORE"] = str(mathlib_store.mounted(config))
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
    binary = str(Path(binary).resolve())
    os.execv(binary, [binary, "workers", "--config", str(config_dir() / "workers.toml"), "manager"])


def enable(*, ci=None, workers=False, maximum=None, vcpus=None, memory_gib=None, disk_gib=None, memory_budget_gib=None):
    config = host_config()
    if ci and not config["ci_allowed"]:
        raise FleetError("This host was deployed with --no-ci; reconfigure its role first")
    if ci:
        proposed = capacity.settings(config, ci=ci, maximum=maximum, vcpus=vcpus, memory_gib=memory_gib, disk_gib=disk_gib, memory_budget_gib=memory_budget_gib)
        changed = any(proposed.get(k) != config.get(k) for k in ("ci_kinds", "ci_max_jobs", "ci_vcpus", "ci_memory_gib", "ci_disk_gib"))
        # The controller reads the slot count every iteration and admits added
        # slots independently. Existing guests need neither retirement nor a
        # controller restart when their shape and labels remain unchanged.
        growing = (
            capacity.jobs(proposed) >= capacity.jobs(config)
            and capacity.kinds(proposed) == capacity.kinds(config)
            and all(proposed.get(k) == config.get(k) for k in ("ci_vcpus", "ci_memory_gib", "ci_disk_gib"))
        )
        if changed and not growing and (config["ci_enabled"] or config["workers_enabled"] or vm.outstanding()):
            raise FleetError("Drain this host and wait for every CI slot to retire before changing its CI classes or capacity")
        config = proposed
    result = doctor(config)
    for role, requested in (("ci", ci), ("workers", workers)):
        if requested and result["readiness"][role] == "blocked":
            reasons = [c["name"] for c in result["checks"] if role in c["roles"] and c["status"] == "blocked"]
            raise FleetError(f"Cannot enable {role}: {', '.join(reasons)}. Run doctor for remedies")
    if ci:
        capacity.authorize(config, github.capacity_policy(config))
        if not (data_dir() / "images/prepared.json").exists():
            raise FleetError("CI image missing; run prepare before enable --ci")
        native_runner.prepare_for_ci(config)
    write_slices(dict(config, workers_enabled=config["workers_enabled"] or workers), resources())
    worker_config(config)
    if ci:
        # Persist the policy before launch, so crash recovery never loses the
        # admission ceiling or tries to replace these guests with the old shape.
        update_host(validate=capacity.staging_slots,
                    **{k: config[k] for k in ("ci_kinds", "ci_max_jobs", "ci_vcpus", "ci_memory_gib", "ci_disk_gib", "ci_memory_budget_gib") if k in config})
        if not config["ci_enabled"] and not vm.outstanding():
            # Verify registration once. The controller admits remaining slots
            # independently, so a later startup error cannot leave a half-enabled role.
            vm.start(dict(config, ci_slot=0))
    value = update_host(
        ci_enabled=config["ci_enabled"] or bool(ci), workers_enabled=config["workers_enabled"] or workers, draining=False
    )
    install_service(restart=False)
    # The usual enable command also provisions read-only telemetry. The broker
    # supplies its approved alias; volunteers need no additional setup command.
    if value.get("ci_broker_url"):
        try:
            from .monitoring import install_local
            install_local()
        except (FleetError, OSError, ValueError):
            print("tauceti-fleet: monitoring setup unavailable; host work remains enabled", file=sys.stderr)
    return dict(ci_enabled=value["ci_enabled"], workers_enabled=value["workers_enabled"],
                ci_kinds=capacity.kinds(value), ci_max_jobs=capacity.jobs(value),
                ci_memory_budget_gib=capacity.memory_budget_gib(value))


def reserve_staging(maximum):
    """Change future guest classes without stopping guests or either admission role."""
    config = host_config()
    proposed = dict(config, ci_staging_slots=maximum)
    capacity.staging_slots(proposed)
    if config["ci_enabled"]:
        native_runner.prepare_for_ci(proposed)
    value = update_host(ci_staging_slots=maximum, validate=capacity.staging_slots)
    pending = []
    for index in range(maximum):
        guest = read(vm.slot_file({"ci_slot": index}, "vm.json"), {})
        if guest and guest.get("ci_kinds", capacity.kinds(config)) != ["staging"]:
            pending.append(index)
        elif not guest:
            intent = read(vm.slot_file({"ci_slot": index}, "ci-registration.json"), {})
            if intent and intent.get("ci_kinds") != ["staging"]:
                pending.append(index)
    event("ci_staging_reservation", staging_slots=maximum, pending_slots=pending)
    return dict(ci_staging_slots=maximum, ci_max_jobs=capacity.jobs(value),
                ci_enabled=value["ci_enabled"], workers_enabled=value["workers_enabled"],
                pending_slots=pending)


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
    return bool(memory_pressure(worker_memory(), resources()))


def set_worker_maximum(maximum, *, lease_seconds=None):
    """Set admissions; optional short leases allow external automation to fail closed."""
    if type(maximum) is not int or not 0 <= maximum <= 4096:
        raise FleetError("Worker maximum must be an integer from 0 to 4096")
    lease = state_dir() / "worker-admission-lease.json"
    if lease_seconds is None:
        lease.unlink(missing_ok=True)
    else:
        if type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
            raise FleetError("Admission lease must be 30 to 3600 seconds")
        atomic(lease, dict(expires_at=time.time() + lease_seconds))
    update_host(max_workers=maximum)
    if maximum == 0:
        atomic(
            state_dir() / "admission.json",
            dict(version=1, expires_at=time.time() + 15, workers=[], reason="new worker work paused"),
        )
    return dict(max_workers=maximum, detail="Admission ceiling; already admitted sessions finish")


def worker_statuses():
    values = []
    for path in (state_dir() / "workers").glob("*.json"):
        value = read(path, {})
        if value.get("id"):
            status = {k: value.get(k) for k in (
                "id", "state", "alive", "heartbeat_at", "phase", "target", "detail",
                "wrapper_pid", "scope_unit", "isolation_error", "restart_count",
            )}
            # Workers without focus reporting omit the key; status must not count them as unfocused.
            if "roadmap_focus" in value:
                status["roadmap_focus"] = value["roadmap_focus"]
            values.append(status)
    return values


def worker_isolation():
    try:
        return read(state_dir() / "workers/diagnostics/isolation.json", {})
    except FleetError:
        return dict(error="Worker isolation diagnostics unavailable")


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
    if fields["Result"] == "oom-kill":
        return running, "oom"
    if fields["Result"] != "success" or fields["ExecMainStatus"] != "0":
        return running, "fault"
    healthy_exit = (
        fields.get("LoadState") == "loaded" and fields["ActiveState"] == "active" and fields["SubState"] == "exited"
    )
    return running, "healthy" if healthy_exit else "unknown"


def queue_ci_result(value, service_health):
    """Persist one result receipt even while runner retirement keeps failing."""
    if not value.get("run_id") or value.get("result_queued"):
        return False
    value["host_health"] = service_health
    pending = read(state_dir() / "pending-ci.json", [])
    identity = (value.get("instance"), value["run_id"], value["runner_id"])
    if not any((p.get("instance"), p.get("run_id"), p.get("runner_id")) == identity for p in pending):
        pending.append(dict(value))
        atomic(state_dir() / "pending-ci.json", pending)
    # Write after the receipt. If interrupted between writes, the identity check
    # deduplicates the next attempt. Keep this marker after collection as well.
    value["result_queued"] = True
    atomic(vm.slot_file({"ci_slot": value.get("slot", 0)}, "vm.json"), value)
    return True


def collect_ci_results(config):
    path = state_dir() / "pending-ci.json"
    remaining = []
    for value in read(path, []):
        if time.time() - value.get("started_at", time.time()) > 7 * 86400:
            event("ci_result_unavailable", run_id=value["run_id"], runner_id=value["runner_id"])
            continue
        try:
            repository = value.get("repository", "TauCetiProject/TauCeti")
            if repository not in ("TauCetiProject/TauCeti", "TauCetiProject/TauCetiRoadmap"):
                raise FleetError("Unknown CI repository")
            data = github.ci_read(
                config,
                f"repos/{repository}/actions/runs/{value['run_id']}/jobs?filter=all&per_page=100",
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
            kind = "pr" if value.get("ci_kinds") in (["roadmap"], ["control"], ["queue-control"]) else "staging"
            if "pr" in value.get("ci_kinds", ["staging"]) and value.get("host_health") != "fault":
                run = github.ci_read(config, f"repos/TauCetiProject/TauCeti/actions/runs/{value['run_id']}")
                # The immutable receipt supplies the run ID, GitHub supplies its
                # event/title. Guest events never claim PR versus staging trust.
                if run.get("event") != "repository_dispatch" or not str(run.get("display_title", "")).startswith("bors branch="):
                    kind = "pr"
            decision = ci.classify(job, host_health=value.get("host_health"), kind=kind,
                                   memory_overcommit=config.get("ci_memory_budget_gib") is not None)
            if value.get("operator_recovery"):
                decision = dict(cooldown=False, reason="operator_memory_recovery", evidence={})
            if job.get("conclusion") == "cancelled" and decision["reason"] == "cancellation_evidence_inconclusive":
                try:
                    run_data, statuses = ci.cancellation_metadata(job, config, lambda path, **_: github.ci_read(config, path))
                    decision = ci.classify(job, run=run_data, statuses=statuses, host_health=value.get("host_health"))
                except (FleetError, ValueError, KeyError, TypeError, OSError):
                    decision = dict(cooldown=True, reason="cancellation_evidence_unavailable", evidence={})
            # One disposable guest's failed job is not evidence that the other
            # slots are unhealthy. Keep automatic recovery local to that slot.
            circuit_path = vm.slot_file({"ci_slot": value.get("slot", 0)}, "ci-slot-backoff.json")
            if decision["cooldown"]:
                # Anchor to the actual failure, so replay/crash recovery never
                # renews the same cooldown or shortens a newer fault's cooldown.
                circuit = read(circuit_path, {})
                delay = 60 if decision["reason"] == "pr_runner_step_failed" else 900
                if completed + delay > circuit.get("until", 0):
                    atomic(circuit_path, dict(until=completed + delay, job_id=job["id"], reason=decision["reason"]))
            event(
                "ci_result",
                identity=identity,
                at=completed,
                run_id=value["run_id"],
                repository=repository,
                job_id=job["id"],
                conclusion=job["conclusion"],
                runner_id=value["runner_id"],
                runtime=value.get("runtime", "vm"),
                instance=value.get("instance"),
                slot=value.get("slot", 0),
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
        self.decision = {}

    def _result(self, state, reason):
        self.decision = dict(
            state=state,
            reason=reason,
            policy="opportunistic",
            cpu_idle_required=0,
            next_increase_at=None,
            next_worker_limit=None,
        )
        return self.target, reason

    def step(self, *, now, enabled, ci_busy, available, reserve, idle, maximum, pause_reason=None):
        if not enabled or available < reserve:
            self.target = 0
            reason = pause_reason or (
                "workers disabled or host draining" if not enabled else "host memory headroom below reserve"
            )
            return self._result("paused", reason)
        # CPU scheduling gives CI first claim on contested resources. Idle CPU
        # and reclaim waits must not cap mostly network-bound inference sessions.
        self.target = maximum
        return self._result(
            "at_ceiling" if maximum else "paused",
            "all configured workers allowed; CI has CPU priority"
            if maximum
            else (pause_reason or "configured worker ceiling is zero"),
        )


class CISlot:
    """Independent guest lifecycle; all slots share the machine's admission limit."""

    def __init__(self, index):
        self.index = index
        self.client = None
        self.buffer = b""
        self.next_vm = 0
        self.next_cleanup = 0

    def close(self):
        if self.client:
            self.client.close()
        self.client = None
        self.buffer = b""

    def step(self, config, *, inventory, refresh, cooling, admitted=None, reservations=None, recovery=None, priority=False):
        config = dict(capacity.for_slot(config, self.index),
                      ci_enabled=config["ci_enabled"] and (self.index < capacity.jobs(config) if admitted is None else admitted))
        cooling = cooling or read(vm.slot_file(config, "ci-slot-backoff.json"), {}).get("until", 0) > time.time()
        path = vm.slot_file(config, "vm.json")
        value = read(path, {})
        if value:
            value = native_runner.reconcile_events(config, value)
        if not config["ci_enabled"] and not value:
            if time.time() >= self.next_cleanup:
                self.next_cleanup = time.time() + 300
                vm.cancel_registration(config)
            return {}
        if value:
            active, service_health = ci_service_state(value["unit"])
            if active is not False and refresh:
                own = next((r for r in inventory if r["id"] == value.get("runner_id")), None)
                # JIT runners serve exactly one job. Once their registration
                # disappears, a surviving guest cannot accept or report work.
                # Require fresh complete inventories across a ten-minute grace;
                # an offline registered runner may still be running a build.
                if own is None and time.time() - value["started_at"] > 180:
                    if "runner_missing_since" not in value:
                        value["runner_missing_since"] = time.time()
                        atomic(path, value)
                    elif time.time() - value["runner_missing_since"] >= 600:
                        run(["systemctl", "--user", "stop", value["unit"]])
                        event("ci_orphan_retired", slot=self.index, runner_id=value["runner_id"],
                              instance=value["instance"], reason="runner_registration_disappeared")
                        active = False
                elif "runner_missing_since" in value:
                    del value["runner_missing_since"]
                    atomic(path, value)
            if active is False and not (value.get("phase") == "starting" and time.time() - value["started_at"] < 60):
                # The daemon collects queued results on its existing schedule;
                # reporting must not delay retiring this guest.
                queue_ci_result(value, service_health)
                if vm.retire(config, value) is not False:
                    value = {}
                    self.close()
                    self.next_vm = time.time() + 5
        if value and self.client is None:
            try:
                self.client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.client.connect(str(Path(value["directory"]) / "events.sock"))
                self.client.setblocking(False)
            except OSError:
                self.close()
        if self.client:
            try:
                chunk = self.client.recv(16384)
                if not chunk:
                    self.close()
                else:
                    self.buffer = (self.buffer + chunk)[-65536:]
            except BlockingIOError:
                pass
            except OSError:
                self.close()
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    notice = json.loads(line)
                    if notice.get("kind") == "ci_started" and not value.get("run_id") and str(notice.get("run_id", "")).isdigit():
                        value.update(busy=True, run_id=int(notice["run_id"]), job_started_at=time.time())
                        atomic(path, value)
                except (ValueError, AttributeError):
                    pass
        if refresh:
            own = next((r for r in inventory if r["id"] == value.get("runner_id")), {})
            if value and own.get("status") == "online" and not value.get("online_observed_at"):
                value["online_observed_at"] = time.time()
                atomic(path, value)
                retired = read(vm.slot_file(config, "ci-last-retired.json"), {})
                event("ci_runner_online", slot=self.index, runner_id=value["runner_id"],
                      previous_runner_id=retired.get("runner_id"),
                      retirement_to_online_seconds=max(0, value["online_observed_at"] - retired["at"]) if retired else None,
                      observation_interval_seconds=30)
            if value and own.get("busy") and not value.get("busy"):
                value["busy"] = True
                atomic(path, value)
            if (value and value.get("phase") != "cleanup" and not value.get("busy")
                    and (not config["ci_enabled"] or cooling or value.get("withdrawing_at")
                         or native_runner.idle_expired(value, own)
                         or (time.time() - value["started_at"] > 180 and own.get("status") != "online"))
                    and (config.get("ci_idle_pool") or time.time() - value["started_at"] >= 60)):
                if config.get("ci_idle_pool"):
                    value["routing_labels"] = ci_pool.labels(value, own)
                    retired = (ci_pool.idle_ready(value, own, reservations)
                               and vm.retire_idle(config, value))
                elif native_runner.is_native(value):
                    # GitHub rejects deregistration if a job won the idle race.
                    value["routing_labels"] = ci_pool.labels(value, own)
                    retired = vm.retire_idle(config, value)
                else:
                    vm.retire(config, value)
                    retired = True
                if retired:
                    value = {}
                    self.close()
                    self.next_vm = time.time() + 5
        if config["ci_enabled"] and not cooling and not value and time.time() >= self.next_vm:
            value = vm.start(dict(config, ci_recover_instance=recovery["instance"],
                                  ci_kinds=recovery["ci_kinds"]) if recovery else config)
            self.next_vm = time.time() + (60 if value else 5)
        if value and priority:
            before = dict(value)
            try:
                ci_priority.classify(config, value, time.time())
            finally:
                value = ci_priority.persist(config, before, value)
        return value


def daemon():
    state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(state_dir() / "daemon.lock"):
        if host_config()["ci_allowed"]:
            install_pressure_service()
        stopping = False
        handover = False

        def stop(*_):
            nonlocal stopping, handover
            stopping = True
            handover = False

        def reload_release(*_):
            nonlocal stopping, handover
            stopping = handover = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGHUP, reload_release)
        scaler = Scaler()
        previous = cpu_sample()
        slots = {}
        last_github = 0
        inventory = []
        reservations = None
        pool_error = None
        error = None
        ids = []
        last_results = 0
        memory_paused = False
        store_warmer = mathlib_store.Warmer(start_mathlib_store)
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
                indexes = sorted(set(range(capacity.jobs(config))) | set(vm.slots_in_use()))
                refresh = (config["ci_enabled"] or vm.outstanding()) and time.time() - last_github >= 30
                if refresh:
                    inventory = github.runners(config)
                    last_github = time.time()
                    if config.get("ci_idle_pool"):
                        try:
                            reservations = ci_pool.reservations(config)
                            pool_error = None
                        except Exception as exc:
                            reservations = None
                            pool_error = str(exc)[:200]
                            event("ci_pool_observation_unavailable", reason=str(exc)[:200])
                existing = [read(vm.slot_file({"ci_slot": index}, "vm.json"), {}) for index in indexes]
                recovery_records = ci_pool.recoveries(existing, reservations) if config.get("ci_idle_pool") else []
                recovery_slots = ci_pool.recovery_slots(config, existing, inventory, reservations, recovery_records)
                admitted = ci_pool.plan(config, existing, inventory, reservations, recovery_slots, bool(recovery_records))
                circuit = read(state_dir() / "ci-backoff.json", {})
                cooling = circuit.get("until", 0) > time.time()
                values = []
                slot_errors = {"pool": pool_error} if pool_error else {}
                for index in indexes:
                    slot = slots.setdefault(index, CISlot(index))
                    try:
                        value = slot.step(config, inventory=inventory, refresh=refresh, cooling=cooling,
                                          admitted=index in admitted, reservations=reservations, recovery=recovery_slots.get(index), priority=True)
                    except (FleetError, OSError, ValueError, KeyError) as exc:
                        slot_errors[str(index)] = str(exc)
                        slot.next_vm = max(slot.next_vm, time.time() + 60)
                        value = read(vm.slot_file({"ci_slot": index}, "vm.json"), {})
                    if value:
                        values.append(value)
                        if value.get("ci_priority_error"):
                            slot_errors[str(index)] = value["ci_priority_error"]
                busy = any(v.get("busy") for v in values)
                if config["workers_enabled"]:
                    if not ids or len(ids) < config["max_workers"]:
                        ids = worker_config(config)
                        start_workers()
                reserve = worker_headroom(caps, ci_busy=busy)
                maximum = config["max_workers"]
                lease = read(state_dir() / "worker-admission-lease.json")
                lease_expired = lease is not None and time.time() >= lease.get("expires_at", 0)
                if lease_expired:
                    maximum = 0
                memory = worker_memory() if config["workers_enabled"] or any(w.get("alive") for w in worker_statuses()) else None
                if memory:
                    try:
                        refresh_worker_memory(config, caps, memory, ci_busy=busy)
                    except (OSError, ValueError) as exc:
                        # Retain the last applied limit and admissions on a transient
                        # tuning error; status keeps the operation and failure visible.
                        memory["allocation_error"] = f"worker memory.high refresh failed: {exc}"
                pressure = memory_pressure(memory, caps, recovering=memory_paused, reserve=reserve) if memory else []
                memory_paused = bool(pressure)
                pause_reason = (
                    "; ".join(pressure) if pressure else ("worker admission lease expired" if lease_expired else None)
                )
                target, reason = scaler.step(
                    now=time.time(),
                    enabled=config["workers_enabled"] and not pressure,
                    ci_busy=busy,
                    available=caps["memory_available"],
                    reserve=reserve,
                    idle=idle,
                    maximum=maximum,
                    pause_reason=pause_reason,
                )
                roadmap_focus = focus.policy(config, ids[:target])
                atomic(
                    state_dir() / "admission.json",
                    dict(
                        version=1,
                        expires_at=time.time() + 15,
                        workers=ids[:target],
                        reason=reason,
                        roadmap_focus=roadmap_focus,
                    ),
                )
                atomic(
                    state_dir() / "status.json",
                    dict(
                        version=1,
                        machine_id=config["machine_id"],
                        controller_release=RUNNING_RELEASE,
                        controller_pid=os.getpid(),
                        live_update_protocol=1,
                        observed_at=time.time(),
                        ci_enabled=config["ci_enabled"],
                        workers_enabled=config["workers_enabled"],
                        ci_busy=busy,
                        ci_cooldown=circuit if cooling else None,
                        active_worker_limit=target,
                        roadmap_focus=roadmap_focus,
                        roadmap_focus_error=roadmap_focus.get("error"),
                        scaling_reason=reason,
                        worker_admission=scaler.decision,
                        worker_memory=memory,
                        memory_pressure=pressure,
                        resources=caps,
                        cpu_idle_fraction=idle,
                        workers=worker_statuses(),
                        worker_isolation=worker_isolation(),
                        error=None,
                        ci_vm=next((v for v in values if v.get("slot", 0) == 0), None),
                        ci_vms=values,
                        ci_jobs_busy=sum(bool(v.get("busy")) for v in values),
                        ci_max_jobs=capacity.jobs(config),
                        ci_memory_budget_gib=capacity.memory_budget_gib(config, caps),
                        ci_kinds=capacity.kinds(config),
                        ci_staging_slots=capacity.reservation(config)[0],
                        ci_staging_reservation_error=capacity.reservation(config)[1],
                        ci_errors=slot_errors,
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
                    dict(machine_id=config["machine_id"], observed_at=time.time(), error=message,
                         controller_release=RUNNING_RELEASE, controller_pid=os.getpid(), live_update_protocol=1),
                )
                for slot in slots.values():
                    slot.next_vm = max(slot.next_vm, time.time() + 60)
            store_warmer.step(config, time.time())
            for _ in range(10):
                if stopping:
                    break
                time.sleep(0.5)
        if not handover:
            atomic(
                state_dir() / "admission.json",
                dict(version=1, expires_at=time.time() + 15, workers=[], reason="controller stopped"),
            )
        for slot in slots.values():
            slot.close()
        if handover:
            binary = str(Path.home() / ".local/bin/tauceti-fleet")
            os.execv(binary, [binary, "host-daemon"])


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
    expected = payload.get("expected_machine_id")
    if expected is not None and expected != host_config()["machine_id"]:
        raise FleetError("Host identity differs from inventory; verify the SSH target before re-enrolling")
    action = payload.get("action")
    if action == "ci-autoscale":
        from .ci_autoscale import host_action

        return host_action(payload)
    if action == "native-runner":
        if payload.get("prepare"):
            return native_runner.prepare()
        return native_runner.status()
    if action == "configure":
        return configure(payload)
    if action == "enroll":
        from . import broker

        return broker.enroll(payload["broker"], code=payload["invitation"])
    if action == "prepare_update":
        config = host_config()
        waiting = {"idle", "waiting-capacity", "waiting-budget", "waiting-quota", "backoff", "stopped", "exited"}
        if config["workers_enabled"] or config["ci_enabled"]:
            raise FleetError("Drain this host before updating")
        if vm.outstanding() or any(
            w.get("alive") and w.get("state") not in waiting for w in worker_statuses()
        ):
            raise FleetError("Active work is still draining; wait before updating")
        # A manager-service stop no longer contains all workers. Slice stop
        # includes scopes, manager survivors, and direct-launch fallback work.
        run(["systemctl", "--user", "stop", "tauceti-fleet-workers.slice"], timeout=45)
        return dict(quiescent=True)
    if action == "doctor":
        result = doctor()
        if payload.get("smoke"):
            result["smoke"] = vm.smoke()
        return result
    if action == "prepare":
        return prepare()
    if action == "enable":
        return enable(ci=payload.get("ci"), workers=bool(payload.get("workers")),
                      maximum=payload.get("maximum"), vcpus=payload.get("vcpus"),
                      memory_gib=payload.get("memory_gib"), disk_gib=payload.get("disk_gib"),
                      memory_budget_gib=payload.get("memory_budget_gib"))
    if action == "focus":
        return focus.configure(payload)
    if action == "workers":
        return set_worker_maximum(payload["maximum"])
    if action == "reserve-staging":
        return reserve_staging(payload["maximum"])
    if action == "drain":
        return drain()
    if action == "budget":
        return budget(payload)
    if action == "snapshot":
        result = snapshot(refresh=False)
        result["lean_statistics"] = read(state_dir() / 'lean-statistics-report.json', {})
        result["status"] = read(state_dir() / "status.json", {})
        result["activity"] = activity.cached(host_config()["machine_id"])
        return result
    if action == "mathlib-store":
        result = mathlib_store.configure(payload)
        if result["mathlib_store"] and "directory" in payload:
            try:
                start_mathlib_store()
            except (FleetError, OSError):
                pass  # The controller starts a pass within its usual interval.
        return result
    if action == "status":
        config = host_config()
        pressure = read(state_dir() / "lean-host-pressure.json", {})
        pressure = dict(pressure, healthy=0 <= time.time() - pressure.get("at", 0) <= 10)
        return dict(
            machine_id=config["machine_id"],
            profile=config["profile"],
            ci_allowed=config["ci_allowed"],
            ci_enabled=config["ci_enabled"],
            ci_kinds=capacity.kinds(config),
            ci_max_jobs=capacity.jobs(config),
            ci_memory_budget_gib=capacity.memory_budget_gib(config),
            ci_staging_slots=capacity.reservation(config)[0],
            ci_staging_reservation_error=capacity.reservation(config)[1],
            ci_enrolled=bool(config.get("ci_broker_url")),
            workers_enabled=config["workers_enabled"],
            max_workers=config["max_workers"],
            lean_host_pressure=pressure,
            lean_statistics=read(state_dir() / 'lean-statistics-report.json', {}),
            ci_autoscale=read(state_dir() / "ci-autoscale.json", {}),
            status=read(state_dir() / "status.json", {}),
            activity=activity.cached(config["machine_id"]),
            ci_overview=ci_overview.cached(config),
            **({"mathlib_store": mathlib_store.cached(config)} if config.get("mathlib_store") else {}),
        )
    raise FleetError("Unknown host operation")
