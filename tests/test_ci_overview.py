import contextlib
import datetime
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

from tauceti_fleet import ci_overview as overview, cli, github, host, remote, store


class Overview(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {**{"TAUCETI_FLEET_" + k.upper(): str(self.root / k) for k in ("config", "state", "data")},
                                     "TAUCETI_GITHUB_SHARED_DIR": str(self.root / "shared")})
        env.start()
        self.addCleanup(env.stop)
        orphans = patch.object(overview, "CONFIRMED_ORPHAN_RUN_IDS", ())
        orphans.start()
        self.addCleanup(orphans.stop)
        self.config = dict(store.host_config(), github_account="science", profile="ai-for-science", max_open_prs=128, workers_enabled=True)

    def worker(self, slot, scope=None):
        identity = "fleet-" + self.config["machine_id"][:8] + "-" + str(slot)
        store.atomic(store.state_dir() / "workers" / (identity + ".json"), dict(id=identity, alive=True, max_open_prs=128,
                     applied_spec=dict(github_account="science", roadmap_only=scope, roadmap_skip=[])))

    def snapshot(self, sources):
        value = dict(version=1, repository=overview.REPOSITORY, sources=sources)
        store.atomic(store.state_dir() / "ci-overview.json", value)
        return value

    def test_foreground_status_never_queries_or_starts_collectors(self):
        value = self.snapshot(dict(backlog=dict(value=dict(pr_builds=732, all_workflows=1352, created_since=overview.QUEUED_RUNS_CREATED_SINCE), observed_at=100)))
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

    def survey_cache(self, name, scope, *, count=200, rows=(), numbers="", complete=True):
        identity = hashlib.sha256(b"https://github-state.taucetiproject.org\0").hexdigest()
        path = self.root / "shared/gateway" / identity / "responses" / (name + ".json")
        store.atomic(path, dict(query=dict(identity=identity, repository=overview.REPOSITORY, operation="survey", args=dict(roadmap=scope, skip="", numbers=numbers)),
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
        self.survey_cache("personal", "RepresentationTheory", count=999)
        with patch.object(overview, "fetch", side_effect=AssertionError("network pressure scan")), patch.object(github, "request", side_effect=AssertionError("network pressure scan")):
            data, _ = overview.pressure(self.config)
        scopes = {r["scope"]: r for r in data["scopes"]}
        self.assertEqual(set(scopes), {"auto", "SymplecticGeometry"})
        self.assertEqual(scopes["auto"]["count"], 200)
        self.assertTrue(scopes["auto"]["blocked"])
        self.assertEqual(scopes["SymplecticGeometry"]["count"], 1)
        self.assertFalse(scopes["SymplecticGeometry"]["blocked"])
        self.assertEqual(data["machine_id"], self.config["machine_id"])

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
        value["sources"]["backlog"] = dict(value=dict(pr_builds=3, all_workflows=8, created_since=overview.QUEUED_RUNS_CREATED_SINCE), observed_at=time.time())
        with contextlib.redirect_stdout(out):
            overview.display(value)
        self.assertIn("Bors: unknown", out.getvalue())
        self.assertIn("CI backlog: 3", out.getvalue())

    def test_unknown_machine_keys_merge_and_use_inventory_names(self):
        machine = "c" * 32
        value = dict(sources=dict(pools=dict(value=dict(counts={"fleet-" + machine: 2, "fleet-" + machine[:12]: 1}), observed_at=time.time())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(overview.combine([value], {"fleet-" + machine[:12]: "Newhost"}))
        self.assertIn("Newhost 3", out.getvalue())
        self.assertNotIn("fleet-" + machine, out.getvalue())

    def test_background_queue_counts_cost_only_two_small_requests(self):
        with patch.object(github, "request", return_value=dict(total_count=0)) as request:
            data, _ = overview.backlog(dict(self.config, github_token_file="private-file"))
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all("per_page=1" in c.args[0] for c in request.call_args_list))
        self.assertEqual(data, dict(pr_builds=0, all_workflows=0, created_since=overview.QUEUED_RUNS_CREATED_SINCE))

    def test_oldest_queued_run_uses_one_final_page_without_scanning_jobs(self):
        responses = [dict(total_count=7), dict(total_count=900),
                     dict(total_count=900, workflow_runs=[dict(created_at="2026-01-01T00:00:00Z")])]
        with patch.object(github, "request", side_effect=responses) as request:
            data, _ = overview.backlog(dict(self.config, github_token_file="private-file"))
        self.assertEqual(request.call_count, 3)
        self.assertTrue(request.call_args.args[0].endswith("&page=900"))
        self.assertTrue(all("created=%3E%3D2026-10-08T00%3A00%3A00Z" in c.args[0] for c in request.call_args_list))
        self.assertEqual(data["oldest_created_at"], 1767225600)
        value = dict(sources=dict(backlog=dict(value=data, observed_at=1767287100)))
        out = io.StringIO()
        with patch.object(overview.time, "time", return_value=1767287100), contextlib.redirect_stdout(out):
            overview.display(value)
        self.assertIn("oldest queued run 17h 5m old", out.getvalue())

    def test_confirmed_orphan_is_removed_from_counts_and_oldest_age_only_while_queued(self):
        orphan = dict(id=41474083442, status="queued", created_at="2026-11-02T15:41:35Z",
                      path=".github/workflows/pr-build.yml")
        responses = [dict(total_count=2), dict(total_count=2),
                     dict(total_count=2, workflow_runs=[orphan]),
                     dict(total_count=2, workflow_runs=[dict(id=99, created_at="2026-11-02T23:00:00Z")])]
        with patch.object(overview, "CONFIRMED_ORPHAN_RUN_IDS", (orphan["id"],)), patch.object(overview, "fetch", return_value=orphan) as direct, patch.object(github, "request", side_effect=responses) as request:
            data, _ = overview.backlog(dict(self.config, github_token_file="private-file"))
        self.assertEqual(data["pr_builds"], 1)
        self.assertEqual(data["all_workflows"], 1)
        self.assertEqual(data["excluded_orphans"], 1)
        self.assertEqual(data["oldest_created_at"], datetime.datetime.fromisoformat("2026-11-02T23:00:00+00:00").timestamp())
        self.assertEqual(request.call_count + direct.call_count, 5)
        with patch.object(overview, "CONFIRMED_ORPHAN_RUN_IDS", (orphan["id"],)), patch.object(overview, "fetch", return_value=dict(orphan, status="completed")), patch.object(github, "request", side_effect=[dict(total_count=1), dict(total_count=1, workflow_runs=[dict(id=99, created_at="2026-11-02T23:00:00Z")])]):
            data, _ = overview.backlog(dict(self.config, github_token_file="private-file"))
        self.assertEqual(data["all_workflows"], 1)
        self.assertNotIn("excluded_orphans", data)

    def test_oldest_queue_age_does_not_claim_accuracy_past_api_limit_or_on_race(self):
        for count, final in ((1001, None), (5, dict(total_count=6, workflow_runs=[]))):
            responses = [dict(total_count=3), dict(total_count=count)] + ([final] if final else [])
            with patch.object(github, "request", side_effect=responses) as request:
                data, _ = overview.backlog(dict(self.config, github_token_file="private-file"))
            self.assertEqual(request.call_count, len(responses))
            self.assertNotIn("oldest_created_at", data)
            self.assertIn("oldest_unavailable", data)

    def test_bors_total_and_legacy_backlog_cache(self):
        batches = [dict(id=i, size=size, state="waiting") for i, size in enumerate((26, 26, 2))]
        value = dict(sources=dict(
            bors=dict(value=dict(batches=batches, held=0), observed_at=time.time()),
            backlog=dict(value=dict(pr_builds=71, all_workflows=100,
                                    oldest_created_at=time.time() - 631 * 3600), observed_at=time.time())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(value)
        self.assertIn("Bors: 54=26+26+2", out.getvalue())
        self.assertIn("CI backlog: refreshing", out.getvalue())
        self.assertNotIn("631h", out.getvalue())
        self.assertIn("excluding stuck runs through 2026-10-07", out.getvalue())

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
