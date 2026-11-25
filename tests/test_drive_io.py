"""Per-drive write latency comes from /proc/diskstats deltas, once a minute."""

import json
import tempfile
import unittest
from pathlib import Path

from tauceti_fleet import drive_io, monitoring


def line(major, minor, name, reads, read_ms, writes, sectors, write_ms, busy=0, flight=0):
    return f"{major:4d} {minor:7d} {name} {reads} 0 {reads * 8} {read_ms} {writes} 0 {sectors} {write_ms} {flight} {busy} 0 0 0 0 0 0 0\n"


class DriveIOTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stats = self.root / "diskstats"

    def write(self, *rows):
        self.stats.write_text("".join(rows))

    def test_latency_is_write_time_over_writes_and_partitions_are_omitted(self):
        self.write(line(259, 0, "nvme0n1", 10, 5, 100, 800, 200), line(259, 2, "nvme0n1p2", 10, 5, 100, 800, 200),
                   line(9, 127, "md127", 0, 0, 200, 1600, 0), line(252, 0, "zram0", 1, 1, 1, 1, 1))
        sampler = drive_io.DriveIO(self.stats, self.root / "history.jsonl")
        self.assertIsNone(sampler.poll(now=0))
        self.write(line(259, 0, "nvme0n1", 70, 65, 6100, 800 + 60 * 2048, 200 + 6000 * 11, busy=30000),
                   line(259, 2, "nvme0n1p2", 70, 65, 6100, 800, 200),
                   line(9, 127, "md127", 0, 0, 12200, 1600 + 120 * 2048, 0))
        self.assertIsNone(sampler.poll(now=30))
        value = sampler.poll(now=60)
        rows = {row["device"]: row for row in value["drives"]}
        self.assertEqual(set(rows), {"nvme0n1", "md127"})
        self.assertEqual(rows["nvme0n1"]["write_latency_ms"], 11.0)
        self.assertEqual(rows["nvme0n1"]["writes_per_second"], 100.0)
        self.assertEqual(rows["nvme0n1"]["write_mib_per_second"], 1.0)
        self.assertEqual(rows["nvme0n1"]["read_latency_ms"], 1.0)
        self.assertEqual(rows["nvme0n1"]["utilization"], 0.5)
        self.assertEqual(rows["md127"]["write_latency_ms"], 0.0)
        self.assertEqual(rows["md127"]["write_mib_per_second"], 2.0)
        history = [json.loads(text) for text in (self.root / "history.jsonl").read_text().splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["interval_seconds"], 60)

    def test_counter_reset_and_idle_drive_are_safe(self):
        self.write(line(259, 0, "nvme0n1", 10, 5, 100, 800, 200), line(259, 1, "nvme1n1", 10, 5, 100, 800, 200))
        sampler = drive_io.DriveIO(self.stats, self.root / "history.jsonl")
        sampler.poll(now=0)
        self.write(line(259, 0, "nvme0n1", 1, 1, 1, 1, 1), line(259, 1, "nvme1n1", 10, 5, 100, 800, 200))
        rows = sampler.poll(now=60)["drives"]
        self.assertEqual([row["device"] for row in rows], ["nvme1n1"])
        self.assertIsNone(rows[0]["write_latency_ms"])
        self.assertEqual(rows[0]["writes_per_second"], 0)

    def test_unreadable_source_keeps_last_value(self):
        sampler = drive_io.DriveIO(self.root / "missing", self.root / "history.jsonl")
        self.assertIsNone(sampler.poll(now=0))
        self.assertFalse((self.root / "history.jsonl").exists())

    def test_history_is_bounded(self):
        history = self.root / "history.jsonl"
        history.write_text("x" * 99 + "\n" + "y" * 99 + "\n")
        sampler = drive_io.DriveIO(self.stats, history)
        original = drive_io.HISTORY_BYTES
        drive_io.HISTORY_BYTES = 100
        self.addCleanup(setattr, drive_io, "HISTORY_BYTES", original)
        sampler.record(dict(at=1))
        self.assertEqual(history.read_text().splitlines(), ["y" * 99, '{"at": 1}'])

    def test_monitoring_projects_only_numbers_and_device_names(self):
        value = monitoring.drives(dict(at=1795579200, interval_seconds=60, drives=[
            dict(device="nvme0n1", write_latency_ms=7.2, writes_per_second=16000, secret="no"),
            dict(device="../etc", write_latency_ms=-1)]))
        self.assertEqual(value["observed_at"], "2026-11-25T04:00:00Z")
        self.assertEqual(value["drives"][0]["device"], "nvme0n1")
        self.assertEqual(value["drives"][0]["write_latency_ms"], 7.2)
        self.assertNotIn("secret", value["drives"][0])
        self.assertIsNone(value["drives"][1]["device"])
        self.assertIsNone(value["drives"][1]["write_latency_ms"])
        self.assertIsNone(monitoring.drives(None))
        self.assertIsNone(monitoring.drives(dict(drives="bad")))


if __name__ == "__main__":
    unittest.main()
