import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import activity, host, report


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "TAUCETI_ACTIVITY_DIR": self.tmp.name,
                "TAUCETI_FLEET_CONFIG": self.tmp.name + "/config",
                "TAUCETI_FLEET_STATE": self.tmp.name + "/state",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_table_aligns_all_rows_and_lists_zero_count_work_types(self):
        value = dict(
            available=True,
            summary=dict(
                prepared_at=100,
                history_since=1,
                rounds={
                    w: {"fix": dict(finished=1, failed=0), "unknown": dict(finished=1, failed=0, unknown=1)}
                    for w in activity.WINDOWS
                },
                pr_windows={w: dict(authored=0, landed=0) for w in activity.WINDOWS},
                running=0,
                open_prs=0,
                unknown_prs=0,
                unknown_landings=0,
            ),
        )
        value["summary"]["rounds"]["total"]["fix"]["finished"] = 12345678
        with contextlib.redirect_stdout(io.StringIO()) as output:
            activity.display(value)
        lines = output.getvalue().splitlines()
        header = next(line for line in lines if line.startswith("Finished rounds"))
        expected_ends = [header.index(h) + len(h) for h in ("3h", "24h", "7d", "Recorded total")]
        data_rows = [line for line in lines if re.search(r"\d+\s+\d+\s+\d+\s+\d+$", line)]
        for line in data_rows:
            self.assertEqual([m.end() for m in re.finditer(r"\d+", line)], expected_ends, line)
        for kind in ("roadmap", "review", "fix-ci", "progress", "security-audit", "no-work"):
            line = next(line for line in data_rows if line.strip().startswith(kind + " "))
            self.assertEqual(re.findall(r"\d+", line), ["0"] * 4)
        self.assertTrue(any("type unrecorded" in line for line in data_rows))
        self.assertIn("Older resource logs did not record round types or outcomes", output.getvalue())
        self.assertIn("Outcome rows are subsets", output.getvalue())

    def test_status_reads_only_prepared_activity_without_process_or_sql(self):
        Path(self.tmp.name, "summary.json").write_text(json.dumps(dict(groups={"m": dict(prepared_at=1)}, errors=[])))
        with (
            patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            patch("tauceti_fleet.github.request", side_effect=AssertionError("GitHub in status")),
        ):
            self.assertEqual(activity.cached("m")["summary"]["prepared_at"], 1)
            self.assertTrue(host.rpc(dict(action="status"))["activity"]["available"] is False)

    def test_snapshot_rpc_cannot_request_github_refresh(self):
        with patch.object(host, "snapshot", return_value={}) as snapshot:
            host.rpc(dict(action="snapshot"))
        snapshot.assert_called_once_with(refresh=False)

    def test_cached_snapshot_prs_keep_verified_landings_without_api(self):
        p = dict(
            scope="m",
            repo="o/r",
            number=7,
            worker="w",
            created=10,
            state="CLOSED",
            landed=20,
            observed=30,
            head="h",
            source="bors-main-evidence",
        )
        Path(self.tmp.name, "prs.json").write_text(json.dumps(dict(prs=[p])))
        with (
            patch.object(report, "export_events", return_value=[]),
            patch.object(report, "settled_costs", return_value=[]),
            patch("tauceti_fleet.process.executable", return_value=None),
            patch.object(report, "refresh_prs", side_effect=AssertionError("API refresh")),
        ):
            value = report.snapshot(dict(machine_id="m", created_at=1))
        self.assertEqual(value["prs"][0]["landed_at"], 20)

    def test_combined_pr_counts_deduplicate_across_hosts(self):
        summary = dict(
            prepared_at=100,
            history_since=1,
            rounds={w: {"review": dict(finished=1, failed=0)} for w in activity.WINDOWS},
            round_types=[*activity.ROUND_TYPES, "future-worker-kind"],
            running=0,
            pr_observed_at=90,
        )
        snapshots = [dict(activity=dict(available=True, summary=summary)) for _ in range(2)]
        result = activity.combine(
            snapshots, {("o/r", 7): dict(state="closed", landed_at=50)}, {("o/r", 7): "a"}, {("o/r", 7): 10}, 100
        )
        s = result["summary"]
        self.assertIn("future-worker-kind", s["round_types"])
        self.assertEqual(s["rounds"]["total"]["review"]["finished"], 2)
        self.assertEqual(s["pr_windows"]["total"], dict(authored=1, landed=1))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            activity.display(result)
        self.assertIn("stale", output.getvalue())


if __name__ == "__main__":
    unittest.main()
