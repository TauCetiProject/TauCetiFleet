import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import host, lean_runtime
from tauceti_fleet.capabilities import GIB


class LeanRuntime(unittest.TestCase):
    def test_worker_environment_installs_standalone_gate_and_preserves_startup(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(lean_runtime, "data_dir", return_value=Path(directory)),
            patch.dict(os.environ, BASH_ENV="/custom/startup"),
        ):
            env = lean_runtime.environment()
            self.assertEqual(env["LAKE_OVERRIDE_LEAN"], "true")
            self.assertTrue(os.access(env["LEAN"], os.X_OK))
            self.assertEqual(env["TAUCETI_PREVIOUS_BASH_ENV"], "/custom/startup")
            self.assertIn("--install-available", Path(env["BASH_ENV"]).read_text())

    def test_bash_step_wraps_toolchain_before_a_clean_environment_sandbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            toolchain = root / "elan/toolchains/snapshot"
            (toolchain / "bin").mkdir(parents=True)
            compiler = toolchain / "bin/lean"
            compiler.write_text("#!/bin/sh\necho compiler\n")
            compiler.chmod(0o755)
            tools = root / "tools"
            tools.mkdir()
            elan = tools / "elan"
            # CI's workspace root often has no selected/default toolchain.
            elan.write_text("#!/bin/sh\nexit 1\n")
            elan.chmod(0o755)
            with patch.object(lean_runtime, "data_dir", return_value=root):
                env = {
                    **os.environ,
                    **lean_runtime.environment(),
                    "PATH": str(tools) + ":" + os.environ["PATH"],
                    "ELAN_HOME": str(root / "elan"),
                    # SSH's top-level noninteractive Bash reads .bashrc instead
                    # of BASH_ENV. This fixture exercises a normal nested step.
                    "SHLVL": "1",
                }
            # A worker can inherit the fleet manager's identical startup hook.
            # It must source that predecessor once, without sourcing itself again.
            previous = root / "previous-startup"
            previous.write_text(lean_runtime.STARTUP)
            env["TAUCETI_PREVIOUS_BASH_ENV"] = str(previous)
            result = subprocess.run(
                ["bash", "-c", "test -f " + shlex.quote(str(toolchain / ".tauceti-lean.json"))],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((toolchain / "bin/lean-unthrottled").exists())
            result = subprocess.run(
                [str(compiler), "--print-prefix"], env={"PATH": os.environ["PATH"]}, text=True, capture_output=True
            )
            self.assertEqual(result.stdout.strip(), str(toolchain))

    def test_ci_bootstrap_installs_hook_and_dotenv_without_new_base_image(self):
        commands = lean_runtime.ci_commands()
        text = "\n".join(commands)
        self.assertIn("LAKE_OVERRIDE_LEAN=true", text)
        self.assertIn("BASH_ENV=/opt/tauceti-lean-env", text)
        self.assertIn("--install-available", text)
        self.assertIn("/opt/actions-runner/.env", text)
        subprocess.run(["bash", "-n"], input=text, text=True, check=True)
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / "runner.env"
            command = commands[-1].split("; chown", 1)[0].replace("/opt/actions-runner/.env", shlex.quote(str(dotenv)))
            subprocess.run(["bash", "-c", command], check=True)
            values = dict(line.split("=", 1) for line in dotenv.read_text().splitlines())
            self.assertEqual(values["LAKE_OVERRIDE_LEAN"], "true")
            self.assertEqual(values["LEAN"], "/usr/local/bin/tauceti-lean")
            self.assertEqual(values["BASH_ENV"], "/opt/tauceti-lean-env")

    def test_workers_and_ci_share_parent_ceiling_and_keep_cpu_priorities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            caps = dict(memory_total=128 * GIB, memory_available=100 * GIB)
            with (
                patch.object(host, "units", return_value=root),
                patch.object(host, "worker_statuses", return_value=[]),
                patch.object(host, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")),
            ):
                maximum = host.write_slices(dict(ci_allowed=True, ci_memory_gib=64), caps)
            self.assertGreater(maximum, 100 * GIB)
            for name in ("tauceti-fleet", "tauceti-fleet-ci", "tauceti-fleet-workers"):
                self.assertIn(f"MemoryMax={int(maximum)}\n", (root / (name + ".slice")).read_text())
            self.assertLess(maximum + GIB, caps["memory_total"])
            self.assertIn("CPUWeight=1000", (root / "tauceti-fleet-ci.slice").read_text())
            self.assertIn("CPUWeight=10", (root / "tauceti-fleet-workers.slice").read_text())


if __name__ == "__main__":
    unittest.main()
