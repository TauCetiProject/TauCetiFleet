"""Daily Cloudflare spend alerts: thresholds, one message per day, and the wrangler login refresh."""

import datetime
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import spend_alert

NOW = datetime.datetime(2026, 11, 25, 13, 0, tzinfo=datetime.UTC).timestamp()


def groups(written=None, r2=None, read=0):
    return dict(
        durableObjectsPeriodicGroups=[dict(dimensions=dict(namespaceId=k), sum=dict(rowsWritten=v, rowsRead=read))
                                      for k, v in (written or {}).items()],
        r2OperationsAdaptiveGroups=[dict(dimensions=dict(bucketName=b, actionType=a), sum=dict(requests=v))
                                    for (b, a), v in (r2 or {}).items()],
    )


BUSY = groups(written={"ns1": 25_000_000, "ns2": 6_000_000},
              r2={("tauceti-repair", "PutObject"): 2_000_000, ("rh", "GetObject"): 35_000_000,
                  ("tauceti-live-ci", "DeleteObject"): 300_000, ("tauceti-cache", "ListObjects"): 500_000})
NAMES = {"ns1": "tauceti-github-state/RepositoryState"}


class Summary(unittest.TestCase):
    def test_classes_names_and_cost(self):
        s = spend_alert.summarize(BUSY, NAMES)
        self.assertEqual(s["totals"]["rows_written"], 31_000_000)
        self.assertEqual(s["totals"]["class_a"], 2_500_000)  # deletes are free; lists are Class A
        self.assertEqual(s["class_b"], {"rh": 35_000_000})
        self.assertEqual(set(s["written"]), {"tauceti-github-state/RepositoryState", "ns2"})
        self.assertEqual(s["totals"]["cost"], round(31 + 2.5 * 4.5 + 35 * 0.36, 2))

    def test_message_names_what_crossed_and_the_top_writers(self):
        s = spend_alert.summarize(BUSY, NAMES)
        self.assertTrue(spend_alert.over(s, spend_alert.ROWS_WRITTEN, spend_alert.CLASS_A))
        self.assertFalse(spend_alert.over(s, 40_000_000, spend_alert.CLASS_A))
        text = spend_alert.message("2026-11-24", s, spend_alert.ROWS_WRITTEN, spend_alert.CLASS_A)
        self.assertIn("Durable Object rows written 31.0M (threshold 30.0M)", text)
        self.assertNotIn("R2 Class A operations 2.5M (threshold", text)
        self.assertIn("tauceti-github-state/RepositoryState 25.0M ($25.00); ns2 6.0M ($6.00)", text)
        self.assertIn("tauceti-repair 2.0M ($9.00); tauceti-cache 500.0k ($2.25)", text)

    def test_due_day_waits_for_the_day_to_settle(self):
        def at(h, m=0):
            return datetime.datetime(2026, 11, 26, h, m, tzinfo=datetime.UTC).timestamp()
        self.assertEqual(spend_alert.due_day(at(1, 59)), "2026-11-24")
        self.assertEqual(spend_alert.due_day(at(2, 0)), "2026-11-25")


class OncePerDay(unittest.TestCase):
    def setUp(self):
        self.sent, self.deliver, self.loads = [], True, 0

    def send(self, text):
        if self.deliver:
            self.sent.append(text)
        return self.deliver

    def load(self, data=BUSY):
        def load(day):
            self.loads += 1
            return spend_alert.summarize(data, NAMES)
        return load

    def test_one_message_per_day_and_retry_after_failed_delivery(self):
        state = {}
        with patch("sys.stdout", io.StringIO()):
            self.deliver = False
            self.assertFalse(spend_alert.check(state, "2026-11-24", NOW, self.load(), self.send))
            self.assertEqual(state["days"], {})
            self.deliver = True
            self.assertTrue(spend_alert.check(state, "2026-11-24", NOW, self.load(), self.send))
            self.assertTrue(spend_alert.check(state, "2026-11-24", NOW, self.load(), self.send))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.loads, 2)  # a reported day is not read again
        self.assertTrue(state["days"]["2026-11-24"]["alerted"])

    def test_quiet_day_is_recorded_without_a_message(self):
        state = {}
        with patch("sys.stdout", io.StringIO()):
            self.assertTrue(spend_alert.check(state, "2026-11-24", NOW, self.load(groups(written={"ns1": 10})),
                                              self.send))
            spend_alert.check(state, "2026-11-24", NOW, self.load(), self.send)
        self.assertEqual(self.sent, [])
        self.assertFalse(state["days"]["2026-11-24"]["alerted"])

    def test_failed_read_records_nothing(self):
        state = {}

        def load(day):
            raise spend_alert.FleetError("down")
        with self.assertRaises(spend_alert.FleetError):
            spend_alert.check(state, "2026-11-24", NOW, load, self.send)
        self.assertEqual(state["days"], {})

    def test_old_days_keep_only_their_record(self):
        state = {"days": {f"2026-01-{d:02d}": dict(alerted=True, rows_written=1) for d in range(1, 31)}
                 | {f"2026-02-{d:02d}": dict(alerted=False, rows_written=1) for d in range(1, 29)}
                 | {f"2026-03-{d:02d}": dict(alerted=False, rows_written=1) for d in range(1, 10)}}
        with patch("sys.stdout", io.StringIO()):
            spend_alert.check(state, "2026-11-24", NOW, self.load(groups()), self.send)
        self.assertEqual(len(state["days"]), 68)
        self.assertEqual(state["days"]["2026-01-01"], dict(alerted=True))
        self.assertEqual(state["days"]["2026-03-09"]["rows_written"], 1)
        self.assertIn("2026-11-24", state["days"])

    def test_unsettled_days_are_retried_after_rollover(self):
        state = {}
        self.assertEqual(spend_alert.pending_days(state, NOW), ["2026-11-24"])
        later = NOW + 2 * 86400
        # Never before the first run; still the days missed since.
        self.assertEqual(spend_alert.pending_days(state, later), ["2026-11-24", "2026-11-25", "2026-11-26"])
        state["days"] = {"2026-11-25": {}}
        self.assertEqual(spend_alert.pending_days(state, later), ["2026-11-24", "2026-11-26"])
        self.assertEqual(spend_alert.pending_days(state, later + 9 * 86400), ["2026-12-03", "2026-12-04", "2026-12-05"])
        self.assertEqual(spend_alert.pending_days(state, later, key="logged"),
                         ["2026-11-24", "2026-11-25", "2026-11-26"])


class Main(unittest.TestCase):
    """The command: log-only runs never consume a day's delivery, and --day records nothing."""

    def run_main(self, argv, state):
        posted = []

        def commit(path, value):
            state.clear()
            state.update(json.loads(json.dumps(value)))

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(spend_alert, "fetch", return_value=(BUSY, NAMES)), \
                patch.object(spend_alert, "state_dir", return_value=Path(directory)), \
                patch.object(spend_alert, "read", side_effect=lambda path, default=None: (
                    json.loads(json.dumps(state)) if str(path).endswith("spend-alert.json") else
                    dict(site="https://example.zulipchat.com", email="b", api_key="k", recipient_id=7))), \
                patch.object(spend_alert, "atomic", side_effect=commit), \
                patch("tauceti_fleet.fleet_alert.zulip", side_effect=lambda config, text: posted.append(text)), \
                patch.object(spend_alert.time, "time", return_value=NOW), \
                patch("sys.stdout", io.StringIO()):
            config = Path(directory) / "zulip.json"
            config.write_text("{}")
            config.chmod(0o600)
            code = spend_alert.main([a if a != "ZULIP" else str(config) for a in argv])
        return code, posted

    def test_log_only_then_configured(self):
        state = {}
        self.assertEqual(self.run_main([], state), (0, []))
        self.assertIn("2026-11-24", state["logged"])
        code, posted = self.run_main(["--zulip-config", "ZULIP"], state)
        self.assertEqual((code, len(posted)), (0, 1))
        self.assertTrue(state["days"]["2026-11-24"]["alerted"])
        self.assertEqual(self.run_main(["--zulip-config", "ZULIP"], state), (0, []))

    def test_dry_run_records_and_sends_nothing(self):
        state = {}
        self.assertEqual(self.run_main(["--zulip-config", "ZULIP", "--dry-run", "--day", "2026-11-20"], state), (0, []))
        self.assertEqual(state, {})
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            self.run_main(["--day", "2026-11-20"], state)


class WranglerLogin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "default.toml"
        self.refreshes = 0

    def tearDown(self):
        self.dir.cleanup()

    def write(self, token, expires):
        stamp = datetime.datetime.fromtimestamp(expires, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.path.write_text(f'oauth_token = "{token}"\nexpiration_time = "{stamp}"\nrefresh_token = "r"\n')

    def credential(self, after=None):
        def refresh():
            self.refreshes += 1
            if after:
                self.write(*after)
        return spend_alert.Credential(wrangler_config=self.path, refresh=refresh, clock=lambda: NOW)

    def test_valid_token_is_used_without_refresh(self):
        self.write("fresh", NOW + 3000)
        self.assertEqual(self.credential().token(), "fresh")
        self.assertEqual(self.refreshes, 0)

    def test_expired_token_is_refreshed_once(self):
        self.write("old", NOW - 10)
        credential = self.credential(after=("new", NOW + 3600))
        self.assertEqual(credential.token(), "new")
        self.assertEqual(self.refreshes, 1)
        self.write("old", NOW - 10)
        with self.assertRaises(spend_alert.AuthError):
            credential.token()  # never a second refresh in one run
        self.assertEqual(self.refreshes, 1)

    def test_refresh_that_fails_is_an_error(self):
        self.write("old", NOW + 30)  # inside the margin
        with self.assertRaises(spend_alert.AuthError):
            self.credential().token()
        self.assertEqual(self.refreshes, 1)

    def test_malformed_token_never_appears_in_errors(self):
        self.write("SECRET\\nX", NOW + 3000)
        with self.assertRaises(spend_alert.AuthError) as caught:
            self.credential().token()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_token_file_is_never_refreshed(self):
        token = Path(self.dir.name) / "token"
        token.write_text("tok\n")
        credential = spend_alert.Credential(token_file=token, refresh=lambda: self.fail("refreshed"))
        self.assertEqual(credential.token(), "tok")
        self.assertFalse(credential.renew())


class Requests(unittest.TestCase):
    def test_rejected_token_is_refreshed_and_the_request_repeated(self):
        tokens, seen = iter(["a", "b"]), []

        class Credential(spend_alert.Credential):
            def token(self):
                return self.current

        credential = Credential(refresh=lambda: setattr(credential, "current", next(tokens)))
        credential.current = next(tokens)

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Opener:
            def open(self, request, timeout):
                seen.append(request.get_header("Authorization"))
                if len(seen) == 1:
                    raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)
                return Response(json.dumps(dict(data=dict(viewer=dict(accounts=[BUSY])))).encode())

        value = spend_alert.call(credential, "https://example.invalid/graphql", dict(query="q"), Opener())
        self.assertEqual(seen, ["Bearer a", "Bearer b"])
        self.assertEqual(value["data"]["viewer"]["accounts"], [BUSY])

    def test_full_page_is_incomplete(self):
        full = dict(BUSY, r2OperationsAdaptiveGroups=[BUSY["r2OperationsAdaptiveGroups"][0]] * spend_alert.LIMIT)

        class Opener:
            def open(self, request, timeout):
                class Response(io.BytesIO):
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False
                return Response(json.dumps(dict(data=dict(viewer=dict(accounts=[full])))).encode())

        credential = spend_alert.Credential(refresh=lambda: None)
        credential.token = lambda: "t"
        with self.assertRaisesRegex(spend_alert.FleetError, "full page"):
            spend_alert.fetch(credential, "2026-11-24", opener=Opener())

    def test_graphql_errors_are_failures(self):
        class Opener:
            def open(self, request, timeout):
                class Response(io.BytesIO):
                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False
                return Response(json.dumps(dict(data=None, errors=[dict(message="bad filter")])).encode())

        credential = spend_alert.Credential(token_file=None, refresh=lambda: None)
        credential.token = lambda: "t"
        with self.assertRaisesRegex(spend_alert.FleetError, "bad filter"):
            spend_alert.fetch(credential, "2026-11-24", opener=Opener())


class Install(unittest.TestCase):
    def test_units(self):
        with tempfile.TemporaryDirectory() as directory, patch("tauceti_fleet.host.units", return_value=Path(directory)), \
                patch("tauceti_fleet.process.run") as run, patch.dict(os.environ, {"PATH": "/bin"}):
            result = spend_alert.install("/tmp/z.json", rows_written=40_000_000)
            service = (Path(directory) / "tauceti-spend-alert.service").read_text()
            timer = (Path(directory) / "tauceti-spend-alert.timer").read_text()
        self.assertEqual(result["installed"], "tauceti-spend-alert.timer")
        self.assertIn('"spend-alert" "--zulip-config" "/tmp/z.json" "--rows-written-threshold" "40000000"', service)
        self.assertIn('Environment="PATH=/bin"', service)
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("Persistent=true", timer)
        run.assert_called()


if __name__ == "__main__":
    unittest.main()
