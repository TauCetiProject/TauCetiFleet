"""macOS namesakes of the functions marked ``@hostos.linux``.

launchd agents stand in for the systemd user units. macOS has no slices, so
nothing bounds what a worker uses and admission counts workers against free
memory instead. A Mac takes no CI runners.
"""

import ctypes
import ctypes.util
import functools
import getpass
import os
import platform
import plistlib
import re
import shutil
import time
from pathlib import Path

from . import host
from .process import run
from .store import FleetError, config_dir, state_dir

GIB = 1024**3
WORKER_MEMORY = 4 * GIB
CONTROLLER = "org.tauceti.fleet"
OBSERVER = "org.tauceti.fleet.observe"
WORKERS = "org.tauceti.fleet.workers"
# A worker's wrapper refreshes heartbeat_at every two seconds while its round
# runs; a status file older than this may be left by a worker that has died.
HEARTBEAT_FRESH = 30


def domain():
    return f"gui/{os.getuid()}"


def agent(label):
    # launchd loads everything in LaunchAgents at login. The worker manager
    # stays out of it: like its systemd unit, only the controller starts it.
    if label == WORKERS:
        return config_dir() / "launchd" / (label + ".plist")
    return Path.home() / "Library/LaunchAgents" / (label + ".plist")


def write_agent(label, arguments, environment, **keys):
    log = state_dir() / "logs" / (label + ".log")
    log.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = agent(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(dict(
        Label=label,
        ProgramArguments=[str(a) for a in arguments],
        EnvironmentVariables=environment,
        StandardOutPath=str(log),
        StandardErrorPath=str(log),
        **keys,
    )))


def loaded(label):
    return run(["launchctl", "print", f"{domain()}/{label}"], check=False).returncode == 0


def bootstrap(label):
    # launchd can still be tearing the previous instance down when bootout
    # returns, and refuses the label until it has finished.
    deadline = time.monotonic() + 10
    while run(["launchctl", "bootstrap", domain(), agent(label)], check=False).returncode:
        if time.monotonic() >= deadline:
            raise FleetError(f"launchd did not load {label}; run doctor")
        time.sleep(0.5)


def start(label, *, restart=False):
    if not loaded(label):
        bootstrap(label)
    elif restart:
        run(["launchctl", "bootout", f"{domain()}/{label}"], check=False)
        bootstrap(label)
    else:
        run(["launchctl", "kickstart", f"{domain()}/{label}"])


def service_environment():
    environment = dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"))
    for name in ("TAUCETI_FLEET_CONFIG", "TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def launcher():
    return Path.home() / ".local/bin/tauceti-fleet"


def install_service(*, restart=True):
    environment = service_environment()
    write_agent(CONTROLLER, [launcher(), "host-daemon"], environment,
                RunAtLoad=True, KeepAlive=dict(SuccessfulExit=False), ThrottleInterval=5)
    write_agent(OBSERVER, [launcher(), "collect-status"], environment, RunAtLoad=True, StartInterval=300, Nice=10)
    start(CONTROLLER, restart=restart)
    start(OBSERVER)


def install_pressure_service():
    return None


def start_workers():
    # launchd keeps the definition it loaded until bootout, which would stop
    # the running manager, so the agent names the launcher that follows the
    # installed release rather than this controller's release.
    write_agent(WORKERS, [launcher(), "run-workers"], service_environment(),
                RunAtLoad=True, KeepAlive=dict(SuccessfulExit=False), ExitTimeOut=30)
    start(WORKERS)


def stop_workers():
    raise FleetError("A macOS host has no worker slice to stop; update hands the controller over live instead")


def write_slices(config, caps):
    return None


def service():
    result = run(["launchctl", "print", f"{domain()}/{CONTROLLER}"], check=False)
    pid = re.search(r"^\s*pid = (\d+)$", result.stdout, re.MULTILINE) if result.returncode == 0 else None
    return dict(MainPID=pid.group(1) if pid else "0", Restart="on-failure")


def reset_failed():
    return None


def signal_controller(name):
    run(["launchctl", "kill", name, f"{domain()}/{CONTROLLER}"])


def start_controller():
    start(CONTROLLER)


def memory_total():
    return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")


def resources():
    try:
        text = run(["vm_stat"]).stdout
        page = int(re.search(r"page size of (\d+) bytes", text).group(1))
        # Free, speculative and inactive pages are what the kernel hands out
        # without swapping or compressing, the nearest thing to MemAvailable.
        pages = re.findall(r"^Pages (?:free|speculative|inactive):\s+(\d+)\.", text, re.MULTILINE)
        if len(pages) != 3:
            raise ValueError("vm_stat")
        total = memory_total()
        return dict(
            cpus=float(os.cpu_count()),
            memory_total=total,
            memory_available=min(total, sum(map(int, pages)) * page),
            cgroup=None,
            disk_available=shutil.disk_usage(Path.home()).free,
        )
    except (FleetError, OSError, ValueError, AttributeError, TypeError):
        return dict(cpus=0, memory_total=0, memory_available=0, cgroup=None)


def worker_memory():
    return None


@functools.cache
def mach_host():
    libc = ctypes.CDLL(ctypes.util.find_library("c"))
    libc.mach_host_self.restype = ctypes.c_uint
    return libc, libc.mach_host_self()


CPU = dict(raw=None, total=0, idle=0)


def cpu_sample():
    libc, port = mach_host()
    raw = (ctypes.c_uint * 4)()  # HOST_CPU_LOAD_INFO: user, system, idle, nice
    if libc.host_statistics(port, 3, raw, ctypes.byref(ctypes.c_uint(4))):
        raise OSError("host_statistics failed")
    if CPU["raw"]:
        # The Mach counters are 32-bit and wrap within weeks on a many-core Mac.
        delta = [(new - old) % 2**32 for new, old in zip(raw, CPU["raw"], strict=True)]
        CPU["total"] += sum(delta)
        CPU["idle"] += delta[2]
    CPU["raw"] = list(raw)
    return CPU["total"], CPU["idle"]


class Scaler:
    """Count each worker as WORKER_MEMORY of free memory.

    Admission is the prefix ``workers[:target]``. The target never exceeds
    what total memory holds, and the prefix takes in idle workers only while
    free memory holds another; workers with a live round in progress cost nothing.
    """

    def __init__(self):
        self.target = 0
        self.decision = {}

    def step(self, *, enabled, available, reserve, maximum, workers=(), pause_reason=None, **_):
        each = f"{WORKER_MEMORY // GIB} GiB each"
        if not enabled or available < reserve:
            self.target = 0
            reason = pause_reason or (
                "workers disabled or host draining" if not enabled else "host memory headroom below reserve"
            )
        else:
            now = time.time()
            busy = {
                w.get("id") for w in host.worker_statuses()
                if w.get("state") in ("running", "surveying") and w.get("alive") is True
                and now - (w.get("heartbeat_at") or 0) < HEARTBEAT_FRESH
            }
            room = (available - reserve) // WORKER_MEMORY
            limit = min(maximum, len(workers), (memory_total() - reserve) // WORKER_MEMORY)
            self.target = 0
            while self.target < limit and (workers[self.target] in busy or room > 0):
                room -= workers[self.target] not in busy
                self.target += 1
            if self.target < maximum:
                reason = f"{self.target} of {maximum} workers fit in free memory at {each}"
            elif maximum:
                reason = f"all configured workers fit in free memory at {each}"
            else:
                reason = pause_reason or "configured worker ceiling is zero"
        self.decision = dict(
            state="at_ceiling" if self.target else "paused",
            reason=reason,
            policy="memory-count",
            cpu_idle_required=0,
            next_increase_at=None,
            next_worker_limit=None,
        )
        return self.target, reason


def platform_check(add):
    add("platform", "ok", f"{platform.system()} {platform.machine()}; workers only, no CI runners")


def service_checks(add, probe):
    reachable = run(["launchctl", "print", domain()], check=False).returncode == 0
    add(
        "user-services",
        "ok" if reachable else "blocked",
        "launchd user domain is reachable" if reachable else "this account has no graphical login session",
        "Log in to this Mac's desktop as this account; its LaunchAgents run only in that session",
    )
    automatic = run(
        ["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"], check=False
    ).stdout.strip() == getpass.getuser()
    add(
        "persistence",
        "ok" if automatic else "warning",
        "automatic login is on for this account" if automatic else "logout/reboot persistence is not enabled",
        "Services stop at logout and start again only when this account logs in; turn on automatic login for it",
    )
    add(
        "resource-controls",
        "warning",
        f"macOS has no CPU or memory limits to apply; workers are admitted by count at {WORKER_MEMORY // GIB} GiB of free memory each",
        "Keep max_workers within what this Mac holds beside its other work",
    )
