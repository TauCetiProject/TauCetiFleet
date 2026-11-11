"""Operator target lists: which list the authoring workers follow and what this host can tell about it.

The worker owns the file format, the selection rules and the pull request overlay
(TauCetiWorker docs/target-lists.md). This module stores or locates the file, checks it
against files already on the host, and never makes a GitHub request.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from . import capabilities
from .process import executable, run
from .store import FleetError, config_dir, host_config, read, update_host

MARKER = re.compile(r"<!--\s*tauceti-targets:v1\s*-->")
HEADING = re.compile(r"^##\s+(.*?)\s*$")
ITEM = re.compile(r"^(\s*-\s*\[)([ xX~])(\]\s*)(.*?)\s*$")
SLUG = re.compile(r"`([^`]+)`")
CLAUSES = re.compile(r"\(([^()]*)\)\s*$")
MARKS = {" ": "open", "~": "inflight", "x": "done", "X": "done"}
STATES = ("done", "inflight", "eligible", "blocked")
# A round's verification reports `verifying`, then `working`, while the round still holds its item.
ACTIVE = ("running", "surveying", "verifying", "working")
# A transferred list travels inside the 1 MiB host request, where JSON escapes can double its size.
LIMIT = 256 * 1024
REPOSITORY = "TauCetiProject/TauCeti"
ROADMAP = "TauCetiProject/TauCetiRoadmap"
FILE_ONLY = "Counted from the file's marks and this host's rounds; workers also count open and merged pull requests"
RESTART_MANAGER = (
    "on the host run `systemctl --user restart tauceti-fleet-workers.service`, then set the list again. "
    "Workers in their own scopes keep running and the new manager adopts them; a worker whose status shows "
    "degraded isolation runs inside the manager's service and its round ends with it"
)


def running_manager():
    """The runtime the active worker manager started from: None when no manager runs, "" when it cannot be told."""
    from . import host

    # A live update installs a new runtime but leaves a running manager on the one it started with.
    if run(["systemctl", "--user", "is-active", "--quiet", "tauceti-fleet-workers.service"], check=False).returncode:
        return None
    return host.running_manager().get("binary") or ""


def stored():
    return config_dir() / "roadmap-targets.md"


def load(path):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > LIMIT:
            raise FleetError(f"Target list {path} exceeds {LIMIT // 1024} KiB")
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
    """The list as the worker reads it: items in file order, plus the item lines it skips."""
    if not MARKER.search(text):
        raise FleetError("Not a target list: the file lacks the <!-- tauceti-targets:v1 --> marker")
    areas, items, malformed, area = [], [], [], None
    for number, line in enumerate(text.splitlines(), 1):
        heading = HEADING.match(line)
        if heading:
            area = heading.group(1) if re.fullmatch(r"[A-Za-z0-9_-]+", heading.group(1)) else None
            if area and area not in areas:
                areas.append(area)
        elif area and re.match(r"\s*-\s*\[", line):
            match = ITEM.match(line)
            slug = SLUG.search(match.group(4)) if match else None
            if not slug:
                malformed.append(number)
                continue
            group = CLAUSES.search(match.group(4)[slug.end():])
            clauses, needs = {}, []
            for clause in group.group(1).split(";") if group else []:
                key, _, value = clause.partition(":")
                key, value = key.strip().lower(), value.strip()
                # As in the worker: the last `needs:` naming slugs counts, and every `partial:` does.
                if key == "needs":
                    if value.lower() != "none":
                        needs = [s.strip().strip("`").strip() for s in value.split(",") if s.strip("` \t")]
                else:
                    clauses[key] = "; ".join(filter(None, (clauses.get(key), value)))
            items.append(dict(area=area, slug=slug.group(1).strip(), status=MARKS[match.group(2)], needs=needs,
                              line=number, clauses=clauses))
    return dict(areas=areas, items=items, malformed=malformed)


def holders(workers):
    """Rounds in progress under the list; a waiting or backing-off slot keeps its last item in its status file."""
    return [dict(worker=w.get("id"), item=w["roadmap_target"]) for w in workers or []
            if w.get("state") in ACTIVE and isinstance(w.get("roadmap_target"), str) and w["roadmap_target"]]


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
            # A slug the list never defines blocks nothing, as in the worker.
            met = all(first.get(need, dict(status="done"))["status"] == "done" for need in item["needs"])
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
        return dict(path=path, **summarize(parse(load(path)), workers))
    except (FleetError, TypeError) as exc:
        return dict(path=path, error=str(exc))


def roadmap_areas():
    """(active, archived, fetched_at) from the workers' shared roadmap mirror, or None before any worker fetched it."""
    shared = Path(os.environ.get("TAUCETI_GITHUB_SHARED_DIR", Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "tauceti/github-shared"))
    base = Path(os.environ.get("TAUCETI_SHARED_REPOSITORIES_DIR", shared / "repositories")) / hashlib.sha256(ROADMAP.encode()).hexdigest()
    try:
        state = read(base / "state.json", {})
        sha = state.get("sha") if isinstance(state, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return None
        listed = run(["git", "--git-dir", base / "mirror.git", "ls-tree", "-d", "--name-only", sha, "TauCetiRoadmap/", "Completed/"], check=False)
    except FleetError:
        return None
    if listed.returncode:
        return None
    names = [line.split("/", 1) for line in listed.stdout.splitlines() if "/" in line]
    return ({name for top, name in names if top == "TauCetiRoadmap"}, {name for top, name in names if top == "Completed"},
            state.get("fetched") or 0)


def area_problems(listing):
    """Headings a worker would author against without a roadmap; None when this host has no mirror to check."""
    known = roadmap_areas()
    if known is None:
        return None
    active, archived, fetched = known
    unfinished = {area: sum(i["area"] == area and i["status"] != "done" for i in listing["items"]) for area in listing["areas"]}
    missing = [area for area in listing["areas"] if area not in active and area not in archived]
    closed = [f"{area} ({unfinished[area]} unfinished)" for area in listing["areas"]
              if area not in active and area in archived and unfinished[area]]
    age = "the workers' roadmap mirror" + (f", fetched {max(0, int(time.time() - fetched)) // 60} min ago" if fetched else "")
    problems = []
    if missing:
        problems.append(f"No roadmap directory for {', '.join(missing)} in {age}")
    if closed:
        problems.append(f"Unfinished items under archived roadmaps: {', '.join(closed)}")
    return problems


def structure_problems(listing):
    lines = {}
    for item in listing["items"]:
        lines.setdefault(item["slug"], []).append(item["line"])
    repeated = [f"`{slug}` (lines {', '.join(map(str, where))})" for slug, where in lines.items() if len(where) > 1]
    undefined = sorted({need for item in listing["items"] for need in item["needs"]} - set(lines))
    return (["Slugs defined more than once: " + ", ".join(repeated)] if repeated else [],
            ["`needs:` names slugs the list never defines, which block nothing: " + ", ".join(undefined)] if undefined else [])


def landings(machine):
    """This host's pull requests as the worker's prepared activity records them, read without GitHub."""
    worker = executable("tauceti")
    if not worker:
        raise FleetError("No worker runtime is installed")
    result = run([worker, "activity", "--inventory", "--group", machine], check=False)
    if result.returncode:
        raise FleetError("tauceti activity --inventory failed")
    return json.loads(result.stdout)["prs"]


def rewritten(line, mark, clause=None):
    """An item line with a new mark and its `in flight:` clause replaced by `clause` or dropped."""
    ending = line[len(line.rstrip("\r\n")):]
    match = ITEM.match(line.rstrip("\r\n"))
    body, group = match.group(4), CLAUSES.search(match.group(4))
    kept = [c.strip() for c in group.group(1).split(";") if c.strip()]
    kept = [clause if c.partition(":")[0].strip().lower() == "in flight" else c for c in kept]
    kept = "; ".join(c for c in kept if c)
    return match.group(1) + mark + match.group(3) + body[:group.start()].rstrip() + (f" ({kept})" if kept else "") + ending


def audit(config, text, workers):
    """Where the file disagrees with this host's files, and the lines `apply` would rewrite."""
    listing = parse(text)
    findings = []

    def note(kind, detail, **more):
        findings.append(dict(kind=kind, detail=detail, **more))

    for number in listing["malformed"]:
        note("format", f"Line {number} is an item line without a `slug`; workers skip it", line=number)
    repeated, undefined = structure_problems(listing)
    for detail in repeated + undefined:
        note("format", detail)
    areas = area_problems(listing)
    for detail in areas or []:
        note("area", detail)
    if areas is None:
        note("area", "Areas not checked: this host has no roadmap mirror yet")
    known = {item["area"] + "/" + item["slug"]: item for item in listing["items"]}
    for held in holders(workers):
        item = known.get(held["item"])
        if item is None or item["status"] == "done":
            note("round", f"{held['worker']} is authoring {held['item']}, which the file "
                 + ("marks done" if item else "does not list"), item=held["item"])
    try:
        prs = {p["number"]: p for p in landings(config["machine_id"]) if p["repo"] == REPOSITORY}
    except (FleetError, ValueError, KeyError, TypeError):
        prs = None
        note("pull-requests", "This host's prepared pull request inventory is unreadable; in-flight items not checked")
    lines = text.splitlines(keepends=True)
    for item in listing["items"] if prs is not None else []:
        named = re.search(r"#(\d+)", item["clauses"].get("in flight", ""))
        pr = prs.get(int(named.group(1))) if named and item["status"] == "inflight" else None
        if not pr or pr.get("state") not in ("CLOSED", "MERGED"):
            continue
        number, name, old = pr["number"], item["area"] + "/" + item["slug"], lines[item["line"] - 1]
        if pr.get("state") != "MERGED" and pr.get("landed") is None:
            note("closed", f"{name}: #{number} closed with no landing on record (a recent merge may not be recorded yet); "
                 "mark it [ ] to author it again or [x] if main has it", item=name, line=item["line"])
            continue
        partial = f"#{number}" in re.findall(r"#\d+", item["clauses"].get("partial", ""))
        new = rewritten(old, " ") if partial else rewritten(old, "x", f"landed: #{number}")
        detail = (f"{name}: #{number} landed and the item records it as partial -> [ ]" if partial else
                  f"{name}: #{number} landed -> [x] (this host cannot read whether its marker said partial; "
                  f"if it did, record `partial: #{number}` and mark it [ ] instead)")
        note("landed", detail, item=name, line=item["line"],
             correction=dict(before=old.rstrip("\r\n"), after=new.rstrip("\r\n")))
        lines[item["line"] - 1] = new
    return findings, "".join(lines)


def merge3(base, ours, theirs):
    """Two edits of `base` merged by `git merge-file`, or None when they touch the same lines."""
    with tempfile.TemporaryDirectory() as directory:
        files = []
        for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
            (Path(directory) / name).write_text(text)
            files.append(Path(directory) / name)
        merged = run(["git", "merge-file", "-p", "-q", *files], check=False)
    return merged.stdout if merged.returncode == 0 else None


def describe(config, workers):
    path = config.get("roadmap_targets")
    notes = []
    if path and (config.get("roadmap_focus") or {}).get("percent"):
        notes.append(f"Roadmap focus ({config['roadmap_focus']['percent']}%) is not applied: workers under a target list take no focus")
    return dict(roadmap_targets=path or None, transferred=bool(path) and Path(path) == stored(),
                list=status(config, workers), notes=notes)


def configure(payload, workers):
    config = host_config()
    before = config.get("roadmap_targets")
    text, path = payload.get("text"), payload.get("path")
    if payload.get("audit"):
        if not before:
            raise FleetError("This host has no target list; set one with targets HOST --file PATH")
        # Workers resolve the list's path when they start, so a symlink must keep pointing at the file they read.
        destination = Path(before).resolve()
        text = load(destination)
        findings, corrected = audit(config, text, workers)
        applied = bool(payload.get("apply")) and corrected != text
        if applied:
            if Path(before).resolve() != destination:
                raise FleetError(f"{before} named another file when the audit ended; nothing written, audit again")
            # The inventory lookup takes a while; keep an edit made meanwhile, as the worker's own curation does.
            current = load(destination)
            merged = corrected if current == text else merge3(text, corrected, current)
            if merged is None:
                raise FleetError(f"{before} changed on the corrected lines during the audit; nothing written, audit again")
            write(destination, merged, stat.S_IMODE(destination.stat().st_mode))
        return dict(roadmap_targets=before, transferred=Path(before) == stored(), findings=findings,
                    corrections=sum("correction" in f for f in findings), applied=applied)
    notes = []
    if payload.get("clear"):
        # The stored copy stays: a round starting before the definitions change still reads it.
        config = update_host(roadmap_targets=None)
    elif text is not None or path is not None:
        if text is not None and path is not None:
            raise FleetError("Transfer a list or name one on the host, not both")
        if capabilities.worker_protocol().get("roadmap_targets") != 1:
            raise FleetError("The installed TauCetiWorker has no target-list support, and its manager loads no worker from "
                             "definitions that name a list; install a release with roadmap_targets protocol support first")
        manager = running_manager()
        if manager == "":
            raise FleetError("Cannot tell which worker runtime the running manager uses; " + RESTART_MANAGER)
        if manager and capabilities.worker_protocol(manager).get("roadmap_targets") != 1:
            raise FleetError("The running worker manager started from a TauCetiWorker without target-list support and would "
                             "reject definitions that name a list; " + RESTART_MANAGER)
        if path is not None:
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise FleetError("A target list on the host is named by its absolute path")
            try:
                path.encode()
            except UnicodeEncodeError:
                raise FleetError("A target list's path must be valid UTF-8") from None
            text = load(path)
            # Workers resolve the path once when they start, so save what it names now; a definition
            # that changes with the link's destination is what restarts them onto the new file.
            resolved = str(Path(path).resolve())
            if resolved != path:
                notes.append(f"{path} resolves to {resolved}; workers read that file, so set the list again after repointing the link")
            path = resolved
        elif not isinstance(text, str) or len(text.encode()) > LIMIT:
            raise FleetError(f"A transferred target list is text of at most {LIMIT // 1024} KiB")
        listing = parse(text)
        repeated, undefined = structure_problems(listing)
        notes += undefined
        areas = area_problems(listing)
        if repeated or areas:
            raise FleetError("; ".join(repeated + (areas or [])))
        if areas is None:
            notes.append("Areas not checked: this host has no roadmap mirror yet")
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
