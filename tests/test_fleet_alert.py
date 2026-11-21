"""Fleet progress alerts: one message per incident, one on recovery, none for disabled hosts."""

import json
import unittest
import urllib.parse
from unittest.mock import patch

from tauceti_fleet import fleet_alert

MINUTE = 60


def report(states, *, enabled=True, finished=40, failed=10, now=0, admission="at_ceiling", detail="leantar crashed"):
    return dict(
        workers_enabled=enabled,
        status=dict(
            workers=[dict(id=f"w{i}", state=s, alive=True, detail=detail if s == "backoff" else None)
                     for i, s in enumerate(states)],
            worker_admission=dict(state=admission),
        ),
        activity=dict(summary=dict(prepared_at=now, rounds={"1h": {"author": dict(finished=finished, failed=failed)}})),
    )


class Run:
    """Ticks once a minute over one host, collecting the messages sent."""

    def __init__(self):
        self.state, self.sent, self.now, self.deliver = {}, [], 0, True

    def send(self, text):
        if self.deliver:
            self.sent.append(text)
        return self.deliver

    def minutes(self, count, make, name="tauchonki"):
        for _ in range(count):
            self.now += MINUTE
            fleet_alert.tick({name: ("m" * 32, make(self.now))}, self.state, self.now, self.send)


class Episodes(unittest.TestCase):
    def test_backoff_alerts_once_after_fifteen_minutes_and_recovers_once(self):
        run = Run()
        run.minutes(14, lambda now: report(["backoff"] * 58 + ["running"] * 2, now=now))
        self.assertEqual(run.sent, [])
        run.minutes(60, lambda now: report(["backoff"] * 58 + ["running"] * 2, now=now))
        self.assertEqual(len(run.sent), 1)
        self.assertIn("**tauchonki**: 58 of 60 workers in backoff or blocked for 15 min", run.sent[0])
        self.assertIn("`leantar crashed`", run.sent[0])
        # A brief healthy minute does not close the incident.
        run.minutes(1, lambda now: report(["running"] * 60, now=now))
        run.minutes(5, lambda now: report(["backoff"] * 60, now=now))
        self.assertEqual(len(run.sent), 1)
        run.minutes(16, lambda now: report(["running"] * 60, now=now))
        self.assertEqual(len(run.sent), 2)
        self.assertIn("**tauchonki** recovered after 1h 35m. Now 60 of 60 workers working", run.sent[1])
        run.minutes(30, lambda now: report(["running"] * 60, now=now))
        self.assertEqual(len(run.sent), 2)

    def test_hosts_with_workers_disabled_never_alert(self):
        run = Run()
        # A drained host keeps its last workers alive, and a CI-only host has none.
        run.minutes(120, lambda now: report(["backoff"] * 31 + ["running"] * 27, enabled=False, finished=0, now=now))
        run.minutes(120, lambda now: report([], enabled=False, finished=0, now=now), name="chungus5")
        self.assertEqual(run.sent, [])

    def test_disabling_workers_closes_an_open_alert(self):
        run = Run()
        run.minutes(20, lambda now: report(["backoff"] * 10, now=now))
        run.minutes(16, lambda now: report([], enabled=False, now=now))
        self.assertEqual(len(run.sent), 2)
        self.assertIn("workers disabled; closing the alert", run.sent[1])

    def test_unreachable_or_cached_reports_neither_open_nor_close(self):
        run = Run()
        run.minutes(20, lambda now: report(["backoff"] * 10, now=now))
        run.minutes(60, lambda now: dict(report(["running"] * 10, now=now), offline=True, error="ssh"))
        run.minutes(60, lambda now: dict(error="ssh"))
        self.assertEqual(len(run.sent), 1)

    def test_admission_waits_and_single_workers_are_not_incidents(self):
        run = Run()
        run.minutes(60, lambda now: report(["waiting-capacity"] * 30 + ["waiting-budget"] * 30, now=now))
        run.minutes(60, lambda now: report(["backoff", "backoff"], now=now), name="small")
        self.assertEqual(run.sent, [])
        run.minutes(20, lambda now: report(["waiting-github"] * 9 + ["running"], now=now))
        self.assertEqual(len(run.sent), 1)

    def test_no_successful_rounds_needs_an_observed_hour_and_open_admission(self):
        run = Run()
        # Workers alive and running, but every round in the window failed.
        run.minutes(59, lambda now: report(["running"] * 20, finished=30, failed=30, now=now))
        self.assertEqual(run.sent, [])
        run.minutes(17, lambda now: report(["running"] * 20, finished=30, failed=30, now=now))
        self.assertEqual(len(run.sent), 1)
        self.assertIn("no rounds succeeded in the last hour with 20 workers alive", run.sent[0])
        self.assertIn("30 rounds finished in the last hour, 30 of them failed", run.sent[0])
        other = Run()
        # Outside a schedule slot admission pauses and rounds stop on purpose; stale counts are unknown.
        other.minutes(120, lambda now: report(["idle"] * 20, finished=0, failed=0, now=now, admission="paused"))
        other.minutes(120, lambda now: report(["running"] * 20, finished=0, failed=0, now=now - 3600))
        self.assertEqual(other.sent, [])

    def test_failed_delivery_is_retried_on_the_next_run(self):
        run = Run()
        run.deliver = False
        run.minutes(20, lambda now: report(["backoff"] * 10, now=now))
        run.deliver = True
        run.minutes(1, lambda now: report(["backoff"] * 10, now=now))
        run.minutes(5, lambda now: report(["backoff"] * 10, now=now))
        self.assertEqual(len(run.sent), 1)
        self.assertIn("for 20 min", run.sent[0])


class Delivery(unittest.TestCase):
    def post(self, config):
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, n):
                return json.dumps(dict(result="success")).encode()

        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                return Response()

        with patch.object(fleet_alert.urllib.request, "build_opener", return_value=Opener()):
            fleet_alert.zulip(config, "hello")
        return requests[0]

    def test_stream_topic_and_private_targets(self):
        base = dict(site="https://example.zulipchat.com", email="bot@example.com", api_key="k")
        request = self.post(dict(base, stream="ops", topic="fleet health"))
        self.assertEqual(request.full_url, "https://example.zulipchat.com/api/v1/messages")
        self.assertEqual(urllib.parse.parse_qs(request.data.decode()),
                         dict(type=["stream"], to=["ops"], topic=["fleet health"], content=["hello"]))
        request = self.post(dict(base, recipient_id=7))
        self.assertEqual(urllib.parse.parse_qs(request.data.decode())["to"], ["[7]"])
        for bad in (dict(base, site="http://example.zulipchat.com", stream="ops", topic="t"), base):
            with self.assertRaises(ValueError):
                fleet_alert.zulip(bad, "hello")


if __name__ == "__main__":
    unittest.main()
