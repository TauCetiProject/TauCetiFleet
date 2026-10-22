"""Disposable QEMU guests with no host mounts, bridges, Docker, or host sudo."""

import hashlib
import json
import os
import secrets
import shlex
import shutil
import time
import urllib.request
from pathlib import Path

from . import github
from .process import executable, run
from .store import FleetError, atomic, data_dir, event, host_config, locked, read, state_dir

NIXPKGS = "github:NixOS/nixpkgs/b6018f87da91d19d0ab4cf979885689b469cdd41"
UBUNTU_URL = "https://cloud-images.ubuntu.com/noble/20260926/noble-server-cloudimg-amd64.img"
UBUNTU_SHA256 = "6a81c37564db9b1ee84e141922625e1d7c5b389b99bb3c572e0243607d5bb4d2"
IMAGE_REVISION = 3
GH_VERSION = "2.102.0"
GH_SHA256 = "bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386"


def download(url, target, sha256):
    if len(sha256) != 64:
        raise FleetError("Download has no pinned SHA-256; refusing to fetch an executable/image")
    target = Path(target)
    if target.exists() and digest(target) == sha256:
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_suffix(target.suffix + ".download")
    try:
        with urllib.request.urlopen(url, timeout=30) as source, temporary.open("wb") as dest:
            shutil.copyfileobj(source, dest)
        if digest(temporary) != sha256:
            raise FleetError(
                "Download checksum mismatch; retain the installed runtime and update the pinned release manifest"
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_tools():
    if not executable("uv"):
        raise FleetError("uv is missing: install uv in your account, then retry prepare")
    if not all(executable(n) for n in ("qemu-system-x86_64", "qemu-img", "cloud-localds")):
        if not executable("nix"):
            raise FleetError(
                "CI tools missing. Ask for qemu and cloud-image-utils, or use an existing user-accessible Nix installation"
            )
        for package, binaries in (("qemu", ("qemu-system-x86_64", "qemu-img")), ("cloud-utils", ("cloud-localds",))):
            roots = data_dir() / "runtime-roots"
            roots.mkdir(parents=True, exist_ok=True)
            result = run(
                [
                    "nix",
                    "--extra-experimental-features",
                    "nix-command flakes",
                    "build",
                    "--out-link",
                    roots / package,
                    "--print-out-paths",
                    NIXPKGS + "#" + package,
                ],
                timeout=1800,
            )
            root = Path(result.stdout.strip().splitlines()[-1])
            for name in binaries:
                (data_dir() / "bin").mkdir(parents=True, exist_ok=True)
                link = data_dir() / "bin" / name
                link.unlink(missing_ok=True)
                link.symlink_to(root / "bin" / name)


def cloud_seed(directory, commands, *, name):
    # JSON is valid YAML. Paths/commands are never interpolated into YAML syntax.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    user = dict(
        users=[dict(name="runner", shell="/bin/bash", lock_passwd=True, sudo=["ALL=(ALL) NOPASSWD:ALL"])],
        ssh_pwauth=False,
        disable_root=True,
        # cloud-init continues between separate runcmd entries on failure.
        # One fail-fast script makes every prerequisite gate the final marker.
        runcmd=[["bash", "-euc", "\n".join(commands)]],
    )
    (directory / "user-data").write_text("#cloud-config\n" + json.dumps(user))
    os.chmod(directory / "user-data", 0o600)
    (directory / "meta-data").write_text(json.dumps({"instance-id": name, "local-hostname": "tauceti-ci"}))
    run(
        [executable("cloud-localds"), directory / "seed.iso", directory / "user-data", directory / "meta-data"],
        timeout=30,
    )
    os.chmod(directory / "seed.iso", 0o600)
    return directory / "seed.iso"


def qemu_args(config, directory, disk, seed, *, small=False):
    return [
        executable("qemu-system-x86_64"),
        "-enable-kvm",
        "-cpu",
        "host",
        "-smp",
        "2" if small else str(config["ci_vcpus"]),
        "-m",
        "2048" if small else str(config["ci_memory_gib"] * 1024),
        "-display",
        "none",
        "-no-reboot",
        "-monitor",
        "none",
        "-serial",
        "file:" + str(directory / "console.log"),
        "-drive",
        f"file={disk},format=qcow2,if=virtio",
        "-drive",
        f"file={seed},format=raw,media=cdrom,readonly=on",
        "-netdev",
        "user,id=net0",
        "-device",
        "virtio-net-pci,netdev=net0",
        "-chardev",
        f"socket,id=events,path={directory / 'events.sock'},server=on,wait=off",
        "-device",
        "virtio-serial",
        "-device",
        "virtserialport,chardev=events,name=org.tauceti.events",
    ]


def scope_args(name, argv, *, small=False):
    return [
        "systemd-run",
        "--user",
        "--quiet",
        "--scope",
        "--unit=" + name,
        "--slice=tauceti-fleet-ci.slice",
        "-p",
        "MemoryMax=" + ("3G" if small else "infinity"),
        *argv,
    ]


def prepare_image(config=None):
    config = config or host_config()
    prepare_tools()
    root = data_dir() / "images"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(root / "prepare.lock"):
        if (root / "prepared.json").exists():
            manifest = read(root / "prepared.json")
            if digest(root / "prepared.qcow2") != manifest["sha256"]:
                raise FleetError("Prepared VM image was modified; remove it and run prepare to rebuild")
            if manifest.get("image_revision") == IMAGE_REVISION and time.time() - manifest["prepared_at"] < 14 * 86400:
                return manifest
        download(UBUNTU_URL, root / "ubuntu.qcow2", UBUNTU_SHA256)
        release = github.request("repos/actions/runner/releases/latest")
        asset = next(
            a
            for a in release["assets"]
            if a["name"].startswith("actions-runner-linux-x64-") and a["name"].endswith(".tar.gz")
        )
        checksum = (asset.get("digest") or "").removeprefix("sha256:")
        if len(checksum) != 64:
            raise FleetError("GitHub runner release has no SHA-256 digest; cannot prepare a trusted image")
        staging = root / "prepare"
        staging.mkdir(exist_ok=True, mode=0o700)
        disk = staging / "disk.qcow2"
        disk.unlink(missing_ok=True)
        run([executable("qemu-img"), "create", "-f", "qcow2", "-F", "qcow2", "-b", root / "ubuntu.qcow2", disk, "20G"])
        commands = [
            "export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y ca-certificates curl git sudo python3 jq libicu74 libssl3t64 zlib1g libkrb5-3 liblttng-ust1t64",
            "curl --fail --location https://github.com/cli/cli/releases/download/v"
            + GH_VERSION
            + "/gh_"
            + GH_VERSION
            + "_linux_amd64.tar.gz -o /tmp/gh.tar.gz; echo "
            + shlex.quote(GH_SHA256 + "  /tmp/gh.tar.gz")
            + " | sha256sum -c -; tar xzf /tmp/gh.tar.gz -C /tmp; install /tmp/gh_"
            + GH_VERSION
            + "_linux_amd64/bin/gh /usr/local/bin/gh; rm -rf /tmp/gh.tar.gz /tmp/gh_"
            + GH_VERSION
            + "_linux_amd64; gh --version",
            "mkdir -p /opt/actions-runner; cd /opt/actions-runner; curl --fail --location "
            + shlex.quote(asset["browser_download_url"])
            + " -o runner.tar.gz; echo "
            + shlex.quote(checksum + "  runner.tar.gz")
            + " | sha256sum -c -; tar xzf runner.tar.gz; rm runner.tar.gz; chown -R runner:runner .",
            "cloud-init clean --logs --seed; rm -f /etc/ssh/ssh_host_*; truncate -s 0 /etc/machine-id; echo FLEET_IMAGE_READY; sync; poweroff",
        ]
        seed = cloud_seed(staging, commands, name="fleet-prepare-" + secrets.token_hex(6))
        unit = "tauceti-fleet-image-" + secrets.token_hex(4)
        try:
            result = run(
                scope_args(unit, qemu_args(config, staging, disk, seed, small=True), small=True),
                timeout=1200,
                check=False,
            )
        finally:
            run(["systemctl", "--user", "stop", unit + ".scope"], check=False)
        if result.returncode:
            raise FleetError(f"Image preparation failed; inspect {staging / 'console.log'}")
        console = (staging / "console.log").read_text(errors="replace")
        if "FLEET_IMAGE_READY" not in console:
            raise FleetError(f"Guest did not shut down cleanly; inspect {staging / 'console.log'}")
        # Flatten: future guests do not depend on a mutable backing chain.
        replacement = root / "prepared.next.qcow2"
        replacement.unlink(missing_ok=True)
        run([executable("qemu-img"), "convert", "-O", "qcow2", disk, replacement], timeout=120)
        os.chmod(replacement, 0o400)
        os.replace(replacement, root / "prepared.qcow2")
        manifest = dict(
            version=1,
            image_revision=IMAGE_REVISION,
            sha256=digest(root / "prepared.qcow2"),
            ubuntu_sha256=UBUNTU_SHA256,
            runner_version=release["tag_name"],
            runner_sha256=checksum,
            gh_version=GH_VERSION,
            gh_sha256=GH_SHA256,
            prepared_at=time.time(),
        )
        atomic(root / "prepared.json", manifest)
        shutil.rmtree(staging)
        return manifest


def start(config):
    with locked(slot_file(config, "vm.lock")):
        return _start(config)


def slot_file(config, name):
    slot = config.get("ci_slot", 0)
    return (state_dir() if slot == 0 else state_dir() / "ci-slots" / str(slot)) / name


def slots_in_use():
    """Include old slot zero and every draining slot, regardless of configured limit."""
    return sorted({0} | {int(p.parent.name) for name in ("vm.json", "ci-registration.json")
                         for p in (state_dir() / "ci-slots").glob("*/" + name) if p.parent.name.isdecimal()})


def outstanding():
    return any(slot_file({"ci_slot": slot}, name).exists() for slot in slots_in_use()
               for name in ("vm.json", "ci-registration.json"))


def _start(config):
    root = data_dir() / "images"
    if not (root / "prepared.json").exists():
        raise FleetError("Prepared CI image missing; run prepare before enabling CI")
    # Host-local lock is the capacity authority, including across daemon restarts.
    prior = read(slot_file(config, "vm.json"), {})
    if prior.get("unit"):
        result = run(["systemctl", "--user", "is-active", prior["unit"]], check=False)
        if result.returncode == 0:
            return prior
        _retire(config, prior)
    registration = slot_file(config, "ci-registration.json")
    intent = read(registration, {}) if config.get("ci_broker_url") else {}
    instance = intent.get("instance") or secrets.token_hex(8)
    if config.get("ci_broker_url"):
        # Keep the request identity across lost replies and controller restarts.
        # The broker can replay the existing JIT config without registering twice.
        atomic(registration, dict(instance=instance))
    name = "fleet-" + config["machine_id"][:12] + "-" + instance
    runner_id, jit = github.jit_config(config, name)
    directory = state_dir() / "vms" / instance
    disk = directory / "disk.qcow2"
    unit = "tauceti-fleet-vm-" + instance
    try:
        directory.mkdir(parents=True, mode=0o700)
        run(
            [
                executable("qemu-img"),
                "create",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                root / "prepared.qcow2",
                disk,
                str(config["ci_disk_gib"]) + "G",
            ]
        )
        # Actions runs .sh hooks with bash, regardless of their shebang.
        hook = "#!/bin/sh\nexec /usr/bin/python3 - <<'PY'\nimport json,os,time\nwith open('/dev/virtio-ports/org.tauceti.events','w') as f: print(json.dumps(dict(kind=KIND,run_id=os.environ.get('GITHUB_RUN_ID'),at=time.time())),file=f)\nPY\n"
        commands = [
            "printf %s "
            + shlex.quote(jit)
            + " > /opt/actions-runner/.jit; chmod 600 /opt/actions-runner/.jit; chown runner:runner /opt/actions-runner/.jit",
            "printf %s "
            + shlex.quote(hook.replace("KIND", "'ci_started'"))
            + " > /opt/hook-start.sh; chmod 755 /opt/hook-start.sh",
            "printf %s "
            + shlex.quote(hook.replace("KIND", "'ci_completed'"))
            + " > /opt/hook-complete.sh; chmod 755 /opt/hook-complete.sh",
            "chmod 666 /dev/virtio-ports/org.tauceti.events; printf '%s\\n' ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/hook-start.sh ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/hook-complete.sh > /opt/actions-runner/.env; chown runner:runner /opt/actions-runner/.env",
            "cd /opt/actions-runner; set +e; su -s /bin/bash runner -c './run.sh --jitconfig \"$(cat .jit)\"'; poweroff",
        ]
        seed = cloud_seed(directory, commands, name=name)
        value = dict(
            slot=config.get("ci_slot", 0),
            instance=instance,
            unit=unit + ".service",
            runner_id=runner_id,
            runner_name=name,
            directory=str(directory),
            started_at=time.time(),
            busy=False,
            phase="starting",
        )
        # Record the prospective unit before launch: a controller/process crash
        # can then recover either the existing guest or the unused registration.
        atomic(slot_file(config, "vm.json"), value)
        # A transient user service survives controller restart; its slice bounds
        # all QEMU threads. No guest-writable path is shared with host secrets.
        run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--unit=" + unit,
                "--slice=tauceti-fleet-ci.slice",
                "-p",
                "KillMode=control-group",
                "-p",
                "RemainAfterExit=yes",
                *qemu_args(config, directory, disk, seed),
            ],
            timeout=30,
        )
        value["phase"] = "ready"
        atomic(slot_file(config, "vm.json"), value)
        event("ci_vm_started", runner_id=runner_id)
        registration.unlink(missing_ok=True)
        return value
    except Exception:
        run(["systemctl", "--user", "stop", unit + ".service"], check=False)
        try:
            github.retire_runner(config, runner_id)
            registration.unlink(missing_ok=True)
        except FleetError:
            pass
        shutil.rmtree(directory, ignore_errors=True)
        if read(slot_file(config, "vm.json"), {}).get("instance") == instance:
            slot_file(config, "vm.json").unlink(missing_ok=True)
        raise


def retire(config, value):
    config = dict(config, ci_slot=value.get("slot", 0))
    with locked(slot_file(config, "vm.lock")):
        return _retire(config, value)


def _retire(config, value):
    run(["systemctl", "--user", "stop", value["unit"]], check=False)
    run(["systemctl", "--user", "reset-failed", value["unit"]], check=False)
    try:
        github.retire_runner(config, value["runner_id"])
    except FleetError:
        if config.get("ci_broker_url"):
            # Preserve local ownership for an idempotent retirement retry. The
            # broker handles GitHub's already-deregistered (404) case itself.
            raise
    registration = slot_file(config, "ci-registration.json")
    if read(registration, {}).get("instance") == value.get("instance"):
        registration.unlink(missing_ok=True)
    directory = Path(value["directory"])
    if directory.parent == state_dir() / "vms":
        shutil.rmtree(directory, ignore_errors=True)
    if read(slot_file(config, "vm.json"), {}).get("instance") == value.get("instance"):
        slot_file(config, "vm.json").unlink(missing_ok=True)
    retired_at = time.time()
    atomic(slot_file(config, "ci-last-retired.json"), dict(at=retired_at, runner_id=value["runner_id"]))
    event("ci_vm_retired", at=retired_at, runner_id=value["runner_id"])


def smoke(config=None):
    """Two small guests prove execution/sandboxing and fresh-disk replacement.

    No GitHub runner registration, publication credentials, or model calls.
    """
    config = config or host_config()
    manifest = read(data_dir() / "images/prepared.json")
    if not manifest:
        raise FleetError("Run prepare before the guest smoke test")
    base = data_dir() / "images/prepared.qcow2"
    if digest(base) != manifest["sha256"]:
        raise FleetError("Prepared base image failed its integrity check")
    root = state_dir() / "smoke"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    canary = "fleet-smoke-canary"
    for number in (1, 2):
        directory = root / str(number)
        directory.mkdir(exist_ok=True, mode=0o700)
        disk = directory / "disk.qcow2"
        disk.unlink(missing_ok=True)
        run([executable("qemu-img"), "create", "-f", "qcow2", "-F", "qcow2", "-b", base, disk, "20G"])
        commands = ["test ! -e /" + canary]
        if number == 1:
            probe = """import os,pathlib,socket
assert pathlib.Path('/proc/self/ns/net').stat().st_ino != int(os.environ['PARENT_NET'])
try:
    pathlib.Path('/etc/fleet-write-probe').write_text('no')
except OSError:
    pass
else:
    raise SystemExit('sandbox allowed a write to /etc')
try:
    socket.create_connection(('1.1.1.1',443),timeout=1)
except OSError:
    pass
else:
    raise SystemExit('sandbox allowed network access')
print('FLEET_SANDBOX_OK')
"""
            commands += [
                "su -s /bin/bash runner -c 'set -e; gh --version; git --version; jq --version; sudo -n dpkg --version >/dev/null; sudo -n apparmor_parser --version'",
                "export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y bubblewrap",
                "if [ \"$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)\" = 1 ]; then printf '%s\\n' 'abi <abi/4.0>,' 'include <tunables/global>' 'profile bwrap /usr/bin/bwrap flags=(unconfined) {' '  userns,' '  include if exists <local/bwrap>' '}' > /etc/apparmor.d/bwrap; apparmor_parser -r /etc/apparmor.d/bwrap; fi",
                "PARENT_NET=$(stat -Lc %i /proc/self/ns/net); export PARENT_NET; runuser -u runner -- env -i PARENT_NET=$PARENT_NET /usr/bin/bwrap --unshare-all --unshare-user --tmpfs / --ro-bind /usr /usr --ro-bind /etc /etc --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 --proc /proc --dev /dev /usr/bin/python3 -c "
                + shlex.quote(probe),
                "cd /opt/actions-runner; su -s /bin/bash runner -c './bin/Runner.Listener --version'",
                "touch /" + canary,
            ]
        commands += [f"echo FLEET_SMOKE_{number}_OK; sync; poweroff"]
        seed = cloud_seed(directory, commands, name="fleet-smoke-" + secrets.token_hex(6))
        unit = "tauceti-fleet-smoke-" + secrets.token_hex(4)
        try:
            result = run(
                scope_args(unit, qemu_args(config, directory, disk, seed, small=True), small=True),
                timeout=600,
                check=False,
            )
        finally:
            run(["systemctl", "--user", "stop", unit + ".scope"], check=False)
        console = (directory / "console.log").read_text(errors="replace")
        if (
            result.returncode
            or f"FLEET_SMOKE_{number}_OK" not in console
            or number == 1
            and "FLEET_SANDBOX_OK" not in console
        ):
            raise FleetError(f"Guest smoke test {number} failed; inspect {directory / 'console.log'}")
        disk.unlink()
        (directory / "seed.iso").unlink()
    if digest(base) != manifest["sha256"]:
        raise FleetError("Guest testing modified the trusted base image")
    result = dict(
        version=1,
        observed_at=time.time(),
        image_sha256=manifest["sha256"],
        guest_execution=True,
        sandbox_enforced=True,
        clean_replacement=True,
        build_tools=True,
        production_registered=False,
    )
    atomic(state_dir() / "smoke.json", result)
    return result
