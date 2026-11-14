import contextlib
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import ci_overview as overview
from tauceti_fleet import cli, github, host, remote, store


class Overview(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {**{"TAUCETI_FLEET_" + k.upper(): str(self.root / k) for k in ("config", "state", "data")},
                                     "TAUCETI_GITHUB_SHARED_DIR": str(self.root / "shared")})
        env.start()
        self.addCleanup(env.stop)
        self.config = dict(store.host_config(), github_account="science", profile="ai-for-science", max_open_prs=128, workers_enabled=True)

    def worker(self, slot, scope=None, limit=128):
        identity = "fleet-" + self.config["machine_id"][:8] + "-" + str(slot)
        store.atomic(store.state_dir() / "workers" / (identity + ".json"), dict(id=identity, alive=True, max_open_prs=limit,
                     applied_spec=dict(github_account="science", roadmap_only=scope, roadmap_skip=[])))

    def snapshot(self, sources):
        value = dict(version=1, repository=overview.REPOSITORY, sources=sources)
        store.atomic(store.state_dir() / "ci-overview.json", value)
        return value

    def test_foreground_status_never_queries_or_starts_collectors(self):
        value = self.snapshot(dict(backlog=dict(value=dict(pr_builds=732, all_workflows=1352, created_since="2026-10-08T00:00:00Z"), observed_at=100)))
        with patch.object(overview, "fetch", side_effect=AssertionError("API in status")), patch.object(github, "request", side_effect=AssertionError("API in status")), patch.object(overview, "collect", side_effect=AssertionError("collector in status")), patch.object(overview.concurrent.futures, "ThreadPoolExecutor", side_effect=AssertionError("background work in status")):
            self.assertEqual(overview.cached(self.config), value)
            self.assertEqual(host.rpc(dict(action="status"))["ci_overview"], value)

    def test_missing_and_corrupt_caches_are_unknown_not_zero(self):
        self.assertFalse(overview.cached()["available"])
        store.atomic(store.state_dir() / "ci-overview.json", [])
        self.assertFalse(overview.cached()["available"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(overview.cached())
        self.assertIn("CI jobs running: unknown", out.getvalue())
        self.assertIn("CI backlog: unknown", out.getvalue())

    def test_failed_attempt_is_rate_limited_and_keeps_last_known_facts(self):
        old = dict(value=dict(pr_builds=9, all_workflows=10), observed_at=123)
        self.snapshot(dict(backlog=old))
        with patch.object(overview, "bors_history", side_effect=RuntimeError), patch.object(overview, "bors", side_effect=RuntimeError), patch.object(overview, "pools", side_effect=RuntimeError), patch.object(overview, "backlog", side_effect=RuntimeError) as api, patch.object(overview, "pressure", side_effect=RuntimeError):
            first = overview.collect(self.config)
            second = overview.collect(self.config)
        api.assert_called_once()
        self.assertEqual(first, second)
        self.assertEqual(first["sources"]["backlog"]["value"], old["value"])
        self.assertEqual(first["sources"]["backlog"]["observed_at"], 123)
        self.assertIn("error", first["sources"]["backlog"])

    def test_pool_observation_requests_only_prepared_shared_data(self):
        value = dict(read_only=True, complete=True, running_jobs=dict(github=3, namespace=15), observed_at=time.time() * 1000)
        with patch.object(overview, "fetch", return_value=value) as fetch:
            data, _ = overview.pools(self.config)
        self.assertIn("/runner-capacity/cached?", fetch.call_args.args[0])
        self.assertEqual(data["counts"]["namespace"], 15)
        for field in ("read_only", "complete", "running_jobs"):
            with patch.object(overview, "fetch", return_value={k: v for k, v in value.items() if k != field}):
                with self.assertRaises(store.FleetError):
                    overview.pools(self.config)

    def test_concurrent_collectors_do_not_duplicate_source_requests(self):
        entered, release = threading.Event(), threading.Event()

        def slow(config):
            entered.set()
            release.wait(5)
            return {}, time.time()

        with patch.object(overview, "bors_history", side_effect=RuntimeError), patch.object(overview, "bors", side_effect=slow) as request, patch.object(overview, "pools", return_value=({}, 100)), patch.object(overview, "backlog", return_value=(dict(pr_builds=1, all_workflows=2), 100)), patch.object(overview, "pressure", return_value=({}, 100)):
            thread = threading.Thread(target=overview.collect, args=(self.config,))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                deadline = time.monotonic() + 2
                while "backlog" not in overview.cached().get("sources", {}) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(overview.cached()["sources"]["backlog"]["value"]["pr_builds"], 1,
                                 'fast sources must be saved before the slow source finishes')
                started = time.monotonic()
                overview.collect(self.config)
                self.assertLess(time.monotonic() - started, .5)
                request.assert_called_once()
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_corrupt_attempt_or_backward_clock_does_not_disable_collection(self):
        for attempt in ([], dict(next_attempt_at=time.time() + overview.INTERVAL * 2)):
            store.atomic(store.state_dir() / "ci-overview-attempt.json", attempt)
            with patch.object(overview, "bors_history", side_effect=RuntimeError), patch.object(overview, "bors", side_effect=RuntimeError) as request, patch.object(overview, "pools", side_effect=RuntimeError), patch.object(overview, "backlog", side_effect=RuntimeError), patch.object(overview, "pressure", side_effect=RuntimeError):
                overview.collect(self.config)
                request.assert_called_once()

    def test_fleet_combination_preserves_distinct_account_pressure(self):
        values = [dict(sources=dict(pressure=dict(value=dict(account=account, scopes=[]), observed_at=stamp)))
                  for account, stamp in (("one", 100), ("two", 200), ("one", 300))]
        combined = overview.combine(values)
        self.assertEqual(set(combined["pressure_accounts"]), {"one", "two"})
        self.assertEqual(combined["pressure_accounts"]["one"]["observed_at"], 300)

    def survey_cache(self, name, scope, *, count=200, rows=(), numbers="", skip="", complete=True):
        identity = hashlib.sha256(b"https://github-state.taucetiproject.org\0").hexdigest()
        args = dict(roadmap=scope, skip=skip, numbers=numbers,
                    stages="bump,fix,fix-ci,lint-repair,overlap-fix,overlap-scan,progress,rebase,review")
        if name is None:
            name = hashlib.sha256(json.dumps([overview.REPOSITORY, "survey", args, False], sort_keys=True).encode()).hexdigest()
        path = self.root / "shared/gateway" / identity / "responses" / (name + ".json")
        store.atomic(path, dict(query=dict(identity=identity, repository=overview.REPOSITORY, operation="survey", args=args),
                                payload=dict(status="ok", complete=complete, author_counts=dict(science=count), data=list(rows))))
        return path

    def test_pressure_uses_actual_cached_scope_not_global_total(self):
        self.worker(1)
        self.worker(2, "SymplecticGeometry")
        self.survey_cache("all", "auto", count=200)
        rows = [dict(pr=dict(author=dict(login="science"), isDraft=False)),
                dict(pr=dict(author=dict(login="other"), isDraft=False)),
                dict(pr=dict(author=dict(login="science"), isDraft=True))]
        self.survey_cache("focused", "SymplecticGeometry", rows=rows)
        self.survey_cache("partial", "Bad", complete=False)
        self.survey_cache("one-pr", "Wrong", numbers="1")
        self.survey_cache("personal", "RepresentationTheory")
        with patch.object(overview, "fetch", side_effect=AssertionError("network pressure scan")), patch.object(github, "request", side_effect=AssertionError("network pressure scan")):
            data, _ = overview.pressure(self.config)
        scopes = {r["scope"]: r for r in data["scopes"]}
        self.assertEqual(set(scopes), {"auto", "SymplecticGeometry"})
        self.assertEqual(scopes["auto"]["count"], 200)
        self.assertTrue(scopes["auto"]["blocked"])
        self.assertEqual(scopes["SymplecticGeometry"]["count"], 1)
        self.assertFalse(scopes["SymplecticGeometry"]["blocked"])
        self.assertEqual(data["machine_id"], self.config["machine_id"])

    def test_fresh_scoped_author_count_updates_each_live_global_limit(self):
        self.worker(1, limit=512)
        self.worker(2, limit=1024)
        now = time.time()
        observations = (
            (self.survey_cache(None, "auto", count=900), now - 36_000),
            (self.survey_cache("newer", "PDE", count=526), now - 20),
            (self.survey_cache("excluded", "MorseTheory", count=12, skip="PDE"), now - 15),
            (self.survey_cache("partial", "PDE", count=12, complete=False), now - 10),
            (self.survey_cache("targeted", "PDE", count=12, numbers="123"), now - 5),
        )
        for path, stamp in observations:
            os.utime(path, (stamp, stamp))

        data, stamp = overview.pressure(self.config)
        self.assertEqual(stamp, observations[1][0].stat().st_mtime)
        self.assertEqual(
            [(row["scope"], row["count"], row["limit"], row["blocked"]) for row in data["scopes"]],
            [("auto", 526, 512, True), ("auto", 526, 1024, False)],
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(dict(sources=dict(pressure=dict(value=data, observed_at=stamp))))
        self.assertIn("all roadmaps: 526/512 blocked, 526/1024 open non-draft roadmap PRs", out.getvalue())
        self.assertNotIn("stale", out.getvalue())

    def test_ci_only_hosts_never_publish_default_worker_pressure(self):
        self.survey_cache("all", "auto", count=200)
        with self.assertRaises(store.FleetError):
            overview.pressure(dict(self.config, workers_enabled=False, profile="ci-enrolled"))

    def test_host_policies_are_distinct_even_for_the_same_account(self):
        values = [dict(sources=dict(pressure=dict(value=dict(account="science", machine_id=machine, scopes=[]), observed_at=100)))
                  for machine in ("a" * 32, "b" * 32)]
        self.assertEqual(len(overview.combine(values)["pressure_accounts"]), 2)

    def test_malformed_source_does_not_abort_other_status_lines(self):
        out = io.StringIO()
        value = dict(sources={name: dict(value={}, observed_at="invalid") for name in ("bors", "pools", "pressure")})
        value["sources"]["backlog"] = dict(value=self.backlog_value(), observed_at=time.time())
        with contextlib.redirect_stdout(out):
            overview.display(value)
        self.assertIn("Bors: unknown", out.getvalue())
        self.assertIn("CI backlog: 1230 observed jobs waiting to start; 53 running", out.getvalue())

    def test_unknown_machine_keys_merge_and_use_inventory_names(self):
        machine = "c" * 32
        value = dict(sources=dict(pools=dict(value=dict(counts={"fleet-" + machine: 2, "fleet-" + machine[:12]: 1}), observed_at=time.time())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(overview.combine([value], {"fleet-" + machine[:12]: "Newhost"}))
        self.assertIn("Newhost 3", out.getvalue())
        self.assertNotIn("fleet-" + machine, out.getvalue())

    def backlog_value(self, *, partial=False):
        def group(**changes):
            return {**dict(pending=0, queued=0, in_progress=0, stuck=0, unconfirmed=0,
                           oldest_waiting_at=None), **changes}
        return dict(schema=overview.BACKLOG_SCHEMA, partial=partial, waiting_jobs=1230, running_jobs=53,
                    pr_builds=group(pending=1200, queued=30, stuck=134),
                    other_workflows=group(pending=2, queued=1, stuck=776, unconfirmed=4))

    def public_backlog(self, *, partial=False):
        scope = "a" * 64
        manifest = dict(schema="tauceti.operations.manifest/v1", scope=scope,
                        domains=dict(jobs=dict(url=f"/v1/operations/objects/{scope}/{'b' * 64}.json")))
        row = self.backlog_value(partial=partial)
        row.pop("schema")
        row.pop("partial")
        jobs = dict(schema="tauceti.operations.jobs/v1", observed_at="2026-11-12T00:00:00Z",
                    truncated=True, source_truncated=True,
                    backlog=dict(schema=overview.BACKLOG_SCHEMA, coverage="partial" if partial else "observed",
                                 repositories={overview.REPOSITORY: row}))
        return manifest, jobs

    def test_backlog_reads_two_shared_objects_without_github_or_job_scans(self):
        manifest, jobs = self.public_backlog()
        with patch.object(overview, "fetch", side_effect=[manifest, jobs]) as fetch, patch.object(github, "request", side_effect=AssertionError("GitHub scan")):
            data, stamp = overview.backlog(self.config)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(data, self.backlog_value())
        self.assertEqual(stamp, 1794441600)
        self.assertTrue(fetch.call_args.args[0].startswith("https://ci-data.taucetiproject.org/v1/operations/objects/"))

    def test_status_reports_actual_jobs_without_turning_workflow_records_into_builds(self):
        data = self.backlog_value(partial=True)
        data['waiting_jobs'] = 178
        data['pr_builds']['oldest_waiting_at'] = time.time() - 8 * 3600
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(overview, 'fetch', side_effect=AssertionError('foreground network')):
            overview.display(dict(sources=dict(backlog=dict(value=data, observed_at=time.time()))))
        text = out.getvalue()
        self.assertIn('CI backlog: 178 observed jobs waiting to start; 53 running (builds and automation;', text)
        self.assertIn('Total waiting work: unknown. The queue monitor cannot account for all workflows.', text)
        for misleading in ('PR builds', 'pending,', 'queued;', 'verified stuck', 'without confirmed active jobs', 'oldest PR wait', '1200', '1230'):
            self.assertNotIn(misleading, text)

    def test_missing_job_evidence_is_a_monitoring_gap_even_when_source_says_observed(self):
        for state in ('pending', 'stuck', 'unconfirmed'):
            data = self.backlog_value()
            for row in (data['pr_builds'], data['other_workflows']):
                row.update(pending=0, queued=0, stuck=0, unconfirmed=0)
            data['pr_builds'][state] = 1022
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                overview.display(dict(sources=dict(backlog=dict(value=data, observed_at=time.time()))))
            text = out.getvalue()
            self.assertIn('Total waiting work: unknown.', text)
            self.assertNotIn('1022', text)

    def test_complete_job_observation_has_no_false_monitoring_alarm(self):
        data = self.backlog_value()
        for row in (data['pr_builds'], data['other_workflows']):
            row.update(pending=0, stuck=0, unconfirmed=0)
        data.update(waiting_jobs=0, running_jobs=0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(dict(sources=dict(backlog=dict(value=data, observed_at=time.time()))))
        self.assertIn('CI backlog: 0 observed jobs waiting to start; 0 running', out.getvalue())
        self.assertNotIn('Total waiting work: unknown.', out.getvalue())

    def test_invalid_or_absent_shared_backlog_does_not_invent_empty_queue(self):
        for alteration in ("absent", "scope", "schema", "negative", "boolean"):
            manifest, jobs = self.public_backlog()
            source = jobs["backlog"]
            if alteration == "absent":
                jobs.pop("backlog")
            elif alteration == "scope":
                source["repositories"] = {}
            elif alteration == "schema":
                source["schema"] = "unknown"
            else:
                source["repositories"][overview.REPOSITORY]["pr_builds"]["pending"] = -1 if alteration == "negative" else True
            with self.subTest(alteration=alteration), patch.object(overview, "fetch", side_effect=[manifest, jobs]), self.assertRaises(store.FleetError):
                overview.backlog(self.config)

    def test_newer_legacy_counts_cannot_replace_detailed_backlog(self):
        good = dict(value=self.backlog_value(), observed_at=time.time() - 30)
        old = dict(value=dict(pr_builds=146, all_workflows=917, created_since="2026-10-08T00:00:00Z"), observed_at=time.time())
        combined = overview.combine([dict(sources=dict(backlog=good)), dict(sources=dict(backlog=old))])
        self.assertEqual(combined["sources"]["backlog"], good)
        self.assertIsNone(overview.source_value("backlog", old))

    def test_bors_total_and_legacy_backlog_cache(self):
        batches = list(reversed([dict(id=i, size=size, state="waiting") for i, size in enumerate((26, 26, 2))]))
        value = dict(sources=dict(
            bors=dict(value=dict(batches=batches, held=0), observed_at=time.time()),
            backlog=dict(value=dict(pr_builds=71, all_workflows=100), observed_at=time.time())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(value)
        self.assertIn("Bors: 54=26+26+2", out.getvalue())
        self.assertIn("CI backlog: unknown", out.getvalue())
        self.assertNotIn("71 PR-build", out.getvalue())

    def test_history_windows_show_outcome_fractions_attempts_and_sampled_job_runtime(self):
        windows = [dict(window=label, completed_batches=4, successful_batches=2,
                        failed_batches=1, canceled_batches=1, landed_prs=12, landed_batches_without_receipts=0,
                        staging_pr_runs=30, staging_unique_prs=10,
                        mean_staging_runs_per_pr=3.0, batches_with_unknown_membership=0)
                   for label in ("1h", "3h", "24h", "7d")]
        data = dict(windows=windows, durations=[dict(window="1h", samples=2, mean_seconds=625,
                                                    p50_seconds=625, p95_seconds=1000)])
        windows[2]["batches_with_unknown_membership"] = 1
        windows[3]["batches_with_unknown_membership"] = 20
        windows[3].update(completed_batches=1, successful_batches=0, failed_batches=0, canceled_batches=1,
                          mean_staging_runs_per_pr=None)
        value = dict(sources=dict(bors_history=dict(value=data, observed_at=time.time())))
        out = io.StringIO()
        with patch.object(overview, "fetch", side_effect=AssertionError("foreground network")), contextlib.redirect_stdout(out):
            overview.display(value)
        text = out.getvalue()
        lines = [line.split() for line in text.splitlines()]
        self.assertIn(["1h", "3h", "24h", "7d"], lines)
        self.assertIn(["Succeeded", "66.7%", "66.7%", "66.7%", "—"], lines)
        self.assertFalse(any(line[:1] == ["Failed"] for line in lines))
        self.assertIn(["Canceled", "1", "1", "1", "1"], lines)
        self.assertIn(["Build", "p50", "10m", "25s", "—", "—", "—"], lines)
        self.assertIn(["Build", "p95", "16m", "40s", "—", "—", "—"], lines)
        self.assertIn(["Build", "samples", "2", "—", "—", "—"], lines)
        self.assertNotIn("Batches missing PR lists", text)
        self.assertNotIn("Mean build", text)
        self.assertNotIn("partial membership", text)
        self.assertNotIn("sample mean; history filling", text)
        self.assertNotIn("Runs/PR counts distinct", text)
        data["windows"][0]["successful_batches"] = 10
        self.assertIsNone(overview.source_value("bors_history", dict(value=data, observed_at=time.time())))

    def test_history_keeps_more_complete_fresh_runtime_backfill_across_hosts(self):
        windows = [dict(window=label, completed_batches=0, successful_batches=0,
                        failed_batches=0, canceled_batches=0, landed_prs=0,
                        landed_batches_without_receipts=0, staging_pr_runs=0,
                        staging_unique_prs=0, mean_staging_runs_per_pr=None,
                        batches_with_unknown_membership=0)
                   for label in ("1h", "3h", "24h", "7d")]
        now = time.time()
        complete = dict(value=dict(windows=windows, durations=[], runtime_runs_checked=20),
                        observed_at=now - 300)
        recent = dict(value=dict(windows=windows, durations=[], runtime_runs_checked=2),
                      observed_at=now)
        hosts = [dict(sources=dict(bors_history=row)) for row in (complete, recent)]
        for values in (hosts, list(reversed(hosts))):
            self.assertEqual(overview.combine(values)["sources"]["bors_history"], complete)
        complete["observed_at"] = now - 700
        self.assertEqual(overview.combine(hosts)["sources"]["bors_history"], recent)

    def test_presentation_keeps_runner_groups_on_one_line(self):
        value = dict(sources=dict(pools=dict(value=dict(counts={"github": 3, "namespace": 15, "fleet-11e2756fc11042d798f759410573a935": 12}), observed_at=time.time())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(value)
        lines = [s for s in out.getvalue().splitlines() if s.startswith("CI jobs running:")]
        self.assertEqual(len(lines), 1)
        self.assertIn("Tauchonki 12", lines[0])
        self.assertIn("Namespace 15", lines[0])

    def test_bors_follows_budgets_and_host_diagnostics_come_last(self):
        summary = dict(prepared_at=time.time(), history_since=1,
                       rounds={},
                       pr_windows={}, running=0, open_prs=0, unknown_prs=0, unknown_landings=0,
                       costs=dict(funding=dict(balance="10", available="10", pending="0", grant_rate="1")),
                       storage_cleanup=dict(removed_count=4, free_bytes=100 * 2**30))
        periods = ("1h", "3h", "24h", "7d", "total")
        summary["rounds"] = {p: {} for p in periods}
        summary["pr_windows"] = {p: {} for p in periods}
        value = dict(machine_id=self.config["machine_id"],
                     activity=dict(available=True, summary=summary),
                     status=dict(workers=[], worker_isolation=dict(events=[dict(
                         at=100, unit="worker.scope", ancestor="workers.slice", workers=[])])))
        for count in (1, 2):
            entries = {f"host{i}": dict(target=f"host{i}", machine_id=str(i)) for i in range(count)}
            values = {name: dict(value, machine_id=entry["machine_id"]) for name, entry in entries.items()}
            out = io.StringIO()
            with patch.object(remote, "inventory", return_value=entries), patch.object(remote, "call", side_effect=AssertionError("network during rendering")), contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["status"], _status_results=values), 0)
            text = out.getvalue()
            self.assertEqual(text.count("Bors:"), 1)
            self.assertEqual(text.count("Storage cleanup:"), count)
            self.assertLess(text.rindex("Budget:"), text.index("Bors:"))
            self.assertLess(text.index("Bors:"), text.index("Recent userspace OOM"))
            self.assertLess(text.index("Recent userspace OOM"), text.index("Storage cleanup:"))

    def test_timer_is_independent_of_foreground_status_and_preserves_paths(self):
        units = overview.unit_files('"/home/user/tauceti-fleet" "collect-status"', dict(TAUCETI_FLEET_STATE="/other/state"))
        self.assertIn("OnUnitInactiveSec=300", units["tauceti-fleet-observe.timer"])
        self.assertIn("RandomizedDelaySec=30", units["tauceti-fleet-observe.timer"])
        self.assertIn("TAUCETI_FLEET_STATE=/other/state", units["tauceti-fleet-observe.service"])
        self.assertNotIn("status\"", units["tauceti-fleet-observe.timer"])

    def test_zero_admissions_does_not_claim_running_rounds_are_stopped(self):
        workers = [dict(id=str(i), state="running" if i < 28 else "waiting-capacity") for i in range(40)]
        value = dict(machine_id=self.config["machine_id"], max_workers=40,
                     status=dict(active_worker_limit=0, workers=workers, memory_pressure=["host memory headroom low"]))
        entry = dict(target="local", machine_id=self.config["machine_id"])
        out = io.StringIO()
        with patch.object(remote, "inventory", return_value=dict(local=entry)), patch.object(remote, "call", return_value=value), patch.object(github, "request", side_effect=AssertionError("foreground API")), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["status"]), 0)
        self.assertIn("28 running", out.getvalue())
        self.assertIn("12 waiting for RAM", out.getvalue())
        self.assertIn("New-round admission limit: 0 / 40", out.getvalue())
