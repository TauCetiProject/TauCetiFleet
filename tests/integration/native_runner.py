"""Unregistered integration harness for the supported native backend.

Run from the repo: python3 tests/integration/native_runner.py SCRIPT [--cancel-after N].
SCRIPT runs as the ordinary runner, inside its real _work directory. It may use
sudo inside the disposable user namespace. No GitHub registration/status is made.
The normal host pressure service supplies compiler credits through the test link.
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tauceti_fleet import native_runner as native  # noqa: E402
from tauceti_fleet import store

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("script", type=Path)
parser.add_argument("--cancel-after", type=float)
parser.add_argument("--timeout", type=int, default=1800)
parser.add_argument("--input", type=Path, help="Copy these trusted offline inputs into /fleet-link/input")
parser.add_argument("--stock-runner", action="store_true", help="Exercise the stock Listener with an invalid, unregistered JIT input")
parser.add_argument("--memory-max-mib", type=int, help="Smaller test-only cgroup limit for local OOM recovery")
parser.add_argument("--expect-infrastructure-failure", action="store_true")
parser.add_argument("--cancel-phase", help="Cancel while this full-workflow phase is active")
args = parser.parse_args()
script = args.script.resolve().read_text()
native.prerequisites()
identity = secrets.token_hex(8)
normal_state = store.state_dir()
root = normal_state / ("native-integration-" + identity)
os.environ["TAUCETI_FLEET_STATE"] = str(root)
directory = root / "vms" / identity
directory.mkdir(parents=True, mode=0o700)
unit = "tauceti-native-integration-" + identity + ".service"
bridge = normal_state / "vms" / ("native-integration-" + identity)
bridge.parent.mkdir(parents=True, exist_ok=True)
bridge.symlink_to(directory, target_is_directory=True)
started, terminal = time.time(), {}
try:
    native.stage({}, directory, "unregistered-offline-test")
    if args.input:
        shutil.copytree(args.input, directory / "control/input", ignore=shutil.ignore_patterns("*.git"))
    (directory / "control/offline-job.sh").write_text(script)
    (directory / "control/offline-job.sh").chmod(0o644)
    subprocess.run(["systemd-run", "--user", "--quiet", "--unit=" + unit, "--slice=tauceti-fleet-ci.slice",
                    "-p", "MemorySwapMax=0", "-p", "KillMode=control-group", "-p", "RemainAfterExit=yes",
                    "-p", "RuntimeMaxSec=" + str(args.timeout), *native.properties(),
                    *(["-p", "MemoryMax=" + str(args.memory_max_mib) + "M"] if args.memory_max_mib else []),
                    *native.argv(directory, offline=not args.stock_runner)], check=True)
    print(json.dumps(dict(unit=unit, directory=str(directory), started_at=started)), flush=True)
    deadline = time.monotonic() + args.timeout
    launched = time.monotonic()
    while time.monotonic() < deadline:
        terminal = store.read(directory / "native-result.json", {})
        if terminal:
            break
        phase = None
        progress = store.read(directory / "control/native-progress.json", {})
        lines = (progress.get("phases") or "").splitlines()
        if lines:
            fields = lines[-1].split()
            if len(fields) == 2 and float(fields[1]) >= started:
                phase = fields[0]
        if ((args.cancel_after is not None and time.monotonic() - launched >= args.cancel_after)
                or (args.cancel_phase and phase == args.cancel_phase)):
            subprocess.run(["systemctl", "--user", "stop", unit], check=True)
            terminal = dict(cancelled=True, phase=phase, seconds=time.time() - started)
            break
        state = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
        if state.returncode:
            fields = subprocess.check_output(["systemctl", "--user", "show", unit, "-p", "Result", "-p", "ExecMainStatus", "-p", "MemoryPeak"], text=True)
            terminal = dict(infrastructure_error=dict(reason="unit exited without a terminal record", service=fields))
            store.atomic(directory / "integration-failure.json", terminal)
            break
        time.sleep(1)
    else:
        raise TimeoutError("Offline integration deadline")
finally:
    subprocess.run(["systemctl", "--user", "stop", unit], check=True)
    fields = subprocess.check_output(["systemctl", "--user", "show", unit, "-p", "ControlGroup", "--value"], text=True).strip()
    if fields and (Path("/sys/fs/cgroup") / fields.lstrip("/") / "cgroup.events").exists():
        assert "populated 0" in (Path("/sys/fs/cgroup") / fields.lstrip("/") / "cgroup.events").read_text()
    native.cleanup(dict(directory=str(directory), unit=unit))
    bridge.unlink()
    assert not directory.exists()
    print(json.dumps(dict(result=terminal, evidence=str(root / "native-results" / identity), released=True)), flush=True)
failed = bool(terminal.get("infrastructure_error")) or (not terminal.get("cancelled") and terminal.get("exit_code") != 0)
if failed != args.expect_infrastructure_failure:
    raise SystemExit(1)
