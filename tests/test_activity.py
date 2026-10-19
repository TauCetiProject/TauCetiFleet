import contextlib
import io
import json
import os
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
            running=0,
            pr_observed_at=90,
        )
        snapshots = [dict(activity=dict(available=True, summary=summary)) for _ in range(2)]
        result = activity.combine(
            snapshots, {("o/r", 7): dict(state="closed", landed_at=50)}, {("o/r", 7): "a"}, {("o/r", 7): 10}, 100
        )
        s = result["summary"]
        self.assertEqual(s["rounds"]["total"]["review"]["finished"], 2)
        self.assertEqual(s["pr_windows"]["total"], dict(authored=1, landed=1))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            activity.display(result)
        self.assertIn("stale", output.getvalue())


if __name__ == "__main__":
    unittest.main()
