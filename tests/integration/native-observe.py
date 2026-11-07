"""Small in-namespace proof of mature audit/lint artifact backing."""
import collections
import json
import os
import time
from pathlib import Path

work = Path('/opt/actions-runner/_work')
dev = work.stat().st_dev
expected = f'{os.major(dev):02x}:{os.minor(dev):02x}'
seen, records = {}, {}
while len(records) < 2:
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            name = proc.joinpath('comm').read_text().strip()
            if name not in ('audit-driver', 'lint-env-driver') or name in records:
                continue
            first = seen.setdefault(proc.name, time.monotonic())
            if time.monotonic() - first < 4:
                continue
            artifacts, tools, paths = collections.Counter(), collections.Counter(), set()
            for line in proc.joinpath('maps').read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) != 6:
                    continue
                if fields[5].startswith(str(work)) and '/.lake/' in fields[5]:
                    artifacts[fields[3]] += 1
                    paths.add(fields[5])
                elif fields[5].startswith('/home/runner/.elan/'):
                    tools[fields[3]] += 1
            if not artifacts:
                continue
            records[name] = dict(pid=int(proc.name), artifact_devices=dict(artifacts),
                                 artifact_files=len(paths), toolchain_devices=dict(tools),
                                 all_artifacts_workspace_disk=set(artifacts) == {expected})
            value = dict(at=time.time(), workspace_device=expected, processes=records)
            target = Path('/fleet-link/telemetry/artifact-mappings.json')
            temporary = target.with_suffix('.next')
            temporary.write_text(json.dumps(value))
            temporary.replace(target)
        except OSError:
            continue
    time.sleep(.2)
