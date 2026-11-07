"""Unregistered disk/tmpfs comparison; all runtime changes live in task state."""
import difflib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('matched', REPO / 'experiments/native-vm-matched-20261107/run.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
ROOT = m.NORMAL_STATE / 'native-disk-workspace-20261107'
CONFIG = m.CONFIG_PATH
ORDER = [('disk-1', 'disk'), ('tmpfs-1', 'tmpfs'), ('tmpfs-2', 'tmpfs'), ('disk-2', 'disk')]
EXPECTED = REPO / 'experiments/native-vm-matched-20261107/results/vm-2/input-identity.json'


def observe(stop):
    with (ROOT / 'observations.jsonl').open('w', buffering=1) as stream:
        while not stop.is_set():
            row = dict(at=time.time(), cpu=Path('/proc/stat').read_text().splitlines()[0],
                       memory_pressure=Path('/proc/pressure/memory').read_text(),
                       io_pressure=Path('/proc/pressure/io').read_text())
            for key, filename in [('host_pressure', 'lean-host-pressure.json'), ('timing', 'lean-statistics-report.json')]:
                value = m.data(m.NORMAL_STATE / filename, {})
                row[key] = value if key == 'host_pressure' else {k: value.get(k) for k in ('at', 'ci', 'instances', 'cpu_usage')}
            stream.write(json.dumps(row) + '\n')
            stop.wait(5)


def runtime():
    destination = ROOT / 'runtime/tauceti_fleet'
    shutil.copytree(REPO / 'tauceti_fleet', destination, ignore=shutil.ignore_patterns('__pycache__'))
    patches = {
        'native_runner.py': (
            '    run([mount, "-t", "tmpfs", "-o", f"size={WORKSPACE_GIB}G,mode=755", "tmpfs", root / "opt/actions-runner/_work"])',
            '    if not (directory / "control/disk-workspace").is_file():\n'
            '        run([mount, "-t", "tmpfs", "-o", f"size={WORKSPACE_GIB}G,mode=755", "tmpfs", root / "opt/actions-runner/_work"])'),
        'native_inside.py': (
            '                if allocated > 24 * 1024**3:',
            '                if allocated > (56 if (LINK / "disk-workspace").is_file() else 24) * 1024**3:')}
    diff = ''
    for name, (old, new) in patches.items():
        path = destination / name
        before = path.read_text()
        assert before.count(old) == 1
        after = before.replace(old, new)
        path.write_text(after)
        diff += ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='a/' + name, tofile='b/' + name))
    (ROOT / 'runtime.patch').write_text(diff)
    original = m.native_runner.properties
    def properties():
        return [arg if not arg.startswith('--setenv=PYTHONPATH=') else '--setenv=PYTHONPATH=' + str(ROOT / 'runtime')
                for arg in original()]
    m.native_runner.properties = properties


def reclaim(group, evidence):
    before = m.sample(group)
    result = dict(at=time.time(), request='32G swappiness=0', scope='only this experimental unit')
    try:
        task = subprocess.run([sys.executable, '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])',
                               str(group / 'memory.reclaim'), result['request']], capture_output=True, text=True, timeout=30)
        result.update(returncode=task.returncode, stdout=task.stdout, stderr=task.stderr)
    except subprocess.TimeoutExpired:
        result['timeout'] = True
    time.sleep(2)
    result.update(ended_at=time.time(), before=before, after=m.sample(group))
    m.save(evidence / 'reclaim-probe.json', result)


def run(label, backend):
    room = m.wait_room()
    identity = m.secrets.token_hex(8)
    directory = ROOT / 'vms' / identity
    directory.mkdir(parents=True)
    unit = 'tauceti-disk-matched-' + label + '-' + identity + '.service'
    evidence = ROOT / 'results' / label
    evidence.mkdir(parents=True)
    m.save(evidence / 'launch.json', dict(label=label, backend=backend, identity=identity, unit=unit, room=room))
    bridge, telemetry, props, terminal = None, None, {}, None
    started = time.time()
    censored = False
    try:
        # Set marker before launch without changing the common stage routine.
        original_stage = m.native_runner.stage
        def stage(config, dest, jit):
            result = original_stage(config, dest, jit)
            if backend == 'disk':
                (Path(dest) / 'control/disk-workspace').touch()
            return result
        m.native_runner.stage = stage
        try:
            bridge, telemetry = m.launch_native(directory, unit)
        finally:
            m.native_runner.stage = original_stage
        props = m.systemd(unit)
        group = Path('/sys/fs/cgroup') / props['ControlGroup'].lstrip('/')
        probed = False
        with (evidence / 'samples.jsonl').open('w', buffering=1) as stream:
            deadline = time.monotonic() + 1900
            while time.monotonic() < deadline:
                row = m.sample(group)
                row['free_disk'] = shutil.disk_usage(ROOT).free
                stream.write(json.dumps(row) + '\n')
                terminal = m.data(directory / 'native-result.json')
                props = m.systemd(unit)
                if terminal is not None or props['SubState'] in ('dead', 'exited', 'failed'):
                    break
                if not probed and (telemetry / 'probe-ready').exists():
                    reclaim(group, evidence)
                    (telemetry / 'reclaim-done').touch()
                    probed = True
                progress = m.data(directory / 'control/native-progress.json', {})
                pressure = m.data(m.NORMAL_STATE / 'lean-host-pressure.json', {})
                report = m.data(m.NORMAL_STATE / 'lean-statistics-report.json', {})
                phases = (progress.get('phases') or '').splitlines()
                body = m.data(telemetry / 'body-start.json')
                reason = None
                if row['free_disk'] < 10 * m.GIB:
                    reason = 'disk_headroom'
                elif body and phases and not (telemetry / 'body-result.json').exists():
                    phase, at = phases[-1].split()
                    ci = report.get('ci', {})
                    fresh = all(time.time() - x.get('at', 0) < 20 for x in (progress, pressure, report))
                    low = pressure.get('memory_available', 1e99) < pressure.get('staging_priority', {}).get('normal_headroom_gib', 0) * m.GIB
                    if fresh and low and time.time() - float(at) >= 600 and ci.get('waiting_now') and not ci.get('running_now') and ci.get('oldest_wait', 0) >= 300:
                        reason = 'host_ram_gate_stall'
                if reason:
                    m.save(evidence / 'censored.json', dict(at=time.time(), classification=reason, progress=progress,
                                                          pressure=pressure, timing=report.get('ci'), unit=unit))
                    censored = True
                    m.call(['systemctl', '--user', 'stop', unit])
                time.sleep(2)
            else:
                raise TimeoutError(label)
        m.save(evidence / 'unit.json', props)
        m.save(evidence / 'terminal.json', terminal)
        m.save(evidence / 'lifetime.json', dict(started_at=started, ended_at=time.time()))
        for path in telemetry.iterdir() if telemetry.exists() else []:
            if path.is_file():
                shutil.copyfile(path, evidence / path.name)
        for name in ('native-result.json', 'native-progress.json', 'runner-console.log', 'native-samples.jsonl'):
            for path in (directory / name, directory / 'control' / name):
                if path.is_file():
                    shutil.copyfile(path, evidence / name)
        actual = m.data(evidence / 'input-identity.json')
        assert actual == m.data(EXPECTED), (label, 'input identity mismatch')
        log = (evidence / 'runner-console.log').read_text()
        modules = sorted(set(re.findall(r'Built (TauCeti\.[A-Za-z0-9_.]+)', log)))
        assert not modules, modules
        m.save(evidence / 'built-modules.json', modules)
        if not censored:
            assert terminal and terminal['exit_code'] == 0, (label, terminal)
            assert m.data(evidence / 'body-result.json')['exit_code'] == 0
        print(json.dumps(dict(completed=label, censored=censored, body=m.data(evidence / 'body-result.json'), memory_peak=props.get('MemoryPeak'))), flush=True)
    finally:
        subprocess.run(['systemctl', '--user', 'stop', unit], check=False)
        props = m.systemd(unit)
        assert props['MainPID'] == '0'
        if props.get('ControlGroup'):
            group = Path('/sys/fs/cgroup') / props['ControlGroup'].lstrip('/')
            assert not (group / 'cgroup.events').exists() or 'populated 0' in (group / 'cgroup.events').read_text()
        m.native_runner.remove_root(directory)
        if bridge and bridge.is_symlink():
            bridge.unlink()
        m.save(evidence / 'cleanup.json', dict(at=time.time(), released=not directory.exists(), unit=props))
        subprocess.run(['systemctl', '--user', 'reset-failed', unit], check=False, capture_output=True)


def main():
    ROOT.mkdir(mode=0o700, exist_ok=False)
    before = CONFIG.read_bytes()
    config = json.loads(before)
    assert not config['ci_enabled'] and not config['workers_enabled']
    m.ROOT, m.INPUT, m.SOURCE = ROOT, ROOT / 'input', SOURCE
    m.INPUT.mkdir()
    os.link(m.ARCHIVE, m.INPUT / 'frozen-input.tar.gz')
    for name in ('input-identity.py', 'cold-toolchain.py'):
        shutil.copyfile(REPO / 'experiments/native-vm-matched-20261107' / name, m.INPUT / name)
    m.call(['curl', '--retry', '3', '-fLsS', 'https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/bubblewrap_0.9.0-1build1_amd64.deb', '-o', m.INPUT / 'bwrap.deb'])
    assert m.digest(m.INPUT / 'bwrap.deb') == 'dde30d1f24da50446d647ed504ec8dc5a714f171974d054c70a589b48ba38b48'
    archive_hash = m.digest(m.ARCHIVE)
    assert archive_hash == '2d1f9d4bda40c094f9a97fb946c81bf7f3917970adfa4aaa44b31ab8256c8dff'
    image = m.store.data_dir() / 'images/prepared.qcow2'
    image_hash = m.digest(image)
    assert image_hash == m.native_runner.image()['vm_sha256']
    m.save(ROOT / 'inputs.json', dict(at=time.time(), archive_sha256=archive_hash, archive_bytes=m.ARCHIVE.stat().st_size,
                                    image_sha256=image_hash, native_image=m.native_runner.image(),
                                    config_sha256=m.hashlib.sha256(before).hexdigest(), memory_max_gib=54, start_gib=40,
                                    script_sha256=m.digest(SOURCE / 'job.sh'), source_revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()))
    m.save(ROOT / 'protocol.json', dict(order=ORDER, cache='No explicit cache purge before any timed body; same toolchain advice',
                                      post_body_probe='32G swappiness=0 reclaim request in only the own experimental cgroup',
                                      stall_stop_seconds=600, offline=True, production_mutations=False))
    runtime()
    os.environ['TAUCETI_FLEET_STATE'] = str(ROOT / 'native-state')
    stop = threading.Event()
    thread = threading.Thread(target=observe, args=(stop,), daemon=True)
    thread.start()
    try:
        for label, backend in ORDER:
            run(label, backend)
    finally:
        stop.set()
        thread.join()
        m.save(ROOT / 'all-done.json', dict(at=time.time(), config_unchanged=CONFIG.read_bytes() == before,
                                          archive_unchanged=m.digest(m.ARCHIVE) == archive_hash,
                                          image_unchanged=m.digest(image) == image_hash,
                                          remaining_instances=[str(p) for p in (ROOT / 'vms').glob('*')]))
        assert CONFIG.read_bytes() == before
        shutil.rmtree(m.INPUT)


if __name__ == '__main__':
    main()
