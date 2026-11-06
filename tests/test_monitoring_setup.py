"""Operator configuration never requires a new volunteer credential or command."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

path = Path(__file__).parents[1] / "services/ci-broker/scripts/setup.py"
spec = importlib.util.spec_from_file_location("monitoring_setup", path)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class OperatorSetup(unittest.TestCase):
    def test_activation_uses_only_approved_public_fields_and_existing_host_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hosts = [{"machine_id": "a" * 32, "alias": "yael"}]
            (root / "operations-hosts.json").write_text(json.dumps(hosts))
            (root / "operations-ingest-token").write_text("t" * 48)
            with patch.object(setup, "DIRECTORY", root), patch.object(setup, "command") as command:
                setup.activate_monitoring()
                self.assertEqual(command.call_args_list[0].kwargs["input"], "t" * 48)
                self.assertIn("OPERATIONS_PUBLIC_HOSTS:", command.call_args_list[1].args[-1])
                hosts[0]["target"] = "PRIVATE_SSH_TARGET"
                (root / "operations-hosts.json").write_text(json.dumps(hosts))
                command.reset_mock()
                with self.assertRaises(SystemExit):
                    setup.activate_monitoring()
                command.assert_not_called()
