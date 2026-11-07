"""A bounded rootless runner backend sharing Fleet's existing slot lifecycle."""

import ctypes
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import capabilities, capacity, lean_guest, lean_pressure, lean_runtime, lean_statistics, native_inside
from .process import executable, run
from .store import FleetError, atomic, data_dir, host_config, locked, read, state_dir, update_host

IMAGE_REVISION = 1
DISK_START_GIB = 48
MEMORY_GIB = 54
RUNTIME = "native-disk"
CGROUP_ROOT = Path("/sys/fs/cgroup")


def image():
    return read(data_dir() / "images/native.json", {})


def ready():
    value = image()
    if not re.fullmatch(r"[a-f0-9]{64}", value.get("sha256", "")):
        return False
    root = data_dir() / "images/native" / value.get("sha256", "missing") / "rootfs"
    return value.get("revision") == IMAGE_REVISION and (root / "opt/actions-runner/bin/Runner.Listener").is_file()


def mapping_ok():
    try:
        return int(Path("/proc/sys/vm/max_map_count").read_text()) >= 1048576
    except (OSError, ValueError):
        return False


def selected(config):
    slot = config.get("ci_native_slot")
    # Recovery may narrow ci_kinds to PR even on a mixed-class host. The
    # reservation count still describes the host's slots in that case.
    reserved = config.get("ci_staging_slots", 0)
    index = config.get("ci_slot", 0)
    return ((slot == "next-general" or (type(slot) is int and slot == index))
            and type(index) is int and type(reserved) is int
            and 0 <= reserved <= index < capacity.jobs(config)
            and "pr" in config.get("ci_kinds", []))


def is_native(value):
    return value.get("runtime") == RUNTIME


def idle_expired(value, own):
    return (is_native(value) and not value.get("busy") and not value.get("run_id")
            and own.get("status") == "online" and not own.get("busy")
            and time.time() - value.get("online_observed_at", time.time()) >= 300)


def instances():
    from . import vm
    return [read(vm.slot_file({"ci_slot": slot}, "vm.json"), {}) for slot in vm.slots_in_use()]


def usage(value):
    result = run(["systemctl", "--user", "show", value["unit"], "-p", "MemoryCurrent", "-p", "ActiveState",
                  "-p", "SubState", "-p", "LoadState", "-p", "ControlGroup"], check=False)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    inactive = (fields.get("ActiveState") in ("inactive", "failed") or fields.get("SubState") == "exited"
                or fields.get("LoadState") == "not-found")
    memory = dict(memory_bytes=None, memory_working_bytes=None, memory_clean_file_bytes=None)
    try:
        group = Path(fields["ControlGroup"])
        if not group.is_absolute() or group == Path("/") or ".." in group.parts:
            raise ValueError("Invalid native cgroup")
        group = CGROUP_ROOT / group.relative_to("/")
        current = int((group / "memory.current").read_text())
        stats = dict(line.split() for line in (group / "memory.stat").read_text().splitlines())
        clean = capabilities.clean_file_bytes({key: int(value) for key, value in stats.items()})
        memory.update(memory_bytes=current, memory_working_bytes=max(0, current - clean), memory_clean_file_bytes=clean)
    except (KeyError, OSError, ValueError):
        pass
    return memory, inactive


def status():
    active = []
    for value in instances():
        if not is_native(value):
            continue
        memory, inactive = usage(value)
        active.append(dict(instance=value["instance"], slot=value["slot"], unit=value["unit"],
                           phase=value.get("phase"), run_id=value.get("run_id"), **memory,
                           unit_inactive=inactive, memory_max_gib=value.get("memory_max_gib"),
                           disk_budget_gib=value.get("disk_budget_gib"), workspace_storage="disk",
                           progress=shared_json(Path(value["directory"]) / "control/native-progress.json", {})))
    return dict(ci_native_slot=host_config().get("ci_native_slot"), image=image(), ready=ready(),
                choice=read(state_dir() / "native-choice.json", {}),
                instances=active)


def choose(config, available):
    """Select disk-native before registration; startup faults remain explicit."""
    if not selected(config):
        return False
    from . import vm
    intent = read(vm.slot_file(config, "ci-registration.json"), {}) if config.get("ci_broker_url") else {}
    # Preserve already-owned registration classes and the mixed-class spare.
    if ((intent and intent.get("ci_kinds") != ["pr"])
            or (config.get("ci_idle_pool") and "staging" in config.get("ci_kinds", [])
                and not config.get("ci_staging_slots", 0))):
        atomic(state_dir() / "native-choice.json", dict(at=time.time(), slot=config.get("ci_slot", 0),
               selection=config.get("ci_native_slot"), backend="vm", reason="preserve existing runner classes"))
        return False
    reason = ("native image not prepared" if not ready() else
              "native host mapping limit is too small" if not mapping_ok() else None)
    atomic(state_dir() / "native-choice.json", dict(at=time.time(), slot=config.get("ci_slot", 0),
           selection=config.get("ci_native_slot"), backend=RUNTIME, reason=reason, available_bytes=available,
           startup_required_bytes=vm.START_MEMORY_BYTES))
    if reason:
        raise FleetError(reason)
    return True


def configure(slot):
    config = host_config()
    if slot is not None:
        general = range(capacity.staging_slots(config), capacity.jobs(config))
        if not general or (slot != "next-general" and (type(slot) is not int or slot not in general)):
            raise FleetError("Choose an existing general PR slot, outside reserved staging slots")
        if "pr" not in capacity.kinds(config):
            raise FleetError("The native canary requires PR jobs on this host")
        if not ready():
            raise FleetError("Prepare the native image before selecting a slot")
    update_host(ci_native_slot=slot)
    return dict(ci_native_slot=slot, next_instance_only=True, image=image())


def prepare(config=None):
    """Export the prepared Ubuntu OS through a small, unregistered user VM."""
    from . import vm
    config = config or host_config()
    prerequisites()
    vm.prepare_tools()
    root = data_dir() / "images"
    with locked(root / "native-prepare.lock"):
        base = read(root / "prepared.json", {})
        if not base or vm.digest(root / "prepared.qcow2") != base.get("sha256"):
            raise FleetError("Prepare and verify the ordinary VM image first")
        current = image()
        if ready() and current.get("vm_sha256") == base["sha256"]:
            return current
        staging = state_dir() / ("native-image-" + secrets.token_hex(6))
        staging.mkdir(mode=0o700)
        disk = staging / "disk.qcow2"
        unit = "tauceti-native-image-" + secrets.token_hex(4)
        archive = staging / "rootfs.tar.gz"
        run([executable("qemu-img"), "create", "-f", "qcow2", "-F", "qcow2", "-b", root / "prepared.qcow2", disk, "20G"])
        commands = [
            "mkdir /export; mount -t 9p -o trans=virtio,version=9p2000.L export /export",
            "systemctl mask --now apt-daily.timer apt-daily-upgrade.timer; systemctl mask apt-daily.service apt-daily-upgrade.service",
            "while systemctl is-active --quiet apt-daily.service || systemctl is-active --quiet apt-daily-upgrade.service; do sleep 1; done",
            "export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y bubblewrap",
            "tar --numeric-owner --one-file-system --exclude=./export --exclude=./dev --exclude=./proc --exclude=./sys --exclude=./run --exclude=./tmp --exclude=./var/log --exclude=./lost+found -czf /export/rootfs.tar.gz -C / .",
            "echo FLEET_NATIVE_IMAGE_READY; sync; poweroff",
        ]
        seed = vm.cloud_seed(staging, commands, name=unit)
        args = vm.qemu_args(config, staging, disk, seed, small=True) + [
            "-fsdev", f"local,id=export,path={staging},security_model=none", "-device", "virtio-9p-pci,fsdev=export,mount_tag=export"]
        extracted = None
        complete = False
        try:
            result = run(vm.scope_args(unit, args, small=True), timeout=1800, check=False)
            if result.returncode or "FLEET_NATIVE_IMAGE_READY" not in (staging / "console.log").read_text(errors="replace"):
                raise FleetError(f"Native image preparation failed; inspect {staging / 'console.log'}")
            sha = vm.digest(archive)
            destination = root / "native" / sha
            destination.mkdir(parents=True, exist_ok=True, mode=0o700)
            extracted = destination / "rootfs"
            if extracted.exists():
                remove_root(extracted)
            extracted.mkdir()
            run(["unshare", "--user", "--map-auto", "--map-root-user", "tar", "--numeric-owner", "-xpf", archive, "-C", extracted], timeout=300)
            (extracted / "tmp").mkdir(mode=0o1777, exist_ok=True)
            (extracted / "tmp").chmod(0o1777)
            if not (extracted / "opt/actions-runner/bin/Runner.Listener").is_file():
                raise FleetError("Native rootfs export is missing the Actions runner")
            value = dict(revision=IMAGE_REVISION, sha256=sha, vm_sha256=base["sha256"],
                         runner_version=base["runner_version"], prepared_at=time.time())
            atomic(root / "native.json", value)
            complete = True
        finally:
            run(["systemctl", "--user", "stop", unit + ".scope"], check=False)
            if not complete:
                for name in ("disk.qcow2", "seed.iso", "rootfs.tar.gz"):
                    (staging / name).unlink(missing_ok=True)
                if extracted is not None and extracted.exists():
                    remove_root(extracted)
        shutil.rmtree(staging)
        return value


def remove_root(root):
    run(["unshare", "--user", "--map-auto", "--map-root-user", "rm", "-rf", "--", root], timeout=120)
    if Path(root).exists():
        raise FleetError("Native filesystem cleanup is incomplete; retain instance ownership")


def stage(config, directory, jit):
    """Stage a small durable launch intent; allocate OS/workspace inside its unit."""
    directory = Path(directory)
    value = image()
    (directory / "control").mkdir(mode=0o700)
    (directory / "control/jit").write_text(jit)
    (directory / "control/jit").chmod(0o600)
    atomic(directory / "native.json", dict(image=value, disk_budget_gib=native_inside.DISK_BUDGET_GIB, memory_gib=MEMORY_GIB))
    return dict(runtime=RUNTIME, native_image_sha256=value["sha256"], workspace_storage="disk",
                disk_budget_gib=native_inside.DISK_BUDGET_GIB, memory_max_gib=MEMORY_GIB,
                vcpus=os.cpu_count(), memory_gib=MEMORY_GIB, memory_is_limit=True)


def materialize(directory):
    """Slow filesystem work belongs to the job unit, never the controller loop."""
    directory = Path(directory)
    value = read(directory / "native.json")["image"]
    source = data_dir() / "images/native" / value["sha256"] / "rootfs"
    root = directory / "rootfs"
    run(["unshare", "--user", "--map-auto", "--map-root-user", "cp", "-a", "--reflink=auto", source, root], timeout=120)
    for name, module in (("tauceti_lean_guest.py", lean_guest), ("tauceti_lean_statistics.py", lean_statistics),
                         ("tauceti-lean", lean_pressure), ("tauceti_native_inside.py", native_inside)):
        target = root / "usr/local/bin" / name
        target.write_text(Path(module.__file__).read_text())
        target.chmod(0o755)
    (root / "opt/tauceti-lean-env").write_text(lean_runtime.STARTUP)


def prerequisites():
    """Check host capabilities without changing host sysctls or requiring root."""
    run(["unshare", "--user", "--map-auto", "--map-root-user", "--mount", "true"])
    if not mapping_ok():
        raise FleetError("Native CI requires host vm.max_map_count >= 1048576")


def argv(directory, *, offline=False):
    return [sys.executable, "-m", __name__, "serve-offline" if offline else "serve", str(directory)]


def properties():
    # .NET's W^X mapper uses a memfd with a large logical size. RLIMIT_FSIZE
    # applies to that RAM-backed file and kills the stock Actions listener.
    # Bound allocated RAM with the cgroup; the watchdog monitors allocated disk.
    return ["-p", f"MemoryMax={MEMORY_GIB}G", "-p", "OOMPolicy=kill", "-p", "OOMScoreAdjust=1000",
            "-p", "LimitFSIZE=infinity", "-p", "LimitCORE=0", "-p", "Delegate=yes", "-p", "TimeoutStopSec=8",
            "--setenv=PYTHONPATH=" + str(Path(__file__).resolve().parent.parent)]


def request_stop(value):
    if not is_native(value):
        return
    result = run(["systemctl", "--user", "show", value["unit"], "-p", "MainPID", "--value"], check=False)
    if result.stdout.strip().isdigit() and int(result.stdout.strip()) > 0:
        notice = dict(at=time.time(), reason="memory recovery" if value.get("operator_recovery") else "requested retirement")
        atomic(Path(value["directory"]) / "native-stop.json", notice)
        return notice


def cleanup(value, *, asynchronous=False):
    """Release only stopped, owned instances; daemon cleanup never blocks on rm."""
    directory = Path(value["directory"])
    if directory.parent != state_dir() / "vms" or not re.fullmatch(r"[a-f0-9]{16}", directory.name):
        raise FleetError("Native cleanup instance ownership is invalid")
    if value.get("unit"):
        result = run(["systemctl", "--user", "is-active", value["unit"]], check=False)
        if result.returncode not in (3, 4):
            raise FleetError("Native unit has not stopped; retain instance ownership")
    if not directory.exists():
        return True
    evidence = state_dir() / "native-results" / directory.name
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (evidence / "archived.json").exists():
        atomic(evidence / "instance.json", value)
        for name in ("native.json", "native-result.json", "native-samples.jsonl", "native-stop.json", "runner-console.log"):
            copy_diagnostic(directory / name, evidence / name)
        for name in ("native-progress.json", "native-resource-error.json", "events.jsonl"):
            copy_diagnostic(directory / "control" / name, evidence / name)
        (evidence / "telemetry").mkdir(exist_ok=True)
        telemetry = directory / "control/telemetry"
        if not telemetry.is_symlink():
            for name in ("phases.tsv", "lake-build.log", "ci-host-resources.json", "runner-diagnostic.log", "artifact-mappings.json"):
                copy_diagnostic(telemetry / name, evidence / "telemetry" / name)
        atomic(evidence / "archived.json", dict(at=time.time()))
        # Bounded small evidence; live roots and their ownership are separate.
        archived = sorted((state_dir() / "native-results").glob("*/archived.json"), key=lambda p: p.stat().st_mtime)
        for old in archived[:-50]:
            shutil.rmtree(old.parent)
    if asynchronous:
        unit = "tauceti-native-cleanup-" + directory.name + ".service"
        fields = run(["systemctl", "--user", "is-active", unit], check=False)
        if fields.returncode == 0:
            return False
        if fields.returncode not in (3, 4):
            raise FleetError("Cannot establish native cleanup state; retain ownership")
        run(["systemctl", "--user", "reset-failed", unit], check=False)
        run(["systemd-run", "--user", "--quiet", "--unit=" + unit, "-p", "MemoryMax=256M", "-p", "RuntimeMaxSec=180",
             "unshare", "--user", "--map-auto", "--map-root-user", "rm", "-rf", "--", directory])
        return False
    remove_root(directory)
    return True


def copy_diagnostic(source, destination):
    """Diagnostics cannot block retirement on a FIFO, permissions or huge log."""
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as incoming:
            if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                return
            with Path(destination).open("wb") as outgoing:
                outgoing.write(incoming.read(32 * 1024**2))
    except OSError:
        pass


def shared_json(path, default=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return default
            raw = stream.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                return default
            value = json.loads(raw)
            return value if isinstance(value, dict) else default
    except (OSError, ValueError):
        return default


def hook_lines(directory, offset=0):
    """Only consume complete, bounded records; hooks are replayable after a restart."""
    path = Path(directory) / "control/events.jsonl"
    if not path.exists():
        return [], offset
    lines = []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return [], offset
        stream.seek(offset)
        while line := stream.readline(4097):
            if len(line) > 4096:
                raise FleetError("Native job hook record is too large")
            if not line.endswith(b"\n"):
                break
            offset = stream.tell()
            try:
                notice = json.loads(line)
            except ValueError:
                continue
            if not isinstance(notice, dict) or notice.get("kind") not in ("ci_started", "ci_completed"):
                continue
            lines.append(line)
            if offset > 65536:
                raise FleetError("Native job hook history is too large")
    return lines, offset


def reconcile_events(config, value):
    """Recover job ownership before ordinary dead-unit/result reconciliation."""
    if not is_native(value):
        return value
    try:
        lines, _ = hook_lines(value["directory"])
    except (OSError, ValueError, FleetError):
        # Bad diagnostics must not strand dead instance ownership.
        return value
    before = dict(value)
    for line in lines:
        notice = json.loads(line)
        if not value.get("run_id") and str(notice.get("run_id", "")).isdigit():
            value.update(busy=True, run_id=int(notice["run_id"]), job_started_at=notice.get("at", time.time()))
        if notice["kind"] == "ci_completed":
            value["native_completed_at"] = notice.get("at")
    if before != value:
        from . import vm
        atomic(vm.slot_file(config, "vm.json"), value)
    return value


class PressureRelay:
    """Expose only the existing pressure stream, never connect to a job path."""
    def __init__(self, directory):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(Path(directory) / "pressure.sock"))
        self.listener.listen(2)
        self.listener.setblocking(False)
        self.parent, self.child = socket.socketpair()
        self.parent.setblocking(False)
        self.host = None
        self.to_host, self.to_guest = b"", b""
        self.ended = False

    def drop(self):
        if self.host:
            self.host.close()
        self.host = None
        self.to_host = b""

    def poll(self):
        if self.ended:
            return
        readers = [self.listener, self.parent] + ([self.host] if self.host else [])
        writers = ([self.parent] if self.to_guest else []) + ([self.host] if self.host and self.to_host else [])
        readable, writable, _ = select.select(readers, writers, [], 0)
        if self.listener in readable:
            self.drop()
            self.host, _ = self.listener.accept()
            self.host.setblocking(False)
            # Discard any interrupted JSON frame before a fresh publisher frame.
            self.to_guest += b"\n"
        if self.parent in readable:
            try:
                data = self.parent.recv(65536)
            except ConnectionResetError:
                data = b""
            if not data:
                self.ended = True
                self.drop()
                return
            if self.host:
                self.to_host = (self.to_host + data)[-65536:]
        try:
            if self.host and self.host in readable:
                data = self.host.recv(65536)
                if not data:
                    self.drop()
                elif len(self.to_guest) + len(data) > 65536:
                    self.drop()
                else:
                    self.to_guest += data
            if self.host and self.host in writable:
                self.to_host = self.to_host[self.host.send(self.to_host):]
        except (OSError, BlockingIOError):
            self.drop()
        if self.parent in writable:
            try:
                self.to_guest = self.to_guest[self.parent.send(self.to_guest):]
            except BlockingIOError:
                pass
            except (BrokenPipeError, ConnectionResetError):
                self.ended = True
                self.drop()

    def close(self):
        self.drop()
        self.listener.close()
        self.parent.close()
        self.child.close()


def publish_memory(directory):
    """Transport the existing absolute local OOM guard into offline /usr mounts."""
    relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    group = Path("/sys/fs/cgroup") / relative.lstrip("/")
    view = Path(directory) / "memory-view"
    view.mkdir(exist_ok=True)
    for name in ("memory.max", "memory.current", "memory.stat"):
        content = (group / name).read_text()
        fd, temporary = tempfile.mkstemp(dir=view)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
            os.chmod(temporary, 0o444)
            os.replace(temporary, view / name)
        finally:
            Path(temporary).unlink(missing_ok=True)


def payload(directory):
    # Root in the runner maps to the fleet UID. Anchor its cgroup namespace in
    # a child of the capped service, so even a new cgroupfs mount cannot expose
    # or raise the service's ancestor limit or reach another job's cgroup.
    relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
    group = Path("/sys/fs/cgroup") / relative.lstrip("/") / "payload"
    group.mkdir(exist_ok=True)
    (group / "cgroup.procs").write_text(str(os.getpid()))
    os.execv(executable("unshare"), ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", "--pid", "--ipc", "--uts", "--cgroup", "--fork", "--kill-child",
             sys.executable, "-m", "tauceti_fleet.native_runner", "inside", str(directory)])


def inside(directory):
    directory = Path(directory).resolve()
    root = directory / "rootfs"
    mount = executable("mount")
    run([mount, "--make-rprivate", "/"])
    run([mount, "--bind", root, root])
    socket.sethostname(b"tauceti-native")
    for name in ("dev", "proc", "run", "sys/fs/cgroup", "fleet-link", "var/log", "opt/actions-runner/_work"):
        (root / name).mkdir(parents=True, exist_ok=True)
    run([mount, "--bind", directory / "control", root / "fleet-link"])
    view = root / "usr/local/share/tauceti/native-memory"
    view.mkdir(parents=True, exist_ok=True)
    run([mount, "--bind", directory / "memory-view", view])
    run([mount, "-o", "remount,bind,ro", view])
    run([mount, "-t", "tmpfs", "-o", "mode=755,size=16M", "tmpfs", root / "dev"])
    for name in ("null", "zero", "full", "random", "urandom", "tty"):
        (root / "dev" / name).touch()
        run([mount, "--bind", "/dev/" + name, root / "dev" / name])
    (root / "dev/shm").mkdir()
    run([mount, "-t", "tmpfs", "-o", "mode=1777,size=64M", "tmpfs", root / "dev/shm"])
    (root / "dev/fd").symlink_to("/proc/self/fd")
    for name, fd in (("stdin", 0), ("stdout", 1), ("stderr", 2)):
        (root / "dev" / name).symlink_to("/proc/self/fd/" + str(fd))
    run([mount, "-t", "proc", "proc", root / "proc"])
    run([mount, "-t", "tmpfs", "-o", "mode=755,size=32M", "tmpfs", root / "run"])
    resolver = root / "etc/resolv.conf"
    resolver.unlink(missing_ok=True)
    resolver.write_text(Path("/etc/resolv.conf").read_text())
    (root / "etc/hosts").write_text("127.0.0.1 localhost tauceti-native\n::1 localhost\n")
    (root / ".oldroot").mkdir(exist_ok=True)
    libc = ctypes.CDLL(None, use_errno=True)
    os.chdir(root)
    if libc.syscall(155, ctypes.c_char_p(b"."), ctypes.c_char_p(b".oldroot")) != 0:
        raise OSError(ctypes.get_errno(), "pivot_root failed")
    os.chdir("/")
    if libc.umount2(ctypes.c_char_p(b"/.oldroot"), 2) != 0:
        raise OSError(ctypes.get_errno(), "old root unmount failed")
    os.rmdir("/.oldroot")
    os.execve("/usr/bin/python3", ["python3", "/usr/local/bin/tauceti_native_inside.py"],
              dict(PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", HOME="/root", LANG="C.UTF-8",
                   TAUCETI_NATIVE_PRESSURE_FD=os.environ["TAUCETI_NATIVE_PRESSURE_FD"]))


def serve(directory, *, offline=False):
    directory = Path(directory)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(directory / "events.sock"))
    listener.listen(4)
    listener.setblocking(False)
    clients, history = [], []
    command = [sys.executable, "-m", "tauceti_fleet.native_runner", "payload", str(directory)]
    child = None
    relay = PressureRelay(directory)
    previous_signal = signal.getsignal(signal.SIGTERM)
    started, offset, next_sample = time.time(), 0, 0
    transport_ended = None
    code, failed = 75, {}
    requested_stop = False
    def stop(*_):
        nonlocal requested_stop
        requested_stop = True
        if child:
            child.terminate()
    signal.signal(signal.SIGTERM, stop)
    try:
        if not (directory / "rootfs").exists():
            materialize(directory)
        if requested_stop:
            code = 143
            return code
        if offline:
            # Explicit unregistered integration mode; the production launch path
            # always runs the stock Actions runner and cannot select this mode.
            script = '#!/bin/bash\nset -euo pipefail\ncd /opt/actions-runner/_work\nexec bash /fleet-link/offline-job.sh\n'
            run(["unshare", "--user", "--map-auto", "--map-root-user", "sh", "-c",
                 'cat > "$1"; chmod 755 "$1"', "sh", directory / "rootfs/opt/actions-runner/run.sh"], input=script)
        with (directory / "runner-console.log").open("ab", buffering=0) as log:
            if requested_stop:
                code = 143
                return code
            publish_memory(directory)
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, pass_fds=(relay.child.fileno(),),
                                     env=dict(os.environ, TAUCETI_NATIVE_PRESSURE_FD=str(relay.child.fileno())))
            relay.child.close()
            with (directory / "native-samples.jsonl").open("a", buffering=1) as samples:
                while True:
                    if requested_stop:
                        code = 143
                        break
                    relay.poll()
                    if relay.ended:
                        transport_ended = transport_ended or time.monotonic()
                        if time.monotonic() - transport_ended > 5 and child.poll() is None:
                            failed = dict(reason="native pressure transport closed")
                            break
                    publish_memory(directory)
                    try:
                        client, _ = listener.accept()
                        client.settimeout(.1)
                        clients.append(client)
                        try:
                            for line in history:
                                client.sendall(line)
                        except OSError:
                            clients.remove(client)
                            client.close()
                    except BlockingIOError:
                        pass
                    try:
                        lines, offset = hook_lines(directory, offset)
                    except (OSError, ValueError, FleetError):
                        # Telemetry cannot kill useful work or block other slots.
                        lines = []
                    history.extend(lines)
                    for client in list(clients):
                        try:
                            for line in lines:
                                client.sendall(line)
                        except OSError:
                            clients.remove(client)
                            client.close()
                    # Drain hooks after poll reports exit too. The completion hook
                    # is often written just before the runner exits.
                    if child.poll() is not None:
                        code = child.wait()
                        break
                    if time.monotonic() >= next_sample:
                        relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
                        group = Path("/sys/fs/cgroup") / relative.lstrip("/")
                        value = dict(at=time.time(), progress=shared_json(directory / "control/native-progress.json", {}))
                        for name in ("memory.current", "memory.peak", "memory.stat", "memory.events", "cpu.stat", "io.stat"):
                            try:
                                value[name] = (group / name).read_text()
                            except OSError:
                                value[name] = None
                        samples.write(json.dumps(value) + "\n")
                        next_sample = time.monotonic() + 5
                    time.sleep(.2)
    except (OSError, ValueError, FleetError) as error:
        failed = dict(reason="native supervisor failure", error=type(error).__name__)
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        signal.signal(signal.SIGTERM, previous_signal)
        failed = failed or shared_json(directory / "control/native-resource-error.json", {})
        if not offline and not requested_stop and not failed and code == 0 and not any(
                str(json.loads(line).get("run_id", "")).isdigit() for line in history):
            failed = dict(reason="native runner exited without a job")
        try:
            relative = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
            group = Path("/sys/fs/cgroup") / relative.lstrip("/")
            peak = int((group / "memory.peak").read_text())
            memory_events = (group / "memory.events").read_text()
        except (OSError, ValueError, StopIteration):
            peak, memory_events = None, None
        atomic(directory / "native-result.json", dict(started_at=started, ended_at=time.time(), exit_code=code,
                                                     infrastructure_error=failed, requested_stop=requested_stop,
                                                     memory_peak_bytes=peak, memory_events=memory_events))
        if (directory / "rootfs").exists() and not requested_stop:
            try:
                remove_root(directory / "rootfs")
            except FleetError:
                pass  # Ordinary recovery retains ownership and retries cleanup.
        for client in clients:
            client.close()
        listener.close()
        relay.close()
    return 75 if failed else code


def main():
    action, directory = sys.argv[1:]
    if action == "payload":
        payload(directory)
    if action == "inside":
        inside(directory)
    if action in ("serve", "serve-offline"):
        return serve(directory, offline=action == "serve-offline")
    raise FleetError("Unknown native runtime action")


if __name__ == "__main__":
    raise SystemExit(main())
