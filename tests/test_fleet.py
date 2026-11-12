import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import activity, capabilities, host, pool_activity, remote, report, store, vm
from tauceti_fleet.cli import main


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environ = patch.dict(
            os.environ,
            {
                "TAUCETI_FLEET_CONFIG": str(self.root / "config"),
                "TAUCETI_FLEET_STATE": str(self.root / "state"),
                "TAUCETI_FLEET_DATA": str(self.root / "data"),
            },
        )
        self.environ.start()
        public_activity = patch.object(pool_activity, "refresh", return_value={})
        public_activity.start()
        self.addCleanup(public_activity.stop)

    def tearDown(self):
        self.environ.stop()
        self.temporary.cleanup()

    def test_self_tested_setting_reaches_worker_specs_without_changing_admission(self):
        import tomllib
        original = store.update_host(max_workers=2, workers_enabled=True, ci_enabled=True)
        lease = store.state_dir() / "worker-admission-lease.json"
        store.atomic(lease, dict(expires_at=100))
        for enabled in (True, False):
            with patch.object(host, "require_self_tested_support"):
                result = host.rpc(dict(action="workers", self_tested=enabled))
            actual = store.host_config()
            self.assertEqual(actual, {**original, "self_tested": enabled})
            self.assertEqual(result["self_tested"], enabled)
            self.assertTrue(lease.exists())
            specs = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
            self.assertEqual(len(specs), 2)
            self.assertTrue(all(spec.get("self_tested", False) == enabled for spec in specs))
        with self.assertRaises(store.FleetError):
            host.rpc(dict(action="workers", self_tested="yes", maximum=0))
        self.assertEqual(store.host_config()["max_workers"], 2)

    def test_unsupported_running_manager_does_not_change_policy_or_admissions(self):
        original = store.update_host(max_workers=2, workers_enabled=True)
        with patch.object(host, "require_self_tested_support", side_effect=store.FleetError("old manager")):
            with self.assertRaisesRegex(store.FleetError, "old manager"):
                host.set_worker_policy(dict(self_tested=True, maximum=0))
        self.assertEqual(store.host_config(), original)

    def test_self_tested_support_checks_the_running_manager(self):
        import socket
        import threading
        from types import SimpleNamespace

        path = store.state_dir() / "worker-runtime/manager.sock"
        path.parent.mkdir(parents=True, exist_ok=True)
        for reply, supported in ((dict(ok=True), False), (dict(ok=True, self_tested=1), True)):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                server.listen(1)
                def serve():
                    connection, _ = server.accept()
                    with connection:
                        connection.recv(4096)
                        connection.sendall(json.dumps(reply).encode() + b"\n")
                thread = threading.Thread(target=serve)
                thread.start()
                with patch.object(host, "executable", return_value="tauceti"), patch.object(
                    host, "run", return_value=SimpleNamespace(stdout='{"self_tested":1}')
                ):
                    if supported:
                        host.require_self_tested_support()
                    else:
                        with self.assertRaisesRegex(store.FleetError, "running worker manager"):
                            host.require_self_tested_support()
                thread.join(5)
            path.unlink()

    def test_controller_rewrite_keeps_newer_self_tested_policy(self):
        import tomllib
        before = store.update_host(max_workers=2, workers_enabled=True, self_tested=False)
        store.update_host(self_tested=True)
        host.worker_config(before)
        specs = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
        self.assertTrue(all(s["self_tested"] for s in specs))

    def test_worker_cli_passes_only_explicit_policy_changes(self):
        with (patch.object(remote, "inventory", return_value={"test": dict(target="target", machine_id="m")}),
              patch.object(remote, "resolve", return_value=dict(target="target", machine_id="m")),
              patch.object(remote, "call", return_value={}) as call,
              patch("sys.stdout", new=io.StringIO())):
            for flag, enabled in (("--self-tested", True), ("--no-self-tested", False)):
                self.assertEqual(main(["workers", "test", flag, "--json"]), 0)
                self.assertEqual(call.call_args.args[1], dict(action="workers", self_tested=enabled))
            self.assertEqual(main(["workers", "test", "--max", "4", "--json"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="workers", maximum=4))

    def test_identity_and_private_configuration_survive_reinstallation(self):
        one = store.host_config()
        store.update_host(workers_enabled=True)
        two = store.host_config()
        self.assertEqual(one["machine_id"], two["machine_id"])
        self.assertTrue(two["workers_enabled"])
        self.assertEqual((store.config_dir() / "host.json").stat().st_mode & 0o077, 0)

    def test_configure_does_not_activate_or_reset_grants_and_transfers_only_named_secrets(self):
        store.host_config()
        with patch.object(host, "install_service"):
            result = host.configure(
                dict(profile="test", ci_allowed=False, credentials={"anthropic_api_key_file": "test-secret"})
            )
        self.assertFalse(result["ci_enabled"])
        self.assertFalse(result["workers_enabled"])
        value = store.host_config()
        secret = Path(value["anthropic_api_key_file"])
        self.assertEqual(secret.read_text(), "test-secret\n")
        self.assertNotIn("test-secret", json.dumps(result))
        self.assertEqual(secret.stat().st_mode & 0o077, 0)
        self.assertFalse((self.root / "state/budget/events.jsonl").exists())

    def test_funding_requires_explicit_host(self):
        with patch("sys.stderr", new=io.StringIO()), patch.object(remote, "inventory", return_value={}):
            self.assertEqual(main(["budget", "--grant", "10"]), 1)

    def test_reconfiguration_requires_drain_and_invalid_credentials_do_not_partially_install(self):
        store.update_host(workers_enabled=True)
        with self.assertRaisesRegex(store.FleetError, "Drain"):
            host.configure(dict(profile="different"))
        with self.assertRaisesRegex(store.FleetError, "JSON object"):
            host.configure(dict(credentials=[]))

    def secrets(self):
        for name in ("key", "token"):
            (self.root / name).write_text(name + "-secret")
            (self.root / name).chmod(0o600)
        return dict(github_account="operator", anthropic_api_key_file=str(self.root / "key"),
                    github_token_file=str(self.root / "token"))

    def test_api_profile_worker_definitions_keep_their_exact_text(self):
        store.atomic(store.config_dir() / "profiles.json", {"api": dict(self.secrets(), author_model="model")})
        payload = remote.profile("api")
        self.assertEqual(payload["credentials"], {"anthropic_api_key_file": "key-secret", "github_token_file": "token-secret"})
        store.update_host(max_workers=1)
        with patch.object(host, "install_service"):
            host.configure(dict(payload, profile="api", ci_allowed=False))
        (wid,) = host.worker_config(store.host_config())
        self.assertEqual(
            (store.config_dir() / "workers.toml").read_text(),
            f'version = 1\n\n[[workers]]\nid = "{wid}"\nagent = "claude"\nmax_open_prs = 8\nclaude_billing = "api"\n'
            f'budget = true\ngithub_account = "operator"\nanthropic_api_key_file = "{store.config_dir() / "secrets/anthropic_api_key_file"}"\n'
            'author_model = "model"\n',
        )

    def test_subscription_profile_transfers_no_api_key_and_writes_no_budget(self):
        import tomllib

        shape = dict(claude_billing="subscription", agent="auto", sandbox="bubble", pace="0:10,50:70",
                     env={"TAUCETI_OVERLAP_BATCH_COMMITS": "80", "A": 'quoted "value"'})
        store.atomic(store.config_dir() / "profiles.json", {"subscription": dict(shape, **self.secrets())})
        payload = remote.profile("subscription")
        self.assertEqual(payload["credentials"], {"github_token_file": "token-secret"})
        store.update_host(max_workers=2, anthropic_api_key_file="/private/earlier-api-key")
        with patch.object(host, "install_service"):
            host.configure(dict(payload, profile="subscription", ci_allowed=False))
        host.worker_config(store.host_config())
        workers = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
        self.assertEqual(len(workers), 2)
        for worker in workers:
            self.assertEqual({k: worker[k] for k in shape}, shape)
            self.assertFalse({"budget", "anthropic_api_key_file"} & set(worker))
        with (
            patch.object(capabilities, "resources", return_value=dict(cpus=8, memory_total=64 * 1024**3, memory_available=64 * 1024**3)),
            patch.object(capabilities, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")),
            patch.object(capabilities, "executable", return_value=None),
            patch.object(capabilities.os, "open", side_effect=PermissionError),
        ):
            checks = {c["name"]: c for c in capabilities.doctor(store.host_config(), probe=False)["checks"]}
            defaults = {c["name"] for c in capabilities.doctor(dict(store.host_config(), agent=None, claude_billing=None, sandbox=None), probe=False)["checks"]}
        for tool in ("claude", "codex", "bubble", "incus"):
            self.assertEqual(checks["worker-tool-" + tool]["status"], "blocked")
            self.assertEqual(checks["worker-tool-" + tool]["roles"], ["workers"])
        self.assertNotIn("anthropic_api_key_file", checks)
        self.assertIn("anthropic_api_key_file", defaults)
        self.assertFalse({"worker-tool-codex", "worker-tool-bubble", "worker-tool-incus", "worker-sandbox-bubble"} & defaults)
        bubble = checks["worker-sandbox-bubble"]
        self.assertEqual((bubble["status"], bubble["roles"]), ("warning", ["workers"]))
        self.assertIn("outside tauceti-fleet-workers.slice", bubble["evidence"])

    def test_profile_rejects_what_the_worker_rejects_and_changes_only_while_drained(self):
        store.host_config()
        rejected = [
            dict(agent="codex"),
            dict(agent="auto"),
            dict(agent="kiro", claude_billing="subscription"),
            dict(claude_billing="oauth"),
            dict(pace="0:10"),
            dict(claude_billing="subscription", pace="50"),
            dict(claude_billing="subscription", pace="0:10,0:20"),
            dict(claude_billing="subscription", pace="120:10"),
            dict(claude_billing="subscription", agent="auto", author_model="model"),
            dict(claude_billing="subscription", agent="auto", author_effort="high"),
            dict(sandbox="vm"),
            dict(env=["A=1"]),
            dict(env={"A": 1}),
            dict(env={"A-B": "1"}),
            dict(env={"A": "line\nbreak"}),
            dict(env={"TAUCETI_DATA_HOME": "/elsewhere"}),
            dict(env={"TAUCETI_FLEET_ADMISSION_FILE": "/elsewhere"}),
            *(dict(env={name: "/elsewhere"}) for name in host.FLEET_OWNED_ENV),
            *(dict(env={name: "/elsewhere"}) for name in ("TAUCETI_BUDGET_DIR", "TAUCETI_ACTIVITY_DIR",
                                                          "TAUCETI_GITHUB_SHARED_DIR", "XDG_STATE_HOME")),
            *(dict(claude_billing="subscription", agent="codex", env={name: "/other-login"})
              for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HOME")),
            dict(env={"A": "x" * 16384}),
        ]
        with patch.object(host, "install_service"):
            for shape in rejected:
                with self.assertRaises(store.FleetError, msg=shape):
                    host.configure(shape)
            self.assertFalse({"agent", "claude_billing", "sandbox", "pace", "env"} & set(store.host_config()))
            host.configure(dict(claude_billing="subscription", agent="codex", author_model="model"))
            store.update_host(workers_enabled=True)
            host.configure(dict(claude_billing="subscription", agent="codex"))
            for shape in (dict(agent="claude"), dict(claude_billing=None, agent=None), dict(env={"A": "1"})):
                with self.assertRaisesRegex(store.FleetError, "Drain", msg=shape):
                    host.configure(shape)
            store.update_host(workers_enabled=False)
            host.configure(dict(claude_billing=None, agent=None))
        fields = host.worker_fields(store.host_config())
        self.assertEqual((fields["agent"], fields["claude_billing"], fields["budget"]), ("claude", "api", True))

    def test_profile_without_model_overrides_clears_the_saved_ones(self):
        store.atomic(store.config_dir() / "profiles.json", {
            "api": dict(self.secrets(), author_model="model", author_effort="high"),
            "auto": dict(github_account="operator", claude_billing="subscription", agent="auto"),
        })
        with patch.object(host, "install_service"):
            host.configure(dict(remote.profile("api"), profile="api", ci_allowed=False))
            host.configure(dict(remote.profile("auto"), profile="auto"))
        fields = host.worker_fields(store.host_config())
        self.assertEqual(fields["agent"], "auto")
        self.assertFalse({"author_model", "author_effort"} & set(fields))

    def forgejo_secrets(self):
        (self.root / "forgejo").write_text("forgejo-secret")
        (self.root / "forgejo").chmod(0o600)
        return dict(self.secrets(), forgejo_token_file=str(self.root / "forgejo"))

    def doctor_checks(self, protocol):
        def probe(argv, **_):
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(protocol) if argv[-1] == "_fleet-protocol" else "0")

        with (
            patch.object(capabilities, "resources", return_value=dict(cpus=8, memory_total=64 * 1024**3, memory_available=64 * 1024**3)),
            patch.object(capabilities, "run", side_effect=probe),
            patch.object(capabilities, "executable", side_effect=lambda name: "/fake/tauceti" if name == "tauceti" else None),
            patch.object(capabilities.os, "open", side_effect=PermissionError),
        ):
            return {c["name"]: c for c in capabilities.doctor(store.host_config(), probe=False)["checks"]}

    def test_forge_selection_rejects_what_the_worker_manager_rejects(self):
        store.host_config()
        token = dict(credentials={"forgejo_token_file": "forgejo-secret"})
        rejected = [
            (dict(forges="forgejo"), "forges must be a list"),
            (dict(forges=["gitlab"]), r"unknown forge\(s\): gitlab; use github and/or forgejo"),
            (dict(forges=[]), "at least one of github, forgejo"),
            (dict(forges=["github", "github"]), "more than once"),
            (dict(new_pr_forge="gitlab"), "new_pr_forge must be github or forgejo"),
            (dict(new_pr_forge="forgejo"), "new_pr_forge must be one of the forges"),
            (dict(forges=["github"], new_pr_forge="forgejo"), "new_pr_forge must be one of the forges"),
            (dict(forges=["github", "forgejo"], env={"TAUCETI_PR": "12"}), "env.TAUCETI_PR targets one source"),
            (dict(forges=["forgejo"], sandbox="bubble", **token), "Forgejo rounds run on the host only"),
            (dict(forges=["github", "forgejo"], new_pr_forge="forgejo", sandbox="bubble", **token), "host only"),
            (dict(forges=["forgejo"], env={"TAUCETI_REPO": "Example/Fork"}, **token), "env.TAUCETI_FORGEJO_ORIGIN"),
            (dict(forges=["forgejo"], env={"TAUCETI_FORGEJO_ORIGIN": "https://forge.example"}, **token),
             "env.TAUCETI_FORGEJO_REPOSITORY_ID"),
            (dict(forges=["forgejo"], env={"TAUCETI_FORGEJO_REPO": "Example/Fork"}, **token), "TAUCETI_FORGEJO_REPOSITORY_ID"),
            (dict(forges=["forgejo"], env={"TAUCETI_FORGEJO_ORIGIN": "https://forge.example",
                                           "TAUCETI_FORGEJO_REPOSITORY_ID": "0"}, **token), "TAUCETI_FORGEJO_REPOSITORY_ID"),
            (dict(forges=["forgejo"], env={"TAUCETI_FORGEJO_ORIGIN": "https://forge.example",
                                           "TAUCETI_FORGEJO_REPOSITORY_ID": "7"}), "needs forgejo_token_file"),
            *((dict(env={name: "x"}), "env must not set " + name) for name in host.FORGE_OWNED_ENV),
            (dict(env={"TAUCETI_FORGEJO_TOKEN_FILE": "/elsewhere"}, **token), "env must not set TAUCETI_FORGEJO_TOKEN_FILE"),
            (dict(forgejo_token_file="/elsewhere"), "transferred as a credential"),
        ]
        with patch.object(host, "install_service"):
            for shape, message in rejected:
                with self.assertRaisesRegex(store.FleetError, message, msg=shape):
                    host.configure(shape)
            self.assertFalse({"forges", "new_pr_forge", "forgejo_token_file", "env"} & set(store.host_config()))
            self.assertFalse((store.config_dir() / "secrets/forgejo_token_file").exists())
            # The canonical repository needs no connection settings; a token file named in env is enough.
            host.configure(dict(forges=["forgejo"]))
            host.configure(dict(forges=["forgejo"], new_pr_forge="forgejo", env={
                "TAUCETI_FORGEJO_ORIGIN": "https://forge.example", "TAUCETI_FORGEJO_REPO": "Example/Fork",
                "TAUCETI_FORGEJO_REPOSITORY_ID": "7", "TAUCETI_FORGEJO_TOKEN_FILE": "/host/forgejo-token",
                "TAUCETI_FORGEJO_FORK": "operator/Fork"}))
            store.update_host(workers_enabled=True)
            for shape in (dict(forges=["github", "forgejo"]), dict(new_pr_forge=None)):
                with self.assertRaisesRegex(store.FleetError, "Drain", msg=shape):
                    host.configure(shape)

    def test_forgejo_profile_transfers_its_token_and_writes_forge_fields(self):
        store.atomic(store.config_dir() / "profiles.json", {
            "forgejo": dict(self.forgejo_secrets(), forges=["forgejo"], new_pr_forge="forgejo",
                            env={"TAUCETI_FORGEJO_FORK": "operator/TauCeti"}),
            "both": dict(self.secrets(), forges=["forgejo", "github"]),
            "github": self.secrets(),
        })
        payload = remote.profile("forgejo")
        self.assertEqual(payload["credentials"]["forgejo_token_file"], "forgejo-secret")
        self.assertEqual(payload["credentials"]["github_token_file"], "token-secret")
        store.update_host(max_workers=1)
        stored = store.config_dir() / "secrets/forgejo_token_file"
        with patch.object(host, "install_service"):
            host.configure(dict(payload, profile="forgejo", ci_allowed=False))
        self.assertEqual(stored.read_text(), "forgejo-secret\n")
        self.assertEqual(stored.stat().st_mode & 0o077, 0)
        with patch.object(host, "worker_protocol", return_value=dict(version=1, forges=1)):
            (wid,) = host.worker_config(store.host_config())
        self.assertEqual(
            (store.config_dir() / "workers.toml").read_text(),
            f'version = 1\n\n[[workers]]\nid = "{wid}"\nagent = "claude"\nmax_open_prs = 8\nclaude_billing = "api"\n'
            f'budget = true\ngithub_account = "operator"\nanthropic_api_key_file = "{store.config_dir() / "secrets/anthropic_api_key_file"}"\n'
            'forges = ["forgejo"]\nnew_pr_forge = "forgejo"\n'
            f'env = {{TAUCETI_FORGEJO_FORK = "operator/TauCeti", TAUCETI_FORGEJO_TOKEN_FILE = "{stored}"}}\n',
        )
        self.assertNotIn("forgejo-secret", (store.config_dir() / "workers.toml").read_text())
        with patch.object(host, "install_service"):
            host.configure(dict(remote.profile("both"), profile="both"))
        self.assertIsNone(store.host_config()["forgejo_token_file"])
        fields = host.worker_fields(store.host_config())
        self.assertEqual((fields["forges"], "new_pr_forge" in fields, "env" in fields), (["github", "forgejo"], False, False))
        with patch.object(host, "install_service"):
            host.configure(dict(remote.profile("github"), profile="github"))
        self.assertFalse({"forges", "new_pr_forge", "env"} & set(host.worker_fields(store.host_config())))
        (self.root / "forgejo").unlink()
        with self.assertRaisesRegex(store.FleetError, "does not exist"):
            remote.profile("forgejo")

    def test_forge_fields_reach_only_a_worker_that_reports_the_forges_protocol(self):
        store.update_host(max_workers=1, forges=["github", "forgejo"], new_pr_forge="github")
        for protocol in ({}, dict(version=1, roadmap_focus=1)):
            with patch.object(host, "worker_protocol", return_value=protocol), self.assertRaisesRegex(store.FleetError, "does not accept forges"):
                host.worker_config(store.host_config())
        self.assertFalse((store.config_dir() / "workers.toml").exists())
        checks = self.doctor_checks(dict(version=1, roadmap_focus=1))
        self.assertEqual((checks["worker-runtime"]["status"], checks["worker-forges"]["status"]), ("ok", "blocked"))
        self.assertEqual(checks["worker-forges"]["roles"], ["workers"])
        self.assertEqual(checks["forgejo-token"]["status"], "blocked")
        token = self.root / "forgejo"
        token.write_text("forgejo-secret")
        token.chmod(0o644)
        store.update_host(forgejo_token_file=str(token))
        self.assertEqual(self.doctor_checks(dict(version=1, forges=1))["forgejo-token"]["status"], "blocked")
        token.chmod(0o600)
        checks = self.doctor_checks(dict(version=1, forges=1))
        self.assertEqual((checks["worker-forges"]["status"], checks["forgejo-token"]["status"]), ("ok", "ok"))
        self.assertIn("github, forgejo; new PRs: github", checks["worker-forges"]["evidence"])
        store.update_host(sandbox="bubble")
        self.assertEqual(self.doctor_checks(dict(version=1, forges=1))["worker-forges"]["status"], "warning")
        store.update_host(forges=None, new_pr_forge=None, sandbox=None)
        self.assertFalse({"worker-forges", "forgejo-token"} & set(self.doctor_checks({})))
        with patch.object(host, "worker_protocol") as probe:
            host.worker_config(store.host_config())
        probe.assert_not_called()

    def test_unsupported_user_services_still_allow_actionable_doctor_reporting(self):
        with patch.object(host, "install_service", side_effect=store.FleetError("user manager unavailable")):
            result = host.configure(dict(ci_allowed=False))
        self.assertIn("user manager", result["service_error"])
        self.assertFalse(result["workers_enabled"])
        self.assertFalse(result["ci_enabled"])

    def test_hard_memory_limit_blocks_admissions_using_working_set(self):
        caps = dict(memory_available=40 * 1024**3, memory_total=128 * 1024**3)
        memory = dict(working_set=96, high=80, maximum=100, stall_some=0, stall_full=0)
        with (
            patch.object(host, "worker_memory", return_value=memory),
            patch.object(host, "resources", return_value=caps),
        ):
            self.assertTrue(host.worker_pool_pressure())
            memory["working_set"] = 89
            self.assertFalse(host.worker_pool_pressure())

    def test_all_slots_available_even_when_ci_is_busy_and_cpu_full(self):
        scaler = host.Scaler()
        args = dict(enabled=True, available=20, reserve=10, idle=0, maximum=12)
        self.assertEqual(scaler.step(now=100, ci_busy=False, **args)[0], 12)
        self.assertEqual(scaler.step(now=101, ci_busy=True, **args)[0], 12)
        self.assertEqual(scaler.decision["policy"], "opportunistic")
        self.assertEqual(scaler.decision["cpu_idle_required"], 0)
        self.assertIsNone(scaler.decision["next_increase_at"])
        args["maximum"] = 6
        self.assertEqual(scaler.step(now=102, ci_busy=True, **args)[0], 6)

    def test_slices_share_ram_keep_os_reserve_and_prioritize_ci(self):
        gib = 1024**3
        with (
            patch.object(host, "units", return_value=self.root),
            patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")),
        ):
            worker = host.write_slices(
                dict(ci_allowed=True, ci_memory_gib=64),
                dict(memory_total=128 * gib, memory_available=128 * gib),
            )
        self.assertLessEqual(worker + gib + int(128 * gib * 0.1), 128 * gib)
        workers = (self.root / "tauceti-fleet-workers.slice").read_text()
        ci = (self.root / "tauceti-fleet-ci.slice").read_text()
        self.assertIn("CPUWeight=10\n", workers)
        self.assertIn("CPUWeight=1000\n", ci)
        self.assertIn(f"MemorySwapMax={gib}\n", workers)
        self.assertIn(f"MemoryMax={int(worker)}\n", workers)
        self.assertIn(f"MemoryHigh={int(worker * 0.95)}\n", workers)

    def test_low_memory_or_disable_blocks_new_work_and_recovers_immediately(self):
        scaler = host.Scaler()
        args = dict(enabled=True, available=20, reserve=10, idle=0, maximum=12, ci_busy=True)
        self.assertEqual(scaler.step(now=100, **args)[0], 12)
        self.assertEqual(scaler.step(now=101, **{**args, "available": 1})[0], 0)
        self.assertEqual(scaler.decision["state"], "paused")
        self.assertIn("host memory", scaler.decision["reason"])
        self.assertEqual(scaler.step(now=102, **args)[0], 12)
        reason = "worker working set near hard memory limit"
        self.assertEqual(scaler.step(now=103, **{**args, "enabled": False, "pause_reason": reason})[0], 0)
        self.assertEqual(scaler.decision["reason"], reason)
        self.assertEqual(scaler.step(now=104, **args)[0], 12)
        self.assertEqual(scaler.step(now=105, **{**args, "maximum": 0})[0], 0)
        self.assertIn("ceiling is zero", scaler.decision["reason"])
        scaler.step(now=106, **{**args, "maximum": 0, "pause_reason": "worker admission lease expired"})
        self.assertEqual(scaler.decision["reason"], "worker admission lease expired")

    def test_drain_changes_admission_not_desired_worker_configuration(self):
        store.host_config()
        store.update_host(ci_enabled=True, workers_enabled=True)
        host.drain()
        policy = store.read(store.state_dir() / "admission.json")
        self.assertEqual(policy["workers"], [])
        self.assertFalse(store.host_config()["ci_enabled"])
        self.assertFalse(store.host_config()["workers_enabled"])

    def test_no_ci_cannot_be_enabled_by_accident(self):
        store.update_host(ci_allowed=False)
        with self.assertRaisesRegex(store.FleetError, "--no-ci"):
            host.enable(ci=True)

    def test_update_refuses_active_round_and_stops_the_complete_worker_slice(self):
        store.host_config()
        with patch.object(host, "worker_statuses", return_value=[dict(alive=True, state="authoring")]):
            with self.assertRaisesRegex(store.FleetError, "draining"):
                host.rpc(dict(action="prepare_update"))
        with (
            patch.object(host, "worker_statuses", return_value=[dict(alive=True, state="waiting-capacity")]),
            patch.object(host, "run") as run,
        ):
            self.assertTrue(host.rpc(dict(action="prepare_update"))["quiescent"])
            self.assertEqual(run.call_args.args[0][-1], "tauceti-fleet-workers.slice")

    def test_worker_launcher_uses_only_the_selected_credential_without_saved_gh_selection(self):
        secret = self.root / "token"
        secret.write_text("fake-selected-token")
        store.update_host(github_token_file=str(secret), github_state_url="https://state.example",
                          github_state_token_file=str(self.root / "state-client-token"))
        with patch.object(host, "executable", return_value="/fake/tauceti"), patch.object(host.os, "execv") as launch:
            host.run_workers()
            self.assertEqual(os.environ["GH_TOKEN"], "fake-selected-token")
            self.assertEqual(Path(os.environ["GH_CONFIG_DIR"]), store.state_dir() / "gh")
            self.assertNotIn("fake-selected-token", str(launch.call_args))
            self.assertEqual(os.environ["TAUCETI_WORKER_SLICE"], "tauceti-fleet-workers.slice")
            self.assertEqual(os.environ["TAUCETI_WORKER_OOM_SCORE_ADJ"], "500")
            self.assertEqual(os.environ["TAUCETI_GITHUB_STATE_URL"], "https://state.example")
            self.assertEqual(os.environ["TAUCETI_GITHUB_STATE_TOKEN_FILE"], str(self.root / "state-client-token"))

    def test_status_with_unreachable_uncached_host_does_not_invent_totals(self):
        entry = dict(machine_id="a", target="unreachable", profile="test")
        output = io.StringIO()
        with (
            patch.object(remote, "inventory", return_value={"missing": entry}),
            patch.object(remote, "call", side_effect=store.FleetError("offline")),
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(["status", "--json"]), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["missing"]["error"], "offline")
        self.assertNotIn("activity", result["missing"])

    def test_status_keeps_a_small_cache_for_unreachable_hosts(self):
        entry = dict(machine_id="a", target="host", profile="test")
        value = dict(machine_id="a", status=dict(active_worker_limit=2), activity=dict(available=False))
        with (
            patch.object(remote, "inventory", return_value={"host": entry}),
            patch.object(remote, "call", return_value=value),
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(main(["status", "--json"]), 0)
        output = io.StringIO()
        with (
            patch.object(remote, "inventory", return_value={"host": entry}),
            patch.object(remote, "call", side_effect=store.FleetError("offline")),
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(["status", "--json"]), 1)
        cached = json.loads(output.getvalue())["host"]
        self.assertTrue(cached["offline"])
        self.assertEqual(cached["status"]["active_worker_limit"], 2)

    def test_status_aggregates_prepared_summaries_and_deduplicates_host_aliases(self):
        entries = {
            name: dict(machine_id=identity, target=name)
            for name, identity in (("a", "one"), ("alias-a", "one"), ("b", "two"))
        }

        def prepared(target, payload, **kwargs):
            self.assertEqual(payload, dict(action="status"))
            self.assertEqual(kwargs["timeout"], 3)
            return dict(
                machine_id=entries[target]["machine_id"],
                activity=dict(available=True, summary=dict(source_id=entries[target]["machine_id"])),
            )

        output = io.StringIO()
        with (
            patch.object(remote, "inventory", return_value=entries),
            patch.object(remote, "call", side_effect=prepared),
            patch.object(activity, "combine_status", return_value=dict(available=False)) as combine,
            patch("sys.stdout", output),
        ):
            self.assertEqual(main(["status", "--json"]), 0)
        summaries = combine.call_args.args[0]
        self.assertEqual([v["summary"]["source_id"] for v in summaries], ["one", "two"])
        self.assertIn("_fleet", json.loads(output.getvalue()))

    def test_ssh_target_cannot_inject_shell_options_or_commands(self):
        for target in ["-oProxyCommand=bad", "host;bad", "host$(bad)", "user@host/path", "host\ncommand"]:
            with self.assertRaises(store.FleetError):
                remote.validate_target(target)
        self.assertEqual(remote.validate_target("kim@ci-host"), "kim@ci-host")

    def test_atomic_worker_event_import_and_partial_tail(self):
        store.host_config()
        p = store.state_dir() / "worker-events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        event = dict(
            version=1, machine_id="a", id="1", kind="pr_created", repository="TauCetiProject/TauCeti", pr=1, at=1
        )
        p.write_text(json.dumps(event) + "\n" + '{"partial":')
        self.assertEqual(len(store.export_events()), 1)
        self.assertEqual(len(store.export_events()), 1)
        p.write_text(json.dumps(event) + "\n" + json.dumps(dict(event, id="2", pr=2)) + "\n")
        self.assertEqual(len(store.export_events()), 2)

    def test_budget_reading_restates_settlements_and_rejects_corruption(self):
        path = self.root / "budget.jsonl"
        records = [
            dict(version=1, seq=1, timestamp=1, events=[dict(type="admit", id="a", worker="w", model="m")]),
            dict(version=1, seq=2, timestamp=2, events=[dict(type="settle", id="a", cost="4")]),
            dict(version=1, seq=3, timestamp=3, events=[dict(type="correction", id="a", cost="3")]),
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        self.assertEqual(report.settled_costs(path)[0], dict(id="a", worker="w", model="m", at=2, usd="3"))
        with path.open("a") as out:
            out.write("partial")
        with self.assertRaises(store.FleetError):
            report.settled_costs(path)
        for amount in ("NaN", "Infinity", "-1", "broken"):
            records[-1]["events"][0]["cost"] = amount
            path.write_text("".join(json.dumps(r) + "\n" for r in records))
            with self.assertRaises(store.FleetError):
                report.settled_costs(path)

    def test_empty_or_incomplete_reports_do_not_invent_zero_spending(self):
        empty = report.summarize([], now=1000)["reports"][-1]
        self.assertIsNone(empty["windows"]["3h"]["settled_api_usd"])
        self.assertIsNone(empty["current"]["pending_estimates_usd"])
        snap = dict(
            machine_id="a",
            observed_at=1000,
            instrumentation_started_at=900,
            events=[dict(id="x", kind="pr_created", at=950, repository="a/b", pr=1)],
            prs=[],
            costs=[],
            coverage_errors=[],
            spending_complete=False,
        )
        value = report.summarize([snap], now=1000)["reports"][0]
        self.assertEqual(value["pr_metadata_unknown"], 1)
        self.assertEqual(value["windows"]["3h"]["prs_originated"], 1)
        self.assertFalse(value["windows"]["3h"]["attribution_covers_window"])
        self.assertIsNone(value["windows"]["3h"]["settled_api_usd"])

    def test_release_archive_contains_only_code_and_manifest(self):
        import zipfile

        with zipfile.ZipFile(io.BytesIO(remote.archive())) as zipped:
            self.assertIn("release.json", zipped.namelist())
            self.assertTrue(all(n.startswith("tauceti_fleet/") or n == "release.json" for n in zipped.namelist()))
            manifest = json.loads(zipped.read("release.json"))
            self.assertEqual(manifest, {"version": 1})

    def test_duplicate_hosts_cannot_double_count_reports(self):
        pr = dict(
            repository="TauCetiProject/TauCeti",
            pr=1,
            state="closed",
            landed_at=900,
            observed_at=1000,
            lean_loc=dict(added=10, deleted=4, net=6),
            head_sha="a" * 40,
        )
        authored = dict(id="created", at=100, kind="pr_created", repository=pr["repository"], pr=1)
        assisted = dict(id="assisted", at=800, kind="pr_activity", repository=pr["repository"], pr=1)
        a = dict(
            machine_id="a",
            observed_at=1000,
            events=[authored],
            prs=[pr],
            costs=[dict(id="x", at=900, usd="2")],
            coverage_errors=[],
        )
        b = dict(machine_id="b", observed_at=1000, events=[assisted], prs=[pr], costs=[], coverage_errors=[])
        result = report.summarize([a, a, b], now=1000, windows={"3h": 10800})
        fleet = result["reports"][-1]["windows"]["3h"]
        self.assertEqual(fleet["prs_landed"], 1)
        self.assertEqual(fleet["prs_assisted"], 1)
        self.assertEqual(fleet["settled_api_usd"], "2")
        self.assertEqual(fleet["lean_loc"]["net"], 6)

    def test_closed_pr_without_landing_is_not_credited(self):
        snap = dict(
            machine_id="a",
            observed_at=1000,
            events=[dict(id="x", kind="pr_created", at=1, repository="a/b", pr=1)],
            prs=[dict(repository="a/b", pr=1, state="closed", landed_at=None)],
            costs=[],
            coverage_errors=[],
        )
        self.assertEqual(report.summarize([snap], now=1000)["reports"][-1]["windows"]["3h"]["prs_landed"], 0)

    def test_bors_requires_landed_head_and_matching_member(self):
        dbpath = self.root / "ci.sqlite"
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute("CREATE TABLE merge_builds(repo,head_sha,members_json,landed)")
            db.execute("CREATE TABLE main_landings(repo,head_sha,landed_at)")
            db.execute(
                "INSERT INTO merge_builds VALUES (?,?,?,1)",
                ("TauCeti", "a" * 40, json.dumps([dict(pr=42, head_sha="b" * 40)])),
            )
            self.assertEqual(report.bors_landings(dbpath), {})
            db.execute("INSERT INTO main_landings VALUES (?,?,?)", ("TauCeti", "a" * 40, "2026-10-15T00:00:00Z"))
        known = report.bors_landings(dbpath)
        self.assertIn(("TauCetiProject/TauCeti", 42, "b" * 40), known)

    def test_base_refresh_reports_busy_image_owner_without_waiting(self):
        with (
            store.locked(store.data_dir() / "images/prepare.lock"),
            patch.object(vm, "prepare_tools"),
            self.assertRaisesRegex(store.FleetError, "already in progress"),
        ):
            vm.prepare_image(store.host_config())

    def test_download_does_not_replace_good_file_with_wrong_image(self):
        target = self.root / "disk"
        target.write_bytes(b"original")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaisesRegex(store.FleetError, "checksum"):
                vm.download("https://example.invalid/image", target, "a" * 64)
        self.assertEqual(target.read_bytes(), b"original")

    def test_cloud_seed_is_data_not_shell_or_yaml_interpolation(self):
        directory = self.root / "seed"
        with patch.object(vm, "run", side_effect=lambda args, **_: Path(args[1]).touch()):
            vm.cloud_seed(directory, ["echo 'quoted'"], name="test")
        user = json.loads((directory / "user-data").read_text().split("\n", 1)[1])
        self.assertEqual(user["runcmd"], [["bash", "-euc", "echo 'quoted'"]])
        self.assertEqual((directory / "seed.iso").stat().st_mode & 0o077, 0)

    def test_controller_restart_adopts_existing_guest_without_second_registration(self):
        store.atomic(store.data_dir() / "images/prepared.json", dict(sha256="a" * 64))
        value = dict(unit="existing.service", instance="existing")
        store.atomic(store.state_dir() / "vm.json", value)
        with (
            patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)),
            patch.object(vm.github, "jit_config") as jit,
        ):
            self.assertEqual(vm.start(store.host_config()), value)
            jit.assert_not_called()

    def test_generated_hooks_run_with_actions_shell_and_emit_json_lines(self):
        store.atomic(store.data_dir() / "images/prepared.json", dict(sha256="a" * 64))
        with (
            patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)),
            patch.object(vm, "executable", side_effect=lambda name: name),
            patch.object(vm.github, "jit_config", return_value=(12, "test-jit")),
            patch.object(vm, "cloud_seed", return_value=self.root / "seed.iso") as seed,
        ):
            vm.start(store.host_config())
        commands = seed.call_args.args[1]
        hooks = [c for c in commands if c.startswith("printf %s ") and "/opt/hook-" in c]
        env_command = next(c for c in commands if "ACTIONS_RUNNER_HOOK_JOB_STARTED=" in c)
        for command, kind in zip(hooks, ("ci_started", "ci_completed"), strict=True):
            words = shlex.split(command)
            script, path = words[2], words[4].removesuffix(";")
            self.assertTrue(path.endswith(".sh"), path)
            self.assertIn(path, env_command)
            output = self.root / (kind + ".jsonl")
            hook = self.root / Path(path).name
            hook.write_text(
                script.replace("/dev/virtio-ports/org.tauceti.events", str(output)).replace(
                    "/usr/bin/python3", shlex.quote(sys.executable)
                )
            )
            subprocess.run(["bash", "-e", str(hook)], env={**os.environ, "GITHUB_RUN_ID": "42"}, check=True)
            raw = output.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual(len(raw.splitlines()), 1)
            notice = json.loads(raw)
            self.assertEqual(notice["kind"], kind)
            self.assertEqual(notice["run_id"], "42")

    def test_failed_ci_job_sets_persistent_cooldown(self):
        store.atomic(store.state_dir() / "pending-ci.json", [dict(run_id=42, runner_id=12)])
        job = dict(
            id=7,
            runner_id=12,
            status="completed",
            conclusion="failure",
            started_at="2026-10-15T00:00:00Z",
            completed_at="2026-10-15T00:01:00Z",
            steps=[dict(name="Resolve PR head", conclusion="failure")],
        )
        with (
            patch.object(host.github, "request", return_value=dict(jobs=[job])),
            patch.object(host.time, "time", return_value=1000),
        ):
            host.collect_ci_results(store.host_config())
        self.assertEqual(
            store.read(store.state_dir() / "ci-slot-backoff.json")["until"], report.timestamp(job["completed_at"]) + 900
        )
        self.assertEqual(store.read(store.state_dir() / "pending-ci.json"), [])

    def test_candidate_failure_does_not_withdraw_healthy_ci_capacity(self):
        self.assertFalse(
            host.ci_infrastructure_failure(
                dict(
                    conclusion="failure",
                    steps=[
                        dict(
                            name="Build exact candidate under bwrap with workflow-pinned audits, offline",
                            conclusion="failure",
                        )
                    ],
                )
            )
        )

    def test_cloud_init_prerequisite_failure_prevents_success_marker(self):
        directory = self.root / "seed"
        marker = self.root / "should-not-exist"
        with patch.object(vm, "run", side_effect=lambda args, **_: Path(args[1]).touch()):
            vm.cloud_seed(directory, ["false", "touch " + str(marker)], name="test")
        user = json.loads((directory / "user-data").read_text().split("\n", 1)[1])
        for command in user["runcmd"]:
            subprocess.run(command, check=False)
        self.assertFalse(marker.exists())

    def test_resource_probe_verifies_kernel_not_just_systemd_exit(self):
        fake = subprocess.CompletedProcess([], 0, stdout='{"cpu.weight":"100"}', stderr="")
        with patch.object(capabilities, "run", return_value=fake):
            with self.assertRaisesRegex(store.FleetError, "not enforced"):
                capabilities.resource_probe()


if __name__ == "__main__":
    unittest.main()
