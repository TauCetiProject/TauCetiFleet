import base64
import io
import json
import os
import subprocess
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import host, pools, remote, store
from tauceti_fleet.cli import parser


def auth(account="account-one", email="test@example.com", expiry=None):
    payload = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account},
        "https://api.openai.com/profile": {"email": email},
        "exp": expiry or time.time() + 3600,
    }
    token = "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"
    return dict(tokens=dict(account_id=account, access_token=token, refresh_token="REAL-REFRESH-SECRET"))


def spec(**kwargs):
    return dict(
        agent="codex",
        billing="subscription",
        github_account="kim-em",
        account="test@example.com",
        max_workers=2,
        max_open_prs=128,
        pacing="exhaust",
        stop_on_exhaustion=True,
        github_token_file="/private/gh",
        codex_auth_file="/private/codex",
        **kwargs,
    )


class Pools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(
            os.environ,
            {
                "TAUCETI_FLEET_CONFIG": str(self.root / "config"),
                "TAUCETI_FLEET_STATE": str(self.root / "state"),
                "TAUCETI_FLEET_DATA": str(self.root / "data"),
            },
        )
        self.env.start()
        self.config = store.update_host(
            workers_enabled=True, max_workers=40, github_account="science", profile="science"
        )

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def configure(self, **settings):
        return pools.configure(
            dict(
                pool="gmail",
                settings={k: v for k, v in {**spec(), **settings}.items() if k in pools.FIELDS},
                credentials={"github_token_file": "SELECTED-GH-SECRET", "codex_auth_file": json.dumps(auth())},
            )
        )

    def test_configure_preserves_live_original_pool_and_strips_refresh(self):
        before = store.host_config()
        result = self.configure()
        after = store.host_config()
        self.assertEqual({k: v for k, v in after.items() if k != "worker_pools"}, before)
        directory, _ = pools.paths("gmail")
        text = (directory / "codex/auth.json").read_text()
        self.assertNotIn("REAL-REFRESH-SECRET", text)
        self.assertIn("placeholder-never-a-real-refresh-token", text)
        self.assertEqual((directory / "codex/auth.json").stat().st_mode & 0o077, 0)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(pools.maximum(after), 2)

    def test_wrong_account_and_invalid_policy_rejected_before_secret_write(self):
        with self.assertRaises(store.FleetError):
            self.configure(account="wrong@example.com")
        self.assertFalse((pools.paths("gmail")[0] / "secrets/github_token_file").exists())
        for changes in [
            dict(max_workers=-1),
            dict(max_workers=True),
            dict(pacing="other"),
            dict(pace="50:70,10:90"),
            dict(pacing="exhaust", pace="0:10,50:70"),
            dict(agent="codex", billing="api"),
        ]:
            with self.subTest(changes=changes), self.assertRaises(store.FleetError):
                self.configure(**changes)

    def test_private_profile_uses_selected_gh_and_never_exports_refresh(self):
        source = self.root / "auth.json"
        source.write_text(json.dumps(auth()))
        source.chmod(0o600)
        store.atomic(
            store.config_dir() / "profiles.json",
            {
                "gmail": dict(
                    agent="codex", account="test@example.com", github_account="kim-em", codex_auth_file=str(source)
                )
            },
        )
        with patch.object(remote, "run", return_value=subprocess.CompletedProcess([], 0, stdout="selected")) as run:
            value = remote.pool_profile("gmail")
        self.assertEqual(run.call_args.args[0], ["gh", "auth", "token", "--user", "kim-em"])
        self.assertEqual(value["billing"], "subscription")
        self.assertNotIn("REAL-REFRESH-SECRET", json.dumps(value))
        source.chmod(0o644)
        with self.assertRaises(store.FleetError):
            remote.pool_profile("gmail")

    def test_existing_worker_ids_adopted_without_spec_restart_and_reductions_only_gate(self):
        directory, _ = pools.paths("gmail")
        directory.mkdir(parents=True)
        value = spec()
        fields = pools.definition(value)
        text = "version = 1\n" + "".join(
            "\n[[workers]]\nid = "
            + json.dumps(w)
            + "\n"
            + "".join(k + " = " + json.dumps(v) + "\n" for k, v in fields.items())
            for w in ["codex-gmail-01", "codex-gmail-02"]
        )
        path = directory / "workers.toml"
        path.write_text(text)
        ids = pools.write_workers("gmail", value, self.config)
        self.assertEqual(ids, ["codex-gmail-01", "codex-gmail-02"])
        self.assertEqual(tomllib.loads(path.read_text()), tomllib.loads(text))
        value["max_workers"] = 1
        self.assertEqual(pools.write_workers("gmail", value, self.config), ids)
        self.assertTrue(all(w.get("enabled", True) for w in tomllib.loads(path.read_text())["workers"]))

    def test_reconciliation_preserves_worker_data_roots_in_both_manager_types(self):
        env = {"TAUCETI_LEGACY_DATA_ROOT": '/persistent/worker data/"original"', "CUSTOM": "a\\b"}
        for kind in ["original", "subscription"]:
            if kind == "original":
                config = dict(self.config, workers_enabled=True, max_workers=2)
                ids = host.worker_config(config)
                path = store.config_dir() / "workers.toml"
            else:
                ids = pools.write_workers("subscription", spec(), self.config)
                path = pools.paths("subscription")[0] / "workers.toml"
            text = path.read_text()
            text = text.replace(
                "id = " + json.dumps(ids[0]), "id = " + json.dumps(ids[0]) + "\n" + pools.environment_line(env)
            )
            path.write_text(text)
            for _ in range(2):
                if kind == "original":
                    host.worker_config(config)
                else:
                    pools.write_workers("subscription", spec(), self.config)
                workers = tomllib.loads(path.read_text())["workers"]
                self.assertEqual(workers[0]["env"], env)
                self.assertNotIn("env", workers[1])

    def test_exhaustion_latches_across_reset_and_restarts_but_not_outages_or_soft_pacing(self):
        now = time.time()
        reading = dict(
            at=now,
            account_id="account-one",
            email="test@example.com",
            windows=[dict(name="weekly", status="exhausted", used=100, resets_at=now + 600)],
        )
        result = pools.transition({}, reading, spec(), now)
        self.assertTrue(result["stopped"])
        reset = dict(at=now + 700, windows=[dict(status="under-pace", used=0, resets_at=now + 900)])
        self.assertTrue(pools.transition(json.loads(json.dumps(result)), reset, spec(), now + 700)["stopped"])
        for changed in [
            dict(error="HTTP 401"),
            dict(rate_limited=True),
            dict(at=now - 121),
            dict(at=now + 1),
            dict(windows=[dict(status="over-pace", used=99, resets_at=now + 600)]),
            dict(windows=[]),
        ]:
            self.assertFalse(pools.transition({}, dict(reading, **changed), spec(), now).get("stopped", False))
        value = spec()
        value["stop_on_exhaustion"] = False
        self.assertFalse(pools.transition({}, reading, value, now).get("stopped", False))

    def test_shutdown_finishes_active_sessions_then_retires_at_round_boundary(self):
        value = spec()
        ids = pools.write_workers("gmail", value, self.config)
        now = time.time()
        workers = [
            dict(id=ids[0], alive=True, state="running", heartbeat_at=now),
            dict(id=ids[1], alive=True, state="waiting-quota", heartbeat_at=now),
        ]
        pools.write_workers("gmail", value, self.config, stopped=True, workers=workers)
        path = pools.paths("gmail")[0] / "workers.toml"
        enabled = [w.get("enabled", True) for w in tomllib.loads(path.read_text())["workers"]]
        self.assertEqual(enabled, [True, False])
        workers[0]["state"] = "waiting-capacity"
        pools.write_workers("gmail", value, self.config, stopped=True, workers=workers)
        self.assertTrue(all(not w["enabled"] for w in tomllib.loads(path.read_text())["workers"]))

    def test_native_controller_observes_asynchronously_and_gates_each_pool(self):
        self.configure()
        config = store.host_config()
        controller = pools.Controller()
        try:
            with (
                patch.object(
                    pools,
                    "observe",
                    return_value=dict(at=time.time(), account_id="account-one", email="test@example.com", windows=[]),
                ),
                patch.object(pools, "start_manager"),
            ):
                summaries, _ = controller.step(config, capacity_open=True, reason="memory available")
                self.assertEqual(summaries[0]["admitted"], 0)
                controller.pending["gmail"][1].result(timeout=1)
                summaries, _ = controller.step(config, capacity_open=True, reason="memory available")
                self.assertEqual(summaries[0]["admitted"], 2)
                summaries, _ = controller.step(config, capacity_open=False, reason="memory pressure")
                self.assertEqual(summaries[0]["admitted"], 0)
                self.assertEqual(store.read(pools.paths("gmail")[1] / "admission.json")["workers"], [])
        finally:
            controller.close()

    def test_stop_and_resume_fence_stale_controller_and_preserve_explicit_latch_on_sync(self):
        self.configure()
        old = store.host_config()
        pools.configure(dict(pool="gmail", operation="stop"))
        state = pools.paths("gmail")[1] / "subscription.json"
        self.assertTrue(store.read(state)["stopped"])
        pools.configure(dict(pool="gmail", operation="sync", credentials={"codex_auth_file": json.dumps(auth())}))
        self.assertTrue(store.read(state)["stopped"])
        controller = pools.Controller()
        try:
            with patch.object(pools, "start_manager"), patch.object(pools, "observe") as observe:
                controller.step(old, capacity_open=True, reason="ready")
                observe.assert_not_called()
            self.assertTrue(store.read(state)["stopped"])
            pools.configure(dict(pool="gmail", operation="resume"))
            self.assertFalse(store.read(state)["stopped"])
            self.assertNotIn("quota", store.read(state))
        finally:
            controller.close()

    def test_exhaustion_from_another_account_does_not_stop_this_pool(self):
        now = time.time()
        reading = dict(
            at=now,
            account_id="wrong",
            email="wrong@example.com",
            windows=[dict(status="exhausted", resets_at=now + 100)],
        )
        self.assertFalse(pools.transition({}, reading, spec(), now).get("stopped", False))

    def test_one_pool_error_does_not_close_the_other_pools_admissions(self):
        self.configure()
        config = store.host_config()
        config["worker_pools"]["other"] = dict(
            config["worker_pools"]["gmail"],
            agent="claude",
            billing="api",
            anthropic_api_key_file="/private/key",
            stop_on_exhaustion=False,
        )
        directory, _ = pools.paths("gmail")
        (directory / "workers.toml").write_text("this is invalid TOML")
        controller = pools.Controller()
        try:
            with patch.object(pools, "start_manager"), patch.object(pools, "observe", return_value={}):
                summaries, _ = controller.step(config, capacity_open=True, reason="ready")
            self.assertEqual(summaries[0]["lifecycle"], "error")
            self.assertEqual(summaries[1]["admitted"], 2)
        finally:
            controller.close()

    def test_per_pool_event_import_is_incremental(self):
        self.configure()
        _, state = pools.paths("gmail")
        state.mkdir(parents=True, exist_ok=True)
        event = dict(version=1, id="pool-event", at=100, kind="pr_created", repository="example/repo", pr=1)
        (state / "worker-events.jsonl").write_text(json.dumps(event) + "\n")
        store.import_worker_events()
        store.import_worker_events()
        with store.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE id='pool-event'").fetchone()[0], 1)

    def test_status_reads_cached_pool_reports_without_observing_usage(self):
        store.atomic(store.state_dir() / "status.json", dict(worker_pools=[dict(name="gmail")]))
        with (
            patch.object(pools, "observe") as observe,
            patch.object(host.activity, "cached", return_value={}),
            patch.object(host.ci_overview, "cached", return_value={}),
        ):
            result = host.rpc(dict(action="status"))
        self.assertEqual(result["status"]["worker_pools"], [dict(name="gmail")])
        observe.assert_not_called()

    def test_pool_launcher_selects_only_pool_credentials_and_account_guard(self):
        self.configure()
        with (
            patch.object(pools, "executable", return_value="/runtime/bin/tauceti"),
            patch.object(pools.os, "execv") as launch,
        ):
            pools.launch("gmail")
        self.assertEqual(os.environ["GH_TOKEN"], "SELECTED-GH-SECRET")
        self.assertEqual(os.environ["TAUCETI_ACCOUNT"], "test@example.com")
        self.assertNotIn("SELECTED-GH-SECRET", str(launch.call_args))
        self.assertTrue(os.environ["GH_CONFIG_DIR"].endswith("/pools/gmail/gh"))

    def test_changed_identity_restarts_manager_but_count_changes_preserve_it(self):
        self.configure()
        config = store.host_config()
        value = config["worker_pools"]["gmail"]
        _, state = pools.paths("gmail")
        store.atomic(state / "manager.json", dict(identity=pools.manager_identity(value)))
        controller = pools.Controller()
        try:
            with patch.object(pools, "start_manager") as start, patch.object(pools, "observe", return_value={}):
                controller.step(config, capacity_open=True, reason="ready")
                start.assert_called_once_with("gmail", restart=False)
                start.reset_mock()
                value["max_workers"] = 3
                controller.step(config, capacity_open=True, reason="ready")
                start.assert_not_called()
                value["account"] = "other@example.com"
                controller.step(config, capacity_open=True, reason="ready")
                start.assert_called_once_with("gmail", restart=True)
        finally:
            controller.close()

    def test_cli_pool_policies_and_status_render(self):
        args = parser().parse_args(
            ["pool", "set", "host", "gmail", "--workers", "16", "--pacing", "exhaust", "--stop-on-exhaustion"]
        )
        self.assertEqual(args.maximum, 16)
        self.assertTrue(args.stop_on_exhaustion)
        output = io.StringIO()
        with patch("sys.stdout", output):
            pools.display([pools.summary("gmail", spec(), [], [], dict(stopped=True, reason="exhausted"))])
        self.assertIn("GitHub kim-em", output.getvalue())
        self.assertIn("test@example.com", output.getvalue())
        self.assertIn("stop on exhaustion", output.getvalue())
        self.assertIn("stopped: exhausted", output.getvalue())


if __name__ == "__main__":
    unittest.main()
