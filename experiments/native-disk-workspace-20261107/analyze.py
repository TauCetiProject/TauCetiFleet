"""Summarize completed matched passes, preserving every sample separately."""
import gzip
import json
import re
import sys
from pathlib import Path

GIB = 1024**3
root = Path(sys.argv[1])
metadata = root if (root / 'inputs.json').exists() else root / 'results'


def text(path):
    if path.exists():
        return path.read_text()
    with gzip.open(str(path) + '.gz', 'rt') as stream:
        return stream.read()


observations_path = metadata / 'observations.jsonl'
if not observations_path.exists() and not Path(str(observations_path) + '.gz').exists():
    observations_path = Path('/tmp/tauceti-native-vm-matched-observations.jsonl')
observations = ([json.loads(line) for line in text(observations_path).splitlines()]
                if observations_path.exists() or Path(str(observations_path) + '.gz').exists() else [])


def read(path):
    return json.loads(path.read_text())


def fields(raw):
    return {a: int(b) for a, b in (line.split() for line in raw.splitlines())}


def io(raw):
    for line in raw.splitlines():
        parts = line.split()
        if parts[0] == '9:127':
            return {a: int(b) for a, b in (part.split('=') for part in parts[1:])}
    return {}


def interval(samples, start, end):
    selected = [s for s in samples if start <= s['at'] <= end]
    if len(selected) < 2:
        return {}
    first, last = selected[0], selected[-1]
    area = sum((b['at'] - a['at']) * (int(a['memory.current']) + int(b['memory.current'])) / 2
               for a, b in zip(selected, selected[1:], strict=False)) / GIB
    def without_clean_file(value):
        st = fields(value['memory.stat'])
        clean = max(0, st['file'] - st.get('shmem', 0) - st.get('file_dirty', 0) - st.get('file_writeback', 0))
        return max(0, int(value['memory.current']) - clean)
    adjusted_area = sum((b['at'] - a['at']) * (without_clean_file(a) + without_clean_file(b)) / 2
                        for a, b in zip(selected, selected[1:], strict=False)) / GIB
    left, right = io(first.get('io.stat', '')), io(last.get('io.stat', ''))
    component_means = {}
    for name in ('anon', 'file', 'shmem', 'file_dirty', 'file_writeback', 'kernel'):
        amounts = [fields(row['memory.stat']).get(name, 0) for row in selected]
        integral = sum((b['at'] - a['at']) * (left + right) / 2
                       for a, b, left, right in zip(selected, selected[1:], amounts, amounts[1:], strict=False))
        component_means[name + '_mean_gib'] = integral / (last['at'] - first['at']) / GIB
    return dict(sampled_seconds=last['at'] - first['at'], memory_gib_seconds=area,
                **component_means,
                memory_peak_sampled_gib=max(int(s['memory.current']) for s in selected) / GIB,
                charged_minus_clean_file_peak_gib=max(without_clean_file(s) for s in selected) / GIB,
                charged_minus_clean_file_gib_seconds=adjusted_area,
                cpu_seconds=(fields(last['cpu.stat'])['usage_usec'] - fields(first['cpu.stat'])['usage_usec']) / 1e6,
                read_gib=(right.get('rbytes', 0) - left.get('rbytes', 0)) / GIB,
                write_gib=(right.get('wbytes', 0) - left.get('wbytes', 0)) / GIB,
                host_available_min_gib=min(s['host']['memory_available'] for s in selected) / GIB,
                host_available_max_gib=max(s['host']['memory_available'] for s in selected) / GIB)


def waits(start, end):
    values = [v for v in observations if start <= v['at'] <= end and v.get('timing', {}).get('ci')]
    if len(values) < 2:
        return {}
    a, b = values[0]['timing']['ci'], values[-1]['timing']['ci']
    return dict(sampled_seconds=values[-1]['at'] - values[0]['at'],
                aggregate_wait_seconds=b['wait_seconds'] - a['wait_seconds'],
                reasons={k: b.get('reasons', {}).get(k, 0) - a.get('reasons', {}).get(k, 0)
                         for k in ('ram', 'cpu', 'admission', 'signal')},
                waiting_peak=max(v['timing']['ci'].get('waiting_now', 0) for v in values))


output = {}
for directory in sorted((root / 'results').iterdir()):
    if not (directory / 'terminal.json').exists():
        continue
    terminal = read(directory / 'terminal.json')
    censored = (directory / 'censored.json').exists()
    if (directory / 'body-result.json').exists():
        body = read(directory / 'body-result.json')
    else:
        assert censored, directory
        body = dict(started_at=read(directory / 'body-start.json')['at'],
                    ended_at=read(directory / 'censored.json')['at'])
    samples = [json.loads(line) for line in text(directory / 'samples.jsonl').splitlines()]
    unit = read(directory / 'unit.json')
    life = read(directory / 'lifetime.json')
    all_events = [fields(s['memory.events']) for s in samples if 'memory.events' in s]
    events = {key: max(v.get(key, 0) for v in all_events) for key in ('max', 'oom', 'oom_kill')}
    phases = []
    if (directory / 'phases.tsv').exists() or (directory / 'phases.tsv.gz').exists():
        phase_text = text(directory / 'phases.tsv')
    else:
        assert censored, directory
        phase_text = read(directory / 'native-progress.json')['phases']
    for line in phase_text.splitlines():
        name, at = line.split()
        phases.append((name, float(at)))
    phase_rows = {}
    for (name, start), (_, end) in zip(phases, phases[1:], strict=False):
        phase_rows[name] = dict(wall_seconds=end - start, **interval(samples, start, end))
    log = text(directory / 'runner-console.log')
    modules = (read(directory / 'built-modules.json') if (directory / 'built-modules.json').exists()
               else sorted(set(re.findall(r'Built (TauCeti\.[A-Za-z0-9_.]+)', log))))
    metrics = dict(backend=read(directory / 'launch.json')['backend'], exit_code=terminal['exit_code'], censored=censored,
                   manifest=read(directory / 'input-identity.json')['manifest_sha256'],
                   tested_tree=read(directory / 'input-identity.json')['tested_tree'],
                   body_seconds=body['ended_at'] - body['started_at'],
                   setup_and_verification_seconds=body['started_at'] - life['started_at'],
                   lifetime_seconds=life['ended_at'] - life['started_at'],
                   memory_peak_gib=int(unit['MemoryPeak']) / GIB, memory_events=events,
                   rebuilt_modules=modules,
                   body=interval(samples, body['started_at'], body['ended_at']), phases=phase_rows)
    if (directory / 'reclaim-probe.json').exists():
        probe = read(directory / 'reclaim-probe.json')
        left, right = probe['before'], probe['after']
        metrics['reclaim'] = dict(seconds=probe['ended_at'] - probe['at'], returncode=probe.get('returncode'),
                                  stderr=probe.get('stderr'), timeout=probe.get('timeout', False),
                                  before_gib=int(left['memory.current']) / GIB,
                                  after_gib=int(right['memory.current']) / GIB,
                                  released_gib=(int(left['memory.current']) - int(right['memory.current'])) / GIB,
                                  before_memory_stat=fields(left['memory.stat']),
                                  after_memory_stat=fields(right['memory.stat']))
    metrics['compiler_waits'] = waits(body['started_at'], body['ended_at'])
    if censored:
        metrics['censor'] = read(directory / 'censored.json')
    metrics['audits'] = re.findall(r'duplicate-declarations: checked [^\n]+', log)
    output[directory.name] = metrics

(metadata / 'summary.json').write_text(json.dumps(output, indent=2))
for label, row in output.items():
    print(label, json.dumps({key: row[key] for key in ('censored', 'body_seconds', 'setup_and_verification_seconds',
                                                     'memory_peak_gib', 'memory_events')}))
if len(output) >= 4:
    rows = list(output.values())
    assert len({r['manifest'] for r in rows}) == 1
    assert len({r['tested_tree'] for r in rows}) == 1
    assert all(r['rebuilt_modules'] == rows[0]['rebuilt_modules'] for r in rows)
    assert all((r['exit_code'] == 0 or r['censored']) and not r['memory_events']['oom'] and not r['memory_events']['oom_kill'] for r in rows)
    print('Input/rebuild identities match. Completed passes succeed; censored passes are explicitly marked. No OOM events.')

# Preserve a conservative CPU lower bound for the optional final-pass observer.
if (metadata / 'du-summary.json').exists():
    body = read(root / 'results/disk-2/body-result.json')
    seen = {}
    for line in text(metadata / 'du-observations.jsonl').splitlines():
        row = json.loads(line)
        if not body['started_at'] <= row['at'] <= body['ended_at']:
            continue
        for process in row['du']:
            key = str(process['pid']) + ':' + str(process['start_ticks'])
            entry = seen.setdefault(key, dict(first=row['at'], first_ticks=process['cpu_ticks']))
            entry.update(last=row['at'], last_ticks=process['cpu_ticks'])
    du_meta = read(metadata / 'du-summary.json')
    calculation = dict(body_start=body['started_at'], body_end=body['ended_at'], processes=seen,
                       observed_du_cpu_seconds_lower_bound=sum(v['last_ticks'] - v['first_ticks'] for v in seen.values()) / du_meta['clock_ticks'],
                       limitation='Only deltas between samples within final disk body; misses short processes and CPU before first/after last observations. No subtraction from cgroup CPU or wall time.')
    (metadata / 'du-body-analysis.json').write_text(json.dumps(calculation, indent=2))
