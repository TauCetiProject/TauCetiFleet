"""Bounded, read-only CPU sampling of existing disk scans, including payload cgroups."""

import json
import os
import subprocess
import time
from pathlib import Path

state = Path.home() / '.local/state/tauceti-fleet'
root = state / 'native-disk-cutover-20261107'
seen, groups = {}, {}
ticks = os.sysconf('SC_CLK_TCK')
last = started = time.monotonic()
next_inventory = 0
maximum_gap = 0
while time.monotonic() - started < 180:
    now = time.monotonic()
    maximum_gap = max(maximum_gap, now - last)
    last = now
    if now >= next_inventory:
        groups = {}
        for receipt in [state / 'vm.json', *(state / 'ci-slots').glob('*/vm.json')]:
            try:
                value = json.loads(receipt.read_text())
                if value.get('runtime') != 'native-disk':
                    continue
                group = subprocess.check_output(['systemctl', '--user', 'show', value['unit'],
                                                 '-p', 'ControlGroup', '--value'], text=True).strip()
                if group.startswith('/') and group != '/':
                    groups[value['instance']] = Path('/sys/fs/cgroup') / group.lstrip('/')
            except (OSError, ValueError, subprocess.CalledProcessError):
                continue
        next_inventory = now + 5
    for identity, group in groups.items():
        for file in group.rglob('cgroup.procs'):
            try:
                pids = file.read_text().split()
            except OSError:
                continue
            for pid in pids:
                try:
                    raw = Path('/proc', pid, 'stat').read_text()
                    name = raw[raw.index('(') + 1:raw.rindex(')')]
                    fields = raw[raw.rindex(')') + 2:].split()
                    if name != 'du':
                        continue
                    key = f'{pid}:{fields[19]}'
                    item = seen.setdefault(key, dict(instance=identity, pid=int(pid), first_at=time.time()))
                    item.update(last_at=time.time(), cpu_seconds=(int(fields[11]) + int(fields[12])) / ticks)
                except (OSError, ValueError, IndexError):
                    continue
    time.sleep(max(0, .1 - (time.monotonic() - now)))
result = dict(ended_at=time.time(), observed_seconds=time.monotonic() - started,
              maximum_poll_gap_seconds=maximum_gap, clock_ticks_per_second=ticks,
              method='Existing du processes only; 100ms recursive cgroup/proc sampling. CPU is a lower bound; '
                     'allow maximum poll gap plus two ticks for each captured process. '
                     'A scan entirely between polls may be missed. No extra scan was launched.',
              processes=list(seen.values()))
(root / 'disk-scan-process-cost.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result), flush=True)
