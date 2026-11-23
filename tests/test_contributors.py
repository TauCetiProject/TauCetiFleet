import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from tauceti_fleet import cli, store
from tauceti_fleet import contributors as c

ZONE = ZoneInfo("Australia/Sydney")


def at(day, hour=12):
    return datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=ZONE).timestamp()


def days(start, count):
    first = datetime.fromisoformat(start).date().toordinal()
    return [datetime.fromordinal(first + i).date().isoformat() for i in range(count)]


class Identities(unittest.TestCase):
    def test_shared_accounts_are_excluded_and_bot_suffixes_merge(self):
        self.assertIsNone(c.canonical("tauceti-ai-for-science", {}))
        self.assertIsNone(c.canonical("kim-em", {}))
        self.assertIsNone(c.canonical("tauceti-review-bot[bot]", {}))
        self.assertEqual(c.canonical("mm3141-bot", {}), "mm3141")
        self.assertEqual(c.canonical("roed314", {"roed314": "roed-math"}), "roed-math")


class Analysis(unittest.TestCase):
    def setUp(self):
        self.now = at("2026-11-23", 17)

    def build(self, github_prs, queue=(), forgejo_prs=None, comments=None):
        github = dict(prs=github_prs)
        forgejo = dict(prs=forgejo_prs or {}, comments=comments or {})
        events, accounts, open_prs = c.collect_events(github, forgejo, dict(inputs=list(queue)), {})
        return c.analyse(events, accounts, open_prs, now=self.now)

    def pr(self, author, day, *, state="CLOSED", title="feat: x", comments=(), labels=()):
        return dict(title=title, state=state, author=author, created=at(day), closed=None, merged=None,
                    labels=list(labels), comments=[list(x) for x in comments], reviews=[], draft=False)

    def test_a_contributor_who_stopped_is_lapsed_and_stopped(self):
        prs = {}
        for i, day in enumerate(days("2026-10-01", 35)):
            for j in range(3):
                prs[str(100 * i + j)] = self.pr("alice", day)
        prs["9999"] = self.pr("alice", "2026-11-02", state="OPEN", labels=["awaiting-author"])
        report = self.build(prs)
        alice = report["people"][0]
        self.assertEqual(alice["identity"], "alice")
        self.assertTrue(alice["sustained"])
        self.assertTrue(alice["lapsed"])
        self.assertTrue(alice["stopped"])
        self.assertEqual(alice["recent14"], 0)
        self.assertEqual(alice["drop_after"], "2026-11-04")
        self.assertEqual(alice["open_prs"]["labels"], {"awaiting-author": 1})
        text = c.markdown(report)
        self.assertIn("| alice | stopped |", text)
        self.assertIn("https://github.com/TauCetiProject/TauCeti/pull/9999", text)

    def test_steady_and_low_volume_contributors_are_not_lapsed(self):
        prs = {}
        for i, day in enumerate(days("2026-10-01", 54)):
            prs[str(1000 + i)] = self.pr("steady", day)
            prs[str(2000 + i)] = self.pr("steady", day)
        for i, day in enumerate(days("2026-10-01", 10)):
            prs[str(3000 + i)] = self.pr("light", day)
        people = {p["identity"]: p for p in self.build(prs)["people"]}
        self.assertTrue(people["steady"]["sustained"])
        self.assertFalse(people["steady"]["lapsed"])
        self.assertFalse(people["light"]["sustained"])
        self.assertFalse(people["light"]["lapsed"])

    def test_reviews_count_only_on_other_peoples_prs_and_queue_landings_win(self):
        prs = {"1": self.pr("bob", "2026-11-20", comments=[("carol", at("2026-11-21")), ("bob", at("2026-11-21"))])}
        prs["1"]["title"] = "[Merged by Bors] - feat: x"
        prs["1"]["closed"] = at("2026-11-22")
        queue = [dict(number=1, forgejo=0, author="bob", state="landed", approved_at=at("2026-11-01"),
                      landed_at=at("2026-11-22"), title="x")]
        report = self.build(prs, queue)
        people = {p["identity"]: p for p in report["people"]}
        self.assertEqual(people["carol"]["streams"]["review"]["total"], 1)
        self.assertEqual(people["bob"]["streams"]["review"]["total"], 0)
        self.assertEqual(people["bob"]["streams"]["landed"]["total"], 1)

    def test_forgejo_comments_count_as_reviews(self):
        forgejo = {"7": self.pr("dave", "2026-11-20")}
        report = self.build({}, forgejo_prs=forgejo, comments={"1": ["7", "erin", at("2026-11-21")]})
        people = {p["identity"]: p for p in report["people"]}
        self.assertEqual(people["erin"]["streams"]["review"]["total"], 1)
        self.assertEqual(people["erin"]["accounts"], ["forgejo:erin"])


class Command(unittest.TestCase):
    def test_offline_command_reads_only_the_cache(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"TAUCETI_FLEET_DATA": root,
                                                                           "TAUCETI_FLEET_CONFIG": root}):
            prs = {str(n): dict(title="t", state="CLOSED", author="alice", created=at("2026-10-01") + n * 3600,
                                closed=None, merged=None, labels=[], comments=[], reviews=[]) for n in range(400)}
            store.atomic(c.cache_dir() / "github.json", dict(prs=prs))
            out = os.path.join(root, "report.json")
            with contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    patch.object(c, "sync_github", side_effect=AssertionError("network")):
                self.assertEqual(cli.main(["contributors", "--offline", "--json", out]), 0)
            self.assertIn("| alice | stopped |", stdout.getvalue())
            self.assertTrue(json.load(open(out))["people"][0]["lapsed"])


if __name__ == "__main__":
    unittest.main()
