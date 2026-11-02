import datetime
import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import bors_history as history


class JobHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch = patch.object(history, "state_dir", return_value=Path(self.tmp.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.now = 1_800_000_000

    def result(self, run_id):
        at = lambda seconds: datetime.datetime.fromtimestamp(self.now - seconds, datetime.timezone.utc).isoformat()
        return dict(total_count=1, jobs=[dict(id=run_id + 100, run_id=run_id,
                    name="sandboxed-build", status="completed", runner_id=123,
                    started_at=at(1200), completed_at=at(600))])

    def test_backfill_is_bounded_rate_limited_and_resumes_without_refetching_completed_runs(self):
        calls = []
        def fetch(url, **kwargs):
            run = int(urllib.parse.urlsplit(url).path.split("/")[-2])
            calls.append(run)
            return self.result(run)
        config = dict(github_token_file="private-file")
        first = history.collect(config, list(range(1, 9)), fetch, now=self.now)
        self.assertEqual(len(calls), 5)
        self.assertEqual(first["runs_checked"], 5)
        self.assertEqual(first["windows"][0]["mean_seconds"], 600)
        history.collect(config, list(range(1, 9)), fetch, now=self.now + 30)
        self.assertEqual(len(calls), 5)
        final = history.collect(config, list(range(1, 9)), fetch, now=self.now + 301)
        self.assertEqual(calls, list(range(1, 9)))
        self.assertEqual(final["runs_checked"], 8)
        self.assertEqual(final["windows"][0]["samples"], 8)

    def test_unavailable_provider_stops_the_backfill_and_unassigned_jobs_have_no_runtime(self):
        with patch.object(history, "MAX_READS", 5):
            fetch = unittest.mock.Mock(return_value=dict(status="unavailable"))
            value = history.collect(dict(github_token_file="private-file"), [1, 2, 3], fetch, now=self.now)
        self.assertEqual(fetch.call_count, 1)
        self.assertIsNone(value["windows"][0]["mean_seconds"])
        job = self.result(1)["jobs"][0]
        self.assertEqual(history.job_facts([dict(job, runner_id=0)], 1, self.now), [])
        self.assertEqual(history.job_facts([dict(job, run_id=2)], 1, self.now), [])


if __name__ == "__main__":
    unittest.main()
