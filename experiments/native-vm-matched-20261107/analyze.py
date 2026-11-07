"""Summarize completed matched passes, preserving every sample separately."""
import json
import re
import sys
from pathlib import Path

GIB = 1024**3
root = Path(sys.argv[1])


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
    left, right = io(first.get('io.stat', '')), io(last.get('io.stat', ''))
    return dict(sampled_seconds=last['at'] - first['at'], memory_gib_seconds=area,
                cpu_seconds=(fields(last['cpu.stat'])['usage_usec'] - fields(first['cpu.stat'])['usage_usec']) / 1e6,
                read_gib=(right.get('rbytes', 0) - left.get('rbytes', 0)) / GIB,
                write_gib=(right.get('wbytes', 0) - left.get('wbytes', 0)) / GIB,
                host_available_min_gib=min(s['host']['memory_available'] for s in selected) / GIB,
                host_available_max_gib=max(s['host']['memory_available'] for s in selected) / GIB)


output = {}
for directory in sorted((root / 'results').iterdir()):
    if not (directory / 'terminal.json').exists():
        continue
    body = read(directory / 'body-result.json')
    samples = [json.loads(line) for line in (directory / 'samples.jsonl').read_text().splitlines()]
    unit = read(directory / 'unit.json')
    life = read(directory / 'lifetime.json')
    terminal = read(directory / 'terminal.json')
    all_events = [fields(s['memory.events']) for s in samples if 'memory.events' in s]
    events = {key: max(v.get(key, 0) for v in all_events) for key in ('max', 'oom', 'oom_kill')}
    phases = []
    for line in (directory / 'phases.tsv').read_text().splitlines():
        name, at = line.split()
        phases.append((name, float(at)))
    phase_rows = {}
    for (name, start), (_, end) in zip(phases, phases[1:], strict=False):
        phase_rows[name] = dict(wall_seconds=end - start, **interval(samples, start, end))
    metrics = dict(backend=read(directory / 'launch.json')['backend'], exit_code=terminal['exit_code'],
                   manifest=read(directory / 'input-identity.json')['manifest_sha256'],
                   tested_tree=read(directory / 'input-identity.json')['tested_tree'],
                   body_seconds=body['ended_at'] - body['started_at'],
                   setup_and_verification_seconds=body['started_at'] - life['started_at'],
                   lifetime_seconds=life['ended_at'] - life['started_at'],
                   memory_peak_gib=int(unit['MemoryPeak']) / GIB, memory_events=events,
                   rebuilt_modules=read(directory / 'built-modules.json'),
                   body=interval(samples, body['started_at'], body['ended_at']), phases=phase_rows)
    log = (directory / 'runner-console.log').read_text(errors='replace')
    metrics['audits'] = re.findall(r'duplicate-declarations: checked [^\n]+', log)
    output[directory.name] = metrics

(root / 'summary.json').write_text(json.dumps(output, indent=2))
for label, row in output.items():
    print(label, json.dumps({key: row[key] for key in ('body_seconds', 'setup_and_verification_seconds',
                                                     'memory_peak_gib', 'memory_events')}))
if len(output) >= 4:
    rows = list(output.values())
    assert len({r['manifest'] for r in rows}) == 1
    assert len({r['tested_tree'] for r in rows}) == 1
    assert all(r['rebuilt_modules'] == rows[0]['rebuilt_modules'] for r in rows)
    assert all(r['exit_code'] == 0 and not any(r['memory_events'].values()) for r in rows)
    print('All completed passes match inputs and rebuild set, pass, and have no memory-limit/OOM events.')
