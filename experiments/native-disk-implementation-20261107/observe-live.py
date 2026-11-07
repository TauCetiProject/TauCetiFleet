"""Read-only, bounded live cutover evidence; no admissions or job mutations."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

home = Path.home()
state = home / '.local/state/tauceti-fleet'
release = (home / '.local/share/tauceti-fleet/current').resolve()
sys.path.insert(0, str(release))
from tauceti_fleet import native_runner as native  # noqa: E402

assert native.RUNTIME == 'native-disk'
root = state / 'native-disk-cutover-20261107'
root.mkdir(exist_ok=True)
known, groups = {}, {}
previous = None
with (root / 'observations.jsonl').open('a', buffering=1) as stream:
    for _ in range(2160):
        at = time.time()
        if (root / 'stop').exists():
            break
        config = native.shared_json(home / '.config/tauceti-fleet/host.json', {})
        status = native.shared_json(state / 'status.json', {})
        row = dict(at=at, observer_release=str(release), config={k: config.get(k) for k in
            ('ci_enabled', 'workers_enabled', 'ci_max_jobs', 'ci_staging_slots', 'ci_native_slot', 'draining')},
            controller={k: status.get(k) for k in ('controller_release', 'controller_pid', 'slot_errors', 'error')}, instances=[])
        for file in [state / 'vm.json', *(state / 'ci-slots').glob('*/vm.json')]:
            value = native.shared_json(file, {})
            if not native.is_native(value):
                continue
            identity = value.get('instance', '')
            directory = Path(value.get('directory', '/invalid'))
            if not re.fullmatch('[a-f0-9]{16}', identity) or directory != state / 'vms' / identity:
                raise RuntimeError('Invalid native ownership')
            dest = root / identity
            dest.mkdir(exist_ok=True)
            known[identity] = dict(value)
            (dest / 'instance.json').write_text(json.dumps(value, indent=2))
            sample = {k: value.get(k) for k in ('instance', 'slot', 'runtime', 'unit', 'run_id', 'runner_id', 'phase', 'busy', 'started_at')}
            if value['unit'] not in groups:
                result = subprocess.run(['systemctl', '--user', 'show', value['unit'], '-p', 'ControlGroup', '--value'], capture_output=True, text=True)
                relative = Path(result.stdout.strip())
                if relative.is_absolute() and relative != Path('/') and '..' not in relative.parts:
                    groups[value['unit']] = Path('/sys/fs/cgroup') / relative.relative_to('/')
            group = groups.get(value['unit'])
            if group:
                for name in ('memory.current', 'memory.peak', 'memory.stat', 'memory.events', 'cpu.stat', 'io.stat', 'cgroup.events'):
                    try:
                        sample[name] = (group / name).read_text()
                    except OSError:
                        pass
            sample['progress'] = native.shared_json(directory / 'control/native-progress.json', {})
            work = directory / 'rootfs/opt/actions-runner/_work'
            for file in [work / '_temp/tested-integration.json', *work.glob('*/*/tested-integration.json')]:
                identity_value = native.shared_json(file)
                if identity_value:
                    sample['tested_integration'] = identity_value
                    (dest / 'tested-integration.json').write_text(json.dumps(identity_value, indent=2))
            row['instances'].append(sample)
        for identity, value in known.items():
            for folder in (Path(value['directory']), state / 'native-results' / identity):
                for name in ('native-result.json', 'native-progress.json', 'archived.json'):
                    path = folder / ('control' if name == 'native-progress.json' and folder.name == identity and folder.parent.name == 'vms' else '') / name
                    record = native.shared_json(path)
                    if record:
                        (root / identity / name).write_text(json.dumps(record, indent=2))
        row['host_pressure'] = native.shared_json(state / 'lean-host-pressure.json', {})
        timing = native.shared_json(state / 'lean-statistics-report.json', {})
        row['timing'] = {key: timing.get(key) for key in ('at', 'ci', 'instances')}
        stream.write(json.dumps(row) + '\n')
        (root / 'latest.json').write_text(json.dumps(row, indent=2))
        current = [(v['slot'], v['instance'], v.get('run_id')) for v in row['instances']]
        if current != previous:
            print(json.dumps(dict(at=at, native=current, errors=row['controller'])), flush=True)
            previous = current
        time.sleep(5)
(root / 'observer-done.json').write_text(json.dumps(dict(at=time.time(), instances=list(known))))
