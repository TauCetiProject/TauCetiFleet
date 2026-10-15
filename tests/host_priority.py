#!/usr/bin/env python3
"""Bounded real-host check: two user services contend on one allowed CPU."""

import json
import os
import subprocess
import sys
import time
import uuid


def main():
    start = time.time() + 3
    core = min(os.sched_getaffinity(0))
    code = """import json,os,sys,time
os.sched_setaffinity(0,{int(sys.argv[2])})
start=float(sys.argv[1]); time.sleep(max(0,start-time.time()))
before=time.process_time()
while time.time()<start+3: pass
print(json.dumps(dict(cpu_seconds=time.process_time()-before)))
"""
    children = []
    for weight in (1000, 10):
        args = [
            "systemd-run",
            "--user",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--unit=fleet-priority-test-" + uuid.uuid4().hex[:8],
            "--slice=fleet-priority-test.slice",
            "-p",
            "CPUWeight=" + str(weight),
            "-p",
            "MemoryMax=64M",
            "-p",
            "TasksMax=16",
            sys.executable,
            "-c",
            code,
            str(start),
            str(core),
        ]
        children.append(subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    measured = []
    for child in children:
        out, err = child.communicate(timeout=20)
        if child.returncode:
            raise RuntimeError("User-service priority probe could not run: " + err[-300:])
        measured.append(json.loads(out)["cpu_seconds"])
    result = dict(
        ci_cpu_seconds=measured[0],
        worker_cpu_seconds=measured[1],
        ratio=measured[0] / max(0.001, measured[1]),
        logical_cpus_used=1,
        runtime_seconds=3,
    )
    print(json.dumps(result, indent=2))
    assert result["ratio"] > 3, "CI CPU priority was not observed; investigate user cgroup scheduling"


if __name__ == "__main__":
    main()
