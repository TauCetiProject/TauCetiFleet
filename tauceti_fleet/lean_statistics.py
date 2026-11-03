"""Bounded, optional compiler timing telemetry. Never used for admission decisions."""

import bisect
import errno
import json
import math
import resource
import selectors
import socket
import struct
import time
from pathlib import Path

BINS = (
    0.01,
    0.025,
    0.05,
    0.1,
    0.2,
    0.25,
    0.5,
    0.75,
    1,
    1.5,
    2,
    2.5,
    3,
    4,
    5,
    6,
    8,
    10,
    12,
    15,
    20,
    25,
    30,
    45,
    60,
    90,
    120,
    180,
    240,
    300,
    450,
    600,
    900,
    1200,
    1800,
    2400,
    3600,
    5400,
    7200,
    10800,
    21600,
    43200,
    86400,
)
REASONS = ("cpu", "ram", "cpu+ram", "signal", "admission")


def empty():
    return dict(
        version=1,
        since=time.time(),
        observed=0,
        admitted=0,
        waited=0,
        completed=0,
        abandoned=0,
        lost=0,
        abandoned_wait_max=0.0,
        wait_seconds=0.0,
        run_seconds=0.0,
        probe_seconds=0.0,
        waits=[0] * (len(BINS) + 1),
        wait_max=0.0,
        reasons={k: 0.0 for k in REASONS},
        waiting_now=0,
        running_now=0,
        oldest_wait=0.0,
    )


def valid(value):
    if not isinstance(value, dict) or set(value) != set(empty()) or value.get("version") != 1:
        return False
    for key in ("observed", "admitted", "waited", "completed", "abandoned", "lost", "waiting_now", "running_now"):
        if type(value[key]) is not int or not 0 <= value[key] <= 10**12:
            return False
    if (
        not isinstance(value["waits"], list)
        or len(value["waits"]) != len(BINS) + 1
        or any(type(x) is not int or not 0 <= x <= 10**12 for x in value["waits"])
    ):
        return False
    if not isinstance(value["reasons"], dict) or set(value["reasons"]) != set(REASONS):
        return False
    if (
        value["admitted"] > value["observed"]
        or value["waited"] > value["observed"]
        or value["completed"] > value["admitted"]
        or sum(value["waits"]) > min(value["waited"], value["admitted"])
        or value["completed"] + value["abandoned"] + value["lost"] > value["observed"]
        or value["waiting_now"] + value["running_now"] > value["observed"]
    ):
        return False
    numbers = [
        value[k]
        for k in (
            "since",
            "wait_seconds",
            "run_seconds",
            "probe_seconds",
            "wait_max",
            "oldest_wait",
            "abandoned_wait_max",
        )
    ]
    return all(
        type(x) in (int, float) and math.isfinite(x) and 0 <= x <= 10**15
        for x in [*numbers, *value["reasons"].values()]
    )


def merge(rows):
    result = empty()
    for row in rows:
        if not valid(row):
            continue
        result["since"] = min(result["since"], row["since"])
        for key in (
            "observed",
            "admitted",
            "waited",
            "completed",
            "abandoned",
            "lost",
            "wait_seconds",
            "run_seconds",
            "probe_seconds",
            "waiting_now",
            "running_now",
        ):
            result[key] += row[key]
        for key in ("wait_max", "oldest_wait", "abandoned_wait_max"):
            result[key] = max(result[key], row[key])
        result["waits"] = [a + b for a, b in zip(result["waits"], row["waits"], strict=True)]
        for key in REASONS:
            result["reasons"][key] += row["reasons"][key]
    for key in ("observed", "admitted", "waited", "completed", "abandoned", "lost", "waiting_now", "running_now"):
        result[key] = min(result[key], 10**12)
    count = sum(result["waits"])
    if count > 10**12:
        result["waits"] = [int(n * 10**12 // count) for n in result["waits"]]
    outcomes = sum(result[k] for k in ("completed", "abandoned", "lost"))
    if outcomes > result["observed"]:
        for key in ("completed", "abandoned", "lost"):
            result[key] = int(result[key] * result["observed"] // outcomes)
    active = result["waiting_now"] + result["running_now"]
    if active > result["observed"]:
        for key in ("waiting_now", "running_now"):
            result[key] = int(result[key] * result["observed"] // active)
    for key in ("wait_seconds", "run_seconds", "probe_seconds", "wait_max", "oldest_wait", "abandoned_wait_max"):
        result[key] = min(result[key], 10**15)
    result["reasons"] = {k: min(v, 10**15) for k, v in result["reasons"].items()}
    return result


def percentile(row, q):
    count = sum(row["waits"])
    if not count:
        return None
    target = math.ceil(q * count)
    accumulated = 0
    for bound, n in zip((*BINS, math.inf), row["waits"], strict=True):
        accumulated += n
        if accumulated >= target:
            return bound


class Collector:
    """A socket survives exec; EOF measures the compiler lifetime without a parent wrapper."""

    def __init__(self, path, *, now=time.monotonic, initial=None):
        self.path = Path(path)
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft == resource.RLIM_INFINITY:
            soft = 2**31
        desired = 16384 if hard == resource.RLIM_INFINITY else min(16384, hard)
        if soft < desired:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
                soft = desired
            except (OSError, ValueError):
                pass
        self.max_clients = min(4096, max(0, (soft - 256) // 2))
        self.now = now
        self.selector = selectors.DefaultSelector()
        self.clients = {}
        self.totals = json.loads(json.dumps(initial)) if valid(initial) else empty()
        unfinished = max(
            0, self.totals["observed"] - self.totals["completed"] - self.totals["abandoned"] - self.totals["lost"]
        )
        lost = min(unfinished, self.totals["running_now"])
        self.totals["lost"] += lost
        self.totals["abandoned"] += unfinished - lost
        self.totals["abandoned_wait_max"] = max(self.totals["abandoned_wait_max"], self.totals["oldest_wait"])
        self.totals["waiting_now"] = self.totals["running_now"] = 0
        self.totals["oldest_wait"] = 0.0
        self.listener = None
        self.accept_after = 0.0
        self.listener_registered = False

    def listen(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.setblocking(False)
        self.listener.bind(str(self.path))
        self.path.chmod(0o666 if str(self.path).startswith("/usr/local/") else 0o600)
        self.listener.listen(256)
        self.selector.register(self.listener, selectors.EVENT_READ, None)
        self.listener_registered = True

    def accept(self, client):
        if len(self.clients) >= self.max_clients:
            client.close()
            return
        client.setblocking(False)
        self.clients[client] = dict(
            at=self.now(),
            phase="prepare",
            buffer=b"",
            wait=0.0,
            started=False,
            had_wait=False,
            wait_since=None,
            seen=False,
            pid=struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0],
        )
        self.totals["observed"] += 1
        self.selector.register(client, selectors.EVENT_READ, self.clients[client])

    def account(self, state, at):
        duration = max(0.0, at - state["at"])
        phase = state["phase"]
        if phase in REASONS:
            self.totals["wait_seconds"] += duration
            self.totals["reasons"][phase] += duration
            state["wait"] += duration
        elif phase == "run":
            self.totals["run_seconds"] += duration
        elif phase == "probe":
            self.totals["probe_seconds"] += duration
        state["at"] = at

    def event(self, state, message):
        phase, stamp = message.decode("ascii").split(":", 1)
        at = float(stamp)
        if not state["seen"]:
            state["at"] = min(at, state["at"])
            state["seen"] = True
        if (
            phase not in (*REASONS, "prepare", "probe", "run")
            or not math.isfinite(at)
            or at < state["at"] - 0.01
            or abs(self.now() - at) > 5
        ):
            raise ValueError("invalid compiler observation")
        at = max(at, state["at"])
        self.account(state, at)
        if phase in REASONS and not state["had_wait"]:
            self.totals["waited"] += 1
            state["had_wait"] = True
        if phase in REASONS and state["wait_since"] is None:
            state["wait_since"] = at
        if phase == "run":
            state["wait_since"] = None
        if phase == "run" and not state["started"]:
            self.totals["admitted"] += 1
            state["started"] = True
            if state["had_wait"]:
                self.totals["waits"][bisect.bisect_left(BINS, state["wait"])] += 1
                self.totals["wait_max"] = max(self.totals["wait_max"], state["wait"])
        state["phase"] = phase

    def finish(self, client):
        state = self.clients.pop(client)
        self.account(state, self.now())
        self.totals["completed" if state["started"] else "abandoned"] += 1
        if not state["started"]:
            self.totals["abandoned_wait_max"] = max(self.totals["abandoned_wait_max"], state["wait"])
        self.selector.unregister(client)
        client.close()

    def poll(self):
        if self.listener and not self.listener_registered and self.now() >= self.accept_after:
            self.selector.register(self.listener, selectors.EVENT_READ, None)
            self.listener_registered = True
        for key, _ in self.selector.select(0):
            if key.data is None:
                for _ in range(64):
                    try:
                        client, _ = self.listener.accept()
                        self.accept(client)
                    except BlockingIOError:
                        break
                    except OSError as error:
                        if error.errno in (errno.EMFILE, errno.ENFILE):
                            self.selector.unregister(self.listener)
                            self.listener_registered = False
                            self.accept_after = self.now() + 1
                            break
                        raise
                continue
            client, state = key.fileobj, key.data
            try:
                chunk = client.recv(4096)
                if not chunk:
                    self.finish(client)
                    continue
                state["buffer"] += chunk
                if len(state["buffer"]) > 8192:
                    raise ValueError("oversized compiler observation")
                while b"\n" in state["buffer"]:
                    line, state["buffer"] = state["buffer"].split(b"\n", 1)
                    self.event(state, line)
            except BlockingIOError:
                pass
            except (OSError, ValueError, UnicodeError):
                if client in self.clients:
                    self.finish(client)

    def snapshot(self):
        now = self.now()
        value = json.loads(json.dumps(self.totals))
        for state in self.clients.values():
            elapsed = max(0.0, now - state["at"])
            if state["phase"] in REASONS:
                value["wait_seconds"] += elapsed
                value["reasons"][state["phase"]] += elapsed
            elif state["phase"] == "run":
                value["run_seconds"] += elapsed
            elif state["phase"] == "probe":
                value["probe_seconds"] += elapsed
        value["waiting_now"] = sum(s["phase"] in REASONS for s in self.clients.values())
        value["running_now"] = sum(s["phase"] == "run" for s in self.clients.values())
        value["oldest_wait"] = max(
            (now - s["wait_since"] for s in self.clients.values() if s["wait_since"] is not None), default=0.0
        )
        return value

    def close(self):
        for client in list(self.clients):
            self.finish(client)
        if self.listener:
            if self.listener_registered:
                self.selector.unregister(self.listener)
            self.listener.close()
            self.path.unlink(missing_ok=True)
        self.selector.close()


def duration(value):
    if value is None:
        return "—"
    if math.isinf(value):
        return ">1d"
    if value < 60:
        return f"{value:.2g}s"
    if value < 3600:
        return f"{value / 60:.2g}m"
    if value < 86400:
        return f"{value / 3600:.2g}h"
    return f"{value / 86400:.2g}d"


def display(value):
    if not isinstance(value, dict):
        return
    rows = [value.get(k, empty()) for k in ("workers", "ci")]
    age = max(0.0, time.time() - value.get("at", 0))
    stale = age > 10
    if stale:
        print(f"Lean timing observation is {duration(age)} old; live counts unavailable")
    if value.get("worker_collector_available") is False:
        print("Lean worker timing collector unavailable; retaining previous totals")
    legacy = value.get("legacy", {})
    if legacy.get("waiting") or legacy.get("running"):
        print(
            f"Untimed Lean processes: {legacy.get('waiting', 0)} sleeping wrappers, "
            f"{legacy.get('running', 0)} running compilers; oldest observed wait ≥{duration(legacy.get('oldest_observed', 0))}"
        )
        total = legacy.get("sampled_wait_seconds", 0) + legacy.get("sampled_run_seconds", 0)
        if total:
            print(
                f"  Existing processes' sampled wait share: {100 * legacy.get('sampled_wait_seconds', 0) / total:.1f}% (5s samples)"
            )
    if not any(valid(r) and r["observed"] for r in rows):
        print("Lean throttling: no timed compiler invocations yet")
        return
    rows = [r if valid(r) else empty() for r in rows]
    print("Lean throttling (compiler elapsed time; quantiles approximate):")
    print(f"  {'':24s}{'Workers':>16s}{'CI':>16s}")

    def line(label, values):
        print(f"  {label:24s}" + "".join(f"{v:>16s}" for v in values))

    line("Observed invocations", [str(r["observed"]) for r in rows])
    line("Abandoned / lost traces", [f"{r['abandoned']} / {r['lost']}" for r in rows])
    line(
        "Timing since (UTC)",
        [
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(min(time.time(), r["since"]))) if r["observed"] else "—"
            for r in rows
        ],
    )
    line(
        "Waiting / running now", [f"{r['waiting_now']} / {r['running_now']}" if not stale else "unknown" for r in rows]
    )
    line(
        "Invocations with waits", [f"{100 * r['waited'] / r['observed']:.1f}%" if r["observed"] else "—" for r in rows]
    )
    line(
        "Waiting share",
        [
            f"{100 * r['wait_seconds'] / (r['wait_seconds'] + r['run_seconds']):.1f}%"
            if r["wait_seconds"] + r["run_seconds"]
            else "—"
            for r in rows
        ],
    )
    line("Admitted wait p50 / p95", [f"{duration(percentile(r, 0.5))} / {duration(percentile(r, 0.95))}" for r in rows])
    line(
        "Admitted max / oldest now",
        [f"{duration(r['wait_max'])} / {duration(r['oldest_wait']) if not stale else '?'}" for r in rows],
    )
    line("Abandoned maximum wait", [duration(r["abandoned_wait_max"]) for r in rows])
    line("CPU measurement probes", [duration(r["probe_seconds"]) for r in rows])
    for label, r in zip(("Workers", "CI"), rows, strict=True):
        causes = ", ".join(f"{k} {duration(v)}" for k, v in r["reasons"].items() if v)
        if causes:
            print(f"  {label} wait causes: {causes}")
    print("  Waiting share = wait / (wait + execution); probes excluded; active waits included.")
    print(f"  CI timing coverage: {value.get('telemetry_guests', 0)} / {value.get('connected_guests', 0)} guests")
