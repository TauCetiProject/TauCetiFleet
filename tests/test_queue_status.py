import contextlib
import copy
import io
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tauceti_fleet import ci_overview, cli, github, pool_activity, remote, store
from tauceti_fleet import queue_status as queue


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

    def test_stuck_heads_line_appears_only_when_the_queue_reports_them(self):
        self.assertNotIn("Stuck heads", self.render())
        self.document["conditions"] = [dict(kind="stuck_heads_observed", since="2026-11-08T23:30:00Z")]
        self.assertIn("  Stuck heads: 0 (no final state", self.render())
        self.document["conditions"] += [dict(kind="stuck_head", since="2026-11-08T18:36:00Z"),
                                        dict(kind="stuck_head", since="2026-11-08T20:00:00Z"),
                                        dict(kind="stuck_heads_truncated")]
        self.assertIn("  Stuck heads: 2+, oldest 5h (no final state", self.render())
        self.document["conditions"] = [dict(kind="stuck_heads_observed", since="2026-11-08T23:30:00Z"),
                                       dict(kind="stuck_heads_partial")]
        self.assertIn("  Stuck heads: 0+ (no final state", self.render())
        self.assertIn("; partial pass).", self.render())

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

    def test_membership_advance_keeps_live_progress_and_other_batches_fresh(self):
        self.batch["version"] += 5
        report = self.report()
        self.assertEqual(len(report["value"]["batches"]), 3)
        self.assertEqual(report["value"]["batches"][1]["active_tasks"][0]["batch_version"], 349)
        output = self.render(report)
        self.assertIn("worker 2: preparation (earlier membership); Lake 36,611/38,051 tasks", output)
        self.assertIn("CI reporting results", output)
        self.assertIn("Waiting for placement: 7 PRs", output)

    def test_unclaimed_task_for_earlier_membership_waits_for_refresh(self):
        self.batch["version"] += 5
        self.document["claims"] = []
        with patch.object(ci_overview, "fetch", return_value=self.document):
            report = queue.refresh(self.manifest)
        self.assertNotIn("error", report)
        output = self.render(report)
        self.assertIn("membership changed; awaiting task refresh", output)
        self.assertNotIn("Lake", output)
        self.assertNotIn("cached", output)

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
                       lambda q: q["batches"][1]["active_tasks"][0].update(batch_version=350),
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

    def test_running_sandboxed_phase_step_is_named(self):
        attempt = self.head["latest_attempt"]
        steps = [("Build exact candidate under bwrap, offline", "building"),
                 ("Audit the candidate under bwrap (duplicates, axioms)", "auditing duplicates and axioms"),
                 ("Check the module system under bwrap", "checking the module system"),
                 ("Environment lint under bwrap", "environment lint"),
                 ("Style lint under bwrap", "style lint"),
                 ("Record declaration names and stage cache outputs under bwrap", "recording declaration names")]
        for index, (name, label) in enumerate(steps):
            with self.subTest(name=name):
                attempt["phases"] = [
                    *(dict(name="candidate / sandboxed-build: " + done, status="completed",
                           started_at="2026-11-08T23:10:00Z", completed_at="2026-11-08T23:18:51Z")
                      for done, _ in steps[:index]),
                    dict(name="candidate / sandboxed-build: " + name, status="in_progress",
                         started_at="2026-11-08T23:20:00Z")]
                output = self.render()
                self.assertIn(f"CI {label} since 09 Nov 10:20 AEDT", output)
                self.assertNotIn("build phase finished", output)
        attempt["phases"][-1].update(status="completed", completed_at="2026-11-08T23:30:00Z")
        attempt["phases"].append(dict(name="candidate / sandboxed-build: Report build and bump-guard statuses to the PR head commit",
                                      status="in_progress", started_at="2026-11-08T23:31:00Z"))
        self.assertIn("CI reporting results since 09 Nov 10:31 AEDT; build phase finished 09 Nov 10:30 AEDT",
                      self.render())

    def live_head(self):
        self.manifest["domains"]["jobs"] = dict(url=self.path)
        self.head["latest_attempt"].update(run_id="44442676966", run_attempt=1, phases=[
            dict(name="candidate / sandboxed-build: Build exact candidate under bwrap, offline",
                 status="in_progress", started_at="2026-11-08T22:16:00Z")])
        progress = dict(coverage="untrusted_counters_trusted_reporter", observed_at="2026-11-08T23:35:30Z",
                        phase="build", completed_modules=25433, last_module="TauCeti.Private.Detail",
                        completed_tasks=103917, total_tasks=113955, tasks_per_minute=850.4)
        other = dict(run_id="44442676966", run_attempt=1, active=True, name="pick-runner / pick",
                     progress=dict(coverage="steps_only", completed_modules=None))
        build = dict(other, name="sandboxed-build", progress=progress)
        jobs = dict(schema="tauceti.operations.jobs/v1", job_pages=[dict(url=self.path), dict(url=self.path)])
        detail = dict(batch=dict(id="392"), task_pages=[])
        return progress, [self.document, detail, jobs, dict(records=[dict(other, run_id="1"), other, build])]

    def test_head_build_shows_lake_jobs_rate_and_eta_from_the_public_job_report(self):
        progress, documents = self.live_head()
        with patch.object(ci_overview, "fetch", side_effect=documents) as fetch, \
                patch.object(github, "request", side_effect=AssertionError("GitHub scan")):
            result = queue.refresh(self.manifest)
        self.assertEqual(fetch.call_count, 4, "the first page held the run, so later pages are not read")
        # A build job already finished while its run continues can sit behind terminal-only pages.
        terminal = dict(records=[dict(run_id="1", run_attempt=1, active=False)])
        with patch.object(ci_overview, "fetch", side_effect=[*documents[:3], terminal, documents[3]]):
            self.assertIn("build_progress", queue.refresh(self.manifest)["value"]["batches"][0])
        output = self.render(result)
        self.assertIn("CI building since 09 Nov 09:16 AEDT\n" + " " * 33
                      + "build: phase build, 103,917/113,955 Lake jobs (91.2%), 850/min, ETA 09 Nov 10:47 AEDT\n",
                      output)
        self.assertNotIn("Private", json.dumps(result))
        # An audit phase keeps the finished counter but has no rate or ETA.
        result["value"]["batches"][0]["build_progress"].update(phase="audit-axioms", completed_tasks=113955,
                                                               tasks_per_minute=None)
        self.assertIn("build: phase axiom audit, 113,955/113,955 Lake jobs (100.0%)\n", self.render(result))
        # A rate too small for any representable ETA keeps the counters.
        result["value"]["batches"][0]["build_progress"].update(phase="build", completed_tasks=100,
                                                               tasks_per_minute=1e-300)
        self.assertIn("build: phase build, 100/113,955 Lake jobs (0.1%), 0/min\n", self.render(result))
        # Stale reports say so; a passed attempt no longer shows the build line.
        self.now += 600
        self.assertIn("reported 10m ago", self.render(result))
        result["value"]["batches"][0]["latest_attempt"]["outcome"] = "pass"
        self.assertNotIn("build: ", self.render(result))

    def test_head_build_without_counters_or_reporter_shows_only_the_step(self):
        progress, documents = self.live_head()
        for patch_value in (dict(completed_tasks=None, total_tasks=None, tasks_per_minute=None),
                            dict(coverage="steps_only"), dict(completed_tasks=9, total_tasks=4)):
            with self.subTest(patch=patch_value):
                progress_copy = dict(progress, **patch_value)
                page = dict(records=[dict(documents[3]["records"][2], progress=progress_copy)])
                with patch.object(ci_overview, "fetch", side_effect=[*documents[:3], page, dict(records=[])]):
                    output = self.render(queue.refresh(self.manifest))
                self.assertIn("CI building since 09 Nov 09:16 AEDT", output)
                if "coverage" in patch_value:
                    self.assertNotIn("build: ", output)
                else:
                    self.assertIn("build: phase build, 25,433 TauCeti modules built\n", output)
        with patch.object(ci_overview, "fetch", side_effect=[*documents[:2], OSError]):
            output = self.render(queue.refresh(self.manifest))
        self.assertIn("CI building since 09 Nov 09:16 AEDT", output)
        self.assertNotIn("build: ", output)

    def test_bad_timezone_fails_before_any_host_calls(self):
        with patch.object(remote, "inventory", side_effect=AssertionError("network")), contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main(["status", "--timezone", "Invalid/Zone"]), 0)


if __name__ == "__main__":
    unittest.main()
