"""Reproduce live resource summaries from the filtered, archived evidence."""

import gzip
import json
import re
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).parent / 'results/live'
GIB = 1024**3


def records(path):
    with gzip.open(path, 'rt') as stream:
        return [json.loads(line) for line in stream]


def counters(value):
    return {key: int(number) for key, number in (line.split() for line in value.splitlines())}


def memory(sample):
    stat = counters(sample['memory.stat'])
    raw = int(sample['memory.current'])
    clean = max(0, stat['file'] - stat['shmem'] - stat['file_dirty'] - stat['file_writeback'])
    return {'charged': raw, 'adjusted': max(0, raw - clean),
            **{key: stat[key] for key in ('anon', 'shmem', 'file_dirty', 'file_writeback', 'kernel')}}


def means(samples, start, end):
    total = {}
    seconds = 0
    for first, last in zip(samples, samples[1:], strict=False):
        left, right = max(start, first['at']), min(end, last['at'])
        if right <= left:
            continue
        duration = last['at'] - first['at']
        a, b = (left - first['at']) / duration, (right - first['at']) / duration
        before, after = memory(first), memory(last)
        for key in before:
            x, y = before[key], after[key]
            area = ((x + (y - x) * a) + (x + (y - x) * b)) / 2 * (right - left)
            total[key] = total.get(key, 0) + area
        seconds += right - left
    return {'sampled_seconds': seconds, **{key + '_gib': value / seconds / GIB for key, value in total.items()}}


def io_bytes(sample, key):
    # Count the MD aggregate once; its component NVMe counters are not additive.
    for line in sample.get('io.stat', '').splitlines():
        device, *fields = line.split()
        if device == '9:127':
            return int(dict(field.split('=') for field in fields).get(key, 0))
    return 0


observations = records(ROOT / 'observations.jsonl.gz')
finished = {}
for folder in sorted(ROOT.iterdir()):
    if not folder.is_dir() or not (folder / 'native-samples.jsonl.gz').exists():
        continue
    terminal = json.loads((folder / 'native-result.json').read_text())
    samples = records(folder / 'native-samples.jsonl.gz')
    with gzip.open(folder / 'phases.tsv.gz', 'rt') as stream:
        phases = [(name, float(at)) for name, at in (line.split() for line in stream)]
    times = dict(phases)
    start, end = times['build'], times['end']
    body = [sample for sample in samples if start <= sample['at'] <= end]
    with gzip.open(folder / 'lake-build.log.gz', 'rt') as stream:
        rebuilt = sorted(set(re.findall(r'Built (TauCeti\.[^\s(]+)', stream.read())))
    progress = [sample['progress'] for sample in samples if sample.get('progress')]
    measurements = sorted({p['disk_measured_at'] for p in progress if p.get('disk_measured_at')})
    timing = [row['timing'].get('instances', {}).get(folder.name, {}) for row in observations]
    summary = {
        'unit_seconds': terminal['ended_at'] - terminal['started_at'],
        'computational_seconds': end - start,
        'setup_to_build_seconds': start - terminal['started_at'],
        'exact_unit_peak_gib': terminal['memory_peak_bytes'] / GIB,
        'body_mean': means(samples, start, end),
        'sampled_body_adjusted_peak_gib': max(memory(s)['adjusted'] for s in body) / GIB,
        'sampled_shmem_peak_gib': max(memory(s)['shmem'] for s in samples) / GIB,
        'body_sampled_cpu_seconds': (counters(body[-1]['cpu.stat'])['usage_usec'] -
                                     counters(body[0]['cpu.stat'])['usage_usec']) / 1e6,
        'body_sampled_md_read_gib': (io_bytes(body[-1], 'rbytes') - io_bytes(body[0], 'rbytes')) / GIB,
        'body_sampled_md_write_gib': (io_bytes(body[-1], 'wbytes') - io_bytes(body[0], 'wbytes')) / GIB,
        'rebuilt_tauceti_modules': len(rebuilt),
        'phase_seconds': {name: after - before for (name, before), (_, after) in zip(phases, phases[1:], strict=False)},
        'sampled_disk_peak_gib': max(p.get('disk_allocated') or 0 for p in progress) / GIB,
        'disk_measurement_intervals': [b - a for a, b in zip(measurements, measurements[1:], strict=False)],
        'disk_scan_failures_max': max(p.get('disk_scan_failures', 0) for p in progress),
        'compiler_wait_seconds': max((t.get('wait_seconds', 0) for t in timing), default=0),
        **{key: terminal[key] for key in ('memory_events', 'exit_code', 'infrastructure_error')},
    }
    (folder / 'analysis.json').write_text(json.dumps(summary, indent=2) + '\n')
    finished[folder.name] = {'build': start, 'end': end, **terminal}

overlap = []
for a, b in combinations(finished, 2):
    left, right = max(finished[a]['started_at'], finished[b]['started_at']), min(finished[a]['ended_at'], finished[b]['ended_at'])
    if right <= left:
        continue
    rows = [(r, {s['instance']: s for s in r['instances']}) for r in observations if left <= r['at'] <= right]
    rows = [(r, s) for r, s in rows if all(i in s and 'memory.current' in s[i] for i in (a, b))]
    body_start = max(finished[a]['build'], finished[b]['build'])
    body_end = min(finished[a]['end'], finished[b]['end'])
    body_rows = [(r, s) for r, s in rows if body_start <= r['at'] <= body_end]
    body_cpu = (sum(counters(body_rows[-1][1][i]['cpu.stat'])['usage_usec'] -
                    counters(body_rows[0][1][i]['cpu.stat'])['usage_usec'] for i in (a, b)) / 1e6
                if len(body_rows) > 1 else None)
    overlap.append({
        'instances': [a, b], 'unit_overlap_seconds': right - left,
        'computational_overlap_seconds': max(0, body_end - body_start),
        'computational_overlap_sampled_seconds': body_rows[-1][0]['at'] - body_rows[0][0]['at'] if body_rows else 0,
        'computational_overlap_sampled_cpu_seconds': body_cpu,
        'sampled_combined_raw_peak_gib': max(sum(memory(s[i])['charged'] for i in (a, b)) for _, s in rows) / GIB,
        'sampled_combined_adjusted_peak_gib': max(sum(memory(s[i])['adjusted'] for i in (a, b)) for _, s in rows) / GIB,
        'sampled_host_available_min_gib': min(r['host_pressure']['memory_available'] for r, _ in rows) / GIB,
    })
(ROOT / 'overlap-analysis.json').write_text(json.dumps(overlap, indent=2) + '\n')
print(json.dumps({'analyzed_instances': list(finished), 'overlap': overlap}, indent=2))
