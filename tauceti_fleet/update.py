"""Stage releases and hand the controller over without stopping useful work."""

import contextlib
import os
import re
import shlex
import sys
import time
import uuid
from pathlib import Path

from . import capacity, native_runner, vm
from .process import executable, run
from .store import FleetError, atomic, data_dir, host_config, locked, read, state_dir


def symlink(path, target):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".update-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_binary(path, contents, mode=0o755):
    temporary = path.with_name(".update-" + uuid.uuid4().hex)
    try:
        temporary.write_bytes(contents)
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def latest_worker_revision():
    """Resolve main once; installation and reporting use that exact commit."""
    result = run(["git", "ls-remote", "--exit-code", "https://github.com/TauCetiProject/TauCetiWorker.git", "refs/heads/main"], timeout=60)
    fields = result.stdout.split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or not re.fullmatch(r"[a-f0-9]{40}", fields[0]):
        raise FleetError("Cannot resolve TauCetiWorker main to one full commit")
    return fields[0]


def stage_worker(revision):
    """Never install into a virtualenv that an existing worker may be using."""
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise FleetError("Worker installation requires a full commit")
    root = data_dir() / "worker-runtimes" / revision
    candidates = [root, *sorted(root.parent.glob(revision + "-*"))]
    for candidate in candidates:
        if read(candidate / "ready.json", {}).get("revision") == revision and (candidate / "bin/tauceti").is_file():
            return candidate
    if root.exists():
        # An interrupted install is retained for diagnosis. Use a distinct path:
        # moving a venv after installation would invalidate its shebangs.
        root = root.with_name(revision + "-" + uuid.uuid4().hex)
    uv = executable("uv")
    if not uv:
        raise FleetError("Install uv before updating the worker runtime")
    run([uv, "venv", root], timeout=120)
    run([uv, "pip", "install", "--python", root / "bin/python",
         "git+https://github.com/TauCetiProject/TauCetiWorker.git@" + revision], timeout=600)
    run([root / "bin/python", "-m", "tauceti_worker", "--help"], timeout=30)
    atomic(root / "ready.json", dict(revision=revision))
    return root


def service():
    result = run(["systemctl", "--user", "show", "tauceti-fleet.service",
                  "-p", "MainPID", "-p", "Restart", "-p", "ActiveState"], check=False)
    if result.returncode:
        raise FleetError("Cannot inspect the Fleet controller service; run doctor")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def controller_running(state):
    """A controller systemd is running or about to restart, not one an operator stopped."""
    return state.get("MainPID", "0") != "0" or state.get("ActiveState") in ("activating", "reloading")


def reset_failed():
    # systemd refuses to reset a unit that is not loaded or not failed; either
    # way there is nothing to reset, and the following start reports real errors.
    run(["systemctl", "--user", "reset-failed", "tauceti-fleet.service"], check=False)


def extend_admission():
    path = state_dir() / "admission.json"
    value = read(path)
    if value and value.get("expires_at", 0) > time.time():
        atomic(path, dict(value, expires_at=time.time() + 120))


def handover(release, *, rollback=False, on_signal=None):
    previous = read(state_dir() / "status.json", {})
    before = service()
    if not rollback and not controller_running(before):
        # A stopped controller stays stopped; it runs the new release when
        # an operator starts it.
        return None
    if rollback:
        reset_failed()
    pid = before.get("MainPID", "0")
    if pid != "0":
        modern = (not rollback and previous.get("live_update_protocol") == 1
                  and str(previous.get("controller_pid")) == pid)
        if before.get("Restart") not in ("on-failure", "always"):
            raise FleetError("Controller requires Restart=on-failure for live handover")
        extend_admission()
        # Legacy controllers empty the admission lease on SIGTERM. Kill only
        # their main process; independent worker scopes and VM services survive.
        with contextlib.ExitStack() as transactions:
            if not modern:
                # A legacy direct-registration host has no durable intent before
                # the JIT reply. Finish any slot transaction before SIGKILL, then
                # release the locks immediately so the replacement can adopt it.
                indexes = sorted(set(range(capacity.jobs(host_config()))) | set(vm.slots_in_use()))
                for index in indexes:
                    transactions.enter_context(locked(vm.slot_file({"ci_slot": index}, "vm.lock")))
            run(["systemctl", "--user", "kill", "--kill-whom=main",
                 "--signal=" + ("SIGHUP" if modern else "SIGKILL"), "tauceti-fleet.service"])
    else:
        # A rollback, or a controller in systemd's restart cycle: a stopped
        # controller returned above.
        if not rollback:
            reset_failed()
        run(["systemctl", "--user", "start", "tauceti-fleet.service"])
    if on_signal:
        on_signal()
    started = time.time()
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current = read(state_dir() / "status.json", {})
        running = service().get("MainPID", "0")
        if current.get("observed_at", 0) > started and not current.get("error") and running != "0":
            if current.get("controller_release") == str(release) and str(current.get("controller_pid")) == running:
                return current
            if rollback and "controller_release" not in current:
                return current
        if running == "0":
            # HUP before Python installs its handler is a clean systemd exit;
            # a crash loop can also exhaust the service's automatic retries.
            reset_failed()
            run(["systemctl", "--user", "start", "tauceti-fleet.service"])
        extend_admission()
        time.sleep(1)
    raise FleetError("Controller did not publish a healthy status after live handover")


def install(release, *, expected_machine_id, expected_revision=False):
    """Runs from the staged release, including when the installed CLI is old.

    expected_revision is the host revision the operator checked ancestry
    against (None for a host without one); False skips the comparison.
    """
    with locked(data_dir() / "update.lock"), locked(state_dir() / "worker-runtime-upgrade.lock"):
        config = host_config()
        if config["machine_id"] != expected_machine_id:
            raise FleetError("Host identity differs from inventory; refusing live update")
        if expected_revision is not False:
            recorded = read(data_dir() / "release.json", {})
            if not isinstance(recorded, dict) or recorded.get("revision") != expected_revision:
                raise FleetError("Host release changed after the ancestry check; inspect status and repeat the update")
        try:
            capacity.controller_config(config)
        except (FleetError, OSError, KeyError, ValueError) as exc:
            raise FleetError(f"Host configuration is invalid for the staged release ({exc}); previous release left running") from None
        release = Path(release).resolve()
        metadata = read(release / "release.json", {})
        current = data_dir() / "current"
        if not current.is_symlink():
            raise FleetError("Live update requires an installed Fleet release symlink")
        previous = current.resolve()
        worker_link = data_dir() / "bin/tauceti"
        if worker_link.exists() and not worker_link.is_symlink():
            raise FleetError("Preserve the existing non-symlink worker executable before updating")
        old_worker = os.readlink(worker_link) if worker_link.is_symlink() else None
        worker_metadata = read(data_dir() / "worker-runtime.json")
        controller = service()
        if controller.get("MainPID", "0") != "0" and controller.get("Restart") not in ("on-failure", "always"):
            raise FleetError("Controller requires Restart=on-failure for live handover")
        if config["ci_enabled"]:
            # Use the installed base image without replacing active guests or
            # waiting for their completion. The old controller keeps serving CI.
            native_runner.prepare_for_ci(config)
        revision = latest_worker_revision() if config["workers_enabled"] or old_worker else None
        worker = stage_worker(revision) if revision else None
        status = read(state_dir() / "status.json", {})
        worker_current = not worker or (worker_link.resolve() == (worker / "bin/tauceti").resolve()
                                         and (worker_metadata or {}).get("revision") == revision)
        if previous == release and worker_current and not controller_running(controller):
            return dict(installed=True, release=release.name, worker_revision=revision, unchanged=True,
                        controller="stopped")
        if (previous == release and status.get("controller_release") == str(release)
                and worker_current
                and not status.get("error") and status.get("observed_at", 0) > time.time() - 15
                and controller.get("MainPID", "0") != "0"
                and str(status.get("controller_pid")) == controller.get("MainPID")):
            return dict(installed=True, release=release.name, worker_revision=revision, unchanged=True)
        # Existing qcow backing images remain untouched, even if a new release
        # would prepare a different image. Image maintenance is a separate task.
        old_metadata = read(data_dir() / "release.json")
        binary = Path.home() / ".local/bin/tauceti-fleet"
        old_binary = binary.read_bytes()
        old_mode = binary.stat().st_mode & 0o777
        signalled = False
        def did_signal():
            nonlocal signalled
            signalled = True
        try:
            if worker:
                symlink(worker_link, worker / "bin/tauceti")
                atomic(data_dir() / "worker-runtime.json", dict(revision=revision))
            atomic(data_dir() / "release.json", metadata)
            symlink(current, release)
            launcher = "import os,sys; sys.path.insert(0,os.path.realpath(" + repr(str(current)) + ")); from tauceti_fleet.cli import main; raise SystemExit(main())"
            python = getattr(sys, "_base_executable", sys.executable)
            write_binary(binary, ("#!/bin/sh\nexec " + shlex.quote(python) + " -c " + shlex.quote(launcher) + ' "$@"\n').encode())
            # handover() checks the controller after the switch: one started
            # before it runs the old release and is handed over, one started
            # later runs this release, and a stopped one stays stopped.
            result = handover(release, on_signal=did_signal)
        except BaseException as exc:
            symlink(current, previous)
            if worker:
                if old_worker:
                    symlink(worker_link, old_worker)
                else:
                    worker_link.unlink(missing_ok=True)
            restore = [(data_dir() / "release.json", old_metadata)]
            if worker:
                restore.append((data_dir() / "worker-runtime.json", worker_metadata))
            for path, value in restore:
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic(path, value)
            write_binary(binary, old_binary, old_mode)
            try:
                if signalled:
                    handover(previous, rollback=True)
            except (FleetError, OSError, ValueError):
                raise FleetError("Live update failed; previous release restored, but controller recovery needs operator attention") from exc
            if not signalled:
                raise FleetError("Live update failed before handover; previous release restored, controller left running") from exc
            raise FleetError("Live update failed; previous release restored and controller recovered") from exc
        if result is None:
            return dict(installed=True, release=release.name, controller="stopped", worker_revision=revision,
                        worker_runtime_scope="future manager starts", image="preserved")
        # Only enable writes slice units, so a handed-over host gets the shared
        # ceiling here. Unmeasured host RAM would compute a 16 MiB ceiling.
        from . import host

        caps = host.resources()
        if caps["memory_total"] and host.install_ceiling(caps):
            run(["systemctl", "--user", "daemon-reload"])
        return dict(installed=True, release=release.name, controller_release=result["controller_release"],
                    worker_revision=revision,
                    active_processes_preserved=True,
                    worker_runtime_scope="future manager starts; running managers and workers retain their existing runtime",
                    image="preserved")
