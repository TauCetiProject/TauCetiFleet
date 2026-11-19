"""Operator target lists: which list the authoring workers follow and what this host can tell about it.

The worker owns the file format, the selection rules and the pull request overlay
(TauCetiWorker docs/target-lists.md). This module stores or locates the file, checks it
against files already on the host, and never makes a GitHub request.
"""

import json
import os
import tempfile
from pathlib import Path

from . import capabilities
from .process import executable, run
from .store import FleetError, config_dir, host_config, locked, update_host

STATES = ("done", "inflight", "eligible", "blocked")
ACTIVE = ("running", "surveying")
# The verifier reports these from inside the round that holds the item, and also from a continuation
# between rounds, which keeps the retry time (`next_action_at`) the ended round left; a round clears it.
VERIFYING = ("verifying", "working", "verification-pending", "verification-incomplete")
# A transferred list travels inside the 1 MiB host request, where JSON escapes can double its size.
LIMIT = 256 * 1024
# A list named on the host never travels; this only bounds what the controller reads into memory.
HOST_LIMIT = 16 * 1024 * 1024
_INSPECTIONS = {}
REPOSITORY = "TauCetiProject/TauCeti"
FILE_ONLY = "Counted from the file's marks and this host's rounds; workers separately resolve open and merged pull request markers at any age"
RESTART_MANAGER = (
    "on the host run `systemctl --user restart tauceti-fleet-workers.service`, then set the list again. "
    "Workers in their own scopes keep running and the new manager adopts them; a worker whose status shows "
    "degraded isolation runs inside the manager's service and its round ends with it"
)


def running_manager():
    """The live manager's executable, not a potentially stale installed-runtime record."""
    from . import host

    if not host.manager_active():
        return None
    process = host.manager_process()
    if process is None:
        return ""
    argv, _ = process
    return next((argv[i - 1] for i in range(1, len(argv))
                 if argv[i] == "workers" and Path(argv[i - 1]).name == "tauceti"), "")


def require_support(*, clearing=False):
    """Gate changes on both the installed runtime and the actual live manager."""
    from . import host

    protocol = capabilities.worker_protocol()
    if not clearing and (protocol.get("roadmap_targets") != 1 or protocol.get("targets_inspect") != 1):
        raise FleetError("The installed TauCetiWorker has no canonical target-list inspection support; install a merged release first")
    manager = running_manager()
    if manager == "":
        raise FleetError("Cannot tell which worker runtime the running manager uses; " + RESTART_MANAGER)
    if manager:
        live = host.manager_ping()
        required = ("graceful_restart",) if clearing else ("roadmap_targets", "targets_inspect", "graceful_restart")
        configured = live.get("config")
        same_config = isinstance(configured, str) and Path(configured).resolve() == (config_dir() / "workers.toml").resolve()
        if not all(live.get(key) == 1 for key in required) or not same_config:
            raise FleetError("The running worker manager lacks target-list inspection or graceful replacement; " + RESTART_MANAGER)
    return manager or executable("tauceti")


def inspect_file(path):
    """Use the Worker-owned strict parser; Fleet never copies its list grammar."""
    binary = running_manager()
    if binary == "":
        raise FleetError("Cannot determine the running target-list runtime")
    binary = binary or executable("tauceti")
    if not binary:
        raise FleetError("No worker runtime is installed")
    binary = str(Path(binary).resolve())
    path = Path(path).resolve()
    try:
        info = path.stat()
    except OSError as exc:
        raise FleetError(f"Cannot read target list {path}: {type(exc).__name__}") from None
    key = (str(path), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size, str(binary))
    if key in _INSPECTIONS:
        return _INSPECTIONS[key]
    load(path)  # Bound the local read before handing it to the shared parser.
    result = run([binary, "_targets-inspect", "--file", str(path)], check=False, timeout=5)
    if result.returncode:
        # This read-only command reports only list diagnostics, never credentials.
        detail = (result.stderr or "target-list inspection failed").strip()[:1000]
        raise FleetError(detail)
    try:
        value = json.loads(result.stdout)
        if value.get("schema") != "tauceti-target-list/v1" or not isinstance(value.get("items"), list):
            raise ValueError("unsupported inspection schema")
        if len(_INSPECTIONS) >= 16:
            _INSPECTIONS.clear()
        _INSPECTIONS[key] = value
        return value
    except (ValueError, TypeError, AttributeError) as exc:
        raise FleetError("Worker returned an invalid target-list inspection result") from exc


def stored():
    return config_dir() / "roadmap-targets.md"


def load(path, limit=HOST_LIMIT):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > limit:
            raise FleetError(f"Target list {path} exceeds {limit // 1024} KiB")
        return raw.decode()
    except (OSError, UnicodeDecodeError) as exc:
        raise FleetError(f"Cannot read target list {path}: {type(exc).__name__}") from None


def write(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".fleet-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def parse(text):
    """Inspect transferred text with the exact same parser as a worker round."""
    with tempfile.TemporaryDirectory(prefix="fleet-targets-") as directory:
        path = Path(directory) / "targets.md"
        path.write_text(text, encoding="utf-8")
        return inspect_file(path)


def holders(workers):
    """Rounds in progress under the list; a waiting or backing-off slot keeps its last item in its status file."""
    def holding(w):
        return w.get("state") in ACTIVE or w.get("state") in VERIFYING and w.get("next_action_at") is None

    return [dict(worker=w.get("id"), item=w["roadmap_target"]) for w in workers or []
            if holding(w) and isinstance(w.get("roadmap_target"), str) and w["roadmap_target"]]


def summarize(listing, workers):
    first = {}
    for item in listing["items"]:
        first.setdefault(item["slug"], item)
    rounds = holders(workers)
    held = {r["item"] for r in rounds}
    areas = {area: dict.fromkeys(STATES, 0) for area in listing["areas"]}
    for item in listing["items"]:
        if item["status"] == "done":
            state = "done"
        elif item["status"] == "inflight" or item["area"] + "/" + item["slug"] in held:
            state = "inflight"
        else:
            # The canonical parser rejects missing/duplicate/cyclic dependencies.
            met = all(first[need]["status"] == "done" for need in item["needs"])
            state = "eligible" if met else "blocked"
        areas[item["area"]][state] += 1
    totals = {state: sum(row[state] for row in areas.values()) for state in STATES}
    return dict(items=len(listing["items"]), **totals, areas=areas, rounds=rounds)


def status(config, workers):
    """The controller's report block; a missing or broken list is reported, never raised."""
    path = config.get("roadmap_targets")
    if not path:
        return None
    try:
        return dict(path=path, **summarize(inspect_file(path), workers))
    except (FleetError, TypeError, KeyError, ValueError, OSError) as exc:
        return dict(path=path, error=str(exc))


def audit(config, text, workers):
    """Read-only consistency findings; local inventories cannot prove complete landing markers."""
    listing = parse(text)
    known = {item["area"] + "/" + item["slug"]: item for item in listing["items"]}
    findings = []
    for held in holders(workers):
        item = known.get(held["item"])
        if item is None or item["status"] == "done":
            findings.append(dict(kind="round", item=held["item"], detail=f"{held['worker']} is authoring {held['item']}, "
                                 + ("which the file marks done" if item else "which the file does not list")))
    findings.append(dict(kind="pull-requests", detail="No file corrections: this offline audit cannot prove complete, "
                         "nonpartial landing markers. Workers resolve those markers on their own forge at any age."))
    return findings, text


def describe(config, workers):
    path = config.get("roadmap_targets")
    notes = []
    if path and (config.get("roadmap_focus") or {}).get("percent"):
        notes.append(f"Roadmap focus ({config['roadmap_focus']['percent']}%) is not applied: workers under a target list take no focus")
    return dict(roadmap_targets=path or None, transferred=bool(path) and Path(path) == stored(),
                list=status(config, workers), notes=notes)


def configure(payload, workers):
    with locked(config_dir() / "worker-policy.lock"):
        return _configure(payload, workers)


def _configure(payload, workers):
    config = host_config()
    before = config.get("roadmap_targets")
    text, path = payload.get("text"), payload.get("path")
    if payload.get("audit"):
        if not before:
            raise FleetError("This host has no target list; set one with targets HOST --file PATH")
        # Workers resolve the list's path when they start, so a symlink must keep pointing at the file they read.
        destination = Path(before).resolve()
        text = load(destination)
        findings, _ = audit(config, text, workers)
        applied = False  # No local inventory contains the authoritative marker evidence.
        return dict(roadmap_targets=before, transferred=Path(before) == stored(), findings=findings,
                    corrections=sum("correction" in f for f in findings), applied=applied)
    notes = []
    if payload.get("clear"):
        # The stored copy stays: a round starting before the definitions change still reads it.
        if before:
            require_support(clearing=True)
        config = update_host(roadmap_targets=None)
    elif text is not None or path is not None:
        if text is not None and path is not None:
            raise FleetError("Transfer a list or name one on the host, not both")
        require_support()
        if path is not None:
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise FleetError("A target list on the host is named by its absolute path")
            # Workers resolve the path once when they start, so save and check what it names now; a
            # definition that changes with the link's destination is what restarts them onto the new file.
            try:
                resolved = str(Path(path).resolve())
                resolved.encode()
            except UnicodeEncodeError:
                raise FleetError("A target list's path must be valid UTF-8") from None
            # The worker strips the path it is given, so it would read a different file.
            if resolved != resolved.strip():
                raise FleetError("A target list's path begins or ends with whitespace; rename the file")
            text = load(resolved)
            if resolved != path:
                notes.append(f"{path} resolves to {resolved}; workers read that file, so set the list again after repointing the link")
            path = resolved
        else:
            try:
                valid = isinstance(text, str) and len(text.encode("utf-8")) <= LIMIT
            except UnicodeEncodeError:
                valid = False
            if not valid:
                raise FleetError(f"A transferred target list is UTF-8 text of at most {LIMIT // 1024} KiB")
        parse(text)  # Strict canonical validation before any persistent write.
        if path is None:
            write(stored(), text)
        config = update_host(roadmap_targets=path or str(stored()))
    result = describe(config, workers)
    if config.get("roadmap_targets") != before and config["workers_enabled"]:
        notes.append("Worker definitions follow on the controller's next pass; the worker manager restarts a worker whose definition changes")
    result["notes"] += notes
    return result


def summary_lines(report):
    if report.get("error"):
        return [f"Target list unusable: {report['error']}"]
    count = "{items} items in {n} areas: {done} done, {inflight} in flight, {eligible} eligible, {blocked} blocked"
    lines = ["Target list: " + count.format(n=len(report["areas"]), **report), "  " + FILE_ONLY]
    if report["rounds"]:
        lines.append("  Rounds in progress: " + ", ".join(f"{r['worker']} {r['item']}" for r in report["rounds"]))
    return lines


def display(value):
    if "findings" in value:
        for finding in value["findings"]:
            print(finding["detail"])
        print(f"{value['corrections']} line(s) " + (f"rewritten in {value['roadmap_targets']}" if value["applied"]
              else "would change" + (" (pass --apply)" if value["corrections"] else "")))
        if value["applied"] and value["transferred"]:
            print("The host's copy changed; make the same edits in the file you transfer")
        return
    if not value["roadmap_targets"]:
        print("Target list: none; workers choose their own roadmap areas")
        return
    print("Target list file: " + value["roadmap_targets"] + (" (transferred copy)" if value["transferred"] else " (on the host)"))
    report = value["list"]
    for line in summary_lines(report):
        print(line)
    for area, row in ({} if report.get("error") else report["areas"]).items():
        print(f"  {area}: {row['done']} done, {row['inflight']} in flight, {row['eligible']} eligible, {row['blocked']} blocked")
    for note in value["notes"]:
        print("Note: " + note)
