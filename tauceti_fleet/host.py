"""Local user-service controller. All admission and resource decisions are local."""

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

from . import activity, capacity, ci, github, vm
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
    if "ci_max_jobs" in updates and (type(updates["ci_max_jobs"]) is not int or not 1 <= updates["ci_max_jobs"] <= 16):
        raise FleetError("ci_max_jobs must be an integer between 1 and 16")
    if "ci_kinds" in updates and updates["ci_kinds"] not in capacity.KINDS.values():
        raise FleetError("ci_kinds must explicitly select pr, staging, or both")
    credentials = payload.get("credentials", {})
    if not isinstance(credentials, dict):
        raise FleetError("credentials must be a JSON object")
    current = host_config()
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
        ci_enrolled=bool(value.get("ci_broker_url")),
        workers_enabled=value["workers_enabled"],
        service_error=service_error,
    )


def prepare(config=None):
    config = config or host_config()
    if config["ci_enabled"] or config["workers_enabled"] or vm.outstanding():
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
    ci = capacity.jobs(config) * (config["ci_memory_gib"] + 2) * GIB if config["ci_allowed"] else 0

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

    current_ci, current_workers = current("ci"), current("workers")
    outstanding_ci = max(0, ci - current_ci)
    worker = (
        min(
            caps["memory_total"] - ci - reserve,
            caps["memory_available"] - outstanding_ci - reserve + current_workers,
        )
        - GIB
    )  # Keep a bounded 1 GiB swap allowance inside the worker reservation.
    live_workers = any(w.get("alive") for w in worker_statuses())
    if live_workers:
        previous = run(["systemctl", "--user", "show", "tauceti-fleet-workers.slice", "-p", "MemoryMax", "--value"], check=False)
        try:
            prior_limit = int(previous.stdout.strip())
        except ValueError:
            prior_limit = caps["memory_total"]
        if previous.returncode or worker < max(GIB, current_workers, prior_limit):
            raise FleetError("Wait for draining workers to finish before shrinking their memory reservation for CI")
    if worker < GIB and config["workers_enabled"]:
        raise FleetError(
            "Insufficient memory for bounded workers after CI and OS reservations; configure a smaller CI shape or --no-ci"
        )
    worker = max(16 * 1024**2, worker)
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


def enable(*, ci=None, workers=False, maximum=None, vcpus=None, memory_gib=None, disk_gib=None):
    config = host_config()
    if ci and not config["ci_allowed"]:
        raise FleetError("This host was deployed with --no-ci; reconfigure its role first")
    if ci:
        proposed = capacity.settings(config, ci=ci, maximum=maximum, vcpus=vcpus, memory_gib=memory_gib, disk_gib=disk_gib)
        changed = any(proposed.get(k) != config.get(k) for k in ("ci_kinds", "ci_max_jobs", "ci_vcpus", "ci_memory_gib", "ci_disk_gib"))
        if changed and (config["ci_enabled"] or config["workers_enabled"] or vm.outstanding()):
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
    write_slices(dict(config, workers_enabled=config["workers_enabled"] or workers), resources())
    worker_config(config)
    if ci:
        # Persist the policy before launch, so crash recovery never loses the
        # admission ceiling or tries to replace these guests with the old shape.
        update_host(**{k: config[k] for k in ("ci_kinds", "ci_max_jobs", "ci_vcpus", "ci_memory_gib", "ci_disk_gib") if k in config})
        if not config["ci_enabled"] and not vm.outstanding():
            # Verify registration once. The controller admits remaining slots
            # independently, so a later startup error cannot leave a half-enabled role.
            vm.start(dict(config, ci_slot=0))
    value = update_host(
        ci_enabled=config["ci_enabled"] or bool(ci), workers_enabled=config["workers_enabled"] or workers, draining=False
    )
    install_service()
    return dict(ci_enabled=value["ci_enabled"], workers_enabled=value["workers_enabled"],
                ci_kinds=capacity.kinds(value), ci_max_jobs=capacity.jobs(value))


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
            data = github.ci_read(
                config,
                f"repos/TauCetiProject/TauCeti/actions/runs/{value['run_id']}/jobs?filter=all&per_page=100",
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
            kind = "staging"
            if "pr" in value.get("ci_kinds", ["staging"]) and value.get("host_health") != "fault":
                run = github.ci_read(config, f"repos/TauCetiProject/TauCeti/actions/runs/{value['run_id']}")
                # The immutable receipt supplies the run ID, GitHub supplies its
                # event/title. Guest events never claim PR versus staging trust.
                if run.get("event") != "repository_dispatch" or not str(run.get("display_title", "")).startswith("bors branch="):
                    kind = "pr"
            decision = ci.classify(job, host_health=value.get("host_health"), kind=kind)
            if job.get("conclusion") == "cancelled" and decision["reason"] == "cancellation_evidence_inconclusive":
                try:
                    run_data, statuses = ci.cancellation_metadata(job, config, lambda path, **_: github.ci_read(config, path))
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

    def step(self, config, *, inventory, refresh, cooling):
        config = dict(config, ci_slot=self.index,
                      ci_enabled=config["ci_enabled"] and self.index < capacity.jobs(config))
        path = vm.slot_file(config, "vm.json")
        value = read(path, {})
        if not config["ci_enabled"] and not value:
            if time.time() >= self.next_cleanup:
                self.next_cleanup = time.time() + 300
                vm.cancel_registration(config)
            return {}
        if value:
            active, service_health = ci_service_state(value["unit"])
            if active is False and not (value.get("phase") == "starting" and time.time() - value["started_at"] < 60):
                if queue_ci_result(value, service_health):
                    collect_ci_results(config)
                vm.retire(config, value)
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
                    if notice.get("kind") == "ci_started" and str(notice.get("run_id", "")).isdigit():
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
            if (value and not value.get("busy")
                    and (not config["ci_enabled"] or cooling
                         or (time.time() - value["started_at"] > 180 and own.get("status") != "online"))
                    and not (time.time() - value["started_at"] < 60)):
                vm.retire(config, value)
                value = {}
                self.close()
                self.next_vm = time.time() + 60
        if config["ci_enabled"] and not cooling and not value and time.time() >= self.next_vm:
            value = vm.start(config)
            self.next_vm = time.time() + 60
        return value


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
        slots = {}
        last_github = 0
        inventory = []
        error = None
        ids = []
        last_results = 0
        memory_paused = False
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
                circuit = read(state_dir() / "ci-backoff.json", {})
                cooling = circuit.get("until", 0) > time.time()
                values = []
                slot_errors = {}
                for index in indexes:
                    slot = slots.setdefault(index, CISlot(index))
                    try:
                        value = slot.step(config, inventory=inventory, refresh=refresh, cooling=cooling)
                    except (FleetError, OSError, ValueError, KeyError) as exc:
                        slot_errors[str(index)] = str(exc)
                        slot.next_vm = max(slot.next_vm, time.time() + 60)
                        value = read(vm.slot_file({"ci_slot": index}, "vm.json"), {})
                    if value:
                        values.append(value)
                busy = any(v.get("busy") for v in values)
                if config["workers_enabled"]:
                    if not ids or len(ids) < config["max_workers"]:
                        ids = worker_config(config)
                        start_workers()
                reserve = max(4 * GIB, int(caps["memory_total"] * 0.1))
                maximum = config["max_workers"]
                lease = read(state_dir() / "worker-admission-lease.json")
                lease_expired = lease is not None and time.time() >= lease.get("expires_at", 0)
                if lease_expired:
                    maximum = 0
                memory = worker_memory() if config["workers_enabled"] else None
                pressure = memory_pressure(memory, caps, recovering=memory_paused) if memory else []
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
                        worker_admission=scaler.decision,
                        worker_memory=memory,
                        memory_pressure=pressure,
                        resources=caps,
                        cpu_idle_fraction=idle,
                        workers=worker_statuses(),
                        error=None,
                        ci_vm=next((v for v in values if v.get("slot", 0) == 0), None),
                        ci_vms=values,
                        ci_jobs_busy=sum(bool(v.get("busy")) for v in values),
                        ci_max_jobs=capacity.jobs(config),
                        ci_kinds=capacity.kinds(config),
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
                    dict(machine_id=config["machine_id"], observed_at=time.time(), error=message),
                )
                for slot in slots.values():
                    slot.next_vm = max(slot.next_vm, time.time() + 60)
            for _ in range(10):
                if stopping:
                    break
                time.sleep(0.5)
        atomic(
            state_dir() / "admission.json",
            dict(version=1, expires_at=time.time() + 15, workers=[], reason="controller stopped"),
        )
        for slot in slots.values():
            slot.close()


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
        return enable(ci=payload.get("ci"), workers=bool(payload.get("workers")),
                      maximum=payload.get("maximum"), vcpus=payload.get("vcpus"),
                      memory_gib=payload.get("memory_gib"), disk_gib=payload.get("disk_gib"))
    if action == "workers":
        return set_worker_maximum(payload["maximum"])
    if action == "drain":
        return drain()
    if action == "budget":
        return budget(payload)
    if action == "snapshot":
        result = snapshot(refresh=False)
        result["status"] = read(state_dir() / "status.json", {})
        result["activity"] = activity.cached(host_config()["machine_id"])
        return result
    if action == "status":
        config = host_config()
        return dict(
            machine_id=config["machine_id"],
            profile=config["profile"],
            ci_allowed=config["ci_allowed"],
            ci_enabled=config["ci_enabled"],
            ci_kinds=capacity.kinds(config),
            ci_max_jobs=capacity.jobs(config),
            ci_enrolled=bool(config.get("ci_broker_url")),
            workers_enabled=config["workers_enabled"],
            max_workers=config["max_workers"],
            status=read(state_dir() / "status.json", {}),
            activity=activity.cached(config["machine_id"]),
        )
    raise FleetError("Unknown host operation")
