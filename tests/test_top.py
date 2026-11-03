import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capacity, cli, store, top, vm

GIB = 1024**3
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class FakeHost(top.Host):
    def __init__(self):
        self.disk, self.net, self.net_mbps, self.cgroup = "nvme0n1", "eth0", 1000, None

    def ci_slice(self):
        return 400.0, 880.0


def sample(t, cpu, guests):
    return dict(t=t, cpu=cpu, disk=(0, 2_000_000, 1_000), net=(10**12, 10**9), guests=guests)


class Top(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + k.upper(): str(self.root / k)
                                     for k in ("config", "state", "data")})
        env.start()
        self.addCleanup(env.stop)
        store.update_host(**capacity.settings(store.host_config(), ci="both", maximum=2))
        for patcher in (patch.object(top, "controller_line", return_value="CTRL  active"),
                        patch.object(top, "pressure", return_value=0.0)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def frame(self, previous, current):
        return [ANSI.sub("", line) for line in top.render(FakeHost(), previous, current, 3.0)]

    def test_frame_shows_guests_and_recent_results(self):
        now = time.time()
        directory = self.root / "vms/a"
        directory.mkdir(parents=True)
        (directory / "disk.qcow2").write_bytes(b"x" * 8192)
        store.atomic(vm.slot_file({"ci_slot": 1}, "vm.json"),
                     dict(slot=1, instance="a", unit="a.service", busy=True, run_id=42, job_started_at=now - 600,
                          directory=str(directory)))
        store.event("ci_result", at=now - 60, conclusion="success", run_seconds=1200,
                    classification=dict(reason="pr_workflow_outcome"))
        store.event("ci_result", at=now - 30, conclusion="failure", run_seconds=600,
                    classification=dict(reason="candidate_result"))
        previous = sample(now - 3, (1000, 600), {"a": (100, 30.0)})
        current = sample(now, (2000, 1100), {"a": (400, 31.5)})
        text = "\n".join(self.frame(previous, current))
        self.assertIn("CI    1 busy / 1 guests / limit 2", text)
        self.assertIn("50.0%", text)  # host CPU busy across the interval
        self.assertIn("CIMEM", text)
        self.assertNotIn("GUARD", text)
        self.assertIn(top.RUNS + "42", text)
        self.assertIn("10.0m", text)
        self.assertIn("last hour: 2 jobs (~48/day), 1 failed, median 15m", text)
        self.assertIn("candidate_result", text)

    def test_empty_host_renders_without_guests_or_events(self):
        now = time.time()
        text = "\n".join(self.frame(sample(now - 3, (0, 0), {}), sample(now, (100, 100), {})))
        self.assertIn("0 busy / 0 guests", text)
        self.assertIn("last hour: 0 jobs", text)

    def test_refreshes_do_not_leak_database_connections(self):
        store.event("ci_result", conclusion="success", run_seconds=60)
        now = time.time()
        frame = (sample(now - 3, (0, 0), {}), sample(now, (100, 50), {}))
        self.frame(*frame)
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(200):
            self.frame(*frame)
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), before)

    def test_events_are_read_only(self):
        store.event("ci_result", conclusion="success", run_seconds=60)
        with patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
            top.recent_results()
        self.assertIn("mode=ro", connect.call_args.args[0])
        rows = json.loads(json.dumps(top.recent_results()[:3]))
        self.assertEqual(rows, [1, 0, 1.0])

    def test_cli_parses_top(self):
        args = cli.parser().parse_args(["top", "--interval", "5", "--once"])
        self.assertEqual((args.command, args.interval, args.once), ("top", 5.0, True))


if __name__ == "__main__":
    unittest.main()
