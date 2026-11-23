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

    def test_a_pr_with_several_ledger_rows_lands_once(self):
        queue = [dict(number=5, forgejo=0, author="bob", state=state, approved_at=at("2026-11-01"),
                      landed_at=at(day), title="x") for state, day in (("cancelled", "2026-11-03"),
                                                                       ("landed", "2026-11-05"))]
        people = {p["identity"]: p for p in self.build({"5": self.pr("bob", "2026-11-01")}, queue)["people"]}
        self.assertEqual(people["bob"]["streams"]["landed"]["total"], 1)

    def test_reviews_are_counted_per_account_and_forgejo_reviews_count(self):
        prs = {"1": self.pr("bob", "2026-11-20", comments=[("alice", at("2026-11-21")),
                                                         ("alice-bot", at("2026-11-21", 13))])}
        forgejo = {"9": dict(self.pr("bob", "2026-11-20"), reviews=[["alice", at("2026-11-22")]])}
        people = {p["identity"]: p for p in self.build(prs, forgejo_prs=forgejo)["people"]}
        self.assertEqual(people["alice"]["streams"]["review"]["total"], 3)
        self.assertEqual(people["alice"]["accounts"], ["forgejo:alice", "github:alice", "github:alice-bot"])

    def test_equal_peaks_prefer_the_window_with_more_active_days(self):
        counts = {datetime.fromisoformat("2026-08-01").date(): 40}
        for day in days("2026-11-01", 8):
            counts[datetime.fromisoformat(day).date()] = 5
        end, total, active = c.peak_window(counts, min(counts), datetime.fromisoformat("2026-11-23").date())
        self.assertEqual((total, active), (40, 8))


class Pagination(unittest.TestCase):
    def test_long_and_legacy_lists_are_completed_page_by_page(self):
        def node(login, when):
            return dict(author=dict(login=login), createdAt=when, submittedAt=when)
        pages = {
            ("comments", "c1"): dict(nodes=[node("x", "2026-11-02T00:00:00Z")], pageInfo=dict(hasNextPage=True, endCursor="c2")),
            ("comments", "c2"): dict(nodes=[node("y", "2026-11-03T00:00:00Z")], pageInfo=dict(hasNextPage=False)),
            ("reviews", None): dict(nodes=[node("z", "2026-11-04T00:00:00Z")], pageInfo=dict(hasNextPage=False)),
        }

        def fake(token, variables, *, query):
            key = "comments" if "comments(" in query else "reviews"
            return dict(rateLimit=dict(remaining=5000, resetAt=""),
                        repository=dict(pullRequest={key: pages[(key, variables["after"])]}))
        prs = {"1": dict(comments=[["w", 1.0]], reviews=[], more={"comments": "c1"}),
               "2": dict(comments=[], reviews=[["old", 1.0]] * 40)}
        with patch.object(c, "graphql", side_effect=fake):
            self.assertEqual(c.complete_github(prs, "token"), 2)
        self.assertEqual([x[0] for x in prs["1"]["comments"]], ["w", "x", "y"])
        self.assertEqual(prs["2"]["reviews"], [["z", c.stamp("2026-11-04T00:00:00Z")]])
        self.assertEqual(prs["1"]["more"], {})
        self.assertEqual(c.complete_github(prs, "token"), 0)

    def test_a_failed_refetch_keeps_the_cached_list_and_the_quota_reserve_holds(self):
        prs = {"2": dict(comments=[], reviews=[["old", 1.0]] * 40)}
        with patch.object(c, "graphql", side_effect=c.FleetError("down")), self.assertRaises(c.FleetError):
            c.complete_github(prs, "token")
        self.assertEqual(len(prs["2"]["reviews"]), 40)
        last = dict(rateLimit=dict(remaining=1, resetAt="soon"),
                    repository=dict(pullRequest=dict(reviews=dict(nodes=[], pageInfo=dict(hasNextPage=False)))))
        prs["3"] = dict(comments=[], reviews=[["old", 1.0]] * 40)
        with patch.object(c, "graphql", return_value=last) as call, self.assertRaises(c.FleetError):
            c.complete_github(prs, "token")
        self.assertEqual(call.call_count, 1)


class ForgejoReviews(unittest.TestCase):
    def test_requests_are_skipped_replies_count_and_short_pages_continue(self):
        pages = {
            ("issues", 1): ([dict(number=4, title="t", state="open", user=dict(login="bob"),
                                  created_at="2026-11-20T00:00:00Z", updated_at="2026-11-21T00:00:00Z", labels=[])],
                            False),
            ("comments", 1): ([], False),
            ("reviews", 1): ([dict(id=1, state="APPROVED", user=dict(login="alice"), comments_count=1,
                                   submitted_at="2026-11-21T00:00:00Z")], True),
            ("reviews", 2): ([dict(id=2, state="REQUEST_REVIEW", user=dict(login="carol"),
                                   submitted_at="2026-11-21T00:00:00Z")], False),
            ("replies", 1): ([dict(user=dict(login="dave"), created_at="2026-11-22T00:00:00Z")], False),
        }

        def fake(path, params, *, with_next=False):
            kind = ("replies" if path.endswith("/comments") and "/reviews/" in path else
                    "reviews" if path.endswith("/reviews") else "comments" if path.endswith("comments") else "issues")
            return pages[(kind, params["page"])]
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"TAUCETI_FLEET_DATA": root}), \
                patch.object(c, "forgejo_get", side_effect=fake), patch.object(c.time, "sleep"), \
                contextlib.redirect_stderr(io.StringIO()):
            cache = c.sync_forgejo()
        self.assertEqual([r[0] for r in cache["prs"]["4"]["reviews"]], ["alice", "dave"])


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
            with open(out) as stream:
                self.assertTrue(json.load(stream)["people"][0]["lapsed"])


if __name__ == "__main__":
    unittest.main()
