"""Follow prepared reports over persistent SSH streams; never run collectors."""

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import threading
import time

from . import activity, host, remote
from .store import FleetError, atomic, config_dir, read, state_dir

MAX_FRAME = 2 * 1024 * 1024


def signature(machine):
    group = activity.activity_root() / "groups" / (hashlib.sha256(machine.encode()).hexdigest() + ".json")
    paths = [config_dir() / "host.json", state_dir() / "status.json", state_dir() / "ci-overview.json", state_dir() / 'lean-statistics-report.json',
             group, activity.activity_root() / "preparation-error.json"]
    if not group.exists():
        paths.append(activity.activity_root() / "summary.json")
    result = []
    for path in paths:
        try:
            st = path.stat()
            result.append((st.st_ino, st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            result.append(None)
    return tuple(result)


def watch_cached(expected, emit, stop):
    previous = previous_frame = None
    sent = time.monotonic()
    while not stop.is_set():
        current = signature(expected)
        if current != previous:
            try:
                frame = dict(ok=True, result=host.rpc(dict(action="status", expected_machine_id=expected)))
            except (FleetError, OSError, ValueError) as error:
                frame = dict(ok=False, error=str(error))
            if frame != previous_frame:
                emit(frame)
                previous_frame = frame
                sent = time.monotonic()
            previous = current
        if time.monotonic() - sent >= 15:
            emit(dict(heartbeat=True))
            sent = time.monotonic()
        stop.wait(1)


def serve(request):
    if not isinstance(request, dict):
        raise FleetError("Invalid live status request")
    expected = request.get("expected_machine_id")
    if not isinstance(expected, str) or not expected:
        raise FleetError("Live status requires an expected machine identity")
    try:
        watch_cached(expected, lambda frame: print(json.dumps(frame, separators=(",", ":")), flush=True),
                     threading.Event())
    except (BrokenPipeError, KeyboardInterrupt):
        return


def receive(entry, emit, stop, processes, process_lock):
    """One reader per machine, with bounded reconnect backoff."""
    delay = 2
    while not stop.is_set():
        process = None
        try:
            if entry["target"] == "local":
                watch_cached(entry["machine_id"], emit, stop)
                return
            remote.validate_target(entry["target"])
            process = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                 "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2", "--", entry["target"],
                 '"$HOME/.local/bin/tauceti-fleet" _watch-status'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
            with process_lock:
                processes.add(process)
            process.stdin.write(json.dumps(dict(expected_machine_id=entry["machine_id"])) + "\n")
            process.stdin.close()
            while not stop.is_set():
                line = process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    break
                if len(line) > MAX_FRAME or not line.endswith("\n"):
                    raise FleetError("Live status response exceeds limit")
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise FleetError("Invalid live status frame")
                if frame.get("ok"):
                    if not isinstance(frame.get("result"), dict) or frame["result"].get("machine_id") != entry["machine_id"]:
                        raise FleetError("Host identity differs from inventory")
                    delay = 2
                emit(frame)
            if not stop.is_set():
                emit(dict(ok=False, error="Live status disconnected; reconnecting (older hosts need a Fleet update)"))
        except (FleetError, OSError, ValueError) as error:
            if not stop.is_set():
                emit(dict(ok=False, error=str(error)))
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()
                with process_lock:
                    processes.discard(process)
        if stop.wait(delay):
            return
        delay = min(30, delay * 2)


def apply_frame(entry, frame, previous):
    if frame.get("heartbeat"):
        return previous
    if frame.get("ok"):
        value = frame.get("result")
        if not isinstance(value, dict) or value.get("machine_id") != entry["machine_id"]:
            raise FleetError("Host identity differs from inventory")
        atomic(config_dir() / "status-cache" / (entry["machine_id"] + ".json"),
               dict(collected_at=time.time(), value=value))
        return dict(value, collected_at=time.time())
    return dict(previous, offline=True, error=frame.get("error", "Live status unavailable"),
                collected_at=previous.get("collected_at", time.time()))


def screen_view(screen, update, render, results):
    import curses

    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    screen.keypad(True)
    screen.timeout(200)
    top = left = 0
    dirty = True
    lines = []
    size = None
    next_clock = 0
    while True:
        changed = update()
        shape = screen.getmaxyx()
        if changed or time.monotonic() >= next_clock:
            lines = render(results).splitlines()
            next_clock = time.monotonic() + 10
            dirty = True
        if shape != size:
            dirty = True
            size = shape
        height, width = shape
        page = max(1, height - 2)
        top = min(top, max(0, len(lines) - page))
        if dirty:
            screen.erase()
            online = sum(not value.get("offline") and not value.get("error") for value in results.values())
            title = f"TauCeti live status {online}/{len(results)} | q quit | arrows / PgUp / PgDn scroll"
            with contextlib.suppress(curses.error):
                screen.addnstr(0, 0, title, max(0, width - 1), curses.A_REVERSE)
            for row, line in enumerate(lines[top:top + page], 1):
                with contextlib.suppress(curses.error):
                    screen.addnstr(row, 0, line[left:], max(0, width - 1))
            footer = f"Lines {top + 1}-{min(len(lines), top + page)}/{len(lines)} | cached reports; Ctrl-C also quits"
            with contextlib.suppress(curses.error):
                screen.addnstr(max(0, height - 1), 0, footer, max(0, width - 1), curses.A_REVERSE)
            screen.refresh()
            dirty = False
        key = screen.getch()
        if key in (ord("q"), ord("Q"), 3):
            return
        if key in (curses.KEY_DOWN, ord("j")):
            top += 1
        elif key in (curses.KEY_UP, ord("k")):
            top = max(0, top - 1)
        elif key in (curses.KEY_NPAGE, ord(" ")):
            top += page
        elif key == curses.KEY_PPAGE:
            top = max(0, top - page)
        elif key in (curses.KEY_HOME, ord("g")):
            top = 0
        elif key in (curses.KEY_END, ord("G")):
            top = max(0, len(lines) - page)
        elif key in (curses.KEY_RIGHT, ord("l")):
            left += 4
        elif key in (curses.KEY_LEFT, ord("h")):
            left = max(0, left - 4)
        elif key != curses.KEY_RESIZE:
            continue
        dirty = True


def run(selected, render, *, json_output=False):
    pending, pending_lock = {}, threading.Lock()
    stop = threading.Event()
    processes, process_lock = set(), threading.Lock()
    results, groups = {}, {}
    for name, entry in selected.items():
        saved = read(config_dir() / "status-cache" / (entry["machine_id"] + ".json"), {})
        if (not isinstance(saved, dict) or not isinstance(saved.get("value", {}), dict)
                or saved.get("value", {}).get("machine_id") not in (None, entry["machine_id"])):
            saved = {}
        results[name] = dict(saved.get("value", {}), offline=True, error="Connecting to live reports",
                             collected_at=saved.get("collected_at", time.time()))
        groups.setdefault(entry["machine_id"], []).append(name)

    def publish(name, frame):
        if frame.get("heartbeat"):
            return
        with pending_lock:
            previous = pending.get(name, {})
            if not frame.get("ok"):
                last_good = previous if previous.get("ok") else previous.get("_last_good")
                if last_good:
                    frame = dict(frame, _last_good=last_good)
            pending[name] = frame

    def update():
        with pending_lock:
            updates = list(pending.items())
            pending.clear()
        for name, frame in updates:
            entry = selected[name]
            previous = results[name]
            if frame.get("_last_good"):
                previous = apply_frame(entry, frame["_last_good"], previous)
            value = apply_frame(entry, frame, previous)
            for alias in groups[entry["machine_id"]]:
                results[alias] = value
        return bool(updates)

    threads = []
    try:
        for aliases in groups.values():
            name = aliases[0]
            thread = threading.Thread(target=receive,
                                      args=(selected[name], lambda frame, n=name: publish(n, frame),
                                            stop, processes, process_lock), daemon=True)
            thread.start()
            threads.append(thread)
        if not json_output and sys.stdin.isatty() and sys.stdout.isatty():
            import curses

            curses.wrapper(screen_view, update, render, results)
        else:
            first = True
            while not stop.is_set():
                if update() or first:
                    text = render(results)
                    print(json.dumps(json.loads(text), separators=(",", ":")) if json_output else text, flush=True)
                    first = False
                stop.wait(.2)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        stop.set()
        with process_lock:
            for process in processes:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
        for thread in threads:
            thread.join(timeout=3)
    return 0


def render_with_cli(argv, results):
    from .cli import main

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        main(argv, _status_results=results)
    return output.getvalue()
