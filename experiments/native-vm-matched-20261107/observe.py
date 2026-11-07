"""Read-only host CPU, PSI and compiler-wait observations for the matched run."""
import json
import time
from pathlib import Path

state = Path.home() / '.local/state/tauceti-fleet'
root = state / 'native-vm-matched-20261107'
for _ in range(4000):
    value = dict(at=time.time(), cpu=Path('/proc/stat').read_text().splitlines()[0],
                 loadavg=Path('/proc/loadavg').read_text().strip(),
                 memory_pressure=Path('/proc/pressure/memory').read_text(),
                 io_pressure=Path('/proc/pressure/io').read_text())
    for name, file in [('host_pressure', 'lean-host-pressure.json'), ('timing', 'lean-statistics-report.json')]:
        try:
            raw = json.loads((state / file).read_text())
            value[name] = raw if name == 'host_pressure' else {k: raw.get(k) for k in ('at', 'ci', 'instances', 'cpu_usage')}
        except (OSError, ValueError):
            pass
    print(json.dumps(value), flush=True)
    if (root / 'all-done.json').exists():
        break
    time.sleep(5)
