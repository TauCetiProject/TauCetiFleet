"""Four serial, unregistered fresh-instance replays; no production mutations."""
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from tauceti_fleet import lean_pressure, lean_runtime, native_inside, native_runner, store, vm  # noqa: E402

GIB = 1024**3
SOURCE = Path(__file__).resolve().parent
NORMAL_STATE = store.state_dir()
ROOT = NORMAL_STATE / 'native-vm-matched-20261107'
INPUT = ROOT / 'input'
ARCHIVE = Path('/tmp/tauceti-native-supported-frozen-input.tar.gz')
CONFIG_PATH = Path.home() / '.config/tauceti-fleet/host.json'


def call(args, **kw):
    return subprocess.run([str(a) for a in args], check=True, **kw)


def save(path, value):
    path.write_text(json.dumps(value, indent=2))


def data(path, fallback=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def systemd(unit):
    r = call(['systemctl', '--user', 'show', unit, '-p', 'ControlGroup', '-p', 'ActiveState',
              '-p', 'SubState', '-p', 'Result', '-p', 'MemoryCurrent', '-p', 'MemoryPeak',
              '-p', 'CPUUsageNSec', '-p', 'MainPID'], capture_output=True, text=True)
    return dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)


def sample(group):
    value = dict(at=time.time())
    for name in ('memory.current', 'memory.peak', 'memory.events', 'memory.stat', 'cpu.stat', 'io.stat', 'memory.pressure'):
        try:
            value[name] = (group / name).read_text()
        except OSError:
            pass
    value['host'] = dict(zip(('memory_total', 'memory_available'), lean_pressure.memory(), strict=True))
    value['host_loadavg'] = Path('/proc/loadavg').read_text().strip()
    return value


def wait_room():
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        total, available = lean_pressure.memory()
        disk = shutil.disk_usage(ROOT).free
        # Serial unregistered replay, below the production canary's deliberately
        # conservative 52-GiB estimate. Apply the same rule to both backends;
        # retain the live compiler gate and bound the complete unit to 54 GiB.
        if available >= 40 * GIB and disk >= 38 * GIB:
            return dict(total=total, available=available, free_disk=disk, at=time.time())
        print(json.dumps(dict(waiting_for_start=True, available_gib=available / GIB, disk_gib=disk / GIB)), flush=True)
        time.sleep(15)
    raise TimeoutError('No suitable host headroom for offline comparison')


def launch_native(directory, unit):
    native_runner.stage({}, directory, 'unregistered-matched-experiment')
    # Small control inputs are per-instance; the immutable archive is hardlinked.
    target = directory / 'control/input'
    target.mkdir()
    for path in INPUT.iterdir():
        os.link(path, target / path.name)
    (directory / 'control/offline-job.sh').write_text((SOURCE / 'job.sh').read_text())
    bridge = NORMAL_STATE / 'vms' / ('matched-native-' + directory.name)
    bridge.symlink_to(directory, target_is_directory=True)
    args = ['systemd-run', '--user', '--quiet', '--unit=' + unit,
            '--slice=tauceti-fleet-ci.slice', '-p', 'MemorySwapMax=0', '-p', 'KillMode=control-group',
            '-p', 'RemainAfterExit=yes', '-p', 'RuntimeMaxSec=3600', '-p', 'CPUWeight=100',
            *native_runner.properties(), *native_runner.argv(directory, offline=True)]
    call(args)
    return bridge, directory / 'control/telemetry'


def launch_vm(directory, unit):
    image = store.data_dir() / 'images/prepared.qcow2'
    disk = directory / 'disk.qcow2'
    call([vm.executable('qemu-img'), 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', image, disk, '80G'], capture_output=True)
    output = directory / 'control'
    output.mkdir(mode=0o777)
    output.chmod(0o777)
    (output / 'telemetry').mkdir(mode=0o777)
    (output / 'telemetry').chmod(0o777)
    for name in ('native_inside.py',):
        shutil.copyfile(Path(native_inside.__file__), output / name)
    shutil.copyfile(SOURCE / 'job.sh', output / 'job.sh')
    hook = '#!/bin/bash\nexec /usr/bin/python3 /usr/local/bin/tauceti_native_inside.py "$1"\n'
    commands = [
        'mkdir -p /fleet-link/input; mount -t 9p -o trans=virtio,version=9p2000.L,msize=1048576 control /fleet-link',
        'mkdir -p /fleet-link/input; mount -t 9p -o ro,trans=virtio,version=9p2000.L,msize=1048576 input /fleet-link/input',
        'sysctl -w vm.max_map_count=1048576; sysctl -w kernel.apparmor_restrict_unprivileged_userns=0',
        'systemctl mask --now apt-daily.timer apt-daily-upgrade.timer; systemctl mask apt-daily.service apt-daily-upgrade.service',
        'while systemctl is-active --quiet apt-daily.service || systemctl is-active --quiet apt-daily-upgrade.service; do sleep 1; done',
        *lean_runtime.ci_commands(),
        'cp /fleet-link/native_inside.py /usr/local/bin/tauceti_native_inside.py',
        'printf %s ' + shlex.quote(hook.replace('"$1"', 'ci_started')) + ' > /opt/hook-start.sh',
        'printf %s ' + shlex.quote(hook.replace('"$1"', 'ci_completed')) + ' > /opt/hook-complete.sh',
        'chmod 755 /opt/hook-start.sh /opt/hook-complete.sh',
        'set +e; runuser -u runner -- env HOME=/home/runner MATCHED_BACKEND=vm bash -c '
        + shlex.quote('ulimit -n 65536; exec bash /fleet-link/job.sh')
        + ' > /fleet-link/runner-console.log 2>&1; code=$?; set -e',
        'python3 - "$code" <<\'FINAL\'\nimport json,sys,time\nfrom pathlib import Path\np=Path("/fleet-link/vm-result.json");p.write_text(json.dumps(dict(exit_code=int(sys.argv[1]),ended_at=time.time())))\nFINAL',
        'sync; poweroff',
    ]
    seed = vm.cloud_seed(directory, commands, name='matched-' + directory.name)
    config = dict(ci_vcpus=96, ci_memory_gib=112, ci_memory_budget_gib=112)
    args = vm.qemu_args(config, directory, disk, seed)
    for tag, path, readonly in [('control', output, False), ('input', INPUT, True)]:
        security = 'none' if readonly else 'mapped-xattr'
        args += ['-fsdev', f'local,id={tag},path={path},security_model={security}' + (',readonly=on' if readonly else ''),
                 '-device', f'virtio-9p-pci,fsdev={tag},mount_tag={tag}']
    bridge = NORMAL_STATE / 'vms' / ('matched-vm-' + directory.name)
    bridge.mkdir()
    (bridge / 'pressure.sock').symlink_to(directory / 'pressure.sock')
    save(directory / 'vm-configuration.json', dict(qemu=args, cpu_weight=100, memory_max_gib=54, swap=0))
    call(['systemd-run', '--user', '--quiet', '--unit=' + unit, '--slice=tauceti-fleet-ci.slice',
          '-p', 'MemoryMax=54G', '-p', 'MemorySwapMax=0', '-p', 'CPUWeight=100',
          '-p', 'KillMode=control-group', '-p', 'RemainAfterExit=yes', '-p', 'RuntimeMaxSec=3600', *args])
    return bridge, output / 'telemetry'


def run(label, backend):
    room = wait_room()
    identity = secrets.token_hex(8)
    directory = ROOT / 'vms' / identity
    directory.mkdir(parents=True)
    unit = 'tauceti-matched-' + label + '-' + identity + '.service'
    evidence = ROOT / 'results' / label
    evidence.mkdir(parents=True)
    save(evidence / 'launch.json', dict(label=label, backend=backend, identity=identity, unit=unit, room=room))
    started = time.time()
    bridge = NORMAL_STATE / 'vms' / ('matched-' + backend + '-' + identity)
    telemetry = None
    try:
        bridge, telemetry = (launch_native if backend == 'native' else launch_vm)(directory, unit)
        props = systemd(unit)
        group = Path('/sys/fs/cgroup') / props['ControlGroup'].lstrip('/')
        with (evidence / 'samples.jsonl').open('w', buffering=1) as samples:
            deadline = time.monotonic() + 3650
            while time.monotonic() < deadline:
                samples.write(json.dumps(sample(group)) + '\n')
                terminal = data(directory / ('native-result.json' if backend == 'native' else 'control/vm-result.json'))
                props = systemd(unit)
                if terminal is not None or props['SubState'] in ('dead', 'exited', 'failed'):
                    break
                time.sleep(2)
            else:
                raise TimeoutError(label)
        save(evidence / 'unit.json', props)
        save(evidence / 'terminal.json', terminal)
        save(evidence / 'lifetime.json', dict(started_at=started, ended_at=time.time()))
        for path in telemetry.iterdir() if telemetry.exists() else []:
            if path.is_file():
                shutil.copyfile(path, evidence / path.name)
        for name in ('native-result.json', 'native-progress.json', 'runner-console.log', 'console.log', 'native-samples.jsonl'):
            for source in (directory / name, directory / 'control' / name):
                if source.is_file():
                    shutil.copyfile(source, evidence / name)
        expected = data(ROOT / 'results/vm-1/input-identity.json')
        actual = data(evidence / 'input-identity.json')
        assert terminal and terminal['exit_code'] == 0, (label, terminal)
        assert data(evidence / 'body-result.json')['exit_code'] == 0
        assert actual and expected and actual == expected, (label, 'consumed input identity differs')
        log = (evidence / 'lake-build.log').read_text()
        modules = sorted(set(re.findall(r'Built (TauCeti\.[A-Za-z0-9_.]+)', log)))
        save(evidence / 'built-modules.json', modules)
        baseline = data(ROOT / 'results/vm-1/built-modules.json')
        assert modules == baseline, (label, 'different rebuilt modules')
        print(json.dumps(dict(completed=label, terminal=terminal, body=data(evidence / 'body-result.json'),
                              memory_peak=props['MemoryPeak'], rebuilt_modules=len(modules))), flush=True)
    finally:
        subprocess.run(['systemctl', '--user', 'stop', unit], check=False)
        props = systemd(unit)
        assert props['MainPID'] == '0'
        if props.get('ControlGroup'):
            events = Path('/sys/fs/cgroup') / props['ControlGroup'].lstrip('/') / 'cgroup.events'
            assert not events.exists() or 'populated 0' in events.read_text()
        if backend == 'native':
            native_runner.remove_root(directory)
        else:
            shutil.rmtree(directory)
        if bridge:
            if bridge.is_symlink():
                bridge.unlink()
            else:
                shutil.rmtree(bridge)
        save(evidence / 'cleanup.json', dict(at=time.time(), released=not directory.exists(), unit=props))


def main():
    ROOT.mkdir(parents=True, exist_ok=False, mode=0o700)
    before = CONFIG_PATH.read_bytes()
    config = json.loads(before)
    assert not config['ci_enabled'] and not config['workers_enabled']
    INPUT.mkdir()
    os.link(ARCHIVE, INPUT / 'frozen-input.tar.gz')
    for path in SOURCE.glob('*.py'):
        if path.name != 'run.py':
            shutil.copyfile(path, INPUT / path.name)
    call(['curl', '--retry', '3', '-fLsS',
          'https://archive.ubuntu.com/ubuntu/pool/main/b/bubblewrap/bubblewrap_0.9.0-1build1_amd64.deb',
          '-o', INPUT / 'bwrap.deb'])
    assert digest(INPUT / 'bwrap.deb') == 'dde30d1f24da50446d647ed504ec8dc5a714f171974d054c70a589b48ba38b48'
    save(ROOT / 'inputs.json', dict(archive_sha256=digest(ARCHIVE), archive_bytes=ARCHIVE.stat().st_size,
                                  code_revision=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                                  native_image=native_runner.image(), prepared_image=store.read(store.data_dir() / 'images/prepared.json'),
                                  script_sha256=digest(SOURCE / 'job.sh'), config_sha256=hashlib.sha256(before).hexdigest(),
                                  experimental_start_gib=40, memory_max_gib=54))
    os.environ['TAUCETI_FLEET_STATE'] = str(ROOT / 'native-state')
    (NORMAL_STATE / 'vms').mkdir(exist_ok=True)
    try:
        for label, backend in [('vm-1', 'vm'), ('native-1', 'native'), ('native-2', 'native'), ('vm-2', 'vm')]:
            run(label, backend)
    finally:
        assert CONFIG_PATH.read_bytes() == before, 'Local fleet configuration changed independently'
        save(ROOT / 'final.json', dict(at=time.time(), config_unchanged=True,
                                      archive_unchanged=digest(ARCHIVE) == data(ROOT / 'inputs.json')['archive_sha256'],
                                      remaining_instances=[str(p) for p in (ROOT / 'vms').glob('*')]))


if __name__ == '__main__':
    main()
