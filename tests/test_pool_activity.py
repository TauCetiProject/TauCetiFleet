import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import cli, ci_overview, github, hostos, pool_activity as pools, remote, status_follow, store

hostos.MACOS = False


class PoolActivity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TAUCETI_FLEET_CONFIG": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.scope = "a" * 64
        self.manifest = dict(schema="tauceti.operations.manifest/v1", scope=self.scope,
                             domains=dict(jobs=dict(url=f"/v1/operations/objects/{self.scope}/{'b' * 64}.json")))
        completed = dict(id="123", run_id="77", run_attempt=2, name="sandboxed-build", status="completed",
                         conclusion="failure", completed_at="2026-11-07T00:23:00Z",
                         url="https://github.com/TauCetiProject/TauCeti/actions/runs/77/job/123")
        running = dict(id="124", run_id="78", status="in_progress", started_at="2026-11-07T00:04:00Z")
        waiting = dict(id="125", run_id="79", status="queued", created_at="2026-11-07T00:19:00Z",
                       started_at="2026-11-07T00:19:00Z")
        summary = dict(running=3, waiting=2, latest_completion=completed, oldest_running=running,
                       oldest_waiting=waiting, windows=[dict(seconds=s, finished=12, success=7, failure=2,
                                                            cancelled=1, skipped=2, other=0) for s in pools.WINDOWS])
        self.jobs = dict(schema="tauceti.operations.jobs/v1", scope=dict(repositories=["TauCetiProject/TauCeti"]),
                         truncated=True, pool_activity=dict(coverage="partial", through="2026-11-07T00:24:00Z",
                         pools=[dict(pool="namespace", builds=summary, all=dict(summary, running=9, waiting=4))]))
        self.now = pools.timestamp("2026-11-07T00:25:00Z")

    def report(self):
        return dict(value=pools.parse(self.manifest, self.jobs))

    def render(self, report=None, now=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            pools.display(self.report() if report is None else report, now=self.now if now is None else now)
        return output.getvalue()

    def test_shared_summary_retains_outcomes_job_identities_and_all_job_scope(self):
        value = self.report()["value"]
        row = value["pools"][0]
        self.assertEqual(row["builds"]["running"], 3)
        self.assertEqual(row["all"]["running"], 9)
        self.assertEqual(row["builds"]["latest_completion"]["run_attempt"], 2)
        self.assertIn("/job/123", row["builds"]["latest_completion"]["url"])
        self.assertEqual(row["builds"]["oldest_waiting"]["id"], "125")
        self.assertEqual(row["builds"]["windows"][0]["skipped"], 2)
        self.assertTrue(value["partial"])

    def test_compact_build_table_has_outcomes_ages_and_partial_freshness(self):
        text = self.render()
        self.assertIn("CI pool activity — builds (partial observations", text)
        self.assertIn("age 1m", text)
        row = next(line for line in text.splitlines() if line.strip().startswith("namespace"))
        self.assertIn("3/2", row)
        self.assertNotIn("9/4", row)
        self.assertIn("2m failure", row)
        self.assertIn("21m", row)
        self.assertIn("6m", row)
        self.assertEqual(row.count("12 f2 c1"), 3)
        self.assertLessEqual(len(row), 120)
        self.assertIn("--json includes job identities/URLs", text)

    def test_missing_completion_is_not_presented_as_an_old_success_or_zero_age(self):
        for category in ("builds", "all"):
            for key in ("latest_completion", "oldest_running", "oldest_waiting"):
                self.jobs["pool_activity"]["pools"][0][category][key] = None
        text = self.render()
        self.assertIn("— = none observed", text)
        row = next(line for line in text.splitlines() if line.strip().startswith("namespace"))
        self.assertIn("unknown", row)
        self.assertNotIn("success", text)

    def test_retained_aggregate_coverage_is_independent_of_capped_job_pages(self):
        self.jobs["pool_activity"]["coverage"] = "retained_observations"
        self.jobs["truncated"] = True
        self.jobs["partition_errors"] = ["bounded detail list"]
        value = self.report()["value"]
        self.assertFalse(value["partial"])
        self.assertIn("retained observations", self.render(dict(value=value)))
        self.jobs["pool_activity"]["coverage"] = "unavailable"
        with self.assertRaises(ValueError):
            pools.parse(self.manifest, self.jobs)

    def test_missing_windows_are_unknown_and_corrupt_counts_are_rejected(self):
        self.jobs["pool_activity"]["pools"][0]["builds"]["windows"] = []
        self.assertIn("unknown", self.render())
        for invalid in (-1, True, "3", None):
            with self.subTest(invalid=invalid):
                bad = copy.deepcopy(self.jobs)
                bad["pool_activity"]["pools"][0]["builds"]["running"] = invalid
                with self.assertRaises(ValueError):
                    pools.parse(self.manifest, bad)

    def test_stale_and_failed_refresh_keeps_last_facts_visible(self):
        previous = self.report()
        store.atomic(pools.cache_path(), previous)
        with patch.object(ci_overview, "fetch", side_effect=OSError("offline")) as fetch:
            result = pools.refresh()
        fetch.assert_called_once()
        self.assertEqual(result["value"], previous["value"])
        text = self.render(result, self.now + 600)
        self.assertIn("STALE", text)
        self.assertIn("cached", text)
        self.assertIn("refresh failed", text)
        self.assertIn("3/2", text)

    def test_no_cache_and_failed_public_source_does_not_invent_zero_counts(self):
        with patch.object(ci_overview, "fetch", side_effect=OSError):
            result = pools.refresh()
        text = self.render(result)
        self.assertIn("CI pool activity: unavailable", text)
        self.assertNotIn("0/0", text)
        pools.cache_path().write_text("broken JSON")
        self.assertIn("error", pools.cached())

    def test_refresh_reads_only_manifest_and_its_public_jobs_object(self):
        with patch.object(ci_overview, "fetch", side_effect=[self.manifest, self.jobs]) as fetch, patch.object(github, "request", side_effect=AssertionError("GitHub scan")):
            result = pools.refresh()
        self.assertNotIn("error", result)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[0].args[0], pools.MANIFEST)
        self.assertEqual(fetch.call_args_list[1].args[0], pools.ORIGIN + self.manifest["domains"]["jobs"]["url"])
        self.assertEqual(pools.cached(), result)

    def test_manifest_cannot_redirect_fetch_to_another_origin_or_object_scope(self):
        for path in ("https://other.example/job.json", "/v1/operations/objects/" + "c" * 64 + "/" + "b" * 64 + ".json"):
            with self.subTest(path=path), patch.object(ci_overview, "fetch", return_value=dict(self.manifest, domains=dict(jobs=dict(url=path)))) as fetch:
                result = pools.refresh()
            fetch.assert_called_once()
            self.assertIn("error", result)

    def test_status_fetches_global_summary_once_for_multiple_hosts_and_json_preserves_it(self):
        entries = {name: dict(target=name, machine_id=name) for name in ("one", "two")}
        with patch.object(remote, "inventory", return_value=entries), patch.object(remote, "call", side_effect=lambda target, *a, **kw: dict(machine_id=target)), patch.object(pools, "refresh", return_value=self.report()) as refresh, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status", "--json"]), 0)
        refresh.assert_called_once()
        value = json.loads(output.getvalue())["_ci"]["pool_activity"]["value"]
        self.assertEqual(value["pools"][0]["builds"]["latest_completion"]["id"], "123")

    def test_follow_redraw_uses_prepared_public_cache_without_network(self):
        entry = dict(target="one", machine_id="one")
        store.atomic(pools.cache_path(), self.report())
        with patch.object(remote, "inventory", return_value=dict(one=entry)), patch.object(pools, "refresh", side_effect=AssertionError("network during redraw")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status", "--json"], _status_results=dict(one=dict(machine_id="one"))), 0)
        self.assertEqual(json.loads(output.getvalue())["_ci"]["pool_activity"]["value"]["pools"][0]["pool"], "namespace")

    def test_all_job_table_and_follow_flag_select_the_all_scope(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            pools.display(self.report(), all_jobs=True, now=self.now)
        self.assertIn("CI pool activity — all jobs", output.getvalue())
        self.assertIn("9/4", output.getvalue())
        entry = dict(target="one", machine_id="one")

        def follow(selected, render, **kwargs):
            self.assertIs(kwargs["refresh"], pools.refresh)
            render(dict(one=dict(machine_id="one")))
            return 0

        with patch.object(remote, "inventory", return_value=dict(one=entry)), patch.object(status_follow, "run", side_effect=follow), patch.object(status_follow, "render_with_cli", return_value="") as render:
            self.assertEqual(cli.main(["status", "one", "--all-jobs", "--follow", "--json"]), 0)
        self.assertEqual(render.call_args.args[0], ["status", "one", "--json", "--all-jobs"])

    def test_cache_write_failure_does_not_hide_a_healthy_public_observation(self):
        with patch.object(ci_overview, "fetch", side_effect=[self.manifest, self.jobs]), patch.object(pools, "atomic", side_effect=OSError):
            result = pools.refresh()
        self.assertEqual(result["value"]["pools"][0]["builds"]["running"], 3)
        self.assertIn("cache could not be saved", result["error"])


if __name__ == "__main__":
    unittest.main()
