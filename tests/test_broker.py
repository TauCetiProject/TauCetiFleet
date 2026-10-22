import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_fleet import broker, github, host, remote, store, vm


class Enrollment(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        env = patch.dict(os.environ, {
            "TAUCETI_FLEET_CONFIG": str(self.root / "config"),
            "TAUCETI_FLEET_STATE": str(self.root / "state"),
            "TAUCETI_FLEET_DATA": str(self.root / "data"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_enrollment_retries_reuse_the_saved_host_credential_without_github_or_agent_auth(self):
        config = store.host_config()
        observed = []

        def redeem(value, path, **kwargs):
            self.assertEqual(path, "/v1/enroll")
            self.assertTrue((store.config_dir() / "enrollment.json").exists())
            observed.append(kwargs["data"]["host_token"])
            if len(observed) == 1:
                raise store.FleetError("response lost")
            return dict(machine_id=config["machine_id"], runner_group_id=3)

        with patch.object(broker, "request", side_effect=redeem), patch.object(broker.getpass, "getpass", return_value="one-time"), patch.object(remote, "add", return_value={}) as add:
            with self.assertRaisesRegex(store.FleetError, "response lost"):
                broker.enroll()
            broker.enroll()
            self.assertEqual(observed[0], observed[1])
            add.assert_called_once_with("local", profile_name="ci-enrolled")
        with patch.object(remote, "run", side_effect=AssertionError("No gh login should be needed")):
            profile = remote.profile("ci-enrolled")
        self.assertEqual(set(profile["credentials"]), {"ci_broker_token_file"})
        self.assertNotIn("github_account", profile)
        path = Path(store.read(store.config_dir() / "profiles.json")["ci-enrolled"]["ci_broker_token_file"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.read_text().strip(), observed[0])

    def test_active_hosts_cannot_enroll(self):
        store.update_host(ci_enabled=True)
        with patch.object(broker.getpass, "getpass", side_effect=AssertionError("must reject first")):
            with self.assertRaisesRegex(store.FleetError, "Drain"):
                broker.enroll()

    def test_draining_guests_and_pending_registrations_must_retire_before_enrollment(self):
        for filename in ("vm.json", "ci-registration.json"):
            with self.subTest(filename=filename):
                path = store.state_dir() / filename
                store.atomic(path, {"instance": "a" * 16})
                with patch.object(broker.getpass, "getpass", side_effect=AssertionError("must reject before redemption")):
                    with self.assertRaisesRegex(store.FleetError, "retire"):
                        broker.enroll()
                path.unlink()

    def test_broker_urls_and_redirects_cannot_leak_the_host_credential(self):
        for url in ("http://fleet.example", "https://user:password@fleet.example", "https://fleet.example/path", "https://fleet.example?token=value"):
            with self.assertRaises(store.FleetError):
                broker.validate_url(url)
        with self.assertRaisesRegex(store.FleetError, "redirected"):
            broker.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.example")

    def test_transport_sends_only_host_credential_and_identity_and_rejects_public_token_files(self):
        credential = self.root / "host-token"
        credential.write_text("host-secret")
        credential.chmod(0o600)
        config = dict(ci_broker_url=broker.DEFAULT_URL, ci_broker_token_file=str(credential), machine_id="a" * 32)
        opener = Mock()
        response = io.BytesIO(json.dumps({"runner_group_id": 3}).encode())
        opener.open.return_value = response
        with patch.object(broker.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(broker.request(config, "/v1/policy"), {"runner_group_id": 3})
        req = opener.open.call_args.args[0]
        self.assertEqual(req.full_url, broker.DEFAULT_URL + "/v1/policy")
        self.assertEqual(req.headers["Authorization"], "Bearer host-secret")
        self.assertEqual(req.headers["X-fleet-machine"], "a" * 32)
        credential.chmod(0o644)
        with self.assertRaisesRegex(store.FleetError, "private"):
            broker.request(config, "/v1/policy")

    def test_only_dns_failures_before_transmission_are_retried(self):
        opener = Mock()
        opener.open.side_effect = [broker.urllib.error.URLError(socket.gaierror("DNS failure")), "response"]
        with patch.object(broker.urllib.request, "build_opener", return_value=opener), patch.object(broker.time, "sleep"):
            self.assertEqual(broker.open_request(Mock()), "response")
        self.assertEqual(opener.open.call_count, 2)
        opener.open.reset_mock()
        opener.open.side_effect = broker.urllib.error.URLError(TimeoutError())
        with patch.object(broker.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(broker.urllib.error.URLError):
                broker.open_request(Mock())
        self.assertEqual(opener.open.call_count, 1)

    def test_all_ci_github_operations_use_the_broker(self):
        config = dict(ci_broker_url=broker.DEFAULT_URL, runner_group_id=3)
        with patch.object(github, "request", side_effect=AssertionError("No GitHub credential belongs on this host")), patch.object(broker, "request") as call:
            call.return_value = {"runner_group_id": 3}
            self.assertEqual(github.runner_policy(config), 3)
            call.return_value = {"runner_id": 7, "encoded_jit_config": "jit"}
            self.assertEqual(github.jit_config(config, "name"), (7, "jit"))
            call.return_value = {"runners": [{"id": 7}]}
            self.assertEqual(github.runners(config), [{"id": 7}])
            github.retire_runner(config, 7)
            github.ci_read(config, "repos/TauCetiProject/TauCeti/actions/runs/9/jobs?filter=all&per_page=100")
            self.assertEqual(call.call_args.args, (config, "/v1/github/actions/runs/9/jobs"))
            with self.assertRaises(store.FleetError):
                github.ci_read(config, "repos/TauCetiProject/Other/actions/runs/9/jobs")

    def test_configuring_enrollment_preserves_inactive_roles(self):
        with patch.object(host, "install_service"):
            result = host.configure(dict(profile="ci-enrolled", ci_broker_url=broker.DEFAULT_URL,
                                         credentials={"ci_broker_token_file": "host-secret"}))
        self.assertFalse(result["workers_enabled"])
        self.assertFalse(result["ci_enabled"])
        self.assertEqual(store.host_config()["ci_broker_url"], broker.DEFAULT_URL)

    def test_interrupted_registration_keeps_its_identity_until_vm_launch(self):
        config = dict(store.host_config(), ci_broker_url=broker.DEFAULT_URL)
        store.atomic(store.data_dir() / "images/prepared.json", {"sha256": "a" * 64})
        names = []

        def register(config, name):
            names.append(name)
            if len(names) == 1:
                raise store.FleetError("response lost after remote registration")
            return 7, "single-job-config"

        with patch.object(github, "jit_config", side_effect=register), patch.object(vm, "run", return_value=subprocess.CompletedProcess([], 0)), patch.object(vm, "executable", side_effect=lambda name: name), patch.object(vm, "cloud_seed", return_value=self.root / "seed.iso"):
            with self.assertRaisesRegex(store.FleetError, "response lost"):
                vm.start(config)
            self.assertTrue((store.state_dir() / "ci-registration.json").exists())
            value = vm.start(config)
        self.assertEqual(names[0], names[1])
        self.assertEqual(value["runner_name"], names[0])
        self.assertFalse((store.state_dir() / "ci-registration.json").exists())

    def test_failed_broker_retirement_preserves_local_ownership_for_retry(self):
        config = dict(store.host_config(), ci_broker_url=broker.DEFAULT_URL)
        value = dict(instance="a" * 16, unit="guest.service", runner_id=7,
                     directory=str(store.state_dir() / "vms" / ("a" * 16)))
        store.atomic(store.state_dir() / "vm.json", value)
        with patch.object(vm, "run"), patch.object(github, "retire_runner", side_effect=store.FleetError("response lost")):
            with self.assertRaisesRegex(store.FleetError, "response lost"):
                vm.retire(config, value)
        self.assertEqual(store.read(store.state_dir() / "vm.json"), value)

    def test_repeated_retirement_failures_queue_the_result_only_once(self):
        config = dict(store.host_config(), ci_broker_url=broker.DEFAULT_URL)
        value = dict(instance="a" * 16, run_id=42, runner_id=7, unit="guest.service",
                     directory=str(store.state_dir() / "vms" / ("a" * 16)))
        store.atomic(store.state_dir() / "vm.json", value)
        for _ in range(2):
            value = store.read(store.state_dir() / "vm.json")
            host.queue_ci_result(value, "healthy")
            with patch.object(vm, "run"), patch.object(github, "retire_runner", side_effect=store.FleetError("broker unavailable")):
                with self.assertRaises(store.FleetError):
                    vm.retire(config, value)
        self.assertEqual(len(store.read(store.state_dir() / "pending-ci.json")), 1)
        # Successful result collection must not cause requeueing while retirement
        # is still unavailable or a revoked credential keeps getting rejected.
        store.atomic(store.state_dir() / "pending-ci.json", [])
        self.assertFalse(host.queue_ci_result(store.read(store.state_dir() / "vm.json"), "healthy"))
        self.assertEqual(store.read(store.state_dir() / "pending-ci.json"), [])

    def test_interrupted_result_queueing_deduplicates_before_setting_the_vm_marker(self):
        value = dict(instance="b" * 16, run_id=43, runner_id=8)
        store.atomic(store.state_dir() / "pending-ci.json", [value])
        self.assertTrue(host.queue_ci_result(value, "healthy"))
        self.assertEqual(len(store.read(store.state_dir() / "pending-ci.json")), 1)
        self.assertTrue(store.read(store.state_dir() / "vm.json")["result_queued"])
