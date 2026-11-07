"""Bound the final unregistered replay's confirmed compiler-gate stall."""
import json
import subprocess
import time
from pathlib import Path

state = Path.home() / '.local/state/tauceti-fleet'
root = state / 'native-vm-matched-20261107'
for _ in range(1440):
    if (root / 'all-done.json').exists():
        break
    try:
        launch = json.loads((root / 'results/native-3/launch.json').read_text())
        directory = root / 'vms' / launch['identity']
        if (root / 'results/native-3/body-result.json').exists() or not directory.exists():
            break
        progress = json.loads((directory / 'control/native-progress.json').read_text())
        phase, at = progress['phases'].splitlines()[-1].split()
        report = json.loads((state / 'lean-statistics-report.json').read_text())
        pressure = json.loads((state / 'lean-host-pressure.json').read_text())
        timing = report['ci']
        fresh = all(time.time() - value['at'] < 20 for value in (report, pressure, progress))
        low_ram = pressure['memory_available'] < pressure['staging_priority']['normal_headroom_gib'] * 1024**3
        if (fresh and low_ram and phase == 'lint-style' and time.time() - float(at) >= 600
                and timing.get('waiting_now') and not timing.get('running_now')
                and timing.get('oldest_wait', 0) >= 300):
            reason = dict(at=time.time(), classification='censored_host_ram_gate_stall', phase=phase,
                          phase_started_at=float(at), phase_seconds=time.time() - float(at),
                          timing={k: timing.get(k) for k in ('waiting_now', 'running_now', 'oldest_wait')},
                          instance=launch['identity'], unit=launch['unit'])
            (root / 'results/native-3/censored.json').write_text(json.dumps(reason, indent=2))
            subprocess.run(['systemctl', '--user', 'stop', launch['unit']], check=True)
            break
    except (OSError, ValueError, IndexError, KeyError):
        pass
    time.sleep(5)
