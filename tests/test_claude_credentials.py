import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capabilities, claude_credentials, cli, host, remote, store

DAY = 86400
TOKEN = "sk-ant-oat01-setup-token"


def login(refresh="refresh-1", **extra):
    return {"claudeAiOauth": dict(accessToken="access-1", refreshToken=refresh, expiresAt=1, subscriptionType="max", **extra)}


class ClaudeCredentials(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environ = patch.dict(os.environ, {"TAUCETI_FLEET_" + name.upper(): str(self.root / name) for name in ("config", "state", "data")})
        environ.start()
        self.addCleanup(environ.stop)
        service = patch.object(host, "install_service")
        service.start()
        self.addCleanup(service.stop)
        self.saved = time.time() - 100 * DAY
        self.files = {}
        for name, text in (("gh", "gh-secret"), ("token", TOKEN + "\n"), ("usage", json.dumps(login()))):
            self.files[name] = self.root / name
            self.write(name, text)
        os.utime(self.files["token"], (self.saved, self.saved))

    def write(self, name, text):
        self.files[name].write_text(text)
        self.files[name].chmod(0o600)

    def add(self, **changes):
        shape = dict(github_account="operator", github_token_file=str(self.files["gh"]), claude_billing="subscription",
                     claude_token_file=str(self.files["token"]), claude_usage_credentials_file=str(self.files["usage"]))
        store.atomic(store.config_dir() / "profiles.json", {"p": {k: v for k, v in dict(shape, **changes).items() if v is not None}})
        payload = remote.profile("p")
        return payload, host.configure(dict(payload, profile="p", ci_allowed=False))

    def workers(self):
        host.worker_config(store.host_config())
        text = (store.config_dir() / "workers.toml").read_text()
        return text, tomllib.loads(text)["workers"]

    def test_token_and_usage_login_reach_workers_as_private_files(self):
        store.update_host(max_workers=2)
        payload, result = self.add(env={"A": "1"})
        self.assertEqual(payload["credentials"]["claude_token_file"], TOKEN)
        secrets = store.config_dir() / "secrets"
        stored = json.loads((secrets / "claude/.credentials.json").read_text())["claudeAiOauth"]
        self.assertEqual(stored, dict(accessToken=TOKEN, expiresAt=int((self.files["token"].stat().st_mtime + 365 * DAY) * 1000),
                                      scopes=["user:inference"], subscriptionType="max"))
        self.assertEqual(json.loads((secrets / "claude-usage/.credentials.json").read_text())["claudeAiOauth"], login()["claudeAiOauth"])
        for path in (secrets, secrets / "claude", secrets / "claude-usage", secrets / "claude/.credentials.json",
                     secrets / "claude-usage/.credentials.json", secrets / "claude-usage/transferred.json"):
            self.assertEqual(path.stat().st_mode & 0o077, 0, path)
        text, workers = self.workers()
        self.assertEqual(len(workers), 2)
        for worker in workers:
            self.assertEqual(worker["env"], {"A": "1", "CLAUDE_CONFIG_DIR": str(secrets / "claude"),
                                             "TAUCETI_CLAUDE_USAGE_CREDENTIALS": str(secrets / "claude-usage/.credentials.json")})
            self.assertIs(worker["auto_refresh"], True)
        status = dict(claude_credentials=claude_credentials.report(store.host_config()), status={})
        clear = text + (store.config_dir() / "host.json").read_text() + json.dumps([result, status])
        for secret in (TOKEN, "access-1", "refresh-1"):
            self.assertNotIn(secret, clear)

        self.add(claude_usage_credentials_file=None)
        worker = self.workers()[1][0]
        self.assertEqual(worker["env"], {"CLAUDE_CONFIG_DIR": str(secrets / "claude")})
        self.assertNotIn("auto_refresh", worker)
        self.add(claude_token_file=None, claude_usage_credentials_file=None)
        self.assertFalse({"env", "auto_refresh"} & set(self.workers()[1][0]))

    def test_repeated_add_keeps_a_login_the_host_has_renewed(self):
        self.add()
        held = store.config_dir() / "secrets/claude-usage/.credentials.json"
        store.atomic(held, login("refresh-2"))
        store.update_host(workers_enabled=True)
        self.add()
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"], login("refresh-2")["claudeAiOauth"])
        self.write("usage", json.dumps(login("refresh-of-a-new-sign-in")))
        self.write("token", "sk-ant-oat01-next")
        self.add()
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"], login("refresh-of-a-new-sign-in")["claudeAiOauth"])
        token = json.loads((held.parent.parent / "claude/.credentials.json").read_text())["claudeAiOauth"]
        self.assertEqual((token["accessToken"], token["subscriptionType"]), ("sk-ant-oat01-next", "max"))
        # The same login in other formatting, or with other metadata, is not a new sign-in.
        store.atomic(held, login("refresh-3"))
        self.write("usage", json.dumps(login("refresh-of-a-new-sign-in", rateLimitTier="x"), indent=4))
        self.add()
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"], login("refresh-3")["claudeAiOauth"])
        self.files["usage"].unlink()
        self.add()
        self.assertTrue(held.is_file())
        with self.assertRaisesRegex(store.FleetError, "Drain"):
            self.add(claude_usage_credentials_file=None)

    def test_replacing_a_login_waits_for_a_renewal_in_progress(self):
        self.add()
        held = store.config_dir() / "secrets/claude-usage/.credentials.json"
        self.write("usage", json.dumps(login("refresh-of-a-new-sign-in")))
        payload = remote.profile("p")
        # The worker's lock name, held as a renewal of the old login holds it across its token exchange.
        with held.with_name(".credentials.json.refresh.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            replacing = threading.Thread(target=host.configure, args=(dict(payload, profile="p", ci_allowed=False),))
            replacing.start()
            time.sleep(0.5)
            store.atomic(held, login("refresh-2"))
            fcntl.flock(lock, fcntl.LOCK_UN)
        replacing.join(10)
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"], login("refresh-of-a-new-sign-in")["claudeAiOauth"])

    def test_a_sign_in_on_the_host_restarts_the_login_lifetime(self):
        now = time.time()
        os.utime(self.files["usage"], (now - 30 * DAY, now - 30 * DAY))
        self.add()
        held = store.config_dir() / "secrets/claude-usage/.credentials.json"

        def usage():
            entry = next(e for e in claude_credentials.report(store.host_config()) if e["name"] == "claude-usage-login")
            return entry["status"], entry["evidence"]

        self.assertEqual(usage(), ("blocked", "usage login stopped renewing 2 days ago"))
        store.atomic(held, login("refresh-of-a-host-sign-in"))
        os.utime(held, (now + 5, now + 5))
        self.assertEqual(usage(), ("ok", "usage login renews for another 28 days"))
        # A later renewal by a worker writes its success marker after the login.
        store.atomic(held, login("refresh-2"))
        renewed = held.with_name(".credentials.json.refresh.last-success")
        store.atomic(renewed, {"at": now + 10})
        for path in (held, renewed):
            os.utime(path, (now + 10, now + 10))
        self.assertEqual(usage(), ("ok", "usage login renews for another 28 days"))
        self.add()
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"], login("refresh-2")["claudeAiOauth"])

    def test_a_usage_login_goes_to_one_host(self):
        self.add()
        failing = set()

        def call(target, payload, expected_machine_id=None, **_):
            machine = "machine-" + target
            self.assertIn(expected_machine_id, (None, machine))
            if payload["action"] == "configure" and target in failing:
                # The host may have installed the login before its reply was lost.
                raise store.FleetError("Remote host did not return a fleet response")
            return dict(machine_id=machine, profile="p", ci_enrolled=False)

        def add(target):
            return remote.add(target, profile_name="p")

        with patch.object(remote, "deploy"), patch.object(remote, "call", side_effect=call):
            add("a")
            add("a")
            with self.assertRaisesRegex(store.FleetError, "transferred to a. Sign in once per host"):
                add("b")
            self.write("usage", json.dumps(login(), indent=2, sort_keys=True))
            with self.assertRaisesRegex(store.FleetError, "transferred to a"):
                add("b")
            self.write("usage", json.dumps(login("refresh-of-a-sign-in-for-b")))
            failing.add("c")
            with self.assertRaisesRegex(store.FleetError, "did not return"):
                add("c")
            with self.assertRaisesRegex(store.FleetError, "transferred to machine-c"):
                add("b")
            self.write("usage", json.dumps(login("refresh-of-a-second-sign-in-for-b")))
            add("b")
            with self.assertRaisesRegex(store.FleetError, "transferred to b"):
                add("a")
            self.files["usage"].unlink()
            add("a")

    def test_signing_in_on_the_host_survives_a_renewal_before_any_report(self):
        now = time.time()
        os.utime(self.files["usage"], (now - 30 * DAY, now - 30 * DAY))
        self.add()
        held = store.config_dir() / "secrets/claude-usage/.credentials.json"
        script = "import json, os, sys; open(os.environ['CLAUDE_CONFIG_DIR'] + '/.credentials.json', 'w').write(sys.argv[1])"
        with self.assertRaisesRegex(store.FleetError, "left no login"):
            claude_credentials.sign_in([sys.executable, "-c", "raise SystemExit(1)"])
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"], login()["claudeAiOauth"])
        claude_credentials.sign_in([sys.executable, "-c", script, json.dumps(login("refresh-of-a-host-sign-in"))])
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"], login("refresh-of-a-host-sign-in")["claudeAiOauth"])
        self.assertEqual(held.stat().st_mode & 0o077, 0)
        # A worker renews before Fleet reports: the login, then its success marker.
        store.atomic(held, login("refresh-2"))
        store.atomic(held.with_name(".credentials.json.refresh.last-success"), {"at": now})
        entry = next(e for e in claude_credentials.report(store.host_config()) if e["name"] == "claude-usage-login")
        self.assertEqual((entry["status"], entry["evidence"]), ("ok", "usage login renews for another 28 days"))
        self.add()
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"], login("refresh-2")["claudeAiOauth"])

    def test_status_does_not_wait_for_a_renewal_in_progress(self):
        self.add()
        held = store.config_dir() / "secrets/claude-usage/.credentials.json"
        replies = []
        with held.with_name(".credentials.json.refresh.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            reading = threading.Thread(target=lambda: replies.append(host.rpc(dict(action="status"))))
            reading.start()
            reading.join(5)
            self.assertFalse(reading.is_alive())
        entries = {e["name"]: e["status"] for e in replies[0]["claude_credentials"]}
        self.assertEqual(entries, {"claude-setup-token": "ok", "claude-usage-login": "ok"})

    def test_a_retry_after_a_lost_transfer_record_keeps_the_renewed_login(self):
        self.add()
        held = store.config_dir() / "secrets/claude-usage/.credentials.json"
        self.write("usage", json.dumps(login("refresh-of-a-new-sign-in")))
        write = claude_credentials.atomic

        def full(path, value):
            if Path(path).name == "transferred.json":
                raise OSError(28, "No space left on device")
            write(path, value)

        with patch.object(claude_credentials, "atomic", side_effect=full), self.assertRaises(OSError):
            self.add()
        # A worker renews the installed login, keeping the file's other keys.
        renewed = json.loads(held.read_text())
        renewed["claudeAiOauth"]["refreshToken"] = "refresh-2"
        store.atomic(held, renewed)
        self.add()
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"]["refreshToken"], "refresh-2")
        recorded = json.loads(held.with_name("transferred.json").read_text())
        self.assertEqual(recorded["sha256"], claude_credentials.fingerprint(json.dumps(login("refresh-of-a-new-sign-in"))))

    def test_a_retry_after_a_lost_transfer_record_keeps_a_later_host_sign_in(self):
        self.add()
        held = store.config_dir() / "secrets/claude-usage/.credentials.json"
        now = time.time()
        self.write("usage", json.dumps(login("refresh-of-a-new-sign-in")))
        os.utime(self.files["usage"], (now - 30 * DAY, now - 30 * DAY))
        write = claude_credentials.atomic

        def full(path, value):
            if Path(path).name == "transferred.json":
                raise OSError(28, "No space left on device")
            write(path, value)

        with patch.object(claude_credentials, "atomic", side_effect=full), self.assertRaises(OSError):
            self.add()
        script = "import os, sys; open(os.environ['CLAUDE_CONFIG_DIR'] + '/.credentials.json', 'w').write(sys.argv[1])"
        claude_credentials.sign_in([sys.executable, "-c", script, json.dumps(login("refresh-of-a-host-sign-in"))])
        self.add()
        self.assertEqual(json.loads(held.read_text())["claudeAiOauth"]["refreshToken"], "refresh-of-a-host-sign-in")
        entry = next(e for e in claude_credentials.report(store.host_config()) if e["name"] == "claude-usage-login")
        self.assertEqual((entry["status"], entry["evidence"]), ("ok", "usage login renews for another 28 days"))

    def test_rejected_profiles_store_nothing(self):
        store.host_config()
        cases = [
            (dict(claude_billing=None), "require claude_billing subscription"),
            (dict(claude_token_file=None), "requires claude_token_file"),
            (dict(env={"TAUCETI_CLAUDE_USAGE_CREDENTIALS": "/elsewhere"}), "env must not set TAUCETI_CLAUDE_USAGE_CREDENTIALS"),
            (dict(env={"A": "x" * (16 * 1024 - 3)}), "credential paths Fleet adds to it exceed 16 KB"),
        ]
        for changes, message in cases:
            with self.assertRaisesRegex(store.FleetError, message, msg=changes):
                self.add(**changes)
        for name, text, message in (("token", "sk-ant-api03-key", "setup-token"), ("token", TOKEN + " x", "setup-token"),
                                    ("usage", TOKEN, "claude auth login"), ("usage", json.dumps(login(refresh=None)), "claude auth login")):
            self.write(name, text)
            with self.assertRaisesRegex(store.FleetError, message, msg=text):
                self.add()
            self.write(name, TOKEN if name == "token" else json.dumps(login()))
        with self.assertRaisesRegex(store.FleetError, "never as a path"):
            host.configure(dict(claude_billing="subscription", claude_token_file="/operator/path"))
        self.files["token"].chmod(0o644)
        with self.assertRaisesRegex(store.FleetError, "chmod 600"):
            self.add()
        self.assertFalse((store.config_dir() / "secrets").exists())
        self.assertFalse(set(claude_credentials.KINDS) & set(store.host_config()))

    def test_doctor_and_status_count_days_and_name_the_sign_in_when_due(self):
        self.assertEqual(claude_credentials.report(store.host_config()), [])
        self.add()
        config = store.host_config()
        directory = store.config_dir() / "secrets/claude-usage"
        signed_in = json.loads((directory / "transferred.json").read_text())["signed_in"]

        def report(days, since=self.saved):
            return {e["name"]: e for e in claude_credentials.report(config, now=since + days * DAY)}

        fresh = report(0)
        self.assertEqual((fresh["claude-setup-token"]["status"], fresh["claude-setup-token"]["evidence"]), ("ok", "setup-token expires in 365 days"))
        self.assertEqual(fresh["claude-setup-token"]["remedy"], "")
        due = report(340)["claude-setup-token"]
        self.assertEqual((due["status"], due["evidence"]), ("warning", "setup-token expires in 25 days"))
        self.assertIn("claude setup-token", due["remedy"])
        self.assertEqual(report(367)["claude-setup-token"]["evidence"], "setup-token expired 2 days ago")
        self.assertEqual(report(367)["claude-setup-token"]["status"], "blocked")

        usage = report(20, signed_in)["claude-usage-login"]
        self.assertEqual((usage["status"], usage["evidence"], usage["remedy"]), ("ok", "usage login renews for another 8 days", ""))
        usage = report(25, signed_in)["claude-usage-login"]
        self.assertEqual((usage["status"], usage["evidence"]), ("warning", "usage login renews for another 3 days"))
        self.assertIn("tauceti-fleet claude-usage-login", usage["remedy"])
        self.assertEqual(report(30, signed_in)["claude-usage-login"]["status"], "blocked")
        store.atomic(directory / ".credentials.json", login(refreshTokenExpiresAt=int((signed_in + 40 * DAY) * 1000)))
        self.assertEqual(report(30, signed_in)["claude-usage-login"]["evidence"], "usage login renews for another 10 days")
        (directory / ".credentials.json").unlink()
        self.assertEqual(report(0, signed_in)["claude-usage-login"]["status"], "blocked")
        self.assertEqual(report(0, signed_in)["claude-setup-token"]["status"], "ok")
        alone = claude_credentials.report(dict(config, claude_usage_credentials_file=None))
        self.assertEqual([(e["name"], e["status"]) for e in alone], [("claude-setup-token", "ok"), ("claude-usage-login", "warning")])

        with (
            patch.object(capabilities, "resources", return_value=dict(cpus=8, memory_total=64 * 1024**3, memory_available=64 * 1024**3)),
            patch.object(capabilities, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")),
            patch.object(capabilities, "executable", return_value=None),
            patch.object(capabilities.os, "open", side_effect=PermissionError),
        ):
            checks = {c["name"]: c for c in capabilities.doctor(config, probe=False)["checks"]}
        self.assertEqual((checks["claude-setup-token"]["status"], checks["claude-setup-token"]["roles"]), ("ok", ["workers"]))
        self.assertEqual(checks["claude-usage-login"]["status"], "blocked")

        out = io.StringIO()
        value = dict(machine_id=config["machine_id"], status={}, claude_credentials=list(report(340).values()))
        with patch.object(remote, "inventory", return_value={"h": dict(target="h", machine_id=config["machine_id"])}), contextlib.redirect_stdout(out):
            cli.main(["status", "h"], _status_results={"h": value})
        self.assertIn("Claude credentials: setup-token expires in 25 days; usage login missing", out.getvalue())
        self.assertIn("  Next step: Run claude setup-token", out.getvalue())


if __name__ == "__main__":
    unittest.main()
