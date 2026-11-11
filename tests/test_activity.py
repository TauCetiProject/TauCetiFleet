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

    def test_attributed_output_pools_local_credit_and_keeps_status_read_only(self):
        def host_value(cost, credit):
            return dict(
                available=True,
                summary=dict(
                    prepared_at=100,
                    history_since=1,
                    rounds={w: {} for w in activity.WINDOWS},
                    pr_windows={w: dict(authored=1, landed=1) for w in activity.WINDOWS},
                    running=0,
                    open_prs=0,
                    unknown_prs=0,
                    unknown_landings=0,
                    costs=dict(
                        accounting_version=3,
                        windows={
                            w: dict(
                                estimated_usd=str(cost),
                                loc_basis="landed_at",
                                landed_loc_added=15000,
                                landed_prs=1,
                                known_loc_prs=1,
                                missing_loc_prs=0,
                                attribution_version=1,
                                attributed_loc=str(credit),
                                attribution_known_prs=1,
                                attribution_missing_prs=0,
                            )
                            for w in ("24h", "7d", "total")
                        },
                    ),
                ),
            )

        first, second = host_value(17, 8000), host_value(20, 2000)
        with (
            patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            patch("pathlib.Path.rglob", side_effect=AssertionError("archive scan in status")),
            patch("tauceti_fleet.github.request", side_effect=AssertionError("GitHub in status")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            combined = activity.combine_status([first, second])
            activity.display(combined)
        row = combined["summary"]["costs"]["windows"]["24h"]
        self.assertEqual(row["attributed_loc"], "10000")
        self.assertEqual(row["estimated_usd"], "37")
        self.assertIn("Estimated $ / attributed LOC", output.getvalue())
        self.assertIn("~$0.0037", output.getvalue())
        self.assertIn("2/2", output.getvalue())
        for row in second["summary"]["costs"]["windows"].values():
            row.update(attributed_loc=None, attribution_known_prs=0, attribution_missing_prs=1)
        with contextlib.redirect_stdout(io.StringIO()) as partial_output:
            partial = activity.combine_status([first, second])
            activity.display(partial)
        self.assertEqual(partial["summary"]["costs"]["windows"]["24h"]["attributed_loc"], "8000")
        self.assertIn("partial ~$0.0046", partial_output.getvalue())
        del second["summary"]["costs"]["windows"]["24h"]["attribution_version"]
        row = activity.combine_status([first, second])["summary"]["costs"]["windows"]["24h"]
        self.assertIsNone(row["attributed_loc"])

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
            work_recovery=dict(
                records=57,
                incomplete=5,
                corrupt=0,
                errors=0,
                recovery_eligible=26,
                recovery_resuming=17,
                cleanup_backlog=219,
                reasons=["worker/round: Provider credit balance exhausted"],
            ),
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
        self.assertIn("Saved work: 57 records; 5 incomplete", output.getvalue())
        self.assertNotIn("Repair recovery:", output.getvalue())
        self.assertNotIn("  Cleanup:", output.getvalue())
        self.assertNotIn("Provider credit balance exhausted", output.getvalue())
        self.assertEqual(summary["work_recovery"]["recovery_resuming"], 17)
        self.assertEqual(len(summary["work_recovery"]["reasons"]), 1)
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
        self.assertIn("Round reporting gaps: 2 in 1h; 4 in 24h (by start time)", output.getvalue())
        self.assertIn("worker-one, round affected", output.getvalue())
        self.assertIn("See worker log", output.getvalue())
        self.assertNotIn("10 all time", output.getvalue())

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
        section = next(i for i, line in enumerate(lines) if line.startswith("Spending "))
        self.assertEqual(lines[section - 1], "")
        self.assertIn("All time", lines[section])
        hours = next(line for line in lines if line.startswith("Worker-hours recorded"))
        self.assertEqual(hours.count("6.00"), 5)
        self.assertLess(lines.index(hours), lines.index(per_worker))
        self.assertLess(lines.index(per_worker), lines.index(row))
        self.assertIn("Currently: 0 workers working;", output.getvalue())
        self.assertNotIn("closed/merged PRs lack landing evidence", output.getvalue())

        # The same rows use live rates, with recorded fallback for old windows.
        summary["costs"].update(
            accounting_version=3,
            quality=dict(native=10, estimated=3, incomplete=1),
            accounting=dict(live=1, settled_estimates=3, recovered_estimates=3, unresolved_stopped=0),
            spending_including_live={
                w: dict(summary["costs"]["spending"][w], usd_per_hour="15", usd_per_worker_hour="7.5")
                for w in ("1h", "3h", "24h", "7d")
            },
            live_estimate=dict(observed_at=990, session_observed_at=995),
        )
        with (
            patch("tauceti_fleet.activity_summary.time.time", return_value=1000),
            patch("sqlite3.connect", side_effect=AssertionError("SQL in status")),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            activity.display(dict(available=True, summary=summary))
        text = output.getvalue()
        lines = text.splitlines()
        row = next(line for line in lines if line.startswith("API spend ($/elapsed hour)"))
        self.assertEqual(re.findall(r"\$\d+\.\d+", row), ["$15.00"] * 4 + ["$12.34"])
        per_worker = next(line for line in lines if line.startswith("API spend ($/worker-hour)"))
        self.assertEqual(re.findall(r"\$\d+\.\d+", per_worker), ["$7.50"] * 4 + ["$6.17"])
        self.assertEqual([m.end() for m in re.finditer(r"\$\d+\.\d+", row)], ends)
        for label in ("With live estimates", "Costs:", "Live estimates", "Accounting:"):
            self.assertNotIn(label, text)

        summary["costs"]["accounting"]["unresolved_stopped"] = 2
        summary["costs"]["live_estimate"]["session_observed_at"] = 800
        with (
            patch("tauceti_fleet.activity_summary.time.time", return_value=1000),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            activity.display(dict(available=True, summary=summary))
        self.assertIn("Accounting: 2 stopped conversations with unresolved usage.", output.getvalue())
        self.assertIn("Live estimates stale: updated 200s ago.", output.getvalue())

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
                        loc_basis="landed_at",
                        landed_loc_added=200,
                        open_loc_added=0,
                        landed_prs=3,
                        open_prs=1,
                        measured_pr_weight=2,
                        known_loc_prs=2,
                        missing_loc_prs=1,
                        usd_per_loc=None,
                    ),
                    "7d": dict(
                        estimated_usd="30",
                        loc_basis="landed_at",
                        landed_loc_added=0,
                        open_loc_added=0,
                        landed_prs=1,
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
        heading = next(line for line in lines if line.startswith("Costs / landed PR output"))
        rate = next(line for line in lines if line.startswith("Estimated $ / landed LOC"))
        expected = [heading.index(h) + len(h) for h in ("7 days", "All time")]
        self.assertEqual([m.end() for m in re.finditer(r"~\$(?:\d+\.\d+|∞)", rate)], expected)
        self.assertIn("~$0.3333", rate)
        self.assertIn("~$∞", rate)
        self.assertIn("~300", output.getvalue())
        combined = activity.combine_status([dict(available=True, summary=summary), dict(available=False)])
        total = combined["summary"]["costs"]["windows"]["total"]
        self.assertEqual(total["loc_basis"], "landed_at")
        self.assertEqual(total["landed_loc"], "300")
        self.assertIsNone(total["usd_per_loc"])
        self.assertIn("Some hosts have no prepared activity", combined["errors"])

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
