"""Build one shared-tree entry in an isolated pool, without touching the host's own pool.

Run from the repo on a host with the native image: python3 tests/integration/ci_tree_build.py [--keep]
The builder runs exactly as the controller would start it, at PR CI priority, but its pool and
state live under a fresh directory; only a pressure bridge goes into the host's vms/ directory,
so the builder's Lean waits for host permits like any guest. No runner is registered.
"""

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tauceti_fleet import store  # noqa: E402

normal_state, normal_data = store.state_dir(), store.data_dir()
root = normal_data.parent / ("tauceti-fleet-ci-tree-test-" + secrets.token_hex(4))
(root / "data").mkdir(parents=True)
(root / "data/images").symlink_to(normal_data / "images", target_is_directory=True)
os.environ["TAUCETI_FLEET_DATA"] = str(root / "data")
os.environ["TAUCETI_FLEET_STATE"] = str(root / "state")
from tauceti_fleet import ci_tree  # noqa: E402

for name in ("entries", "staging", "trash", "leases"):
    (ci_tree.root() / name).mkdir(parents=True, exist_ok=True)
unit = ci_tree.start()
state = ci_tree.builder_state()
bridge = normal_state / "vms" / ("ci-tree-test-" + state["identity"])
bridge.symlink_to(state["instance"], target_is_directory=True)
print(json.dumps(dict(root=str(root), unit=unit, directory=state["directory"])), flush=True)
try:
    while ci_tree.unit_active(unit):
        time.sleep(10)
finally:
    bridge.unlink(missing_ok=True)
result = store.read(Path(state["directory"]) / "build-result.json", {})
print(json.dumps(dict(result=result, entries=[e for e, _ in ci_tree.entries()]), indent=1))
subprocess.run(["systemctl", "--user", "reset-failed", unit], check=False)
if "--keep" not in sys.argv:
    # The tree and instance are owned by the guest's mapped runner.
    subprocess.run(["unshare", "--user", "--map-auto", "--map-root-user", "rm", "-rf", "--", str(root)], check=False)
raise SystemExit(0 if result.get("outcome") == "published" else 1)
