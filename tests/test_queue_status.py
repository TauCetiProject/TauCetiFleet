import contextlib
import copy
import io
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tauceti_fleet import ci_overview, cli, github, hostos, pool_activity, remote, store
from tauceti_fleet import queue_status as queue

hostos.MACOS = False


class QueueStatus(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.env = patch.dict(os.environ, {"TAUCETI_FLEET_CONFIG": temporary.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.scope = "a" * 64
        self.path = f"/v1/operations/objects/{self.scope}/{'b' * 64}.json"
        self.manifest = dict(schema="tauceti.operations.manifest/v1", scope=self.scope,
                             domains=dict(queue=dict(url=self.path)))
        self.now = queue.timestamp("2026-11-08T23:36:00Z")
        self.build = dict(stage="preparation", completed_tasks=36611, total_tasks=38051,
                          sampled_at="2026-11-08T23:35:30Z", last_progress_at="2026-11-08T23:35:25Z",
                          log_updated_at="2026-11-08T23:35:25Z")
        self.task = dict(id="79", batch_id="391", batch_version=349, kind="prepare", state="open",
                         resolution="", created_at="2026-11-08T22:37:20Z", resolved_at=None,
                         progress=dict(coverage="observed", state="artifacts_ready",
                                       observed_at="2026-11-08T23:27:03Z", build=self.build))
        self.batch = dict(id="391", repository=ci_overview.REPOSITORY, state="open", sealed=True,
                          size=349, version=349, sealed_at="2026-11-08T22:37:20Z", landed_at=None,
                          active_tasks=[self.task], latest_attempt=None, detail=dict(url=self.path))
        self.open = dict(self.batch, id="392", size=570, version=571, sealed=False,
                         sealed_at=None, active_tasks=[])
        self.head = dict(self.batch, id="389", state="head", size=876, version=881, active_tasks=[],
                         latest_attempt=dict(batch_id="389", outcome="pending", status="in_progress",
                                             phases=[dict(name="Report build and bump-guard statuses", status="in_progress")]))
        self.document = dict(schema="tauceti.operations.queue/v1", scope=dict(repositories=[ci_overview.REPOSITORY]),
                             observed_at="2026-11-08T23:35:30Z", coverage="observed", truncated=False,
                             batches=[self.head, self.batch, self.open], counts=dict(waiting=7),
                             workers=[dict(id="worker-one", state="alive / idle", observed_at="2026-11-08T23:35:30Z"),
                                      dict(id="worker-two", state="alive / artifacts_ready", observed_at="2026-11-08T23:35:30Z")],
                             claims=[dict(batch_id="391", task_id="79", worker="worker-two",
                                          expires_at="2026-11-08T23:50:00Z", created_at="2026-11-08T23:26:02Z")])

    def report(self):
        return dict(scope=self.scope, value=queue.parse(self.manifest, self.document))

    def render(self, report=None, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            queue.display(self.report() if report is None else report, now=self.now, **kwargs)
        return output.getvalue()

    def test_batch_roles_progress_and_live_ci_phase_are_distinct(self):
        output = self.render()
        self.assertIn("head", output)
        self.assertIn("next head", output)
        self.assertIn("unsealed", output)
        self.assertIn("CI reporting results", output)
        self.assertIn("worker 2: preparation; Lake 36,611/38,051 tasks", output)
        self.assertIn("advanced 09 Nov 10:35 AEDT", output)
        self.assertIn("Waiting for placement: 7 PRs", output)
        self.assertIn("worker 1 alive / idle", output)
        self.assertNotIn("96%", output, "discovered build totals may grow")

    def test_worker_inventory_names_disambiguate_hosts_during_migration(self):
        identity = "repair-3@chungus2"
        alias = "queue-worker-" + hashlib.sha256(identity.encode()).hexdigest()[:12]
        workers = [{"id":"worker-1"},{"id":alias}]
        names = queue.worker_names(workers,{"chungus2":{"target":"kim@chungus2.example.org"}})
        self.assertEqual(names[alias],identity)

    def test_live_phase_has_its_own_start_time_instead_of_old_checkpoint(self):
        self.task["progress"] = dict(coverage="observed",state="audit_axioms",observed_at="2026-11-08T23:35:00Z")
        output = self.render()
        self.assertIn("axiom audit since 09 Nov 10:35 AEDT",output)
        self.task["progress"]["state"]="artifacts_ready"
        self.assertIn("artifacts ready; checkpoint",self.render())

    def test_queued_task_without_a_live_claim_is_not_shown_as_building(self):
        self.document["claims"][0]["expires_at"] = "2026-11-08T23:30:00Z"
        output = self.render()
        self.assertIn("preparation queued", output)
        self.assertNotIn("Lake", output)
        self.document["claims"] = []
        self.assertIn("preparation queued", self.render())

    def test_progress_from_a_released_attempt_does_not_describe_a_new_owner(self):
        self.document["claims"][0]["created_at"] = "2026-11-08T23:30:00Z"
        output = self.render()
        self.assertIn("awaiting fresh worker progress", output)
        self.assertNotIn("Lake", output)

    def test_missing_progress_does_not_invent_a_build(self):
        self.task["progress"] = dict(coverage="unavailable", observed_at=None)
        output = self.render()
        self.assertIn("worker 2: preparation", output)
        self.assertNotIn("Lake", output)
        self.assertNotIn("0/0", output)

    def test_last_landing_and_next_head_wait_are_explained(self):
        self.document["batches"][0] = dict(self.head, state="landed", landed_at="2026-11-08T23:41:08Z")
        output = self.render()
        self.assertIn("Last landing: batch 389, 876 PRs at 09 Nov 10:41 AEDT", output)
        self.assertIn("Next head: batch 391 after its active preparation releases", output)

    def test_two_sealed_batches_show_next_head_and_waiting_without_admitting_more_prs(self):
        self.document["batches"][0] = dict(self.head, state="landed", landed_at="2026-11-08T23:41:08Z")
        self.document["batches"][2].update(sealed=True, sealed_at="2026-11-08T23:35:00Z")
        output = self.render()
        first = next(line for line in output.splitlines() if line.strip().startswith("391 "))
        second = next(line for line in output.splitlines() if line.strip().startswith("392 "))
        self.assertIn("next head", first)
        self.assertIn("sealed, waiting", second)
        self.assertIn("membership fixed; waiting", second)
        self.assertNotIn("receiving PRs", second)
        self.assertNotIn("sealed successor", output)

    def test_sydney_daylight_saving_and_utc_override(self):
        self.assertIn("09 Nov 10:35 AEDT", self.render())
        self.assertIn("08 Nov 23:35 UTC", self.render(timezone="UTC"))
        self.assertEqual(queue.clock("2026-06-01T00:00:00Z"), "01 Jun 10:00 AEST")

    def test_cached_stale_and_partial_facts_keep_their_observation_time(self):
        report = self.report()
        report["error"] = "Queue refresh failed (OSError)"
        report["value"]["observed_at"] = "2026-11-08T23:00:00Z"
        report["value"]["partial"] = True
        output = self.render(report)
        self.assertIn("09 Nov 10:00 AEDT", output)
        self.assertIn("STALE", output)
        self.assertIn("partial", output)
        self.assertIn("cached", output)
        self.assertIn("refresh failed", output)

    def test_no_observation_reports_unknown_not_empty_queue(self):
        self.assertIn("unavailable", self.render({}))
        self.assertNotIn("0 PRs", self.render({}))
        self.assertNotIn("No active batches", self.render({}))

    def test_invalid_source_data_is_rejected(self):
        for mutate in (lambda q: q.update(scope={}),
                       lambda q: q.update(observed_at="2026-11-08T23:35:30"),
                       lambda q: q["batches"].append(copy.deepcopy(q["batches"][0])),
                       lambda q: q["batches"][1].update(sealed="true"),
                       lambda q: q["batches"][1].update(version=350),
                       lambda q: q["batches"][1]["active_tasks"][0]["progress"]["build"].update(completed_tasks=True)):
            with self.subTest(mutate=mutate):
                document = copy.deepcopy(self.document)
                mutate(document)
                with self.assertRaises(ValueError):
                    queue.parse(self.manifest, document)

    def test_untrusted_url_cannot_redirect_or_cross_public_scope(self):
        for url in ("https://elsewhere.example/queue", self.path.replace(self.scope, "c" * 64), self.path + "?token=secret"):
            manifest = copy.deepcopy(self.manifest)
            manifest["domains"]["queue"]["url"] = url
            with self.subTest(url=url), patch.object(ci_overview, "fetch") as fetch:
                self.assertIn("error", queue.refresh(manifest))
                fetch.assert_not_called()

    def test_source_failure_preserves_same_scope_facts_but_drops_a_withdrawn_scope(self):
        previous = self.report()
        with patch.object(ci_overview, "fetch", side_effect=OSError):
            result = queue.refresh(self.manifest, previous)
        self.assertEqual(result["value"], previous["value"])
        other = dict(self.manifest, scope="c" * 64)
        with patch.object(ci_overview, "fetch", side_effect=OSError):
            self.assertNotIn("value", queue.refresh(other, previous))

    def test_only_idle_batch_history_is_fetched_and_prior_preparation_is_explained(self):
        previous = dict(self.task, id="78", batch_id="392", batch_version=542, state="submitted",
                        resolution="adopted", resolved_at="2026-11-08T23:10:53Z")
        detail = dict(batch=dict(id="392"), task_pages=[dict(url=self.path)])
        with patch.object(ci_overview, "fetch", side_effect=[self.document, detail, dict(records=[previous])]) as fetch, patch.object(github, "request", side_effect=AssertionError("GitHub scan")):
            result = queue.refresh(self.manifest)
        self.assertEqual(fetch.call_count, 3)
        self.assertIn("prepared 09 Nov 10:10 AEDT, then changed", self.render(result))

    def test_adoption_version_increment_alone_does_not_imply_stale_preparation(self):
        report = self.report()
        batch = report["value"]["batches"][-1]
        batch.update(version=543, previous_preparation=dict(batch_version=542, resolved_at="2026-11-08T23:10:53Z"))
        self.assertNotIn("then changed", self.render(report))

    def test_history_failure_does_not_discard_current_batches(self):
        with patch.object(ci_overview, "fetch", side_effect=[self.document, OSError]):
            result = queue.refresh(self.manifest)
        self.assertEqual(len(result["value"]["batches"]), 3)
        self.assertIn("preparation history unavailable", self.render(result))

    def test_cli_text_json_and_follow_redraw_use_the_same_cached_queue(self):
        report = dict(queue=self.report())
        entries = dict(one=dict(target="one", machine_id="one"))
        with patch.object(remote, "inventory", return_value=entries), patch.object(remote, "call", return_value=dict(machine_id="one")), patch.object(pool_activity, "refresh", return_value=report) as refresh, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status", "--json", "--timezone", "UTC"]), 0)
        refresh.assert_called_once()
        value = json.loads(output.getvalue())
        self.assertEqual(value["_ci"]["queue"]["value"]["batches"][1]["id"], "391")
        self.assertEqual(value["_ci"]["queue_timezone"], "UTC")
        store.atomic(pool_activity.cache_path(), report)
        with patch.object(remote, "inventory", return_value=entries), patch.object(pool_activity, "refresh", side_effect=AssertionError("network during redraw")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["status", "--timezone", "UTC"], _status_results=dict(one=dict(machine_id="one"))), 0)
        self.assertIn("08 Nov 23:35 UTC", output.getvalue())

    def test_follow_preserves_the_requested_timezone(self):
        from tauceti_fleet import status_follow

        entries = dict(one=dict(target="one", machine_id="one"))
        def follow(selected, render, **kwargs):
            render(dict(one=dict(machine_id="one")))
            return 0
        with patch.object(remote, "inventory", return_value=entries), patch.object(status_follow, "run", side_effect=follow), patch.object(status_follow, "render_with_cli", return_value="") as render:
            self.assertEqual(cli.main(["status", "--follow", "--timezone", "UTC"]), 0)
        self.assertIn("UTC", render.call_args.args[0])

    def test_shared_public_refresh_keeps_queue_independent_of_job_summary_failure(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["domains"]["jobs"] = dict(url=self.path)
        with patch.object(ci_overview, "fetch", side_effect=[manifest, OSError("jobs unavailable"), OSError("history unavailable")]), patch.object(queue, "refresh", return_value=self.report()) as refresh:
            result = pool_activity.refresh()
        self.assertIn("error", result)
        self.assertEqual(result["queue"]["value"]["waiting"], 7)
        refresh.assert_called_once_with(manifest, None)
        self.assertEqual(pool_activity.cached()["queue"], self.report())

    def test_withdrawn_queue_domain_clears_previous_facts(self):
        store.atomic(pool_activity.cache_path(), dict(queue=self.report()))
        with patch.object(ci_overview, "fetch", side_effect=[dict(self.manifest, domains={})]):
            result = pool_activity.refresh()
        self.assertNotIn("value", result["queue"])

    def test_ci_completion_is_not_a_passing_verdict_until_bound_success(self):
        attempt = self.head["latest_attempt"]
        attempt["phases"].append(dict(name="Build exact candidate under bwrap", status="completed", completed_at="2026-11-08T23:18:51Z"))
        self.assertIn("build phase finished 09 Nov 10:18 AEDT", self.render())
        self.assertNotIn("build/audits passed", self.render())
        attempt["verdicts"] = dict(build=dict(state="success", bound=True))
        self.assertIn("build/audits passed 09 Nov 10:18 AEDT", self.render())

    def test_bad_timezone_fails_before_any_host_calls(self):
        with patch.object(remote, "inventory", side_effect=AssertionError("network")), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main(["status", "--timezone", "Invalid/Zone"]), 0)


if __name__ == "__main__":
    unittest.main()
