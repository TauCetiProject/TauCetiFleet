import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from tauceti_fleet import ci_overview, cli, pool_activity, queue_status, remote, store
from tauceti_fleet import queue_history as history


class History(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_CONFIG": root.name})
        env.start()
        self.addCleanup(env.stop)
        self.now = 1794183000.0
        windows = [
            dict(window=w, **{k: 0 for k in history.COUNTS}, run_p50_seconds=None, run_p95_seconds=None)
            for w in ("1h", "3h", "24h", "7d")
        ]
        for w in windows:
            w.update(landed_prs=876, landed_batches=1, ci_passed=1, ci_completed=1)
        self.source = dict(
            schema="tauceti-queue.statistics/v1",
            repository=ci_overview.REPOSITORY,
            canonical_branch="main",
            coverage="controller_ledger",
            observed_at=self.now,
            windows=windows,
        )

    def test_current_ledger_displays_the_876_pr_landing_and_precise_metric_names(self):
        report = dict(value=history.parse(self.source))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            history.display(report, now=self.now)
        self.assertIn("876", output.getvalue())
        self.assertIn("current controller ledger", output.getvalue())
        self.assertNotIn("Bors history", output.getvalue())
        self.assertIn("CI run p50", output.getvalue())
        self.assertIn("not build-only", output.getvalue())

    def test_short_ledger_is_not_presented_as_a_complete_seven_day_history(self):
        self.source["since"] = self.now - 86400
        with contextlib.redirect_stdout(io.StringIO()) as output:
            history.display(dict(value=history.parse(self.source)), now=self.now)
        self.assertIn("7d limited to ledger since", output.getvalue())

    def test_source_failure_keeps_the_original_time_and_landing_counts(self):
        with patch.object(ci_overview, "fetch", return_value=self.source):
            history.refresh()
        with patch.object(ci_overview, "fetch", side_effect=OSError):
            r = history.refresh()
        self.assertEqual(r["value"]["observed_at"], self.now)
        self.assertEqual(r["value"]["windows"][0]["landed_prs"], 876)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            history.display(r, now=self.now + 600)
        self.assertIn("STALE; cached", output.getvalue())

    def test_retired_feed_or_wrong_repository_is_never_used_as_current_history(self):
        for key, value in [
            ("schema", "tauceti-bors.statistics/v1"),
            ("repository", "Other/Repo"),
            ("coverage", "unknown"),
        ]:
            bad = copy.deepcopy(self.source)
            bad[key] = value
            with self.assertRaises(ValueError):
                history.parse(bad)

    def test_older_follow_writer_cannot_delete_queue_details(self):
        report = {
            "scope": "a" * 64,
            "checked_at": 10,
            "value": {"observed_at": "2026-11-09T00:00:00Z", "batches": [], "waiting": 7, "workers": [], "claims": []},
        }
        store.atomic(queue_status.cache_path(), report)
        store.atomic(pool_activity.cache_path(), {"value": {"old_client": "without_queue"}})
        entries = dict(one=dict(target="one", machine_id="one"))
        with (
            patch.object(remote, "inventory", return_value=entries),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            cli.main(["status", "--json"], _status_results=dict(one=dict(machine_id="one")))
        self.assertEqual(json.loads(output.getvalue())["_ci"]["queue"], report)

    def test_normal_status_and_follow_render_never_display_retired_bors_history(self):
        entries = dict(one=dict(target="one", machine_id="one"))
        with (
            patch.object(remote, "inventory", return_value=entries),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            cli.main(["status"], _status_results=dict(one=dict(machine_id="one")))
        self.assertIn("Queue history: unavailable", output.getvalue())
        self.assertNotIn("Bors history", output.getvalue())

    def test_separate_queue_cache_survives_manifest_failure(self):
        previous = {"scope": "a" * 64, "checked_at": 10, "value": {"observed_at": "2026-11-09T00:00:00Z"}}
        store.atomic(queue_status.cache_path(), previous)
        with patch.object(ci_overview, "fetch", side_effect=OSError):
            r = pool_activity.refresh()
        self.assertEqual(r["queue"]["value"], previous["value"])


if __name__ == "__main__":
    unittest.main()
