"""Plot actual charges and reclaim probe; never turn a stall into a success."""
import gzip
import json
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

root = Path(__file__).resolve().parent / 'results'
summary = json.loads((root / 'summary.json').read_text())
fig, axes = plt.subplots(3, 1, figsize=(9, 9), layout='constrained')
for label, row in summary.items():
    directory = root / label
    if (directory / 'body-result.json').exists():
        body = json.loads((directory / 'body-result.json').read_text())
        start, end = body['started_at'], body['ended_at']
    else:
        start = json.loads((directory / 'body-start.json').read_text())['at']
        end = json.loads((directory / 'censored.json').read_text())['at']
    path = directory / 'samples.jsonl'
    with (path.open() if path.exists() else gzip.open(str(path) + '.gz', 'rt')) as stream:
        rows = [json.loads(line) for line in stream]
    rows = [r for r in rows if start <= r['at'] <= end]
    minutes = [(r['at'] - start) / 60 for r in rows]
    color = '#2563eb' if label.startswith('disk') else '#d97706'
    style = '--' if label.endswith('2') else '-'
    title = label + (' (stopped)' if row['censored'] else '')
    charge = [int(r['memory.current']) / 1024**3 for r in rows]
    adjusted = []
    for r in rows:
        st = {a: int(b) for a, b in (line.split() for line in r['memory.stat'].splitlines())}
        clean = max(0, st['file'] - st.get('shmem', 0) - st.get('file_dirty', 0) - st.get('file_writeback', 0))
        adjusted.append(max(0, int(r['memory.current']) - clean) / 1024**3)
    axes[0].plot(minutes, charge, color=color, linestyle=style, label=title)
    axes[1].plot(minutes, adjusted, color=color, linestyle=style)
    axes[2].plot(minutes, [r['host']['memory_available'] / 1024**3 for r in rows], color=color, linestyle=style)
axes[0].set(title='Identical native replays: disk workspace versus tmpfs', ylabel='Charged RAM (GiB)', ylim=(0, 40))
axes[0].legend(ncols=2)
axes[1].set(ylabel='Charge minus clean file cache (GiB)', ylim=(0, 35))
axes[2].set(ylabel='Host headroom (GiB)', xlabel='Minutes from computational section start', ylim=(0, 60))
axes[2].axhline(19.022, color='#b91c1c', linestyle=':', label='Existing compiler threshold ≈19 GiB')
axes[2].legend()
for axis in axes:
    axis.grid(alpha=.2)
fig.savefig(root / 'comparison.png', dpi=160)
fig.savefig(root / 'comparison.svg')
