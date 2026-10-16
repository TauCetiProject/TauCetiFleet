"""Cancellation decisions use GitHub metadata, never guest-controlled logs."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import ci, host, report, store


def step(name, outcome="success", number=1, start="00:00:01", end="00:00:02"):
    return dict(
        name=name,
        conclusion=outcome,
        status="completed",
        number=number,
        started_at=f"2026-10-16T{start}Z",
        completed_at=f"2026-10-16T{end}Z",
    )


def evidence():
    job = dict(
        id=7,
        run_id=42,
        runner_id=12,
        run_attempt=1,
        head_sha="b" * 40,
        name="sandboxed-build",
        status="completed",
        conclusion="cancelled",
        started_at="2026-10-16T00:00:00Z",
        completed_at="2026-10-16T00:01:00Z",
        steps=[
            step("Set up job"),
            step("Checkout workflow-pinned trusted build tooling", number=2),
            step(ci.BUILD, "cancelled", 3, "00:00:03", "00:00:50"),
            step("Complete job", number=4, start="00:00:59", end="00:01:00"),
        ],
    )
    run = dict(
        id=42,
        run_attempt=1,
        repository=dict(full_name="TauCetiProject/TauCeti"),
        path=".github/workflows/pr-build.yml",
        event="repository_dispatch",
        head_branch="main",
        head_sha="b" * 40,
        actor=dict(id=ci.BORS_ID),
        triggering_actor=dict(id=ci.BORS_ID),
        name="bors branch=main batch=857 head=" + "a" * 40 + " base=" + "b" * 40,
    )
    statuses = [
        dict(
            id=123,
            context="bors",
            creator=dict(id=ci.BORS_ID),
            state="failure",
            description="Canceled",
            target_url="https://bors.taucetiproject.org/batches/857",
            created_at="2026-10-16T00:00:30Z",
        )
    ]
    return job, run, statuses


class Classification(unittest.TestCase):
    def classify(self, job, run, statuses):
        return ci.classify(job, run=run, statuses=statuses, host_health="healthy")

    def test_verified_cancellation_is_healthy_with_bounded_evidence(self):
        decision = self.classify(*evidence())
        self.assertFalse(decision["cooldown"])
        self.assertEqual(decision["reason"], "verified_bors_cancellation")
        self.assertEqual(decision["evidence"]["cancellation_status_id"], 123)
        self.assertLess(len(json.dumps(decision)), 400)

    def test_faults_override_authenticated_cancellation(self):
        for name in ("Set up job", "Fetch Mathlib", "Install bubblewrap", "Checkout current base"):
            with self.subTest(name=name):
                job, run, statuses = evidence()
                job["steps"].append(step(name + " fault", "failure", 5))
                self.assertTrue(self.classify(job, run, statuses)["cooldown"])
        job, run, statuses = evidence()
        self.assertEqual(
            ci.classify(job, run=run, statuses=statuses, host_health="fault")["reason"], "host_service_fault"
        )
        for conclusion in ("timed_out", "startup_failure"):
            job["conclusion"] = conclusion
            self.assertTrue(self.classify(job, run, statuses)["cooldown"])

    def test_source_compilation_failure_keeps_healthy_capacity(self):
        job, _, _ = evidence()
        job["conclusion"] = "failure"
        job["steps"][2]["conclusion"] = "failure"
        self.assertFalse(ci.classify(job)["cooldown"])

    def test_source_guard_failures_preserve_existing_capacity_policy(self):
        for name in (
            "Preflight exact-candidate import headers",
            "Scope guard — allow TauCeti sources and validated pins",
            "Import-boundary guard (TauCeti/ and TauCeti.lean ⊬ roadmap/review)",
        ):
            with self.subTest(name=name):
                job = dict(conclusion="failure", steps=[step(name, "failure")])
                self.assertFalse(ci.classify(job)["cooldown"])

    def test_missing_or_conflicting_metadata_keeps_cooldown(self):
        job, run, statuses = evidence()
        self.assertTrue(ci.classify(job)["cooldown"])
        mutations = [
            lambda j, r, s: s.clear(),
            lambda j, r, s: s[0].update(creator=dict(id=1)),
            lambda j, r, s: s[0].update(description="Build failed"),
            lambda j, r, s: s[0].update(target_url="https://bors.taucetiproject.org/batches/856"),
            lambda j, r, s: s[0].update(created_at="2026-10-16T00:01:30Z"),
            lambda j, r, s: s[0].update(created_at=None),
            lambda j, r, s: s.insert(0, {**s[0], "description": "Running"}),
            lambda j, r, s: r.update(head_branch="candidate"),
            lambda j, r, s: r.update(event="pull_request"),
            lambda j, r, s: r.update(path=".github/workflows/other.yml"),
            lambda j, r, s: r.update(repository=dict(full_name="someone/TauCeti")),
            lambda j, r, s: r.update(actor=dict(id=1)),
            lambda j, r, s: r.update(triggering_actor=dict(id=1)),
            lambda j, r, s: r.update(head_sha="c" * 40),
            lambda j, r, s: r.update(id=41),
            lambda j, r, s: j.update(run_attempt=2),
            lambda j, r, s: r.update(run_attempt=2),
            lambda j, r, s: j["steps"].pop(),
            lambda j, r, s: j["steps"].append(step("New unknown step", "failure", 5)),
            lambda j, r, s: j["steps"][2].update(conclusion="failure"),
            lambda j, r, s: j["steps"].append(step("Checkout late network failure", "cancelled", 5)),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                values = copy.deepcopy((job, run, statuses))
                mutate(*values)
                self.assertTrue(self.classify(*values)["cooldown"])

    def test_report_expected_failure_requires_both_successful_status_writes(self):
        job, run, statuses = evidence()
        job["steps"].insert(3, step(ci.REPORT, "failure", 5, "00:00:51", "00:00:55"))
        self.assertTrue(self.classify(job, run, statuses)["cooldown"])
        for context in ("scope", "bump-guard"):
            statuses.append(
                dict(
                    id=124,
                    context=context,
                    creator=dict(id=ci.ACTIONS_ID),
                    state="success",
                    target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/42",
                    created_at="2026-10-16T00:00:53Z",
                )
            )
        self.assertFalse(self.classify(job, run, statuses)["cooldown"])
        for change in (
            dict(state="failure"),
            dict(target_url="https://github.com/TauCetiProject/TauCeti/actions/runs/41"),
            dict(created_at="2026-10-16T00:00:20Z"),
        ):
            original = dict(statuses[-1])
            statuses[-1].update(change)
            self.assertTrue(self.classify(job, run, statuses)["cooldown"])
            statuses[-1] = original

    def test_missing_host_health_is_unknown_even_with_valid_github_proof(self):
        job, run, statuses = evidence()
        for health in (None, "unknown", "", False):
            self.assertEqual(
                ci.classify(job, run=run, statuses=statuses, host_health=health)["reason"],
                "cancellation_host_health_unknown",
            )

    def test_host_service_receipt_distinguishes_fault_unknown_and_clean_exit(self):
        from types import SimpleNamespace

        for fields, code, expected in (
            ("", 0, (None, "unknown")),
            ("ActiveState=inactive\nSubState=dead\n", 0, (None, "unknown")),
            (
                "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n",
                1,
                (None, "unknown"),
            ),
            (
                "LoadState=loaded\nActiveState=active\nSubState=exited\nResult=success\nExecMainStatus=0\n",
                0,
                (False, "healthy"),
            ),
            ("ActiveState=active\nSubState=running\nResult=success\nExecMainStatus=0\n", 0, (True, "unknown")),
            ("ActiveState=failed\nSubState=failed\nResult=oom-kill\nExecMainStatus=9\n", 0, (False, "fault")),
            ("ActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=7\n", 0, (False, "fault")),
        ):
            with (
                self.subTest(fields=fields, code=code),
                patch.object(host, "run", return_value=SimpleNamespace(returncode=code, stdout=fields)),
            ):
                self.assertEqual(host.ci_service_state("test.service"), expected)


class Collection(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(
            os.environ, TAUCETI_FLEET_STATE=self.temp.name, TAUCETI_FLEET_CONFIG=self.temp.name + "/config"
        )
        env.start()
        self.addCleanup(env.stop)
        self.pending = Path(self.temp.name) / "pending-ci.json"
        self.backoff = Path(self.temp.name) / "ci-backoff.json"
        self.config = store.host_config()

    def collect(self, *, fault=False, api_fault=False, host_health="healthy"):
        job, run, statuses = evidence()
        if fault:
            job["conclusion"] = "timed_out"
        value = dict(run_id=42, runner_id=12, host_health=host_health)
        store.atomic(self.pending, [value, value])

        def request(path, **_):
            if "/jobs?" in path:
                return dict(jobs=[job])
            if api_fault:
                raise store.FleetError("network unavailable")
            return statuses if "/statuses?" in path else run

        with patch.object(host.github, "request", side_effect=request):
            host.collect_ci_results(self.config)
        return job

    def test_healthy_collection_and_duplicate_replay(self):
        self.collect()
        self.assertFalse(self.backoff.exists())
        self.assertEqual(store.read(self.pending), [])
        self.assertEqual(len(store.export_events()), 1)
        self.collect()
        self.assertEqual(len(store.export_events()), 1)

    def test_fault_replay_never_extends_or_shortens_other_cooldown(self):
        job = self.collect(fault=True)
        expiry = report.timestamp(job["completed_at"]) + 900
        self.assertEqual(store.read(self.backoff)["until"], expiry)
        self.collect(fault=True)
        self.assertEqual(store.read(self.backoff)["until"], expiry)
        store.atomic(self.backoff, dict(until=expiry + 1000, job_id=99))
        self.collect()
        self.assertEqual(store.read(self.backoff)["job_id"], 99)
        self.assertEqual(len(store.export_events()), 1)

    def test_missing_evidence_and_host_fault_cool_down(self):
        self.collect(api_fault=True)
        self.assertEqual(store.read(self.backoff)["reason"], "cancellation_evidence_unavailable")

    def test_legacy_pending_without_host_receipt_keeps_cooldown(self):
        self.collect(host_health=None)
        self.assertEqual(store.read(self.backoff)["reason"], "cancellation_host_health_unknown")

    def test_host_fault_is_not_overridden(self):
        self.collect(host_health="fault")
        self.assertEqual(store.read(self.backoff)["reason"], "host_service_fault")

    def test_incomplete_and_ambiguous_jobs_stay_pending(self):
        value = dict(run_id=42, runner_id=12)
        store.atomic(self.pending, [value])
        job, _, _ = evidence()
        for jobs in ([], [{**job, "status": "in_progress"}], [job, {**job, "id": 8}]):
            with patch.object(host.github, "request", return_value=dict(jobs=jobs)):
                host.collect_ci_results(self.config)
            self.assertEqual(store.read(self.pending), [value])
        self.assertFalse(store.export_events())
