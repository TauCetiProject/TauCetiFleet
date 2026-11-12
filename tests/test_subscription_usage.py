import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import cli, host, remote, store, subscription_usage

NOW = 1_800_000_000.0
SUBSCRIPTION = dict(claude_billing="subscription", agent="auto")


def iso(at):
    return datetime.fromtimestamp(at, UTC).isoformat().replace("+00:00", "Z")


def flat(session, weekly):
    return dict(five_hour=session, seven_day=weekly)


class SubscriptionUsage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environ = patch.dict(os.environ, {
            "HOME": str(self.root / "home"),
            "TAUCETI_FLEET_CONFIG": str(self.root / "config"),
            "TAUCETI_FLEET_STATE": str(self.root / "state"),
            "TAUCETI_FLEET_DATA": str(self.root / "data"),
        })
        environ.start()
        self.addCleanup(environ.stop)
        self.login = self.root / "home/.claude/.tauceti-quota/usage"

    def runtime(self, revision):
        return store.data_dir() / "worker-runtimes" / revision / "lib/python3.12/site-packages/tauceti_worker"

    def cache(self, path, payload, age, **extra):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(fetched_at=NOW - age, payload=payload, **dict(dict(fp="account"), **extra))))

    def codex(self, path, age, *windows, **extra):
        records = [dict(used_percent=used, limit_window_seconds=seconds, reset_after_seconds=after)
                   for used, seconds, after in windows]
        self.cache(path, dict(rate_limit=dict(zip(("primary_window", "secondary_window"), records, strict=False))), age, **extra)

    def test_profile_selects_the_providers_and_api_billing_adds_nothing_to_status(self):
        for config, expected in (
            ({}, []),
            (dict(claude_billing="api", agent="claude"), []),
            (dict(claude_billing="subscription"), ["claude"]),
            (dict(claude_billing="subscription", agent="codex"), ["codex"]),
            (SUBSCRIPTION, ["claude", "codex"]),
        ):
            self.assertEqual(subscription_usage.providers(config), expected, config)
        self.cache(self.login / "quota-claude-0123456789abcdef.json", flat(None, None), 60)
        self.assertEqual(subscription_usage.readings(store.host_config(), NOW), {})
        self.assertNotIn("subscription_usage", host.rpc(dict(action="status")))

    def test_newest_reading_per_provider_reaches_status_from_the_worker_caches(self):
        hour = 3600
        self.cache(self.login / "quota-claude-0123456789abcdef.json", flat(
            dict(utilization=37.5, resets_at=iso(NOW + 2 * hour)), dict(utilization=59, resets_at=iso(NOW + 80 * hour))), 600)
        self.cache(self.login / "quota-claude-fedcba9876543210.json", flat(dict(utilization=99, resets_at=iso(NOW)), None), 4000)
        self.cache(self.runtime("b" * 40) / "state/.quota-usage/0011223344556677/quota-claude-0123456789abcdef.json",
                   flat(dict(utilization=90, resets_at=iso(NOW + hour)), None), 900)
        # The newer reading is in the runtime the workers ran before an update, and lists the weekly window first.
        self.codex(self.runtime("a" * 40) / "state/fleet-1/cache/quota-codex.json", 120, (12, 7 * 24 * hour, 100 * hour), (40.0, 5 * hour, hour))
        self.codex(self.runtime("b" * 40) / "state/fleet-2/cache/quota-codex.json", 500, (1, 5 * hour, 4 * hour))
        (self.runtime("b" * 40) / "state/fleet-3/cache").mkdir(parents=True)
        (self.runtime("b" * 40) / "state/fleet-3/cache/quota-codex.json").write_text('{"fetched_at": ')
        self.cache(self.runtime("b" * 40) / "state/fleet-4/cache/quota-codex.json", {}, -30)
        account = hashlib.sha256(b"account").hexdigest()[:8]
        expected = dict(
            claude=dict(credential=account, other_credentials=0, fetched_at=NOW - 600, windows=dict(
                session=dict(seconds=5 * hour, used=37.5, left=62.5, resets_at=NOW + 2 * hour),
                weekly=dict(seconds=7 * 24 * hour, used=59.0, left=41.0, resets_at=NOW + 80 * hour))),
            codex=dict(credential=account, other_credentials=0, limit_reached=False, fetched_at=NOW - 120, windows=dict(
                weekly=dict(seconds=7.0 * 24 * hour, used=12.0, left=88.0, resets_at=NOW - 120 + 100 * hour),
                session=dict(seconds=5.0 * hour, used=40.0, left=60.0, resets_at=NOW - 120 + hour))),
        )
        self.assertEqual(subscription_usage.readings(SUBSCRIPTION, NOW), expected)
        self.assertEqual(subscription_usage.readings(dict(SUBSCRIPTION, agent="claude"), NOW), dict(claude=expected["claude"]))
        elsewhere = dict(SUBSCRIPTION, env=dict(TAUCETI_LEGACY_DATA_ROOT=str(self.root / "kept")))
        host.worker_fields(elsewhere)
        self.cache(self.root / "kept/state/.quota-usage/0011223344556677/quota-claude-0123456789abcdef.json",
                   flat(dict(utilization=50, resets_at=iso(NOW + hour)), None), 5)
        self.assertEqual(subscription_usage.readings(elsewhere, NOW)["claude"]["fetched_at"], NOW - 5)
        self.codex(self.root / "kept/state/fleet-1/cache/quota-codex.json", 5, (3, 5 * hour, hour))
        self.assertEqual(subscription_usage.readings(elsewhere, NOW)["codex"]["windows"]["session"]["left"], 97.0)

        store.update_host(**SUBSCRIPTION)
        entry = dict(target="local", machine_id=store.host_config()["machine_id"])
        output = io.StringIO()
        with (
            patch.object(subscription_usage.time, "time", return_value=NOW),
            patch("subprocess.run", side_effect=AssertionError("process in status")),
            patch("urllib.request.urlopen", side_effect=AssertionError("request in status")),
        ):
            value = host.rpc(dict(action="status"))
            self.assertEqual(value["subscription_usage"], expected)
            with patch.object(remote, "inventory", return_value=dict(local=entry)), contextlib.redirect_stdout(output):
                cli.main(["status"], _status_results=dict(local=dict(value, status=dict(workers=[]))))
        text = output.getvalue()
        self.assertIn("Claude usage: 5h 62% left, resets in 2h00m; 7d 41% left, resets in 3d08h (read 10m ago)\n", text)
        self.assertIn("Codex usage: 5h 60% left, resets in 58m; 7d 88% left, resets in 4d03h (read 2m ago)\n", text)

    def test_windows_follow_the_worker_reading_of_each_response_shape(self):
        hour = 3600
        scoped = dict(group="weekly_scoped", percent=95, resets_at=iso(NOW + hour), scope=dict(model="one"))
        limits = dict(
            five_hour=dict(utilization=1, resets_at=iso(NOW + hour)),
            limits=[scoped, "noise", dict(kind="weekly_all", percent=140, resets_at=iso(NOW + 30 * hour), scope=None),
                    dict(group="session", percent=20, resets_at=iso(NOW - 60))],
        )
        cases = {
            "limits": (limits, dict(session=(None, NOW - 60), weekly=(0.0, NOW + 30 * hour))),
            "unopened": (flat(None, dict(utilization=None, resets_at=None)), dict(session=(100.0, None), weekly=(100.0, None))),
            "zero": (flat(dict(utilization=0), dict(utilization=4, resets_at=None)), dict(session=(100.0, None), weekly=(96.0, None))),
            "unusable": (flat({}, dict(utilization=True, resets_at="soon")) | dict(limits=7), dict(session=(None, None), weekly=(None, None))),
            "one window": (dict(seven_day=[1], five_hour=dict(utilization=-3, resets_at=10**400)), dict(session=(None, None), weekly=(None, None))),
            "empty": ({}, {}),
        }
        path = self.login / "quota-claude-0123456789abcdef.json"
        for name, (payload, expected) in cases.items():
            self.cache(path, payload, 30)
            windows = subscription_usage.readings(dict(claude_billing="subscription"), NOW)["claude"]["windows"]
            self.assertEqual({k: (v["left"], v["resets_at"]) for k, v in windows.items()}, expected, name)
        self.assertEqual(subscription_usage.readings(SUBSCRIPTION, NOW)["codex"], None)
        self.codex(self.runtime("a" * 40) / "state/fleet-1/cache/quota-codex.json", 30, (10**400, "long", None), (55, 5 * hour, 10))
        codex = subscription_usage.readings(SUBSCRIPTION, NOW)["codex"]["windows"]
        self.assertEqual(codex["weekly"], dict(seconds=None, used=None, left=None, resets_at=None))
        self.assertEqual(codex["session"], dict(seconds=5.0 * hour, used=None, left=None, resets_at=NOW - 20))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            subscription_usage.display(dict(
                claude=dict(fetched_at=NOW - 7200, windows=dict(
                    session=dict(seconds=5 * hour, used=None, left=None, resets_at=NOW - 60),
                    weekly=dict(seconds=7 * 24 * hour, used=0.0, left=100.0, resets_at=None))),
                codex=dict(fetched_at=NOW - 30, windows=codex | dict(session=dict(seconds=5.0 * hour, used=4.0, left=96.0, resets_at=None))),
                kiro=None, other=dict(fetched_at=NOW, windows={})), NOW)
        self.assertEqual(output.getvalue().splitlines(), [
            "Claude usage: 5h reset since this reading; 7d 100% left, no window open (read 2h00m ago)",
            "Codex usage: weekly unreadable; 5h 96% left (read 0m ago)",
            "Kiro usage: no reading yet",
            "Other usage: unreadable response (read 0m ago)",
        ])

    def test_credentials_with_served_readings_are_listed_rather_than_one_chosen(self):
        hour = 3600

        def claude(fp, used, age, **extra):
            name = hashlib.sha256(fp.encode()).hexdigest()[:16]
            self.cache(self.login / f"quota-claude-{name}.json", flat(dict(utilization=used, resets_at=iso(NOW + hour)), None),
                       age, fp=fp, **extra)

        def label(fp):
            return hashlib.sha256(fp.encode()).hexdigest()[:8]

        claude("first", 99, 600)
        claude("second", 10, 60)
        claude("third", 50, 2 * hour)
        claude("fourth", 20, 30, valid_until=NOW - 1)
        value = subscription_usage.readings(dict(claude_billing="subscription"), NOW)["claude"]
        self.assertEqual([(each["credential"], each["windows"]["session"]["left"]) for each in value["credentials"]],
                         [(label("second"), 90.0), (label("first"), 1.0)])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            subscription_usage.display(dict(claude=value), NOW)
        self.assertEqual(output.getvalue().splitlines(), [
            "Claude usage: 2 credentials have current readings; the workers use the active one",
            f"  credential {label('second')}: 5h 90% left, resets in 1h00m; 7d 100% left, no window open (read 1m ago)",
            f"  credential {label('first')}: 5h 1% left, resets in 1h00m; 7d 100% left, no window open (read 10m ago)",
        ])

        # A reading no worker would serve any more gives way to one that would; with none served, the newest stands.
        (self.login / f"quota-claude-{hashlib.sha256(b'second').hexdigest()[:16]}.json").unlink()
        self.assertEqual(subscription_usage.readings(dict(claude_billing="subscription"), NOW)["claude"]["credential"], label("first"))
        (self.login / f"quota-claude-{hashlib.sha256(b'first').hexdigest()[:16]}.json").unlink()
        self.assertEqual(subscription_usage.readings(dict(claude_billing="subscription"), NOW)["claude"]["credential"], label("fourth"))

        self.codex(self.runtime("a" * 40) / "state/fleet-1/cache/quota-codex.json", 120, (40, 5 * hour, hour), fp="one")
        self.codex(self.runtime("a" * 40) / "state/fleet-2/cache/quota-codex.json", 60, (5, 5 * hour, hour), fp="two")
        self.codex(self.runtime("a" * 40) / "state/fleet-3/cache/quota-codex.json", 30, (7, 5 * hour, hour))
        codex = subscription_usage.readings(dict(agent="codex"), NOW)["codex"]["credentials"]
        self.assertEqual([each["credential"] for each in codex], [label("account"), label("two"), label("one")])
        self.codex(self.runtime("a" * 40) / "state/fleet-1/cache/quota-codex.json", 700, (40, 5 * hour, hour), fp="one")
        self.codex(self.runtime("a" * 40) / "state/fleet-2/cache/quota-codex.json", 60, (5, 5 * hour, hour))
        self.assertEqual(subscription_usage.readings(dict(agent="codex"), NOW)["codex"]["fetched_at"], NOW - 30)

    def test_an_expired_reading_with_an_exhausted_window_stays_attributed_until_that_window_resets(self):
        hour = 3600

        def claude(fp, session, weekly, age):
            name = hashlib.sha256(fp.encode()).hexdigest()[:16]
            self.cache(self.login / f"quota-claude-{name}.json", flat(
                dict(utilization=session, resets_at=iso(NOW + hour)), dict(utilization=weekly, resets_at=iso(NOW + 72 * hour))),
                age, fp=fp)

        def label(fp):
            return hashlib.sha256(fp.encode()).hexdigest()[:8]

        claude("active", 20, 100, 3700)
        claude("other", 10, 10, 60)
        value = subscription_usage.readings(dict(claude_billing="subscription"), NOW)["claude"]
        self.assertNotIn("windows", value)
        self.assertEqual([(each["credential"], each["windows"]["weekly"]["left"]) for each in value["credentials"]],
                         [(label("other"), 90.0), (label("active"), 0.0)])

        # Once nothing else is current, the reading that remains is still named beside the ones set aside.
        claude("active", 20, 99, 3700)
        value = subscription_usage.readings(dict(claude_billing="subscription"), NOW)["claude"]
        self.assertEqual((value["credential"], value["other_credentials"]), (label("other"), 1))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            subscription_usage.display(dict(claude=value), NOW)
        self.assertEqual(output.getvalue(), f"Claude usage, credential {label('other')} (1 other credential has an older reading): "
                         "5h 90% left, resets in 1h00m; 7d 90% left, resets in 3d00h (read 1m ago)\n")

        self.cache(self.runtime("a" * 40) / "state/fleet-1/cache/quota-codex.json", dict(rate_limit=dict(
            limit_reached=True, primary_window=dict(used_percent=30, limit_window_seconds=5 * hour, reset_after_seconds=2 * hour))),
            900, fp="one")
        self.codex(self.runtime("a" * 40) / "state/fleet-2/cache/quota-codex.json", 60, (5, 5 * hour, hour), fp="two")
        codex = subscription_usage.readings(dict(agent="codex"), NOW)["codex"]["credentials"]
        self.assertEqual([(each["credential"], each["limit_reached"]) for each in codex], [(label("two"), False), (label("one"), True)])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            subscription_usage.display(dict(codex=dict(credentials=codex)), NOW)
        self.assertEqual(output.getvalue().splitlines()[2],
                         f"  credential {label('one')}: limit reached; 5h 70% left, resets in 1h45m (read 15m ago)")

    def test_claude_readings_are_found_beside_the_login_each_worker_recorded_as_its_source(self):
        hour = 3600
        payload = flat(dict(utilization=25, resets_at=iso(NOW + hour)), None)
        # A login chosen in the launcher's environment is recorded in each worker's isolated home.
        chosen = self.root / "chosen-login"
        marker = self.runtime("a" * 40) / "state/fleet-1/home/.claude/.tauceti-creds-source"
        marker.parent.mkdir(parents=True)
        marker.write_text(f"{chosen}\n")
        self.cache(chosen / ".tauceti-quota/usage/quota-claude-0123456789abcdef.json", payload, 60)
        self.assertEqual(subscription_usage.readings(dict(claude_billing="subscription"), NOW)["claude"]["fetched_at"], NOW - 60)
        # macOS keeps isolated worker homes under the login user's ~/.tauceti instead.
        other = self.root / "other-login"
        marker = self.root / "home/.tauceti/fleet-2/.claude/.tauceti-creds-source"
        marker.parent.mkdir(parents=True)
        marker.write_text(str(other))
        self.cache(other / ".tauceti-quota/usage/quota-claude-0123456789abcdef.json", payload, 30)
        self.assertEqual(subscription_usage.readings(dict(claude_billing="subscription"), NOW)["claude"]["fetched_at"], NOW - 30)
        marker.write_text("relative/login")
        self.assertEqual(subscription_usage.claude_logins(dict(claude_billing="subscription")),
                         [self.root / "home/.claude", chosen])

    def test_worker_data_root_inherited_by_the_worker_launcher_is_searched_by_a_later_status(self):
        hour = 3600
        kept = self.root / "durable"
        token = self.root / "token"
        token.write_text("synthetic-token")
        store.update_host(github_token_file=str(token), agent="codex")
        self.codex(kept / "state/fleet-1/cache/quota-codex.json", 60, (20, 5 * hour, hour))
        self.assertIsNone(subscription_usage.readings(store.host_config(), NOW)["codex"])
        with (
            patch.dict(os.environ, {"TAUCETI_LEGACY_DATA_ROOT": str(kept)}),
            patch.object(host, "executable", return_value="/fake/tauceti"),
            patch.object(host.os, "execv"),
        ):
            host.run_workers()
        # Status runs in another process, which does not share the launcher's environment.
        self.assertNotIn("TAUCETI_LEGACY_DATA_ROOT", os.environ)
        self.assertEqual(subscription_usage.readings(store.host_config(), NOW)["codex"]["windows"]["session"]["left"], 80.0)
        with patch.object(host, "executable", return_value="/fake/tauceti"), patch.object(host.os, "execv"):
            host.run_workers()
        self.assertIsNone(subscription_usage.readings(store.host_config(), NOW)["codex"])

    def test_worker_data_root_of_an_already_running_manager_is_recorded_when_its_service_is_kept(self):
        hour = 3600
        kept = self.root / "durable"
        store.update_host(agent="codex")
        self.codex(kept / "state/fleet-1/cache/quota-codex.json", 60, (20, 5 * hour, hour))

        def systemctl(args, **_):
            output = f"LANG=C\nTAUCETI_LEGACY_DATA_ROOT={kept}\n" if args[-1] == "show-environment" else ""
            return subprocess.CompletedProcess(args, 0, output, "")

        units = self.root / "units"
        units.mkdir()
        with patch.object(host, "units", return_value=units), patch.object(host, "run", side_effect=systemctl):
            host.start_workers()
        self.assertEqual(subscription_usage.readings(store.host_config(), NOW)["codex"]["windows"]["session"]["left"], 80.0)
        with (
            patch.object(host, "units", return_value=units),
            patch.object(host, "run", return_value=subprocess.CompletedProcess([], 1, "", "")),
        ):
            host.start_workers()
        self.assertEqual(subscription_usage.readings(store.host_config(), NOW)["codex"]["windows"]["session"]["left"], 80.0)


if __name__ == "__main__":
    unittest.main()
