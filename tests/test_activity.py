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
                rounds={w: {"fix": dict(finished=1, failed=0)} for w in activity.WINDOWS},
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
        expected_ends = [header.index(h) + len(h) for h in ("1h", "3h", "24h", "7d", "All time")]
        data_rows = [line for line in lines if re.search(r"\d+\s+\d+\s+\d+\s+\d+$", line)]
        for line in data_rows:
            self.assertEqual([m.end() for m in re.finditer(r"\d+", line)], expected_ends, line)
        for kind in ("roadmap", "review", "fix-ci", "progress", "security-audit", "no-work"):
            line = next(line for line in data_rows if line.strip().startswith(kind + " "))
            self.assertEqual(re.findall(r"\d+", line), ["0"] * 5)
        self.assertNotIn("unrecorded", output.getvalue())
        self.assertNotIn("Older resource logs", output.getvalue())
        self.assertNotIn("totals cover", output.getvalue())
        self.assertNotIn("Outcome rows are subsets", output.getvalue())
        pr_start = next(i for i, line in enumerate(lines) if line.startswith("PRs authored"))
        self.assertEqual(lines[pr_start - 1], "")

    def test_storage_and_expansion_status_uses_only_prepared_data(self):
        summary = dict(
            prepared_at=100,
            history_since=1,
            rounds={w: {} for w in activity.WINDOWS},
            pr_windows={w: {} for w in activity.WINDOWS},
            running=0,
            open_prs=0,
            unknown_prs=0,
            unknown_landings=0,
            storage_cleanup=dict(
                at=100,
                managed_bytes=64 * 2**30,
                protected_bytes=32 * 2**30,
                free_bytes=200 * 2**30,
                reclaimed_bytes=8 * 2**30,
                pressure=True,
            ),
            expanded_cache=dict(workers=2, hits=1200, expanded=10, seconds=4),
        )
        value = dict(available=True, summary=summary)
        with (
            patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            patch("pathlib.Path.rglob", side_effect=AssertionError("storage scan in status")),
            patch("tauceti_fleet.github.request", side_effect=AssertionError("GitHub in status")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            activity.display(value)
            combined = activity.combine_status([value, value])
        self.assertIn("Managed caches: 64.0 GiB", output.getvalue())
        self.assertIn("32.0 GiB held by live workers", output.getvalue())
        self.assertIn("1200 reused; 10 expanded", output.getvalue())
        self.assertEqual(combined["summary"]["storage_cleanup"]["managed_bytes"], 128 * 2**30)
        self.assertEqual(combined["summary"]["expanded_cache"]["hits"], 2400)

    def test_status_reads_only_prepared_activity_without_process_or_sql(self):
        Path(self.tmp.name, "summary.json").write_text(json.dumps(dict(groups={"m": dict(prepared_at=1)}, errors=[])))
        with (
            patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            patch("tauceti_fleet.github.request", side_effect=AssertionError("GitHub in status")),
        ):
            self.assertEqual(activity.cached("m")["summary"]["prepared_at"], 1)
            self.assertTrue(host.rpc(dict(action="status"))["activity"]["available"] is False)

    def test_cached_missing_finish_details_reach_combined_status_without_io(self):
        summary = dict(
            prepared_at=100,
            history_since=1,
            rounds={w: {} for w in activity.WINDOWS},
            pr_windows={w: {} for w in activity.WINDOWS},
            running=0,
            open_prs=0,
            unknown_prs=0,
            unknown_landings=0,
            interrupted_unfinished=5,
            interrupted_unfinished_details=dict(
                started_1h=1, started_24h=2, latest=dict(started=90, worker="worker-one", round_id="affected")
            ),
        )
        Path(self.tmp.name, "summary.json").write_text(json.dumps(dict(groups={"m": summary}, errors=[])))
        with (
            patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            patch("tauceti_fleet.github.request", side_effect=AssertionError("GitHub in status")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            value = activity.cached("m")
            activity.display(activity.combine_status([value, value]))
        self.assertIn("2 in 1h; 4 in 24h; 10 all time", output.getvalue())
        self.assertIn("worker-one, round affected", output.getvalue())
        self.assertIn("Check worker logs", output.getvalue())

    def test_combined_status_warns_only_for_stale_unresolved_pr_evidence(self):
        now = 1_000_000
        summary = dict(
            prepared_at=now,
            history_since=1,
            rounds={w: {} for w in activity.WINDOWS},
            pr_windows={w: dict(authored=1, landed=0) for w in activity.WINDOWS},
            running=0,
            open_prs=1,
            unknown_prs=0,
            unknown_landings=0,
            pr_observed_at=now - 3 * 86400,
        )
        for freshness, expected in (
            (None, None),
            (dict(observed_at=now - 2700, missing_since=None), None),
            (dict(observed_at=now - 10800, missing_since=None), "oldest unresolved PR observation 3h 0m"),
            (dict(observed_at=now - 2700, missing_since=now - 86400), "without an observation; created 24h 0m"),
        ):
            with self.subTest(freshness=freshness):
                row = dict(summary)
                if freshness is not None:
                    row["pr_freshness"] = freshness
                with (
                    patch("tauceti_fleet.activity_summary.time.time", return_value=now),
                    patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
                    patch("subprocess.run", side_effect=AssertionError("process in status")),
                    patch("tauceti_fleet.github.request", side_effect=AssertionError("GitHub in status")),
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    activity.display(activity.combine_status([dict(available=True, summary=row)]))
                if expected:
                    self.assertIn(expected, output.getvalue())
                else:
                    self.assertNotIn("PR evidence:", output.getvalue())

    def test_spending_rates_render_from_prepared_json_without_io_or_misalignment(self):
        summary = dict(
            prepared_at=100,
            history_since=1,
            rounds={w: {} for w in activity.WINDOWS},
            pr_windows={w: dict(authored=0, landed=0) for w in activity.WINDOWS},
            costs=dict(
                accounting_version=2,
                spending={
                    w: dict(spent_usd="37.035", covered_seconds=10800, worker_seconds=21600, usd_per_hour="12.345")
                    for w in activity.WINDOWS
                },
            ),
            running=0,
            open_prs=0,
            unknown_prs=0,
            unknown_landings=0,
        )
        Path(self.tmp.name, "summary.json").write_text(json.dumps(dict(groups={"m": summary}, errors=[])))
        with (
            patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            patch("tauceti_fleet.github.request", side_effect=AssertionError("GitHub in status")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            activity.display(activity.cached("m"))
        lines = output.getvalue().splitlines()
        heading = next(line for line in lines if line.startswith("Finished rounds"))
        ends = [heading.index(h) + len(h) for h in ("1h", "3h", "24h", "7d", "All time")]
        row = next(line for line in lines if line.startswith("API spend ($/elapsed hour)"))
        self.assertEqual([m.end() for m in re.finditer(r"\$\d+\.\d+", row)], ends)
        self.assertEqual(row.count("$12.34"), 5)
        per_worker = next(line for line in lines if line.startswith("API spend ($/worker-hour)"))
        self.assertEqual(per_worker.count("$6.17"), 5)
        section = next(i for i, line in enumerate(lines) if line.startswith("Spending (recorded usage)"))
        self.assertEqual(lines[section - 1], "")
        self.assertIn("All time", lines[section])
        hours = next(line for line in lines if line.startswith("Worker-hours recorded"))
        self.assertEqual(hours.count("6.00"), 5)
        self.assertLess(lines.index(hours), lines.index(per_worker))
        self.assertLess(lines.index(per_worker), lines.index(row))
        self.assertIn("Currently: 0 workers working;", output.getvalue())
        self.assertNotIn("closed/merged PRs lack landing evidence", output.getvalue())

    def test_snapshot_rpc_cannot_request_github_refresh(self):
        with patch.object(host, "snapshot", return_value={}) as snapshot:
            host.rpc(dict(action="snapshot"))
        snapshot.assert_called_once_with(refresh=False)

    def test_partial_loc_rates_render_from_cached_snapshots_without_io(self):
        summary = dict(
            prepared_at=100,
            history_since=1,
            rounds={w: {} for w in activity.WINDOWS},
            pr_windows={w: dict(authored=0, landed=0) for w in activity.WINDOWS},
            costs=dict(
                windows={
                    "total": dict(
                        estimated_usd="100",
                        landed_loc_added=200,
                        open_loc_added=0,
                        landed_prs=2,
                        open_prs=1,
                        measured_pr_weight=2,
                        known_loc_prs=2,
                        missing_loc_prs=1,
                        usd_per_loc=None,
                    ),
                    "7d": dict(
                        estimated_usd="30",
                        landed_loc_added=0,
                        open_loc_added=0,
                        landed_prs=0,
                        open_prs=1,
                        measured_pr_weight=0,
                        known_loc_prs=0,
                        missing_loc_prs=1,
                        usd_per_loc=None,
                    ),
                }
            ),
            running=0,
            open_prs=1,
            unknown_prs=0,
            unknown_landings=0,
        )
        Path(self.tmp.name, "summary.json").write_text(json.dumps(dict(groups={"m": summary}, errors=[])))
        with (
            patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            patch("tauceti_fleet.github.request", side_effect=AssertionError("GitHub in status")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            activity.display(activity.cached("m"))
        lines = output.getvalue().splitlines()
        heading = next(line for line in lines if line.startswith("Costs / initiated PR output"))
        rate = next(line for line in lines if line.startswith("Estimated $ / weighted LOC"))
        expected = [heading.index(h) + len(h) for h in ("7 days", "All time")]
        self.assertEqual([m.end() for m in re.finditer(r"~\$(?:\d+\.\d+|∞)", rate)], expected)
        self.assertIn("~$0.4000", rate)
        self.assertIn("~$∞", rate)
        self.assertIn("~250", output.getvalue())

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

    def test_report_uses_prepared_inventory_after_old_data_is_dropped(self):
        prepared = dict(
            scope="m",
            repo="o/r",
            number=2,
            worker="w",
            created=50,
            state="OPEN",
            landed=None,
            observed=90,
            head="h",
            source=None,
        )
        events = [dict(id=str(n), kind="pr_created", repository="o/r", pr=n, at=at) for n, at in ((1, 10), (2, 55))]
        with report.database() as db:
            db.execute(
                "INSERT INTO prs VALUES ('o/r',1,?)", (json.dumps(dict(repository="o/r", pr=1, state="closed")),)
            )
        with (
            patch.object(report, "export_events", return_value=events),
            patch.object(report, "settled_costs", return_value=[]),
            patch("tauceti_fleet.process.executable", return_value=None),
            patch.object(activity, "inventory", return_value=[prepared]),
            patch.object(activity, "cached", return_value=dict(available=True)),
            patch.object(report, "refresh_prs", side_effect=AssertionError("API refresh")),
            patch.object(report, "bors_landings", side_effect=AssertionError("Old closed PR restored")),
        ):
            snapshot = report.snapshot(dict(machine_id="m", created_at=1))
        self.assertEqual([p["pr"] for p in snapshot["prs"]], [2])
        self.assertEqual([(e["pr"], e["at"]) for e in snapshot["events"] if e["kind"] == "pr_created"], [(2, 50)])
        summary = report.summarize([snapshot], now=100)
        self.assertEqual(summary["reports"][-1]["windows"]["3h"]["prs_originated"], 1)

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
