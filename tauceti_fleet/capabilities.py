"""Measured, role-specific readiness without making administrator changes."""

import fcntl
import json
import os
import platform
import shutil
import sys
from pathlib import Path

from .process import executable, run
from .store import FleetError, data_dir, host_config, read, state_dir

GIB = 1024**3


def memory_accounting(current, high, maximum, stats, pressure, events, swap=0):
    """Conservative working-set estimate; cold dirty cache is not immediately free."""
    cold_clean = max(
        0,
        min(stats["inactive_file"], stats["file"] - stats.get("shmem", 0))
        - stats["file_dirty"]
        - stats["file_writeback"],
    )
    return dict(
        charged=current,
        high=high,
        maximum=maximum,
        anon=stats["anon"],
        file=stats["file"],
        kernel=stats["kernel"],
        cold_clean=cold_clean,
        working_set=max(0, current - cold_clean) + swap,
        swap=swap,
        dirty=stats["file_dirty"],
        writeback=stats["file_writeback"],
        stall_some=pressure["some"],
        stall_full=pressure["full"],
        oom_kill=events["oom_kill"],
    )


def worker_memory():
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
            "MemoryMax",
            "-p",
            "MemorySwapMax",
            "-p",
            "ControlGroup",
        ]
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if not fields.get("ControlGroup"):
        raise FleetError("Worker memory cgroup is unavailable")
    cg = Path("/sys/fs/cgroup") / fields["ControlGroup"].lstrip("/")

    def counters(name):
        return {k: int(v) for k, v in (line.split() for line in (cg / name).read_text().splitlines())}

    pressure = {}
    for line in (cg / "memory.pressure").read_text().splitlines():
        name, *values = line.split()
        pressure[name] = float(dict(x.split("=", 1) for x in values)["avg60"])
    return memory_accounting(
        int(fields["MemoryCurrent"]),
        int(fields["MemoryHigh"]),
        int(fields["MemoryMax"]),
        counters("memory.stat"),
        pressure,
        counters("memory.events"),
        int((cg / "memory.swap.current").read_text()),
    )


def memory_pressure(memory, caps, *, recovering=False):
    reasons = []
    if caps["memory_available"] < max(4 * GIB, int(caps["memory_total"] * 0.1)):
        reasons.append("host memory headroom low")
    if memory["working_set"] >= memory["maximum"] * (0.85 if recovering else 0.90):
        reasons.append("worker working set near hard memory limit")
    # Reclaim PSI includes throttling against MemoryHigh and is not evidence of
    # host exhaustion. Let the kernel reclaim cache without pausing API work.
    return reasons


def resources():
    memory = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        cg = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split(":", 2)[2].lstrip("/")
        total = memory["MemTotal"]
        available = memory["MemAvailable"]
        cpus = float(len(os.sched_getaffinity(0)))
        for parent in [cg, *cg.parents]:
            if parent == Path("/sys/fs"):
                break
            maximum = parent / "memory.max"
            if maximum.exists() and maximum.read_text().strip() != "max":
                total = min(total, int(maximum.read_text()))
                available = min(
                    available, max(0, int(maximum.read_text()) - int((parent / "memory.current").read_text()))
                )
            quota = parent / "cpu.max"
            if quota.exists():
                limit, period = quota.read_text().split()
                if limit != "max":
                    cpus = min(cpus, int(limit) / int(period))
        return dict(
            cpus=cpus,
            memory_total=total,
            memory_available=min(total, available),
            cgroup=str(cg),
            disk_available=shutil.disk_usage(Path.home()).free,
        )
    except (OSError, ValueError, KeyError, AttributeError, IndexError):
        return dict(cpus=0, memory_total=0, memory_available=0, cgroup=None)


def resource_probe():
    code = """import json,pathlib
p=pathlib.Path('/sys/fs/cgroup')/pathlib.Path('/proc/self/cgroup').read_text().strip().split(':',2)[2].lstrip('/')
print(json.dumps({n:(p/n).read_text().strip() for n in ('cpu.weight','memory.high','memory.max','memory.swap.max','pids.max')}))
"""
    result = run(
        [
            "systemd-run",
            "--user",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "-p",
            "CPUWeight=1000",
            "-p",
            "MemoryHigh=64M",
            "-p",
            "MemoryMax=128M",
            "-p",
            "MemorySwapMax=16M",
            "-p",
            "TasksMax=32",
            sys.executable,
            "-c",
            code,
        ],
        check=False,
        timeout=15,
    )
    if result.returncode:
        raise FleetError("User service could not apply CPU/memory/process limits")
    values = json.loads(result.stdout)
    expected = {
        "cpu.weight": "1000",
        "memory.high": str(64 * 1024**2),
        "memory.max": str(128 * 1024**2),
        "memory.swap.max": str(16 * 1024**2),
        "pids.max": "32",
    }
    if values != expected:
        raise FleetError("User service limits were accepted but not enforced by the kernel")
    return values


def doctor(config=None, *, probe=True):
    config = config or host_config()
    checks = []

    def add(name, status, evidence, remedy="", admin="", roles=("workers", "ci")):
        checks.append(
            dict(
                name=name,
                status=status,
                evidence=evidence,
                remedy=remedy,
                administrator_request=admin,
                roles=list(roles),
            )
        )

    linux = platform.system() == "Linux" and platform.machine() in ("x86_64", "amd64")
    add(
        "platform",
        "ok" if linux else "blocked",
        f"{platform.system()} {platform.machine()}",
        "Version 1 requires Linux x86-64 with systemd and cgroup v2",
    )
    add(
        "python",
        "ok" if sys.version_info >= (3, 11) else "blocked",
        platform.python_version(),
        "Install Python 3.11 or newer",
    )
    try:
        result = run(["systemctl", "--user", "show", "-p", "ControlGroup", "--value"], check=False)
        if result.returncode or not result.stdout.strip():
            raise FleetError("user manager unavailable")
        add("user-services", "ok", "systemd user manager is reachable")
    except FleetError:
        add(
            "user-services",
            "blocked",
            "systemd user manager is unavailable",
            "Connect through a normal login session and retry",
            "Enable systemd user services for this account",
        )
    try:
        result = run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"], check=False)
        linger = result.returncode == 0 and result.stdout.strip() == "yes"
    except FleetError:
        linger = False
    add(
        "persistence",
        "ok" if linger else "warning",
        "lingering enabled" if linger else "logout/reboot persistence is not enabled",
        "Services may stop after logout and will not automatically start at boot",
        f"Run: loginctl enable-linger {__import__('getpass').getuser()}",
    )
    if probe:
        try:
            measured = resource_probe()
            add("resource-controls", "ok", f"Verified kernel values: {json.dumps(measured, sort_keys=True)}")
        except (FleetError, OSError, ValueError, KeyError):
            add(
                "resource-controls",
                "blocked",
                "CPU/memory limits could not be verified",
                "Retry doctor in a systemd user session",
                "Delegate cpu, memory and pids controllers to this user's systemd manager",
            )
    caps = resources()
    add(
        "capacity",
        "ok" if caps["cpus"] and caps["memory_total"] else "blocked",
        f"Effective allocation: {caps['cpus']:g} CPUs, {caps['memory_total'] / GIB:.1f} GiB RAM",
        "Effective ancestor cgroup limits must be readable",
    )
    from .capacity import jobs, kinds

    count = jobs(config)
    ci_memory = count * config["ci_memory_gib"] * GIB
    reserve = max(4 * GIB, int(caps["memory_total"] * 0.1))
    fits = caps["cpus"] >= count * config["ci_vcpus"] and caps["memory_total"] >= ci_memory + count * 2 * GIB + reserve
    add(
        "ci-shape",
        "ok" if fits else "blocked",
        f"CI requests {count} shared jobs, each {config['ci_vcpus']} vCPUs and {config['ci_memory_gib']} GiB, plus 2 GiB per guest and {reserve / GIB:.1f} GiB OS headroom",
        "Configure a smaller CI shape, or redeploy with --no-ci",
        roles=("ci",),
    )
    try:
        used = run(
            ["systemctl", "--user", "show", "tauceti-fleet-ci.slice", "-p", "MemoryCurrent", "--value"], check=False
        )
        committed = int(used.stdout.strip())
        if committed > caps["memory_total"]:
            committed = 0
    except (FleetError, ValueError):
        committed = 0
    needed = max(0, ci_memory + count * 2 * GIB - committed) + reserve
    add(
        "available-memory-headroom",
        "ok" if caps["memory_available"] >= needed else "blocked",
        f"{caps['memory_available'] / GIB:.1f} GiB available; {needed / GIB:.1f} GiB needed for CI and OS headroom",
        "Wait for other workloads to release memory, configure a smaller CI shape, or use --no-ci",
        roles=("ci",),
    )
    try:
        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        try:
            version = fcntl.ioctl(fd, 0xAE00, 0)
            vm = fcntl.ioctl(fd, 0xAE01, 0)
            os.close(vm)
        finally:
            os.close(fd)
        add("kvm", "ok", f"KVM API {version}; an empty VM was created and closed", roles=("ci",))
    except OSError:
        add(
            "kvm",
            "blocked",
            "Cannot create a KVM virtual machine",
            "Workers-only operation is available with --no-ci",
            "Enable hardware virtualization/KVM and grant this account access to /dev/kvm",
            roles=("ci",),
        )
    for name in ("qemu-system-x86_64", "qemu-img", "cloud-localds"):
        path = executable(name)
        add(
            name,
            "ok" if path else "blocked",
            path or "not installed",
            "Run tauceti-fleet prepare on this host to fetch the pinned Nix tools; otherwise ask for qemu and cloud-image-utils",
            roles=("ci",),
        )
    free = shutil.disk_usage(Path.home()).free
    add(
        "storage",
        "ok" if free >= count * config["ci_disk_gib"] * GIB else "blocked",
        f"{free / GIB:.1f} GiB free; CI requires {count * config['ci_disk_gib']} GiB for {count} jobs",
        "Free space, lower ci_disk_gib, or choose a larger writable data directory",
        roles=("ci",),
    )
    worker = executable("tauceti")
    for name in ("gh", "git", "uvx", "jq", "lake", "claude"):
        path = executable(name)
        add(
            "worker-tool-" + name,
            "ok" if path else "blocked",
            path or "not installed",
            "Install this tool in the account's PATH before enabling workers; lake is supplied by elan and claude by Claude Code",
            roles=("workers",),
        )
    compatible = False
    if worker:
        try:
            result = run([worker, "_fleet-protocol"], check=False)
            compatible = result.returncode == 0 and json.loads(result.stdout).get("version") == 1
        except (FleetError, ValueError, AttributeError):
            pass
    add(
        "worker-runtime",
        "ok" if compatible else "blocked",
        worker if compatible else "tauceti missing or its fleet admission protocol is unsupported",
        "Run prepare to install the pinned TauCetiWorker package",
        roles=("workers",),
    )
    image = data_dir() / "images/prepared.json"
    add(
        "ci-image",
        "ok" if image.is_file() else "blocked",
        "prepared image manifest present" if image.is_file() else "prepared disposable CI image missing",
        "Run prepare; image preparation boots a small guest without registering production CI",
        roles=("ci",),
    )
    manifest = read(image, {})
    from .vm import IMAGE_REVISION

    add(
        "ci-image-tools",
        "ok" if manifest.get("image_revision") == IMAGE_REVISION else "blocked",
        "Current CI tool image"
        if manifest.get("image_revision") == IMAGE_REVISION
        else "Prepared image needs updated CI dependencies",
        "Drain and run prepare to rebuild the image",
        roles=("ci",),
    )
    smoke = read(state_dir() / "smoke.json", {})
    tested = bool(
        manifest.get("sha256")
        and smoke.get("image_sha256") == manifest["sha256"]
        and all(
            smoke.get(k) is True for k in ("guest_execution", "sandbox_enforced", "clean_replacement", "build_tools")
        )
    )
    add(
        "ci-isolation-smoke",
        "ok" if tested else "blocked",
        "Prepared image passed guest isolation and replacement checks"
        if tested
        else "No successful smoke test for this image",
        "Run prepare or doctor --smoke before enabling CI",
        roles=("ci",),
    )
    if config.get("ci_allowed"):
        try:
            from .github import runner_policy

            runner_policy(config)
            add(
                "github-runner-policy",
                "ok",
                "Runner-group read access and trusted-workflow restriction verified; write access checked at activation",
                roles=("ci",),
            )
        except (FleetError, OSError, ValueError) as exc:
            add(
                "github-runner-policy",
                "blocked",
                str(exc),
                "Enroll this host with tauceti-fleet enroll local, or configure runner_group_id and a CI registration credential",
                roles=("ci",),
            )
    credential_names = ["anthropic_api_key_file", "github_token_file"]
    if config.get("ci_broker_url"):
        credential_names.append("ci_broker_token_file")
    for name in credential_names:
        value = config.get(name)
        exists = bool(value and Path(value).is_file() and Path(value).stat().st_size)
        private = exists and Path(value).stat().st_mode & 0o077 == 0
        roles = ("workers",)
        if name == "ci_broker_token_file":
            roles = ("ci",)
        elif name == "github_token_file" and not config.get("ci_broker_url"):
            roles = ("workers", "ci")
        add(
            name,
            "ok" if private else "blocked",
            "private credential file present" if private else "credential missing or readable by other users",
            "Configure the selected profile and repeat add; use chmod 600 for credential files",
            roles=roles,
        )
    if config.get("github_token_file"):
        try:
            from .github import request

            identity = request("user", token_file=config["github_token_file"])["login"]
            matches = identity.lower() == config.get("github_account", "tauceti-ai-for-science").lower()
            add(
                "worker-github-identity",
                "ok" if matches else "blocked",
                "Selected credential belongs to " + identity,
                "Choose a matching github_account and credential in the profile, then repeat add",
                roles=("workers",),
            )
        except (FleetError, OSError, KeyError, TypeError):
            add(
                "worker-github-identity",
                "blocked",
                "Selected GitHub identity could not be verified",
                "Check network access and refresh the profile's GitHub credential",
                roles=("workers",),
            )
    readiness = {}
    for role in ("workers", "ci"):
        relevant = [c for c in checks if role in c["roles"]]
        readiness[role] = (
            "blocked"
            if any(c["status"] == "blocked" for c in relevant)
            else ("ready-with-limitations" if any(c["status"] == "warning" for c in relevant) else "ready")
        )
    if not config["ci_allowed"]:
        readiness["ci"] = "disabled-by-configuration"
    return dict(
        version=1,
        machine_id=config["machine_id"],
        readiness=readiness,
        checks=checks,
        resources=caps,
        ci_shape=dict({k: config[k] for k in ("ci_vcpus", "ci_memory_gib", "ci_disk_gib")},
                      ci_max_jobs=count, ci_kinds=kinds(config)),
        observed_at=__import__("time").time(),
    )
