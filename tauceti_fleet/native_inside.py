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


def progress(stop):
    try:
        measure_progress(stop)
    except Exception:
        # A failed monitor (including disk-full writes) must fail closed rather
        # than silently disabling resource bounds while useful work continues.
        stop.set()


def measure_progress(stop):
    next_disk_check = 0
    while not stop.wait(2):
        fs = os.statvfs(WORK)
        value = dict(at=time.time(), workspace_allocated=(fs.f_blocks-fs.f_bfree)*fs.f_frsize,
                     workspace_available=fs.f_bavail*fs.f_frsize, phases=None)
        for file in WORK.glob('*/*/pr/.lake/telemetry/phases.tsv'):
            try:
                fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    if stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        value['phases'] = stream.read(4096).decode(errors='replace')
            except OSError:
                pass
        atomic(LINK / 'native-progress.json', value)
        if value['workspace_available'] < 1024**2 or fs.f_favail == 0:
            atomic(LINK / 'native-resource-error.json', dict(at=time.time(), reason='workspace ENOSPC'))
        disk = os.statvfs('/')
        if disk.f_bavail * disk.f_frsize < 8 * 1024**3:
            atomic(LINK / 'native-resource-error.json', dict(at=time.time(), reason='host disk headroom'))
        if time.monotonic() >= next_disk_check:
            try:
                usage = subprocess.run(['/usr/bin/du', '-sx', '--block-size=1', '--exclude=/fleet-link', '/'],
                                       capture_output=True, text=True, timeout=10)
                allocated = int(usage.stdout.split()[0])
                if allocated > 24 * 1024**3:
                    atomic(LINK / 'native-resource-error.json', dict(at=time.time(), reason='native disk budget', disk_bytes=allocated))
            except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
                pass
            next_disk_check = time.monotonic() + 10


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
    sampler = threading.Thread(target=progress, args=(stop,), daemon=True)
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
            children = Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()
            for child in children:
                if int(child) not in (runner.pid, receiver.pid):
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
