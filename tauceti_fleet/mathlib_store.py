"""Builder of the read-only Mathlib store that sandboxed worker rounds mount.

The worker reads `<directory>/store/<revision>/` (`TAUCETI_MATHLIB_STORE`) and never
writes it; its docs/mathlib-store.md is the contract for an entry and for building and
removing one. All of `store/` is visible in every container, so checkouts being built
and downloads live beside it, on the same filesystem for the publishing rename.
"""

import functools
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from .process import executable, run
from .store import FleetError, atomic, data_dir, host_config, read, state_dir, try_lock, update_host

READY = ".tauceti-store-ready"
REPOSITORY = "TauCetiProject/TauCeti"
SOURCE = "https://github.com/" + REPOSITORY + ".git"
MATHLIB = "https://github.com/leanprover-community/mathlib4.git"
# Building runs Lake code from the pinned commit; GitHub serves fork commits through MATHLIB too.
TRUSTED = ("master",)
INTERVAL = 1800
ROUND_TIMEOUT = 5400
MIN_FREE = 20 * 1024**3
REVISION = re.compile(r"[0-9a-f]{40}")
UNIT = "tauceti-fleet-mathlib-store.service"


def state_file():
    return state_dir() / "mathlib-store.json"


def configure(payload):
    if payload.get("clear"):
        directory = None
    elif "directory" in payload:
        directory = payload["directory"]
        # The worker hands the path to its sandbox as SOURCE:TARGET:MODE.
        if not isinstance(directory, str) or not re.fullmatch(r"/[A-Za-z0-9._/+-]+", directory):
            raise FleetError("The Mathlib store needs an absolute path made of letters, digits and . _ + - /")
        try:
            mounted(dict(mathlib_store=directory)).mkdir(parents=True, exist_ok=True)
        except OSError:
            raise FleetError(f"Cannot create {directory}; create it for this account and retry") from None
    else:
        return dict(mathlib_store=host_config().get("mathlib_store"))
    update_host(mathlib_store=directory)
    return dict(mathlib_store=directory)


def mounted(config):
    return Path(config["mathlib_store"]) / "store"


def hold(config):
    """How long an entry outlives its last pin: no lock says when the last round linking it has ended.

    Twice the longest round, counting a `TAUCETI_ROUND_TIMEOUT` the profile gives its workers.
    """
    try:
        timeout = int((config.get("env") or {}).get("TAUCETI_ROUND_TIMEOUT", ROUND_TIMEOUT))
    except (TypeError, ValueError, AttributeError):
        timeout = ROUND_TIMEOUT
    return 2 * max(timeout, ROUND_TIMEOUT)


def git(*args, timeout=120):
    # The pass's unit ends with its main process, which would kill detached maintenance half-way.
    return run(["git", "-c", "gc.autoDetach=false", "-c", "maintenance.autoDetach=false", *args],
               timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}).stdout


def open_prs(config):
    """Open pull requests of this host's workers and their heads, as the worker last prepared them."""
    binary = executable("tauceti")
    if not binary:
        raise FleetError("TauCetiWorker is missing; run prepare")
    rows = json.loads(run([binary, "activity", "--inventory", "--group", config["machine_id"]], timeout=60).stdout)
    return {
        p["number"]: p["head"]
        for p in rows["prs"]
        if p.get("repo") == REPOSITORY and p.get("state") == "OPEN" and type(p.get("number")) is int
    }


def pins(config, known=None):
    """Mathlib revision and toolchain pinned by main and by each open pull request of this host's workers.

    The pull requests come from the worker's prepared inventory. One fetch brings main and
    the heads that moved into a blobless cache, from which the two pin files are read.
    Returns the pins, a note on what could not be read, and whether every pin was read: a
    pull request whose pins are unreadable keeps the revision `known` (the last pass's
    pins) gives it.
    """
    cache = data_dir() / "mathlib-store" / "tauceti.git"
    repo = functools.partial(git, "--git-dir", cache)
    if not (cache / "HEAD").exists():
        git("init", "-q", "--bare", cache)
        repo("remote", "add", "origin", SOURCE)
    notes = []
    held = dict(
        line.split() for line in repo("for-each-ref", "--format=%(refname:lstrip=2) %(objectname)", "refs/pins/").splitlines()
    )
    try:
        prs = {str(number): head for number, head in open_prs(config).items()}
        for name in held.keys() - prs.keys() - {"main"}:
            repo("update-ref", "-d", "refs/pins/" + name)
    except (FleetError, ValueError, KeyError, TypeError, AttributeError):
        prs = {name: head for name, head in held.items() if name != "main"}
        notes.append("open pull requests unavailable from the worker's activity inventory; keeping the last known ones")
    fetch = ("fetch", "-q", "--depth=1", "--filter=blob:none", "--no-tags", "--no-write-fetch-head", "origin")
    main = "+refs/heads/main:refs/pins/main"
    moved = [f"+refs/pull/{name}/head:refs/pins/{name}" for name, head in prs.items() if held.get(name) != head]
    try:
        repo(*fetch, main, *moved, timeout=600)
    except FleetError:
        if not moved:
            raise FleetError("Cannot fetch TauCeti main to read its Mathlib pin") from None
        try:
            repo(*fetch, main, timeout=600)
        except FleetError:
            raise FleetError("Cannot fetch TauCeti main to read its Mathlib pin") from None
        # A single pull request ref the remote no longer has fails the whole fetch.
        unfetched = 0
        for refspec in moved:
            try:
                repo(*fetch, refspec, timeout=600)
            except FleetError:
                unfetched += 1
        if unfetched:
            notes.append("some pull request heads could not be fetched")
    last = {who: (revision, entry.get("toolchain")) for revision, entry in (known or {}).items()
            for who in entry.get("pinned_by", [])}
    needed, unread = {}, []
    for name in ["main", *sorted(prs, key=int)]:
        who = name if name == "main" else "#" + name
        try:
            packages = json.loads(repo("show", f"refs/pins/{name}:lake-manifest.json"))["packages"]
            revision = next(p["rev"] for p in packages if p.get("name") == "mathlib")
            toolchain = repo("show", f"refs/pins/{name}:lean-toolchain").strip()
            if not REVISION.fullmatch(revision):
                raise ValueError("revision")
        except (FleetError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
            if name == "main":
                raise FleetError("Cannot read the Mathlib pin of TauCeti main") from None
            unread.append(who)
            if who not in last:
                continue
            revision, toolchain = last[who]
        entry = needed.setdefault(revision, dict(toolchain=toolchain, pinned_by=[]))
        entry["pinned_by"].append(who)
    if unread:
        notes.append("pins unreadable at " + ", ".join(unread) + "; keeping their last known revisions")
    return needed, "; ".join(notes) or None, not notes


def trusted_history():
    """Commits of the TRUSTED Mathlib branches, from a local commits-only clone.

    Membership is decided on fetched history alone; asking the server for a commit would also
    find one that only a fork's pull request contains.
    """
    cache = data_dir() / "mathlib-store" / "mathlib.git"
    repo = functools.partial(git, "--git-dir", cache)
    if not (cache / "HEAD").exists():
        git("init", "-q", "--bare", cache)
        repo("remote", "add", "origin", MATHLIB)
    refs = ["refs/trusted/" + branch for branch in TRUSTED]
    try:
        repo("fetch", "-q", "--filter=tree:0", "--no-tags", "--no-write-fetch-head", "origin",
             *(f"+refs/heads/{branch}:refs/trusted/{branch}" for branch in TRUSTED), timeout=1800)
    except FleetError:
        raise FleetError("not built: cannot fetch Mathlib " + ", ".join(TRUSTED) + " to check the revision") from None
    return set(repo("rev-list", *refs, timeout=600).split())


def ready(store, revision):
    return (store / revision / READY).is_file()


def build(root, revision):
    """Publish `store/<revision>`; an attempt that fails publishes nothing."""
    checkout = root / "staging" / revision
    try:
        git("init", "-q", checkout)
        # Mathlib's cache tool chooses its download location from the checkout's `origin`.
        git("-C", checkout, "remote", "add", "origin", MATHLIB)
        git("-C", checkout, "fetch", "-q", "--depth=1", "--no-tags", "origin", revision, timeout=1800)
        git("-C", checkout, "checkout", "-q", "--detach", "FETCH_HEAD", timeout=600)
        # With an artifact cache Lake may keep outputs outside the build directory.
        environment = {k: v for k, v in os.environ.items() if k != "LAKE_ARTIFACT_CACHE"}
        environment["MATHLIB_CACHE_DIR"] = str(root / "downloads")
        # Lake rewrites some `.hash` files the first time it replays an unpacked build,
        # which a read-only mount refuses; the replay here settles them.
        for arguments in (("exe", "cache", "get"), ("build", "--no-build", "Mathlib")):
            result = run([executable("lake") or "lake", *arguments], cwd=checkout, env=environment,
                         timeout=3 * 3600, check=False)
            if result.returncode:
                print(f"tauceti-fleet: lake {' '.join(arguments)} at Mathlib {revision}:\n"
                      + (result.stderr or result.stdout)[-2000:], file=sys.stderr, flush=True)
                raise FleetError(f"lake {' '.join(arguments)} failed; see journalctl --user -u {UNIT}")
        (checkout / READY).write_text(revision + "\n")
        checkout.rename(root / "store" / revision)
    finally:
        shutil.rmtree(checkout, ignore_errors=True)


def record(directory):
    try:
        state = read(state_file(), {})
    except FleetError:
        return {}
    return state if state.get("directory") == directory else {}


def warm(config=None, now=None):
    """One background pass: refresh the pins, remove entries unpinned for long enough, publish missing ones."""
    config = config or host_config()
    directory = config.get("mathlib_store")
    if not directory:
        return None
    with try_lock(state_dir() / "mathlib-store.lock") as owned:
        if not owned:
            return None
        now = time.time() if now is None else now
        root, store = Path(directory), mounted(config)
        previous = record(directory)
        state = dict(directory=directory, checked_at=previous.get("checked_at"), needed=previous.get("needed", {}),
                     unpinned=previous.get("unpinned", {}), building=None, failures={}, error=None)
        try:
            store.mkdir(parents=True, exist_ok=True)
            shutil.rmtree(root / "staging", ignore_errors=True)
            try:
                state["needed"], state["error"], complete = pins(config, state["needed"])
                state["checked_at"] = now
            except FleetError as exc:
                state["error"], complete = str(exc), False
            needed, failures = state["needed"], state["failures"]
            held = [p.name for p in store.iterdir() if REVISION.fullmatch(p.name)]
            # Only a check that read every open pull request's pins says what is unpinned: after any
            # failed read no clock starts and nothing is removed.
            if complete:
                state["unpinned"] = {name: state["unpinned"].get(name, now) for name in held
                                     if name not in needed and ready(store, name)}
            else:
                state["unpinned"] = {name: at for name, at in state["unpinned"].items() if name not in needed}
            for name in held:
                if ready(store, name):
                    expired = now - state["unpinned"].get(name, now) >= hold(config)
                else:
                    # Its marker was deleted to withdraw it, which is when the directory last changed.
                    expired = now - (store / name).stat().st_mtime >= hold(config)
                if complete and expired:
                    shutil.rmtree(store / name)
                    state["unpinned"].pop(name, None)
            history, untrusted = None, set()
            for revision, entry in needed.items():
                if not ready(store, revision):
                    if not (store / revision).exists() and history is None:
                        try:
                            history = trusted_history()
                        except FleetError as exc:
                            history = exc
                    if (store / revision).exists():
                        failures[revision] = "marker withdrawn; rebuilt once its last rounds have ended"
                    elif isinstance(history, FleetError):
                        failures[revision] = str(history)
                    elif revision not in history:
                        untrusted.add(revision)
                        failures[revision] = "untrusted: not in the history of Mathlib " + ", ".join(TRUSTED)
                    elif shutil.disk_usage(store).free < MIN_FREE:
                        failures[revision] = f"not built: under {MIN_FREE // 1024**3} GiB free"
                    else:
                        state["building"] = revision
                        atomic(state_file(), state)
                        try:
                            build(root, revision)
                        except (FleetError, OSError) as exc:
                            failures[revision] = str(exc)
                        state["building"] = None
                    if revision in failures:
                        continue
                built = (store / revision / "lean-toolchain").read_text().strip()
                if built != entry["toolchain"]:
                    failures[revision] = f"built with {built}, pinned with {entry['toolchain']}"
            if all(ready(store, revision) or revision in untrusted for revision in needed):
                shutil.rmtree(root / "downloads", ignore_errors=True)
        except OSError as exc:
            state["error"] = f"{directory}: {exc.strerror or type(exc).__name__}"
        atomic(state_file(), state)
        return state


def cached(config):
    """The last pass's record and the entries on disk; a small local read for status."""
    directory = config["mathlib_store"]
    state = record(directory)
    store, needed = mounted(config), state.get("needed", {})
    try:
        held = sorted(p.name for p in store.iterdir() if REVISION.fullmatch(p.name))
    except OSError:
        held = []
    return dict(
        directory=directory,
        checked_at=state.get("checked_at"),
        needed={revision: dict(entry, ready=ready(store, revision)) for revision, entry in needed.items()},
        unpinned=[revision for revision in held if revision not in needed],
        building=state.get("building"),
        failures=state.get("failures", {}),
        error=state.get("error"),
    )


def describe(value, now=None):
    now = time.time() if now is None else now
    needed, failures = value.get("needed") or {}, value.get("failures") or {}
    if value.get("checked_at") is None:
        parts = ["pinned revisions not checked yet"]
    else:
        count = sum(bool(entry.get("ready")) for entry in needed.values())
        parts = [f"{count} of {len(needed)} pinned revisions ready "
                 f"(checked {max(0, int(now - value['checked_at'])) // 60} min ago)"]
    for revision, entry in needed.items():
        who = entry.get("pinned_by", [])
        label = (f"{revision[:10]} ({entry.get('toolchain')}; "
                 + ", ".join(who[:3]) + (f" and {len(who) - 3} more" if len(who) > 3 else "") + ")")
        if value.get("building") == revision:
            parts.append("building " + label)
        elif revision in failures:
            parts.append(label + ": " + failures[revision])
        elif not entry.get("ready"):
            parts.append("missing " + label)
    if value.get("unpinned"):
        parts.append(f"{len(value['unpinned'])} unpinned awaiting removal")
    if value.get("error"):
        parts.append(value["error"])
    return "; ".join(parts)


class Warmer:
    """Starts the background pass from the controller loop, which never waits for it."""

    def __init__(self, start):
        self.start = start
        self.attempted = 0
        self.error = None

    def step(self, config, now):
        if not config.get("mathlib_store") or now - self.attempted < INTERVAL:
            return
        self.attempted = now
        try:
            self.start()
            self.error = None
        except (FleetError, OSError) as exc:
            message = f"Mathlib store pass not started: {exc}"
            if message != self.error:
                print("tauceti-fleet: " + message, file=sys.stderr, flush=True)
                self.error = message
