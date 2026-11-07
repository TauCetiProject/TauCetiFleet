"""Optional trusted prefill images owned by Fleet's existing VM image lifecycle.

Complete, flattened versions have immutable filenames. Selecting one never does
network I/O or hashes a multi-gigabyte image on the guest admission path. Integrity
is established at publication and bound to the immutable file's stat identity.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import dependency_prefill, prefill_guest
from .process import executable, run
from .store import FleetError, atomic, data_dir, host_config, locked, read, state_dir, update_host

DIGEST = re.compile(r"[0-9a-f]{64}")
ELAN_VERSION = "v4.2.3"
ELAN_SHA256 = "df0b2b3a439961ffcbb3985214365ffe40f49bc871df04dff268c7d8e21ca8b2"


def root():
    return data_dir() / "images/ci-prefill"


def identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or info.st_nlink != 1:
        raise ValueError("image must be an immutable ordinary file")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def immutable_atomic(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".ci-image-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o400)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def identifier(value, pattern=DIGEST):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def producer_identity():
    """Bind guest code, builder commands and bootstrap pins to the producer."""
    value = hashlib.sha256()
    for path in (Path(__file__), Path(prefill_guest.__file__)):
        value.update(path.read_bytes())
    return value.hexdigest()


def validate_base(value):
    from . import vm

    if (
        not isinstance(value, dict)
        or not identifier(value.get("sha256"))
        or value.get("image_revision") != vm.IMAGE_REVISION
        or value.get("ubuntu_sha256") != vm.UBUNTU_SHA256
    ):
        raise ValueError("ordinary base has a different image revision or Ubuntu identity")


def validate_payload(payload):
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema")) is not int
        or payload["schema"] != 1
        or payload.get("producer") != "TauCetiFleet/ci-prefill-v1"
        or not identifier(payload.get("producer_revision"), prefill_guest.SHA)
        or payload.get("platform") != "Linux-x86_64"
        or payload.get("project_path") != str(prefill_guest.PROJECT)
        or type(payload.get("uid")) is not int
        or payload["uid"] < 0
        or not identifier(payload.get("toolchain"), prefill_guest.RELEASE)
        or not identifier(payload.get("payload_sha256"))
        or type(payload.get("payload_bytes")) is not int
        or not 0 < payload["payload_bytes"] <= 1024**3
        or not isinstance(payload.get("config_sha256"), dict)
        or set(payload["config_sha256"]) != set(prefill_guest.CONFIG)
        or any(not identifier(v) for v in payload["config_sha256"].values())
    ):
        raise ValueError("incomplete trusted payload identity")


def validate_proof(proof):
    if (
        not isinstance(proof, dict)
        or proof.get("native_executable_reused") is not True
        or type(proof.get("native_cache_get_exit_status")) is not int
        or proof["native_cache_get_exit_status"] != 0
        or not identifier(proof.get("native_executable_sha256"))
        or not isinstance(proof.get("consumer"), dict)
        or proof["consumer"].get("hit") is not True
    ):
        raise ValueError("missing native prefill validation")


def manifest(digest):
    from . import vm

    if not identifier(digest):
        raise ValueError("invalid image digest")
    path = root() / (digest + ".json")
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError("missing ordinary image manifest")
    value = read(path)
    identity(path)
    if not isinstance(value, dict):
        raise ValueError("image manifest is not an object")
    validate_payload(value.get("payload"))
    validate_proof(value.get("validation"))
    if (
        type(value.get("schema")) is not int
        or value["schema"] != 1
        or value.get("sha256") != digest
        or value.get("image_revision") != vm.IMAGE_REVISION
        or value.get("ubuntu_sha256") != vm.UBUNTU_SHA256
        or value.get("kind") != "dependency-prefill-v1"
        or not identifier(value.get("base_sha256"))
        or not identifier(value.get("producer_sha256"))
        or not identifier(value.get("consumer_sha256"))
        or type(value.get("virtual_size")) is not int
        or value["virtual_size"] <= 0
        or type(value.get("producer_seconds")) not in (int, float)
        or not math.isfinite(value["producer_seconds"])
        or value["producer_seconds"] < 0
        or value.get("file_identity") != identity(root() / (digest + ".qcow2"))
    ):
        raise ValueError("prepared image identity mismatch")
    return value


def current():
    pointer = root() / "current.json"
    if pointer.is_symlink() or not pointer.is_file() or pointer.stat().st_size > 4096:
        return None
    value = read(pointer)
    if not isinstance(value, dict):
        raise ValueError("image pointer is not an object")
    return manifest(value.get("sha256"))


def select(config):
    # Mixed/staging slots keep their existing image. The ordinary job's
    # integrated-config attestation remains the payload acceptance authority.
    if config.get("ci_prefill_enabled") is not True or config.get("ci_kinds") != ["pr"]:
        return None
    try:
        value = current()
        if value:
            base = read(data_dir() / "images/prepared.json")
            validate_base(base)
            disk = config.get("ci_disk_gib")
            if (
                value["base_sha256"] != base["sha256"]
                or value["producer_sha256"] != producer_identity()
                or type(disk) is not int
                or disk * 1024**3 < value["virtual_size"]
            ):
                return None
            return dict(value, path=str(root() / (value["sha256"] + ".qcow2")))
    except (FleetError, OSError, ValueError, TypeError, KeyError) as error:
        dependency_prefill.record("ci_prefill_image_ignored", reason=(str(error) or type(error).__name__)[:160])
    return None


def enable(enabled):
    if type(enabled) is not bool:
        raise FleetError("Prefill enablement must be explicit")
    value = update_host(ci_prefill_enabled=enabled)
    return dict(enabled=enabled, selected=select(dict(value, ci_kinds=["pr"])))


def publish(image, payload, *, base, seconds, validation, consumer, producer=None):
    """Called only with a fully validated guest produced by prepare()."""
    from . import vm

    info = image.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FleetError("Publication requires an ordinary private image")
    info = json.loads(run([executable("qemu-img"), "info", "--output=json", image]).stdout)
    if info.get("format") != "qcow2" or info.get("backing-filename"):
        raise FleetError("Prepared image must be flattened before publication")
    validate_base(base)
    validate_payload(payload)
    validate_proof(validation)
    if type(info.get("virtual-size")) is not int or info["virtual-size"] <= 0:
        raise ValueError("missing image virtual size")
    digest = vm.digest(image)
    image.chmod(0o400)
    with image.open("rb") as stream:
        os.fsync(stream.fileno())
    directory = root()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked(directory / "publish.lock"):
        target = directory / (digest + ".qcow2")
        if target.exists() or target.is_symlink():
            identity(target)
            if vm.digest(target) != digest:
                raise FleetError("Existing immutable image changed")
            identity(target)
        else:
            # No backing pathname is ever replaced underneath a live overlay.
            image.rename(target)
        value = dict(
            schema=1,
            kind="dependency-prefill-v1",
            sha256=digest,
            image_revision=base["image_revision"],
            ubuntu_sha256=base["ubuntu_sha256"],
            virtual_size=info["virtual-size"],
            producer_sha256=producer or producer_identity(),
            consumer_sha256=consumer,
            base_sha256=base["sha256"],
            payload=payload,
            validation=validation,
            file_identity=identity(target),
            prepared_at=time.time(),
            producer_seconds=seconds,
            image_bytes=target.stat().st_size,
            image_allocated_bytes=target.stat().st_blocks * 512,
        )
        metadata = directory / (digest + ".json")
        # Always validate the exact metadata that future selectors will read.
        # Identical backing bytes may have newer validation/provenance; metadata
        # is not a backing file and can be repaired atomically before publication.
        immutable_atomic(metadata, value)
        checked = manifest(digest)
        if checked != value:
            raise FleetError("Published image metadata did not round trip")
        sync_directory(directory)
        atomic(directory / "current.json", dict(sha256=digest))
        sync_directory(directory)
    return value


@contextlib.contextmanager
def try_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True


def reference_files():
    from . import vm

    paths = list((state_dir() / "vms").glob("*/disk.qcow2"))
    paths += [vm.slot_file({"ci_slot": slot}, "vm.json") for slot in vm.slots_in_use()]
    result = {}
    for path in paths:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("unknown image reference file")
        result[path] = (info.st_dev, info.st_ino)
    return result


def retire_unused():
    """Scan references outside admission's lock; defer on concurrent changes.

    Holding publish.lock freezes the current version. A pending start that chose
    the previous version must either still hold ci-start.lock, or have created a
    new reference file; either case defers deletion. New starts can choose only
    the kept current version. No subprocess or blocking lock runs under start.
    """
    directory = root()
    if not directory.exists():
        return []
    # Retirement can be reached from _start while this process already holds
    # admission. Avoid doing even the reference scan on that path.
    with try_lock(state_dir() / "ci-start.lock") as free:
        if not free:
            return []
    with try_lock(directory / "publish.lock") as acquired:
        if not acquired:
            return []
        try:
            active = current()
            if active is None:
                return []
            keep = {active["sha256"]}
            snapshot = reference_files()
            for path in snapshot:
                if path.name == "vm.json":
                    value = read(path)
                    if not isinstance(value, dict):
                        raise ValueError("unknown slot image reference")
                    if value.get("image_sha256"):
                        keep.add(value["image_sha256"])
                else:
                    value = json.loads(
                        run([executable("qemu-img"), "info", "--force-share", "--output=json", path], timeout=2).stdout
                    )
                    if not isinstance(value, dict):
                        raise ValueError("unknown disk backing reference")
                    backing = Path(value.get("full-backing-filename") or value.get("backing-filename") or "")
                    if not backing.is_absolute():
                        backing = path.parent / backing
                    if backing.parent.resolve() == directory.resolve():
                        keep.add(backing.stem)
            with try_lock(state_dir() / "ci-start.lock") as acquired:
                if not acquired or reference_files() != snapshot:
                    return []
                removed = []
                names = {
                    p.stem for pattern in ("*.json", "*.qcow2") for p in directory.glob(pattern) if identifier(p.stem)
                }
                for name in sorted(names - keep):
                    for suffix in (".qcow2", ".json", ".build.log"):
                        (directory / (name + suffix)).unlink(missing_ok=True)
                    removed.append(name)
                return removed
        except (FleetError, OSError, ValueError, TypeError, KeyError):
            # Incomplete reference evidence is never permission to delete.
            return []


def trusted_main():
    """One immutable public-main capture; no candidate ref or credential input."""
    value = (
        run(
            [
                executable("git"),
                "-c",
                "credential.helper=",
                "ls-remote",
                "--exit-code",
                prefill_guest.REPOSITORY,
                "refs/heads/main",
            ],
            env=prefill_guest.environment(),
            timeout=120,
        )
        .stdout.strip()
        .split()
    )
    if len(value) != 2 or not prefill_guest.SHA.fullmatch(value[0]) or value[1] != "refs/heads/main":
        raise FleetError("Could not capture immutable trusted main")
    hashes = {}
    for name in (*prefill_guest.CONFIG, "scripts/dependency_prefill.py"):
        url = "https://raw.githubusercontent.com/TauCetiProject/TauCeti/" + value[0] + "/" + name
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read(4 * 1024**2 + 1)
        if len(content) > 4 * 1024**2:
            raise FleetError("Trusted configuration exceeds supported size")
        hashes[name] = hashlib.sha256(content).hexdigest()
    consumer = hashes.pop("scripts/dependency_prefill.py")
    return value[0], hashes, consumer


@contextlib.contextmanager
def discard_build_disks(staging):
    """Keep diagnostics, but release private disk allocations on every exit."""
    try:
        yield
    finally:
        for name in ("disk.qcow2", "scratch.qcow2", "complete.qcow2", "seed.iso"):
            (staging / name).unlink(missing_ok=True)


def builder_resources(images):
    from . import vm

    available = vm.start_memory_available()
    if available is None:
        from .capabilities import resources

        available = resources()["memory_available"]
    # This gates only optional construction; ordinary admissions are unchanged.
    if available < 10 * 1024**3 + vm.START_MEMORY_BYTES:
        raise FleetError("Optional builder needs 10 GiB plus ordinary CI start headroom")
    if shutil.disk_usage(images).free < 64 * 1024**3:
        raise FleetError("Optional builder needs 64 GiB free for private scratch and ordinary work")


def prepare(config=None, *, trusted=None, cancelled=None, scope_started=None):
    """Build without registration, draining or modifying the ordinary base image."""
    from . import vm

    config = config or host_config()
    started = time.monotonic()
    producer = producer_identity()
    script = Path(prefill_guest.__file__).read_text()
    revision, hashes, consumer = trusted or trusted_main()
    images = data_dir() / "images"
    with try_lock(images / "prepare.lock") as acquired:
        if not acquired:
            raise FleetError("Image preparation already in progress; ordinary work remains enabled")
        base = read(images / "prepared.json")
        validate_base(base)
        staging = images / "prepare-prefill"
        # Recover only this owner's abandoned, unregistered build before even a
        # reuse/resource decision. A failed refresh must not pin scratch forever.
        previous = read(staging / "build.json", {})
        if (
            isinstance(previous, dict)
            and isinstance(previous.get("unit"), str)
            and re.fullmatch(r"tauceti-fleet-prefill-[a-f0-9]{16}\.scope", previous["unit"])
        ):
            run(["systemctl", "--user", "stop", previous["unit"]], check=False)
        with discard_build_disks(staging):
            pass
        try:
            prior = current()
        except (FleetError, OSError, ValueError, TypeError, KeyError):
            prior = None
        if (
            prior
            and prior["base_sha256"] == base["sha256"]
            and prior["payload"]["config_sha256"] == hashes
            and prior["producer_sha256"] == producer
            and prior["consumer_sha256"] == consumer
        ):
            return dict(prior, reused=True)
        if vm.digest(images / "prepared.qcow2") != base["sha256"]:
            raise FleetError("Prepare a verified ordinary base image before prefill construction")
        vm.prepare_tools()
        builder_resources(images)
        base_info = json.loads(run([executable("qemu-img"), "info", "--output=json", images / "prepared.qcow2"]).stdout)
        if not isinstance(base_info, dict) or type(base_info.get("virtual-size")) is not int:
            raise FleetError("Ordinary base has no supported disk size")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(mode=0o700)
        unit = "tauceti-fleet-prefill-" + secrets.token_hex(8)
        atomic(staging / "build.json", dict(unit=unit + ".scope", revision=revision))
        with discard_build_disks(staging):
            disk = staging / "disk.qcow2"
            run(
                [
                    executable("qemu-img"),
                    "create",
                    "-f",
                    "qcow2",
                    "-F",
                    "qcow2",
                    "-b",
                    images / "prepared.qcow2",
                    disk,
                    str(base_info["virtual-size"]),
                ]
            )
            scratch = staging / "scratch.qcow2"
            run([executable("qemu-img"), "create", "-f", "qcow2", scratch, "48G"])
            commands = [
                "trap 'sync; poweroff' EXIT",
                "export DEBIAN_FRONTEND=noninteractive; command -v zstd >/dev/null || { apt-get update; apt-get install -y zstd; }",
                "curl --fail --location https://github.com/leanprover/elan/releases/download/"
                + ELAN_VERSION
                + "/elan-x86_64-unknown-linux-gnu.tar.gz -o /tmp/elan.tar.gz; echo "
                + shlex.quote(ELAN_SHA256 + "  /tmp/elan.tar.gz")
                + " | sha256sum -c -; tar xzf /tmp/elan.tar.gz -C /tmp elan-init",
                "mkdir -p /mnt/fleet-prefill-build; mkfs.ext4 -q /dev/disk/by-id/virtio-fleet-prefill-build; "
                "mount /dev/disk/by-id/virtio-fleet-prefill-build /mnt/fleet-prefill-build; "
                "mkdir -p /mnt/fleet-prefill-build/toolchains /mnt/fleet-prefill-build/work "
                "/home/runner/.elan/toolchains /opt/actions-runner/_work; "
                "mount --bind /mnt/fleet-prefill-build/toolchains /home/runner/.elan/toolchains; "
                "mount --bind /mnt/fleet-prefill-build/work /opt/actions-runner/_work; "
                "chown -R runner:runner /home/runner/.elan /opt/actions-runner/_work",
                "su -s /bin/bash runner -c '/tmp/elan-init -y --default-toolchain none --no-modify-path'; "
                "rm /tmp/elan.tar.gz /tmp/elan-init",
                "printf %s " + shlex.quote(script) + " > /opt/prefill-guest.py; chmod 444 /opt/prefill-guest.py",
                "install -d -o runner -g runner -m 700 /opt/.tauceti-prefill-build /opt/actions-runner/_work",
                "su -s /bin/bash runner -c "
                + shlex.quote(
                    "python3 /opt/prefill-guest.py --revision "
                    + revision
                    + " --output /opt/.tauceti-prefill-build/payload"
                ),
                "mv /opt/.tauceti-prefill-build/payload /opt/tauceti-dependency-prefill; "
                "chown -R root:root /opt/tauceti-dependency-prefill; chmod 555 /opt/tauceti-dependency-prefill; "
                "rm -rf /opt/.tauceti-prefill-build /opt/prefill-guest.py",
                "umount /opt/actions-runner/_work; umount /home/runner/.elan/toolchains; "
                "umount /mnt/fleet-prefill-build; rmdir /mnt/fleet-prefill-build; "
                "rm -rf /home/runner/.elan /home/runner/.cache /home/runner/.config /opt/actions-runner/_work",
                "cloud-init clean --logs --seed; rm -f /etc/ssh/ssh_host_*; truncate -s 0 /etc/machine-id; "
                "fstrim /; echo FLEET_CI_IMAGE_READY; sync; poweroff",
            ]
            seed = vm.cloud_seed(staging, commands, name=unit)
            arguments = vm.qemu_args(
                config, staging, disk, seed, small=True, shape=dict(vcpus=4, memory_gib=8), discard=True, events=False
            )
            # Explicit boot identity matters with the scratch device attached.
            drive = arguments.index("-drive") + 1
            arguments[drive] = arguments[drive].replace("if=virtio", "if=none,id=fleet-root")
            arguments += [
                "-device",
                "virtio-blk-pci,drive=fleet-root,bootindex=1",
                "-drive",
                f"file={scratch},format=qcow2,if=none,id=scratch,discard=unmap",
                "-device",
                "virtio-blk-pci,drive=scratch,serial=fleet-prefill-build",
            ]
            # Kill the optional builder before useful CI on host memory exhaustion.
            adjustment = "import os,sys;p='/proc/self/oom_score_adj';v=int(open(p).read());open(p,'w').write(str(max(v,500)));os.execv(sys.argv[1],sys.argv[1:])"
            arguments = [
                "-p",
                "MemorySwapMax=0",
                "-p",
                "RuntimeMaxSec=3600",
                "-p",
                "CPUWeight=10",
                "--",
                sys.executable,
                "-c",
                adjustment,
                *arguments,
            ]
            scope = vm.scope_args(unit, arguments)
            scope[scope.index("MemoryMax=infinity")] = "MemoryMax=10G"
            if scope_started is not None:
                scope_started(unit + ".scope")
            if cancelled is not None and cancelled.is_set():
                raise FleetError("Optional construction cancelled before launch")
            try:
                result = run(scope, timeout=3600, check=False)
            finally:
                run(["systemctl", "--user", "stop", unit + ".scope"], check=False)
            scratch_allocated = scratch.stat().st_blocks * 512
            scratch.unlink()
            if cancelled is not None and cancelled.is_set():
                raise FleetError("Optional construction cancelled; previous image remains usable")
            if not (staging / "console.log").exists():
                # This scope has no registration or secret-bearing arguments.
                raise FleetError("Image builder did not start its guest: " + result.stderr[-600:])
            console = (staging / "console.log").read_text(errors="replace")
            matches = re.findall(r"FLEET_PREFILL_READY (\{[^\r\n]+\})", console)
            if result.returncode or "FLEET_CI_IMAGE_READY" not in console or len(matches) != 1:
                raise FleetError(
                    f"Optional image build failed; previous image remains usable; inspect {staging / 'console.log'}"
                )
            proof = json.loads(matches[0])
            proof["builder_scratch_allocated_bytes"] = scratch_allocated
            payload = proof["manifest"]
            if (
                payload["producer_revision"] != revision
                or payload["config_sha256"] != hashes
                or proof.get("native_executable_reused") is not True
            ):
                raise FleetError("Image did not prove the captured configuration and native executable reuse")
            replacement = staging / "complete.qcow2"
            run([executable("qemu-img"), "convert", "-O", "qcow2", disk, replacement], timeout=600)
            value = publish(
                replacement,
                payload,
                base=base,
                seconds=time.monotonic() - started,
                validation=proof,
                producer=producer,
                consumer=consumer,
            )
            shutil.copyfile(staging / "console.log", root() / (value["sha256"] + ".build.log"))
            shutil.rmtree(staging)
    retire_unused()
    return dict(value, reused=False)


class Owner:
    """Refresh enabled PR images without blocking the controller or hot retrying."""

    def __init__(self):
        self.thread = None
        self.next_refresh = 0.0
        self.failure_key = None
        self.failures = 0
        self.retry_after = 0.0
        self.cancelled = threading.Event()
        self.unit = None

    def step(self, config, *, now=None):
        now = time.monotonic() if now is None else now
        enabled = (
            config.get("ci_prefill_enabled") is True and config.get("ci_enabled") and config.get("ci_kinds") == ["pr"]
        )
        if not enabled:
            self.cancelled.set()
            if self.unit is not None and self.thread is not None and self.thread.is_alive():
                try:
                    run(["systemctl", "--user", "--no-block", "stop", self.unit], timeout=2, check=False)
                except (FleetError, OSError):
                    pass  # RuntimeMaxSec and the next controller tick bound recovery.
            return
        if now < self.next_refresh or self.thread is not None and self.thread.is_alive():
            return
        self.next_refresh = now + 900
        self.cancelled = threading.Event()

        def refresh():
            key = None
            try:
                trusted = trusted_main()
                base = read(data_dir() / "images/prepared.json", {})
                key = (json.dumps(base, sort_keys=True), producer_identity(), json.dumps(trusted[1:], sort_keys=True))
                if key == self.failure_key and time.monotonic() < self.retry_after:
                    return
                prepare(
                    dict(config),
                    trusted=trusted,
                    cancelled=self.cancelled,
                    scope_started=lambda unit: setattr(self, "unit", unit),
                )
                self.failure_key, self.failures, self.retry_after = None, 0, 0.0
            except Exception as error:
                self.failures = self.failures + 1 if key == self.failure_key else 1
                self.failure_key = key
                self.retry_after = time.monotonic() + min(21600, 900 * 2 ** min(self.failures - 1, 5))
                dependency_prefill.record(
                    "ci_prefill_refresh_deferred", reason=(str(error) or type(error).__name__)[:160]
                )
            finally:
                self.unit = None

        try:
            self.thread = threading.Thread(target=refresh, name="ci-prefill-images", daemon=True)
            self.thread.start()
        except (RuntimeError, OSError) as error:
            dependency_prefill.record("ci_prefill_refresh_deferred", reason=(str(error) or type(error).__name__)[:160])


def mount_commands():
    return [
        "if ! mount --bind /opt/tauceti-dependency-prefill /opt/tauceti-dependency-prefill "
        "|| ! mount -o remount,bind,ro,nosuid,nodev,noexec /opt/tauceti-dependency-prefill; then "
        "chmod 000 /opt/tauceti-dependency-prefill 2>/dev/null || true; "
        "echo 'optional prefill mount unavailable; using ordinary setup'; fi"
    ]
