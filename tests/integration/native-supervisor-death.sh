#!/bin/bash
# Unregistered namespace test: losing the pressure supervisor must fail the job.
set -euo pipefail
sudo -n python3 - <<'PY'
import os
import signal
from pathlib import Path

supervisors = []
for path in Path('/proc').iterdir():
    if not path.name.isdigit():
        continue
    try:
        command = (path / 'cmdline').read_bytes().split(b'\0')
    except OSError:
        continue
    if command[-2:] == [b'pressure-loop', b'']:
        supervisors.append(int(path.name))
assert len(supervisors) == 1, supervisors
os.kill(supervisors[0], signal.SIGKILL)
print('killed disposable namespace pressure supervisor', flush=True)
PY
sleep 30
# Success here makes --expect-infrastructure-failure reject a surviving job.
echo 'runner incorrectly survived loss of its pressure supervisor' >&2
exit 0
