"""Standalone trusted bootstrap inside a native runner's private namespace."""

import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

LINK = Path("/fleet-link")
WORK = Path("/opt/actions-runner/_work")
DISK_BUDGET_GIB = 56
# Full roots contain tens of thousands of cache artifacts. A healthy populated
# runner already takes ~6s to scan; a 10s wall deadline fails under ordinary I/O
# contention. Space/inode headroom remains checked every two seconds below.
DISK_SCAN_INTERVAL = 300
DISK_SCAN_TIMEOUT = 120
DISK_SCAN_FAILURES = 3


def atomic(path, value):
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(value) + '\n')
    temporary.replace(path)


def hook(kind):
    # Preserve the terminal event before optional diagnostics. The controller can
    # recover this identity even if it was restarting when the unit exited.
    with (LINK / "events.jsonl").open("a") as stream:
        stream.write(json.dumps(dict(kind=kind, run_id=os.environ.get("GITHUB_RUN_ID"), at=time.time())) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if kind == "ci_completed" and workspace:
        source = Path(workspace) / "pr/.lake/telemetry"
        destination = LINK / "telemetry"
        for name in ("phases.tsv", "lake-build.log", "ci-host-resources.json"):
            try:
                fd = os.open(source / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as incoming:
                    if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                        continue
                    content = incoming.read(8 * 1024**2)
                with (destination / name).open("wb") as outgoing:
                    outgoing.write(content)
            except OSError:
                pass


def pressure():
    import tauceti_lean_guest as g
    g.SOCKET.unlink(missing_ok=True)
    broker = g.Broker()
    try:
        broker.statistics = g.lean_statistics.Collector('/usr/local/share/tauceti/lean-statistics.sock')
        broker.statistics.listen()
    except Exception:
        broker.statistics = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(g.SOCKET))
        g.SOCKET.chmod(0o666)
        listener.listen(256)
        broker.run(int(os.environ['TAUCETI_NATIVE_PRESSURE_FD']), listener)


class DiskUsage:
    """One bounded scan at a time; polling never blocks cheap resource checks."""
    def __init__(self):
        self.child = None
        self.lock = threading.Lock()
        self.next_scan = 0
        self.started = None
        self.allocated = None
        self.measured_at = None
        self.error = None
        self.failures = 0
        self.scan_seconds = None

    def failed(self, error):
        # Keep the last verified measurement and its original timestamp. The
        # error/failure fields expose staleness; repeated failures still retire
        # the runner, and host free-space checks do not depend on this scan.
        self.error = error
        self.failures += 1

    def poll(self):
        now = time.monotonic()
        with self.lock:
            if self.child is not None:
                code = self.child.poll()
                if code is not None or now - self.started >= DISK_SCAN_TIMEOUT:
                    self.scan_seconds = max(0, now - self.started)
                    if code is None:
                        self.child.kill()
                    stdout, _ = self.child.communicate(timeout=1)
                    self.child = None
                    try:
                        if code != 0:
                            raise ValueError("disk usage scan timed out" if code is None else "disk usage scan failed")
                        allocated = int(stdout.split()[0])
                        if allocated < 0:
                            raise ValueError("invalid disk usage")
                        self.allocated, self.measured_at = allocated, time.time()
                        self.error, self.failures = None, 0
                    except (ValueError, IndexError) as error:
                        self.failed(str(error))
            if self.child is None and now >= self.next_scan:
                self.next_scan = now + DISK_SCAN_INTERVAL
                self.started = now
                try:
                    self.child = subprocess.Popen(
                        ['/usr/bin/du', '-sx', '--block-size=1', '--exclude=/fleet-link', '/'],
                        # A busy workspace can produce many disappearing-file
                        # diagnostics. Never leave an unread stderr pipe that
                        # can block du before it emits its one-line summary.
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                except OSError as error:
                    self.failed(str(error))

    def snapshot(self):
        return dict(disk_allocated=self.allocated, disk_measured_at=self.measured_at,
                    disk_measurement_age=None if self.measured_at is None else max(0, time.time() - self.measured_at),
                    disk_scan_running=self.child is not None, disk_scan_error=self.error,
                    disk_scan_failures=self.failures, disk_scan_seconds=self.scan_seconds,
                    disk_scan_timeout_seconds=DISK_SCAN_TIMEOUT, disk_scan_interval_seconds=DISK_SCAN_INTERVAL,
                    disk_budget_bytes=DISK_BUDGET_GIB * 1024**3)

    def close(self):
        with self.lock:
            if self.child is not None:
                self.child.kill()
                self.child.communicate(timeout=1)
                self.child = None


def progress(stop, usage):
    try:
        measure_progress(stop, usage)
    except Exception:
        # A failed monitor cannot silently disable resource bounds.
        stop.set()
    finally:
        usage.close()


def measure_progress(stop, usage):
    while not stop.wait(2):
        disk = os.statvfs('/')
        usage.poll()
        value = dict(at=time.time(), workspace_storage="disk", host_disk_available=disk.f_bavail * disk.f_frsize,
                     host_disk_available_inodes=disk.f_favail, phases=None, **usage.snapshot())
        for file in WORK.glob('*/*/pr/.lake/telemetry/phases.tsv'):
            try:
                fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as incoming:
                    if stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                        value['phases'] = incoming.read(4096).decode(errors='replace')
            except OSError:
                pass
        atomic(LINK / 'native-progress.json', value)
        reason = None
        if value['host_disk_available'] < 8 * 1024**3:
            reason = 'host disk headroom'
        elif disk.f_favail == 0:
            reason = 'host inode exhaustion'
        elif usage.allocated is not None and usage.allocated > DISK_BUDGET_GIB * 1024**3:
            reason = 'native disk budget'
        elif usage.failures >= DISK_SCAN_FAILURES:
            reason = 'disk usage monitor unavailable'
        if reason:
            atomic(LINK / 'native-resource-error.json', dict(at=time.time(), reason=reason, **usage.snapshot()))
            stop.set()


def boot():
    os.chmod('/', 0o755)
    os.chmod(LINK, 0o755)
    os.chmod('/home/runner', 0o755)
    os.chmod('/tmp', 0o1777)
    os.chmod('/usr/bin/sudo', 0o4755)
    os.chown(WORK, 1000, 1000)
    (LINK / 'events.jsonl').touch()
    os.chown(LINK / 'events.jsonl', 1000, 1000)
    (LINK / 'telemetry').mkdir(exist_ok=True)
    os.chown(LINK / 'telemetry', 1000, 1000)
    Path('/usr/local/share/tauceti').mkdir(parents=True, exist_ok=True)
    Path('/usr/local/share/tauceti/host-pressure.required').touch()
    for name, kind in (('start','ci_started'), ('complete','ci_completed')):
        target = Path('/opt/hook-'+name+'.sh')
        target.write_text('#!/bin/bash\nexec /usr/bin/python3 /usr/local/bin/tauceti_native_inside.py '+kind+'\n')
        target.chmod(0o755)
    envfile = Path('/opt/actions-runner/.env')
    envfile.write_text('ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/hook-start.sh\n'
                       'ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/hook-complete.sh\n'
                       'LAKE_OVERRIDE_LEAN=true\nLEAN=/usr/local/bin/tauceti-lean\n'
                       'TAUCETI_LEAN_WRAPPER=/usr/local/bin/tauceti-lean\nBASH_ENV=/opt/tauceti-lean-env\n')
    os.chown(envfile, 1000, 1000)
    pressure_fd = int(os.environ['TAUCETI_NATIVE_PRESSURE_FD'])
    receiver = subprocess.Popen([sys.executable, __file__, 'pressure-loop'], pass_fds=(pressure_fd,))
    stop = threading.Event()
    usage = DiskUsage()
    sampler = threading.Thread(target=progress, args=(stop, usage), daemon=True)
    sampler.start()
    try:
        jit = (LINK / 'jit').read_text()
        (LINK / 'jit').unlink()
        # Runner CommandSettings consumes and removes this input. Keep JIT
        # credentials out of the host's world-readable process command lines.
        environment = dict(os.environ, HOME='/home/runner', ACTIONS_RUNNER_INPUT_JITCONFIG=jit)
        environment.pop('TAUCETI_NATIVE_PRESSURE_FD', None)
        runner = subprocess.Popen(['/usr/sbin/runuser', '-u', 'runner', '--', './run.sh'],
                                  cwd='/opt/actions-runner', env=environment)
        while runner.poll() is None:
            # This bootstrap is namespace PID 1. Reap adopted children without
            # stealing the two Popen objects' own exit statuses.
            with usage.lock:
                children = Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()
                owned = {runner.pid, receiver.pid}
                if usage.child is not None:
                    owned.add(usage.child.pid)
                for child in children:
                    if int(child) not in owned:
                        try:
                            os.waitpid(int(child), os.WNOHANG)
                        except ChildProcessError:
                            pass
            if stop.is_set() or (LINK / 'native-resource-error.json').exists():
                runner.terminate()
                raise RuntimeError('Native resource bound reached; retire as infrastructure failure')
            if receiver.poll() is not None:
                runner.terminate()
                raise RuntimeError('Native pressure receiver exited; retire this instance as infrastructure failure')
            time.sleep(.5)
        return runner.wait()
    finally:
        stop.set()
        sampler.join(timeout=3)
        receiver.terminate()
        receiver.wait(timeout=5)
        try:
            with (LINK / 'telemetry/runner-diagnostic.log').open('wb') as destination:
                for path in sorted(Path('/opt/actions-runner/_diag').glob('*.log'))[-4:]:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as stream:
                        if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 256 * 1024))
                            destination.write(stream.read(256 * 1024))
        except OSError:
            pass


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else 'boot'
    if action == 'pressure':
        pressure()
    elif action == 'pressure-loop':
        while True:
            subprocess.run([sys.executable, __file__, 'pressure'], check=False,
                           pass_fds=(int(os.environ['TAUCETI_NATIVE_PRESSURE_FD']),))
            time.sleep(2)
    elif action in ('ci_started','ci_completed'):
        hook(action)
    else:
        raise SystemExit(boot())
