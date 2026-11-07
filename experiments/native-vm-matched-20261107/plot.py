"""Plot measured job charge and host headroom; do not hide the censored run."""
import gzip
import json
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

root = Path(__file__).resolve().parent / 'results'
fig, axes = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True, layout='constrained')
for label, color, style in [('vm-2', '#2563eb', '-'), ('vm-3', '#2563eb', '--'),
                            ('native-2', '#d97706', '-'), ('native-3', '#d97706', '--')]:
    directory = root / label
    if (directory / 'body-result.json').exists():
        body = json.loads((directory / 'body-result.json').read_text())
        start, end = body['started_at'], body['ended_at']
    else:
        start = json.loads((directory / 'body-start.json').read_text())['at']
        end = json.loads((directory / 'censored.json').read_text())['at']
    with gzip.open(directory / 'samples.jsonl.gz', 'rt') as stream:
        rows = [json.loads(line) for line in stream]
    rows = [r for r in rows if start <= r['at'] <= end]
    minutes = [(r['at'] - start) / 60 for r in rows]
    charge = [int(r['memory.current']) / 1024**3 for r in rows]
    headroom = [r['host']['memory_available'] / 1024**3 for r in rows]
    name = label + (' (stopped)' if label == 'native-3' else '')
    axes[0].plot(minutes, charge, color=color, linestyle=style, label=name, linewidth=1.5)
    axes[1].plot(minutes, headroom, color=color, linestyle=style, linewidth=1.5)
axes[0].set(ylabel='Job cgroup RAM (GiB)', ylim=(0, 45), title='Identical restored-input replays on chungus2')
axes[0].legend(ncols=2, loc='lower right')
axes[1].axhline(19.022, color='#b91c1c', linestyle=':', label='Existing compiler headroom ≈19 GiB')
axes[1].set(ylabel='Measured host headroom (GiB)', xlabel='Minutes from computational section start', ylim=(0, 35))
axes[1].legend(loc='upper right')
for axis in axes:
    axis.grid(alpha=.2)
fig.savefig(root / 'comparison.png', dpi=160)
fig.savefig(root / 'comparison.svg')
