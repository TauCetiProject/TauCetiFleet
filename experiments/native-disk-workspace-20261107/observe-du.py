"""Read-only final-pass du CPU observations; partial coverage is explicit."""
import json
import os
import subprocess
import time
from pathlib import Path

root = Path.home() / '.local/state/tauceti-fleet/native-disk-workspace-20261107'
launch = json.loads((root / 'results/disk-2/launch.json').read_text())
relative = subprocess.check_output(['systemctl', '--user', 'show', launch['unit'], '-p', 'ControlGroup', '--value'], text=True).strip()
group = Path('/sys/fs/cgroup') / relative.lstrip('/')
assert relative and group.is_dir()
maximum = {}
start = time.time()
with (root / 'du-observations.jsonl').open('w', buffering=1) as stream:
    while group.exists() and time.time() - start < 1800:
        pids = set()
        for file in group.rglob('cgroup.procs'):
            try:
                pids.update(file.read_text().split())
            except OSError:
                pass
        rows = []
        for pid in pids:
            try:
                raw = (Path('/proc') / pid / 'stat').read_text()
                name = raw.split('(', 1)[1].rsplit(')', 1)[0]
                if name != 'du':
                    continue
                parts = raw.rsplit(')', 1)[1].split()
                ticks = int(parts[11]) + int(parts[12])
                key = pid + ':' + parts[19]
                maximum[key] = max(maximum.get(key, 0), ticks)
                rows.append(dict(pid=int(pid), start_ticks=int(parts[19]), cpu_ticks=ticks))
            except (OSError, ValueError, IndexError):
                pass
        if rows:
            stream.write(json.dumps(dict(at=time.time(), du=rows)) + '\n')
        time.sleep(.25)
(root / 'du-summary.json').write_text(json.dumps(dict(started_at=start, ended_at=time.time(),
    unit=launch['unit'], clock_ticks=os.sysconf('SC_CLK_TCK'), observed_cpu_ticks=maximum,
    limitation='Final disk pass only, from mid-setup; sampled lower bounds omit CPU after last observed sample and short-lived missed processes.'), indent=2))
