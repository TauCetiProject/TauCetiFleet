"""Per-worker rows and recent PR events for status. Reads local files only."""

import json
import os
import re
import time

from .pool_activity import elapsed

TAIL = 64 * 1024
EVENTS = 15
IN_ROUND = ("running", "verifying", "working")
# The worker logs "<local date and time> tauceti: <message>" and opens each round with "→ STAGE: target".
LOG_LINE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) tauceti: (.*)")
ROUND_OPENING = re.compile(r"→ [A-Z-]+: ")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PHRASES = dict(pr_created="opened", branch_push="pushed to", reply="replied on", closure="closed")


def text(value, limit):
    # These strings reach terminals, and log lines quote GitHub content.
    if not isinstance(value, str):
        return None
    return "".join(c if c.isprintable() else " " for c in value).strip()[:limit] or None


def tail(path, limit=TAIL):
    """Complete lines from the last `limit` bytes of a file."""
    with open(path, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        stream.seek(max(0, size - limit))
        lines = stream.read(limit).decode(errors="replace").splitlines()
    return lines[1:] if size > limit else lines


def log_facts(path, opening=None):
    """The last message in a worker's console log, and when its latest round opened if that round is `opening`."""
    last = started = None
    # open() would take an integer as a file descriptor of this process.
    if isinstance(path, str) and path:
        try:
            lines = tail(path)
        except (OSError, ValueError):
            lines = []
        for line in reversed(lines):
            match = LOG_LINE.match(ANSI.sub("", line))
            if not match:
                continue
            last = last or text(match[2], 200)
            if ROUND_OPENING.match(match[2]):
                # A closure after spent repair attempts reports a fix without logging an opening line.
                if not opening or match[2] != opening and not match[2].startswith(opening + "   ["):
                    break
                try:
                    started = time.mktime(time.strptime(match[1], "%Y-%m-%d %H:%M:%S"))
                except (ValueError, OverflowError):
                    pass
                break
    return last, started


def row(value):
    """View fields for one worker, from its status and the end of its console log."""
    detail, target, phase = value.get("detail"), value.get("target"), value.get("phase")
    in_round = bool(phase) and value.get("state") in IN_ROUND
    opening = f"→ {phase.upper()}: {target}" if isinstance(phase, str) and isinstance(target, str) else None
    line, started = log_facts(value.get("log_file"), opening)
    provider = re.match(r"provider=([\w.-]{1,40})", detail) if in_round and isinstance(detail, str) else None
    pr = re.match(r"PR #(\d{1,10})\b", target) if isinstance(target, str) else None
    return dict(
        agent=text(value.get("agent"), 40),
        provider=provider[1] if provider else None,
        pr=int(pr[1]) if pr else None,
        round_started_at=started if in_round else None,
        log_line=line,
    )


def rows(statuses):
    """Worker statuses with the view fields in place of the log path."""
    return [{**{k: v for k, v in status.items() if k != "log_file"}, **row(status)} for status in statuses]


def event(value):
    if not isinstance(value, dict) or value.get("kind") not in ("pr_created", "pr_activity"):
        return None
    at, pr, repository = value.get("at"), value.get("pr"), value.get("repository")
    activity, publication = text(value.get("activity"), 40), value.get("publication")
    if publication not in ("reply", "closure"):
        publication = None
    # Label and metadata writes outnumber every other record and say nothing a reader acts on.
    if value["kind"] == "pr_activity" and (not activity or activity == "github_mutation" and not publication):
        return None
    if type(at) not in (int, float) or not 0 <= at < 1e11 or type(pr) is not int or not 0 < pr < 1e10:
        return None
    if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]{1,100}/[\w.-]{1,100}", repository):
        repository = None
    return dict(at=at, worker=text(value.get("worker"), 80), kind=value["kind"],
                activity=activity if value["kind"] == "pr_activity" else None, publication=publication,
                repository=repository, pr=pr)


def events(path, count=EVENTS):
    """The newest PR events in the workers' journal, oldest first."""
    try:
        lines = tail(path, 2 * TAIL)
    except OSError:
        return []
    found = []
    for line in reversed(lines):
        try:
            value = event(json.loads(line))
        except (ValueError, RecursionError):
            continue
        if value:
            found.append(value)
            if len(found) == count:
                break
    return found[::-1]


def natural(worker):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(worker.get("id", "")))]


def display(status, *, now=None):
    now = time.time() if now is None else now
    workers = sorted((w for w in status.get("workers") or [] if isinstance(w, dict)), key=natural)
    if workers and not any("log_line" in w for w in workers):
        print("Worker rows: not reported by this host's Fleet release")
    elif workers:
        table = [["Worker", "State", "Stage", "Holds", "Agent", "In round", "Last log line"]]
        for worker in workers:
            agent, provider = worker.get("agent"), worker.get("provider")
            pr, started = worker.get("pr"), worker.get("round_started_at")
            # Outside a round and the back-off after a failed one, stage and target are left over.
            held = worker.get("state") in (*IN_ROUND, "backoff")
            table.append([
                str(worker.get("id", "?")),
                str(worker.get("state") or "unknown"),
                str(held and worker.get("phase") or "—"),
                f"#{pr}" if held and pr else str(held and worker.get("target") or "—").split("  ")[0][:40],
                f"{agent}→{provider}" if provider and agent not in (None, provider) else str(provider or agent or "—"),
                elapsed(now - started) if isinstance(started, (int, float)) else "—",
                str(worker.get("log_line") or "—")[:100],
            ])
        widths = [max(len(cells[index]) for cells in table) for index in range(len(table[0]))]
        for cells in table:
            print("  " + "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)).rstrip())
    recent = status.get("worker_events")
    if isinstance(recent, list):
        print("Recent worker events:" + ("" if recent else " none"))
        for value in recent:
            activity = value.get("publication") or value.get("activity") or value.get("kind")
            where = f"{value.get('repository') or ''}#{value.get('pr')}"
            print(f"  {elapsed(now - value.get('at', now)):>6} ago  {value.get('worker') or '?'}  "
                  f"{PHRASES.get(activity) or f'finished {activity} on'} {where}")
