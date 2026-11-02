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

from tauceti_fleet import ci_overview as overview, github, host, store


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

    def worker(self, slot, scope=None):
        identity = "fleet-" + self.config["machine_id"][:8] + "-" + str(slot)
        store.atomic(store.state_dir() / "workers" / (identity + ".json"), dict(id=identity, alive=True, max_open_prs=128,
                     applied_spec=dict(github_account="science", roadmap_only=scope, roadmap_skip=[])))

    def snapshot(self, sources):
        value = dict(version=1, repository=overview.REPOSITORY, sources=sources)
        store.atomic(store.state_dir() / "ci-overview.json", value)
        return value

    def test_foreground_status_never_queries_or_starts_collectors(self):
        value = self.snapshot(dict(backlog=dict(value=dict(pr_builds=732, all_workflows=1352), observed_at=100)))
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
        with patch.object(overview, "bors", side_effect=RuntimeError), patch.object(overview, "pools", side_effect=RuntimeError), patch.object(overview, "backlog", side_effect=RuntimeError) as api, patch.object(overview, "pressure", side_effect=RuntimeError):
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
        self.assertIn("cached_only=true", fetch.call_args.args[0])
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

        with patch.object(overview, "bors", side_effect=slow) as request, patch.object(overview, "pools", return_value=({}, 100)), patch.object(overview, "backlog", return_value=(dict(pr_builds=1, all_workflows=2), 100)), patch.object(overview, "pressure", return_value=({}, 100)):
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
            with patch.object(overview, "bors", side_effect=RuntimeError) as request, patch.object(overview, "pools", side_effect=RuntimeError), patch.object(overview, "backlog", side_effect=RuntimeError), patch.object(overview, "pressure", side_effect=RuntimeError):
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
        value["sources"]["backlog"] = dict(value=dict(pr_builds=3, all_workflows=8), observed_at=time.time())
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
        with patch.object(github, "request", return_value=dict(total_count=7)) as request:
            data, _ = overview.backlog(dict(self.config, github_token_file="private-file"))
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all("per_page=1" in c.args[0] for c in request.call_args_list))
        self.assertEqual(data, dict(pr_builds=7, all_workflows=7))

    def test_presentation_keeps_runner_groups_on_one_line(self):
        value = dict(sources=dict(pools=dict(value=dict(counts={"github": 3, "namespace": 15, "fleet-11e2756fc11042d798f759410573a935": 12}), observed_at=time.time())))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            overview.display(value)
        lines = [s for s in out.getvalue().splitlines() if s.startswith("CI jobs running:")]
        self.assertEqual(len(lines), 1)
        self.assertIn("Tauchonki 12", lines[0])
        self.assertIn("Namespace 15", lines[0])

    def test_timer_is_independent_of_foreground_status_and_preserves_paths(self):
        units = overview.unit_files('"/home/user/tauceti-fleet" "collect-status"', dict(TAUCETI_FLEET_STATE="/other/state"))
        self.assertIn("OnUnitInactiveSec=300", units["tauceti-fleet-observe.timer"])
        self.assertIn("RandomizedDelaySec=30", units["tauceti-fleet-observe.timer"])
        self.assertIn("TAUCETI_FLEET_STATE=/other/state", units["tauceti-fleet-observe.service"])
        self.assertNotIn("status\"", units["tauceti-fleet-observe.timer"])
