"""Live, read-only dashboard of this host's CI guests and the resources they share.

`tauceti-fleet top` complements `status`: it samples the local kernel every few
seconds (CPU, disk, network, memory and pressure stall information), each CI
guest's QEMU process (CPU, resident memory, overlay size) and the local events log.
It never changes fleet state and makes no GitHub requests.
"""

import json
import os
import select
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

from . import capacity
from .ci_overview import REPOSITORY
from .store import FleetError, host_config, read, state_dir

GIB = 1024**3
RUNS = f"https://github.com/{REPOSITORY}/actions/runs/"
RESET, GREEN, YELLOW, RED, DIM, BOLD = "\033[0m", "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[1m"
PAGE = os.sysconf("SC_PAGE_SIZE")
HZ = os.sysconf("SC_CLK_TCK")


def _output(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


class Host:
    """Sampling of host counters. Devices are detected once, on first use."""

    def __init__(self):
        source = (_output(["df", "--output=source", str(Path.home())]).split() or [""])[-1]
        self.disk = _output(["lsblk", "-no", "PKNAME", source]).strip() or Path(source).name
        route = _output(["ip", "route", "show", "default"]).split()
        self.net = route[route.index("dev") + 1] if "dev" in route else None
        try:
            speed = int(Path(f"/sys/class/net/{self.net}/speed").read_text())
        except (OSError, ValueError):
            speed = 0
        self.net_mbps = speed if speed > 0 else 1000
        self.cgroup = None

    def cpu(self):
        with open("/proc/stat") as stream:
            values = [int(x) for x in stream.readline().split()[1:9]]
        return sum(values), values[3] + values[4]

    def diskstat(self):
        with open("/proc/diskstats") as stream:
            for line in stream:
                fields = line.split()
                if fields[2] == self.disk:
                    return int(fields[5]), int(fields[9]), int(fields[12])  # sectors read, written; ms busy
        return 0, 0, 0

    def netstat(self):
        with open("/proc/net/dev") as stream:
            for line in stream:
                name, _, rest = line.partition(":")
                if name.strip() == self.net:
                    fields = rest.split()
                    return int(fields[0]), int(fields[8])
        return 0, 0

    def guests(self):
        """instance -> (cpu ticks, resident GiB) for each CI guest's QEMU process."""
        found = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as stream:
                    command = stream.read()
                if b"qemu-system" not in command:
                    continue
                part = next((p for p in command.split(b"\0") if b"/vms/" in p), None)
                if part is None:
                    continue
                with open(f"/proc/{pid}/stat") as stream:
                    stat = stream.read().rsplit(")", 1)[1].split()
                instance = part.split(b"/vms/")[1].split(b"/")[0].decode()
                found[instance] = (int(stat[11]) + int(stat[12]), int(stat[21]) * PAGE / GIB)
            except (OSError, IndexError, ValueError):
                continue
        return found

    def ci_slice(self):
        """(current, limit) GiB of the CI slice. Guest overlay pages on tmpfs are charged here too."""
        try:
            if self.cgroup is None:
                path = _output(["systemctl", "--user", "show", "tauceti-fleet-ci.slice", "-p", "ControlGroup", "--value"]).strip()
                self.cgroup = Path("/sys/fs/cgroup" + path) if path else None
            current = int((self.cgroup / "memory.current").read_text()) / GIB
            raw = (self.cgroup / "memory.max").read_text().strip()
            return current, int(raw) / GIB if raw.isdigit() else 0.0
        except (OSError, ValueError, TypeError):
            self.cgroup = None
            return 0.0, 0.0

    def sample(self):
        return dict(t=time.time(), cpu=self.cpu(), disk=self.diskstat(), net=self.netstat(), guests=self.guests())


def pressure(kind, line):
    try:
        with open(f"/proc/pressure/{kind}") as stream:
            return float(stream.read().splitlines()[line].split()[1].split("=")[1])  # avg10
    except (OSError, IndexError, ValueError):
        return 0.0


def meminfo():
    with open("/proc/meminfo") as stream:
        return {line.split(":")[0]: int(line.split()[1]) * 1024 for line in stream}


def bar(percent, width=20):
    percent = max(0.0, min(100.0, percent))
    filled = round(percent / 100 * width)
    color = GREEN if percent < 60 else YELLOW if percent < 85 else RED
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def duration(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m {seconds % 60:02d}s"


def slots():
    paths = [state_dir() / "vm.json"] + sorted((state_dir() / "ci-slots").glob("*/vm.json"),
                                               key=lambda p: int(p.parent.name) if p.parent.name.isdecimal() else 0)
    values = [read(p, {}) for p in paths]
    return sorted((v for v in values if v.get("unit")), key=lambda v: v.get("slot", 0))


def recent_results(window=3600):
    """(jobs, failures, median minutes, last six notable events) from the local events log."""
    path = state_dir() / "events.sqlite"
    if not path.exists():
        return 0, 0, None, []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT data FROM events WHERE kind='ci_result' AND at >= ?", (time.time() - window,)).fetchall()
        recent = connection.execute("SELECT at, kind, data FROM events WHERE kind IN "
                                    "('ci_result', 'ci_result_unavailable', 'ci_orphan_retired') ORDER BY at DESC LIMIT 6").fetchall()
    finally:
        connection.close()
    results = [json.loads(data) for (data,) in rows]
    minutes = sorted(r["run_seconds"] / 60 for r in results if r.get("run_seconds"))
    failures = sum(r.get("conclusion") != "success" for r in results)
    return len(results), failures, (statistics.median(minutes) if minutes else None), recent


def controller_line():
    out = _output(["systemctl", "--user", "show", "tauceti-fleet", "-p", "ActiveState", "-p", "CPUUsageNSec",
                   "-p", "MemoryCurrent", "-p", "MemoryPeak", "-p", "ActiveEnterTimestampMonotonic", "-p", "NRestarts"])
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def number(key):
        return int(values[key]) if values.get(key, "").isdigit() else 0

    if values.get("ActiveState") != "active":
        return f"CTRL  {RED}tauceti-fleet.service {values.get('ActiveState', 'unknown')}{RESET}"
    wall = time.clock_gettime(time.CLOCK_MONOTONIC) - number("ActiveEnterTimestampMonotonic") / 1e6
    cpu = number("CPUUsageNSec") / 1e9
    restarts = number("NRestarts")
    return (f"CTRL  {GREEN}active{RESET} {duration(wall)}  cpu {duration(cpu)} ({100 * cpu / max(1, wall):.1f}% of a core)  "
            f"mem {number('MemoryCurrent') / 2**20:.0f}M peak {number('MemoryPeak') / 2**20:.0f}M"
            + (f"  {YELLOW}restarts {restarts}{RESET}" if restarts else ""))


def render(host, previous, current, interval):
    """Frame lines for two consecutive samples."""
    elapsed = max(1e-6, current["t"] - previous["t"])
    config = host_config()
    guests = slots()
    lines = [f"{BOLD}tauceti-fleet top{RESET}  {time.strftime('%H:%M:%S')}  {DIM}refresh {interval:g}s · q to quit{RESET}"]
    try:
        limit = capacity.jobs(config)
    except FleetError:
        limit = "?"
    lines.append(f"CI    {BOLD}{sum(bool(g.get('busy')) for g in guests)}{RESET} busy / {len(guests)} guests / limit {limit}")

    total, idle = current["cpu"][0] - previous["cpu"][0], current["cpu"][1] - previous["cpu"][1]
    busy = 100 * (1 - idle / max(1, total))
    lines.append(f"CPU   {bar(busy)} {busy:5.1f}%  load {os.getloadavg()[0]:5.1f}/{os.cpu_count()}  "
                 f"stall {pressure('cpu', 0):4.1f}%")
    read_rate = (current["disk"][0] - previous["disk"][0]) * 512 / elapsed / 1e6
    write_rate = (current["disk"][1] - previous["disk"][1]) * 512 / elapsed / 1e6
    utilization = min(100.0, (current["disk"][2] - previous["disk"][2]) / (elapsed * 10))
    io_stall = pressure("io", 1)
    lines.append(f"DISK  {bar(utilization)} {utilization:5.1f}%  r {read_rate:6.0f} w {write_rate:6.0f} MB/s  {host.disk}  "
                 f"io-stall {bar(io_stall, 10)} {io_stall:4.1f}%")
    link = host.net_mbps / 8
    down = (current["net"][0] - previous["net"][0]) / elapsed / 1e6
    up = (current["net"][1] - previous["net"][1]) / elapsed / 1e6
    history = (previous.get("down_history") or []) + [down]
    current["down_history"] = history = history[-max(1, int(300 / interval)):]
    booted = time.clock_gettime(time.CLOCK_BOOTTIME)
    lines.append(f"NET ↓ {bar(100 * down / link)} {down:6.1f} MB/s  peak5m {max(history):6.1f}  avg5m {sum(history) / len(history):6.1f}  "
                 f"{DIM}{host.net} {host.net_mbps / 1000:g} Gb/s · since boot {current['net'][0] / 1e12:.2f} TB "
                 f"({current['net'][0] / booted / 1e6:.0f} MB/s avg){RESET}")
    lines.append(f"NET ↑ {bar(100 * up / link)} {up:6.1f} MB/s  {DIM}since boot {current['net'][1] / 1e9:.0f} GB{RESET}")

    rows, guest_memory, overlays = [], 0.0, 0.0
    for guest in guests:
        instance = guest.get("instance", "")
        ticks, resident = current["guests"].get(instance, (None, 0.0))
        before = previous["guests"].get(instance, (ticks, 0.0))[0]
        cpu = (ticks - before) / HZ / elapsed * 100 if ticks is not None else 0.0
        try:
            overlay = os.stat(Path(guest["directory"], "disk.qcow2")).st_blocks * 512 / GIB
        except (OSError, KeyError, TypeError):
            overlay = 0.0
        guest_memory += resident
        overlays += overlay
        if guest.get("busy"):
            started = guest.get("job_started_at") or guest.get("started_at") or current["t"]
            state, job = f"{GREEN}busy{RESET} ", f"{(current['t'] - started) / 60:6.1f}m"
            link_text = RUNS + str(guest["run_id"]) if guest.get("run_id") else ""
        else:
            state, job, link_text = f"{DIM}idle{RESET} ", "      -", ""
        rows.append(f"{guest.get('slot', 0):>4}  {state}  {cpu:5.0f}%  {job}  {resident:5.1f}G {overlay:5.1f}G "
                    f"{BOLD}{resident + overlay:6.1f}G{RESET}  {DIM}{link_text}{RESET}")

    memory = meminfo()
    used = (memory["MemTotal"] - memory["MemAvailable"]) / GIB
    lines.append(f"RAM   {bar(100 * used * GIB / memory['MemTotal'])} {used:5.0f}/{memory['MemTotal'] / GIB:.0f} GiB  "
                 f"avail {memory['MemAvailable'] / GIB:4.0f}G")
    slice_now, slice_limit = host.ci_slice()
    root = capacity.disk_root()
    try:
        tmpfs = os.statvfs(root).f_type == 0x01021994  # TMPFS_MAGIC
    except (AttributeError, OSError):
        tmpfs = _output(["stat", "-f", "-c", "%T", str(root)]).strip() == "tmpfs"
    overlay_label = "overlays in RAM" if tmpfs else "overlays"
    lines.append(f"CIMEM {bar(100 * slice_now / slice_limit if slice_limit else 0)} {slice_now:5.0f}/{slice_limit:.0f} GiB  "
                 f"guest RAM {guest_memory:5.1f}G + {overlay_label} {overlays:5.1f}G = {BOLD}{guest_memory + overlays:6.1f}G{RESET}")
    free = shutil.disk_usage(Path.home()).free / GIB
    lines.append(f"HOME  {DIM}free {free:6.0f} GiB{RESET}")
    lines.append(controller_line())
    lines.append("")
    lines.append(f"{DIM}slot  state  vm-cpu   job time  vm-mem   disk   total  run{RESET}")
    lines.extend(rows)
    try:
        jobs, failures, median, recent = recent_results()
    except (sqlite3.Error, ValueError) as exc:
        lines.append(f"{RED}events unavailable: {exc}{RESET}")
        return lines
    lines.append("")
    lines.append(f"last hour: {BOLD}{jobs}{RESET} jobs {DIM}(~{jobs * 24}/day){RESET}, "
                 f"{RED if failures else ''}{failures} failed{RESET}, median {f'{median:.0f}m' if median else '-'}")
    for at, kind, data in recent:
        value = json.loads(data)
        when = time.strftime("%H:%M", time.localtime(at))
        if kind == "ci_result":
            color = GREEN if value.get("conclusion") == "success" else RED
            lines.append(f"  {when} {color}{value.get('conclusion', '?'):>8}{RESET} {value.get('run_seconds', 0) / 60:5.1f}m  "
                         f"{DIM}{value.get('classification', {}).get('reason', '')}{RESET}")
        else:
            lines.append(f"  {when} {RED}{kind}{RESET} {DIM}{value.get('reason', '')}{RESET}")
    return lines


def run(interval=3.0, once=False):
    host = Host()
    previous = host.sample()
    if once:
        time.sleep(min(interval, 1.0))
        print("\n".join(render(host, previous, host.sample(), interval)))
        return 0
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    saved = termios.tcgetattr(descriptor) if sys.stdin.isatty() else None
    try:
        if saved:
            tty.setcbreak(descriptor)
        sys.stdout.write("\033[?25l\033[2J")
        time.sleep(0.5)
        while True:
            current = host.sample()
            lines = render(host, previous, current, interval)
            sys.stdout.write("\033[H" + "\n".join(line + "\033[K" for line in lines) + "\033[J")
            sys.stdout.flush()
            previous = current
            ready = select.select([sys.stdin], [], [], interval)[0] if saved else time.sleep(interval)
            if ready and sys.stdin.read(1).lower() == "q":
                break
    except KeyboardInterrupt:
        pass
    finally:
        if saved:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\n")
    return 0
