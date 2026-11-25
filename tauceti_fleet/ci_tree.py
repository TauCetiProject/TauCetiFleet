"""A trusted, read-only shared build tree per host for native CI guests (TauCetiWorker decision 211).

Phase 1: the pool, its builder and the read-only bind into guests. No job uses it yet.

Layout under ``data_dir()/ci-tree/v1``:

* ``entries/<id>/``: one immutable entry. ``lake/`` is a complete ``.lake`` as CI's trusted steps
  leave it for one published ``main`` commit, ``ready.json`` its key and summary, and
  ``inventory.jsonl.gz`` the stat record of every file, taken after the final permission state.
* ``staging/<id>/``: a builder instance, and an entry being assembled before its atomic rename.
* ``trash/<id>/``: entries collection has retired, awaiting removal.
* ``leases/<id>/<instance>``: one file per guest using the entry.

Nothing in an entry comes from a job: the builder is a Fleet-owned native instance that runs only
trusted code (TauCeti ``main``'s Lake configuration, Mathlib's cache tool and Tools ``main``'s
scripts) and restores CI's published outputs with ``lake build --no-build``. The guest verifier
(``ci_tree_guest.py``) checks every output against its content hash and the published map; the
host then checks structure (types, owners, modes, links) and records the inventory. A guest's
lease is taken under the collection lock before its unit starts and released only after the unit
and all its processes have gone. Guests see one leased entry, bound read-only at
``/opt/tauceti-shared-tree``.
"""

import gzip
import json
import os
import re
import secrets
import stat
import sys
import time
from pathlib import Path

from .process import run
from .store import FleetError, atomic, data_dir, locked, read, state_dir

SETTING = "ci_shared_tree"
# Which PR slots are offered an entry: all, or only even- or odd-numbered ones, so the other half
# runs today's path on the same PRs and drives at the same time (decision 216).
SLOTS_SETTING = "ci_shared_tree_slots"
SLOT_CHOICES = ("all", "even", "odd")
GUEST_PATH = "/opt/tauceti-shared-tree"
WORKSPACE = "/opt/actions-runner/_work/TauCeti/TauCeti"
ENTRY = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}-[0-9a-f]{8}")
INSTANCE = re.compile(r"[a-f0-9]{16}")
# Collection keeps every leased entry plus the newest KEEP.
KEEP = 2
# Never build while this many entries exist (leased ones included): a disk bound.
MAX_ENTRIES = 4
# A build's peak is its entry (38 GB measured) plus its rootfs, toolchain and Mathlib download
# scratch: 60 GiB bounds it. After that, two guest starts must still pass their own 48 GiB
# start checks, with the 8 GiB host margin on top.
BUILD_PEAK_GIB = 60
BUILD_FREE_GIB = BUILD_PEAK_GIB + 2 * 48 + 8
CHECK_SECONDS = 30 * 60
# Published main maps come about every 1.2 hours (phase 0): do not rebuild an entry younger.
MIN_AGE_SECONDS = 45 * 60
FAILURE_BACKOFF_SECONDS = 2 * 3600
BUILD_TIMEOUT_SECONDS = 2 * 3600
# Bounds for a sane entry; a larger tree is a bug, not an entry.
MAX_ENTRY_BYTES = 120 * 1024**3
MAX_ENTRY_FILES = 4_000_000
RUNNER_UID = RUNNER_GID = 1000
# Run as `python -m`, where __name__ would be __main__.
MODULE = "tauceti_fleet.ci_tree"
ISOLATION = ("TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA", "TAUCETI_FLEET_CONFIG")

BUILD_SCRIPT = r"""#!/bin/bash
# Builds one shared-tree entry: the .lake pr-build.yml's trusted steps leave for a published
# main commit, at the same paths and with the same environment, restored and never compiled.
set -euxo pipefail
export PATH=/home/runner/.elan/bin:/usr/local/bin:/usr/bin:/bin HOME=/home/runner
export LAKE_OVERRIDE_LEAN=true LEAN=/usr/local/bin/tauceti-lean TAUCETI_LEAN_WRAPPER=/usr/local/bin/tauceti-lean BASH_ENV=/opt/tauceti-lean-env
export RUNNER_TEMP=/opt/actions-runner/_work/_temp GITHUB_WORKSPACE=/opt/actions-runner/_work/TauCeti/TauCeti
REPORT=/fleet-link/telemetry/ci-tree-report.json
TOOL=/usr/local/bin/tauceti_ci_tree.py
mkdir -p "$RUNNER_TEMP"
cd "$GITHUB_WORKSPACE"
ulimit -n 65536
phase() { printf '%s\t%s\n' "$1" "$(date +%s.%N)" >> /fleet-link/telemetry/ci-tree-phases.tsv; }
phase checkout
git -C pr init -q
git -C pr remote add origin https://github.com/TauCetiProject/TauCeti
git -C pr fetch -q --filter=blob:none origin main
commit=$(python3 "$TOOL" newest-published --repo pr --ref FETCH_HEAD --map-out "$RUNNER_TEMP/published-map.jsonl")
git -C pr checkout -q --detach "$commit"
# pr-build.yml's gate: the project's .github, scripts and tauceti.toml at the trusted ref, with
# Tools main materialized over its scripts.
mkdir gate
git -C pr archive "$commit" .github scripts tauceti.toml | tar -xf - -C gate
git clone -q --depth 1 https://github.com/TauCetiProject/TauCetiTools .tauceti-tools
PYTHONPATH=$PWD/.tauceti-tools python3 -m tauceti_tools materialize "$RUNNER_TEMP/tauceti-shared-scripts" --conf gate/tauceti.toml --policy-dir gate/scripts
cp -a "$RUNNER_TEMP/tauceti-shared-scripts/." gate/scripts/
phase toolchain
curl --fail --retry 3 --retry-all-errors --connect-timeout 15 --max-time 60 -sSL \
  "https://github.com/leanprover/elan/releases/download/v4.2.3/elan-x86_64-unknown-linux-gnu.tar.gz?download=1" -o "$RUNNER_TEMP/elan.tar.gz"
echo "df0b2b3a439961ffcbb3985214365ffe40f49bc871df04dff268c7d8e21ca8b2  $RUNNER_TEMP/elan.tar.gz" | sha256sum -c -
tar -xzf "$RUNNER_TEMP/elan.tar.gz" -C "$RUNNER_TEMP" elan-init
"$RUNNER_TEMP/elan-init" -y --default-toolchain none
(cd pr && elan toolchain install "$(cat lean-toolchain)")
watchdog="$RUNNER_TEMP/lean-watchdog-toolchain"
bash gate/scripts/perf/prepare-watchdog-toolchain.sh "$watchdog" "$(cd pr && elan which lean)"
# Same commit and same key (toolchain, wrapper, watchdog, pins): the newest entry still serves.
key=$(python3 "$TOOL" key --project pr --watchdog "$watchdog" | python3 -c 'import json,sys; print(json.load(sys.stdin)["key_digest"])')
if [ "$commit" = "$(cat /fleet-link/input/previous-commit 2>/dev/null || true)" ] \
   && [ "$key" = "$(cat /fleet-link/input/previous-key 2>/dev/null || true)" ]; then
  printf '{"ok": false, "unchanged": true, "commit": "%s"}\n' "$commit" > "$REPORT"
  exit 0
fi
phase mathlib
if (( $(nproc) > 16 )); then export RAYON_NUM_THREADS=16; fi
if ! (cd pr && lake exe cache get); then (cd pr && lake exe cache get!); fi
unset RAYON_NUM_THREADS
mkdir -p pr/.lake/tmp
phase restore
export LAKE_CACHE_DIR=$PWD/pr/.lake/cache LAKE_ARTIFACT_CACHE=true LAKE_RESTORE_ARTIFACTS=true
: > "$RUNNER_TEMP/github-env"
GITHUB_ENV="$RUNNER_TEMP/github-env" LAKE_CACHE_MAX_REVS=1 LAKE_CACHE_BASE_REV="$commit" bash gate/scripts/lake-cache-get.sh pr
# An unclean restore appends the cache-disable lines; that is not an entry.
test ! -s "$RUNNER_TEMP/github-env"
phase build

# The sandboxed build's environment (sandbox-build.sh), restoring only: --no-build fails if any
# module would need compiling.
(cd pr && env -i PATH="$HOME/.elan/bin:/usr/bin:/bin" HOME="$HOME" CI=true LAKE_NO_CACHE=true \
  LAKE_ARTIFACT_CACHE=true LAKE_RESTORE_ARTIFACTS=true LAKE_CACHE_DIR="$LAKE_CACHE_DIR" \
  LAKE_OVERRIDE_LEAN=true LEAN="$watchdog/bin/lean" TMPDIR="$PWD/.lake/tmp" RAYON_NUM_THREADS=4 \
  bash -c 'ulimit -s unlimited 2>/dev/null || ulimit -s "$(ulimit -H -s)"; exec lake build --iofail --no-build')
phase verify
python3 "$TOOL" verify --project pr --commit "$commit" --map "$RUNNER_TEMP/published-map.jsonl" \
  --hasher gate/scripts/CacheHash.lean --watchdog "$watchdog" --report "$REPORT"
phase done
"""


def root():
    return data_dir() / "ci-tree" / "v1"


def enabled(config):
    return config.get(SETTING) == "on"


def slots(config):
    return config.get(SLOTS_SETTING, "all")


def validate(config):
    if config.get(SETTING, "off") not in ("on", "off"):
        raise FleetError(f"{SETTING} must be on or off")
    if slots(config) not in SLOT_CHOICES:
        raise FleetError(f"{SLOTS_SETTING} must be one of " + ", ".join(SLOT_CHOICES))


def offered(config):
    """Whether a guest starting in this slot config should lease an entry.

    Only PR-class slots: staging, infra and the protected special-purpose slots keep today's
    path (the Tools consumer takes the tree only for ordinary PR builds anyway). The slot split
    leaves the other half of the PR slots unbound, so they are a same-time control group.
    """
    if not enabled(config) or "pr" not in (config.get("ci_kinds") or []):
        return False
    choice = slots(config)
    if choice == "all":
        return True
    slot = config.get("ci_slot", 0)
    return isinstance(slot, int) and slot % 2 == (0 if choice == "even" else 1)


def entries():
    """Published entries, newest first, as (id, ready record)."""
    found = []
    directory = root() / "entries"
    for path in directory.iterdir() if directory.is_dir() else []:
        if not ENTRY.fullmatch(path.name):
            continue
        try:
            ready = read(path / "ready.json", None)
        except FleetError:
            ready = None
        if isinstance(ready, dict) and ready.get("id") == path.name and (path / "lake").is_dir():
            found.append((path.name, ready))
    return sorted(found, key=lambda item: item[0], reverse=True)


def leases(entry):
    directory = root() / "leases" / entry
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


def lease(instance):
    """Lease the newest entry for a guest about to start; None if there is none.

    Selection and the lease file are one step under the collection lock, which collection and
    publication also hold, so a selected entry can never be retired before its lease exists.
    """
    if not INSTANCE.fullmatch(instance):
        raise FleetError("Invalid CI instance identity")
    with locked(root() / "collection.lock"):
        available = entries()
        if not available:
            return None
        entry, ready = available[0]
        directory = root() / "leases" / entry
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.close(os.open(directory / instance, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass
        return dict(entry=entry, path=str(root() / "entries" / entry), commit=ready.get("commit"),
                    key_digest=ready.get("key_digest"))


def release(instance):
    """Drop every lease the instance holds; called only once its unit and processes are gone."""
    if not INSTANCE.fullmatch(instance):
        return
    with locked(root() / "collection.lock"):
        for directory in (root() / "leases").glob("*") if (root() / "leases").is_dir() else []:
            (directory / instance).unlink(missing_ok=True)
            try:
                directory.rmdir()
            except OSError:
                pass


def guest_bind(value):
    """The entry directory a guest may bind, from its native.json lease, or None."""
    if not isinstance(value, dict) or not ENTRY.fullmatch(str(value.get("entry", ""))):
        return None
    path = root() / "entries" / value["entry"]
    if Path(value.get("path", "")) != path or not (path / "ready.json").is_file():
        return None
    return path


def collect():
    """Retire unneeded entries and stale leases; remove trash outside the lock."""
    with locked(root() / "collection.lock"):
        available = entries()
        keep = {entry for entry, _ in available[:KEEP]}
        for directory in (root() / "leases").glob("*") if (root() / "leases").is_dir() else []:
            for holder in list(directory.iterdir()):
                # An instance whose directory and unit are both gone can never release normally.
                if (not (state_dir() / "vms" / holder.name).exists()
                        and not unit_active("tauceti-fleet-vm-" + holder.name + ".service")):
                    holder.unlink(missing_ok=True)
            if any(directory.iterdir()):
                keep.add(directory.name)
            else:
                directory.rmdir()
        retired = []
        for entry, _ in available:
            if entry not in keep:
                (root() / "trash").mkdir(parents=True, exist_ok=True, mode=0o700)
                os.rename(root() / "entries" / entry, root() / "trash" / entry)
                retired.append(entry)
        # Entry directories without a valid ready record never became entries.
        for path in (root() / "entries").iterdir() if (root() / "entries").is_dir() else []:
            if path.name not in {entry for entry, _ in available}:
                (root() / "trash").mkdir(parents=True, exist_ok=True, mode=0o700)
                os.rename(path, root() / "trash" / ("invalid-" + secrets.token_hex(4) + "-" + path.name[:40]))
        # Builder directories no build owns any more (a lost builder record, say).
        state = builder_state()
        for path in (root() / "staging").iterdir() if (root() / "staging").is_dir() else []:
            if str(path) != state.get("directory"):
                (root() / "trash").mkdir(parents=True, exist_ok=True, mode=0o700)
                os.rename(path, root() / "trash" / ("staging-" + path.name))
        for path in instances_root().iterdir() if instances_root().is_dir() else []:
            if str(path) != state.get("instance"):
                remove_async(path)
    for path in (root() / "trash").iterdir() if (root() / "trash").is_dir() else []:
        remove_async(path)
    return retired


def remove_async(path):
    """Remove a mapped-UID tree in a bounded background unit, like native cleanup."""
    unit = "tauceti-ci-tree-remove-" + re.sub(r"[^A-Za-z0-9]", "-", path.name)[-60:] + ".service"
    if unit_active(unit):
        return
    run(["systemctl", "--user", "reset-failed", unit], check=False)
    run(["systemd-run", "--user", "--quiet", "--unit=" + unit, "-p", "MemoryMax=256M", "-p", "RuntimeMaxSec=1800",
         "-p", "CPUWeight=10", "-p", "IOWeight=10",
         "unshare", "--user", "--map-auto", "--map-root-user", "rm", "-rf", "--", path], check=False)


# The builder.


def builder_state():
    try:
        return read(root() / "builder.json", {}) or {}
    except FleetError:
        return {}


def unit_active(unit):
    """Whether the unit may still have processes: anything but positively stopped."""
    result = run(["systemctl", "--user", "is-active", unit], check=False)
    return not (result.returncode in (3, 4) and result.stdout.strip() in ("inactive", "failed", "unknown", ""))


def free_bytes():
    usage = os.statvfs(root())
    return usage.f_bavail * usage.f_frsize


def due(config, now=None):
    """Why a build should not start now, or None."""
    now = time.time() if now is None else now
    if not enabled(config):
        return "disabled"
    state = builder_state()
    if state.get("unit") and unit_active(state["unit"]):
        return "building"
    if now - state.get("last_attempt_at", 0) < CHECK_SECONDS:
        return "checked recently"
    if state.get("last_outcome") == "failed" and now - state.get("last_attempt_at", 0) < FAILURE_BACKOFF_SECONDS:
        return "backing off after a failure"
    available = entries()
    if available and now - available[0][1].get("created_at", 0) < MIN_AGE_SECONDS:
        return "newest entry is recent"
    if len(list((root() / "entries").iterdir())) >= MAX_ENTRIES:
        return "entry limit"
    if free_bytes() < BUILD_FREE_GIB * 1024**3:
        return "disk headroom"
    from . import native_runner
    if not native_runner.ready():
        return "native image not prepared"
    return None


def tick(config):
    """Controller loop hook: finish a build, collect, then maybe start the next build."""
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("entries", "staging", "trash", "leases"):
        (root() / name).mkdir(exist_ok=True, mode=0o700)
    finish()
    collect()
    reason = due(config)
    if reason is None:
        start()
    return reason


def instances_root():
    # Short: the instance's AF_UNIX sockets must fit in 108 bytes.
    return state_dir() / "ct"


def start():
    """Launch one builder instance as a transient unit at PR CI priority.

    The instance (sockets, control link, rootfs) lives under a short state path; the tree it
    writes lives in the pool's staging directory, on the entries' filesystem, so publication is
    a rename.
    """
    from . import native_inside, native_runner
    identity = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
    directory = root() / "staging" / identity
    instance = instances_root() / identity[-8:]
    (directory / "tree").mkdir(parents=True, mode=0o700)
    (instance / "control/input").mkdir(parents=True, mode=0o700)
    # The guest's runner reads its inputs; the host directories are the guest's root's.
    (instance / "control/input").chmod(0o755)
    previous = entries()
    if previous:
        for name, value in (("previous-commit", previous[0][1]["commit"]), ("previous-key", previous[0][1]["key_digest"])):
            (instance / "control/input" / name).write_text(value)
            (instance / "control/input" / name).chmod(0o644)
    (instance / "control/offline-job.sh").write_text(BUILD_SCRIPT)
    # The guest bootstrap consumes a JIT input; the offline runner never registers.
    (instance / "control/jit").write_text("unregistered-ci-tree-builder")
    (instance / "control/jit").chmod(0o600)
    atomic(instance / "native.json", dict(image=native_runner.image(), disk_budget_gib=native_inside.DISK_BUDGET_GIB,
                                          memory_gib=native_runner.MEMORY_GIB, lean_runtime=True,
                                          ci_tree_builder=str(directory / "tree")))
    # The host pressure service finds guests under vms/; the builder's Lean waits for permits too.
    bridge = state_dir() / "vms" / ("ci-tree-" + identity)
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge.symlink_to(instance, target_is_directory=True)
    unit = "tauceti-ci-tree-build-" + identity + ".service"
    state = builder_state()
    state.update(unit=unit, identity=identity, directory=str(directory), instance=str(instance),
                 started_at=time.time(), last_attempt_at=time.time(), bridge=str(bridge))
    atomic(root() / "builder.json", state)
    try:
        run(["systemd-run", "--user", "--quiet", "--unit=" + unit, "--slice=tauceti-fleet-ci.slice",
             "-p", "CPUWeight=100", "-p", "ManagedOOMPreference=avoid", "-p", "MemorySwapMax=0",
             # No RemainAfterExit: an exited builder must read as inactive to finish().
             "-p", "KillMode=control-group", "-p", f"RuntimeMaxSec={BUILD_TIMEOUT_SECONDS}",
             *native_runner.properties(),
             # An isolated test root (tests/integration/ci_tree_build.py) reaches the unit too.
             *[f"--setenv={k}={v}" for k, v in sorted(os.environ.items()) if k in ISOLATION],
             sys.executable, "-m", MODULE, "build", instance, directory], timeout=30)
    except FleetError:
        # A timeout can come after systemd created the unit: keep ownership (and the pressure
        # bridge) for finish() unless the unit is positively stopped.
        if not unit_active(unit):
            state.update(unit=None, last_outcome="failed", last_error="builder unit did not start")
            atomic(root() / "builder.json", state)
            bridge.unlink(missing_ok=True)
        raise
    return unit


def builder_tree(value):
    """The staging tree a builder instance may bind at its workspace's .lake, or None."""
    path = Path(str(value or ""))
    if path.name != "tree" or path.parent.parent != root() / "staging" or not path.is_dir() or path.is_symlink():
        return None
    return path


def finish():
    """Record a finished builder unit's outcome and release its staging directory."""
    state = builder_state()
    unit = state.get("unit")
    if not unit or unit_active(unit):
        return None
    directory = Path(state.get("directory") or "")
    instance = Path(state.get("instance") or "")
    result = {}
    try:
        result = read(directory / "build-result.json", {}) or {}
    except FleetError:
        pass
    outcome = result.get("outcome") or "failed"
    state.update(unit=None, directory=None, instance=None, last_outcome=outcome, last_finished_at=time.time(), last_result=result,
                 last_error=result.get("error") if outcome == "failed" else None)
    run(["systemctl", "--user", "reset-failed", unit], check=False)
    run(["systemctl", "--user", "stop", unit], check=False)
    Path(state.get("bridge", "/nonexistent")).unlink(missing_ok=True)
    if directory.parent == root() / "staging" and directory.exists():
        (root() / "trash").mkdir(parents=True, exist_ok=True, mode=0o700)
        os.rename(directory, root() / "trash" / ("staging-" + directory.name))
    if instance.parent == instances_root() and instance.exists():
        remove_async(instance)
    with (root() / "builds.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(at=time.time(), unit=unit, **result), sort_keys=True) + "\n")
    history = root() / "builds.jsonl"
    if history.stat().st_size > 1024**2:
        lines = history.read_text().splitlines(keepends=True)
        history.write_text("".join(lines[len(lines) // 2:]))
    atomic(root() / "builder.json", state)
    return outcome


def build(instance, directory):
    """The builder unit's body: run the offline instance, then verify and publish its tree."""
    from . import native_runner
    instance, directory = Path(instance), Path(directory)
    started = time.time()
    result = dict(identity=directory.name, started_at=started)
    try:
        code = native_runner.serve(instance, offline=True)
        report = native_runner.shared_json(instance / "control/telemetry/ci-tree-report.json", {}) or {}
        native = read(instance / "native-result.json", {}) or {}
        result.update(build_seconds=round(time.time() - started, 1), exit_code=code, io=native.get("io"),
                      phases=_phases(instance / "control/telemetry/ci-tree-phases.tsv"))
        if report.get("unchanged"):
            result.update(outcome="unchanged", commit=report.get("commit"))
        elif code != 0 or report.get("ok") is not True:
            result.update(outcome="failed", error=str(report.get("error") or f"builder exited {code}")[:500])
        else:
            result.update(outcome="published", **publish(directory, report))
    except (FleetError, OSError, ValueError, KeyError) as error:
        result.update(outcome="failed", error=f"{type(error).__name__}: {error}"[:500])
    result["seconds"] = round(time.time() - started, 1)
    atomic(directory / "build-result.json", result)
    return 0 if result["outcome"] != "failed" else 1


def _phases(path):
    try:
        rows = [line.split("\t") for line in Path(path).read_text().splitlines()]
        return [[name, float(at)] for name, at in rows]
    except (OSError, ValueError):
        return None


def publish(directory, report):
    """Check the tree's structure, record its inventory and rename it into entries/ atomically."""
    commit = report.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise FleetError("builder report has no commit")
    if not isinstance(report.get("key_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", report["key_digest"]):
        raise FleetError("builder report has no key")
    identity = directory.name[:16] + "-" + commit[:12] + "-" + secrets.token_hex(4)
    if not ENTRY.fullmatch(identity):
        raise FleetError("invalid entry identity")
    entry = directory / "entry"
    entry.mkdir(mode=0o755)
    # The tree is owned by the guest's mapped runner; moving a directory needs its owner's rights.
    run(["unshare", "--user", "--map-auto", "--map-root-user", "mv", "-T", "--", directory / "tree", entry / "lake"],
        timeout=60)
    inventory = entry / "inventory.jsonl.gz"
    checked = run(["unshare", "--user", "--map-auto", "--map-root-user", sys.executable, "-m", MODULE,
                   "inventory", entry / "lake", inventory], timeout=3600, check=False)
    if checked.returncode != 0:
        detail = (checked.stdout.strip().splitlines() or checked.stderr.strip().splitlines() or ["no detail"])[-1]
        raise FleetError("structure check failed: " + detail[:300])
    summary = json.loads(checked.stdout.strip().splitlines()[-1])
    ready = dict(version=1, id=identity, commit=commit, key=report.get("key"), key_digest=report["key_digest"],
                 created_at=time.time(), files=summary["files"], bytes=summary["bytes"],
                 inventory_sha256=summary["inventory_sha256"], verifier={k: report.get(k) for k in (
                     "modules", "root_traces", "unmapped_root_traces", "package_outputs", "artifacts",
                     "hashed_files", "removed", "verify_seconds")})
    atomic(entry / "ready.json", ready)
    os.chmod(entry / "ready.json", 0o644)
    os.chmod(inventory, 0o644)
    with locked(root() / "collection.lock"):
        os.rename(entry, root() / "entries" / identity)
    return dict(entry=identity, commit=commit, files=summary["files"], bytes=summary["bytes"])


def inventory(tree, output):
    """Fail-closed structure check and stat inventory of an entry tree, as the mapped root.

    Only directories, regular files and symlinks that stay inside the tree; every file owned by
    the guest's runner; no set-ID, sticky or world-writable modes; and every hard link's other
    names inside the tree, so no inode is shared with anything outside it. Modes stay as Lake
    left them: read-only comes from the bind, and Lake rewrites `.hash` and `.trace` in place.
    """
    import hashlib
    tree = Path(tree)
    if not tree.is_dir() or tree.is_symlink():
        raise FleetError("entry tree is missing")
    real_tree = os.path.realpath(tree)
    links = {}
    count = total = 0
    digest = hashlib.sha256()
    with gzip.open(output, "wt") as stream:
        def fail(error):
            raise FleetError(f"cannot read the entry tree: {error}")
        for directory, directories, files in os.walk(tree, onerror=fail):
            directories.sort()
            for name in sorted(directories) + sorted(files):
                path = Path(directory) / name
                info = path.lstat()
                relative = str(path.relative_to(tree))
                mode = info.st_mode
                if info.st_uid != RUNNER_UID or info.st_gid != RUNNER_GID:
                    raise FleetError(f"{relative} is not owned by the guest runner")
                if mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX) or (mode & stat.S_IWOTH and not stat.S_ISLNK(mode)):
                    raise FleetError(f"{relative} has a forbidden mode {stat.filemode(mode)}")
                target = None
                if stat.S_ISLNK(mode):
                    target = os.readlink(path)
                    try:
                        # The whole chain, not the text: `b -> a/..` with `a -> .` escapes.
                        resolved = os.path.realpath(path, strict=True)
                    except (OSError, RuntimeError):
                        raise FleetError(f"{relative} is a dangling or looping link") from None
                    if os.path.isabs(target) or os.path.commonpath([real_tree, resolved]) != real_tree:
                        raise FleetError(f"{relative} links outside the entry")
                elif stat.S_ISREG(mode):
                    if info.st_nlink > 1:
                        seen = links.setdefault(info.st_ino, [info.st_nlink, 0])
                        seen[1] += 1
                        if seen[1] == 1:
                            total += info.st_blocks * 512  # Each inode's blocks once.
                    else:
                        total += info.st_blocks * 512
                elif not stat.S_ISDIR(mode):
                    raise FleetError(f"{relative} has a forbidden file type")
                count += 1
                if count > MAX_ENTRY_FILES or total > MAX_ENTRY_BYTES:
                    raise FleetError("entry exceeds its size bounds")
                record = [relative, stat.S_IFMT(mode), stat.S_IMODE(mode), info.st_size, info.st_mtime_ns,
                          info.st_ctime_ns, info.st_ino, info.st_nlink, target]
                line = json.dumps(record) + "\n"
                digest.update(line.encode())
                stream.write(line)
    outside = [ino for ino, (nlink, seen) in links.items() if seen != nlink]
    if outside:
        raise FleetError(f"{len(outside)} hard-linked files have names outside the entry")
    return dict(files=count, bytes=total, inventory_sha256=digest.hexdigest())


def status():
    """Small summary for status.json; never walks a tree."""
    try:
        state = builder_state()
        return dict(entries=[dict(id=entry, commit=ready.get("commit"), created_at=ready.get("created_at"),
                                  files=ready.get("files"), bytes=ready.get("bytes"), leases=len(leases(entry)))
                             for entry, ready in entries()],
                    building=bool(state.get("unit")), last_outcome=state.get("last_outcome"),
                    last_finished_at=state.get("last_finished_at"), last_error=state.get("last_error"),
                    last_result={k: (state.get("last_result") or {}).get(k) for k in (
                        "outcome", "commit", "entry", "seconds", "build_seconds", "files", "bytes")})
    except (OSError, FleetError) as error:
        return dict(error=str(error)[:200])


def main():
    action, *args = sys.argv[1:]
    if action == "build":
        return build(args[0], args[1])
    if action == "inventory":
        try:
            print(json.dumps(inventory(args[0], args[1])))
        except (FleetError, OSError) as error:
            print(f"{type(error).__name__}: {error}")
            return 1
        return 0
    raise FleetError("Unknown shared tree action")


if __name__ == "__main__":
    raise SystemExit(main())
