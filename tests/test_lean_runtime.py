import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import host, lean_pressure, lean_runtime
from tauceti_fleet.capabilities import GIB


class LeanRuntime(unittest.TestCase):
    def test_ci_wraps_watchdog_without_invalidating_official_toolchain_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            toolchain = root / "elan/toolchains/snapshot"
            (toolchain / "bin").mkdir(parents=True)
            compiler = toolchain / "bin/lean"
            compiler.write_text("#!/bin/sh\necho compiler\n")
            compiler.chmod(0o755)
            snapshot = compiler.read_bytes()
            watchdog = root / "watchdog"
            (watchdog / "bin").mkdir(parents=True)
            wrapped = watchdog / "bin/lean-real"
            wrapped.symlink_to(compiler)
            helper = root / "tauceti-lean"
            helper.write_bytes(Path(lean_pressure.__file__).read_bytes())
            helper.chmod(0o755)
            startup = root / "ci-startup"
            startup.write_text(lean_runtime.STARTUP)
            env = {
                **os.environ,
                "BASH_ENV": str(startup),
                "WATCHDOG_TOOLCHAIN": str(watchdog),
                "TAUCETI_LEAN_WRAPPER": str(helper),
                "SHLVL": "1",
            }
            # A pre-existing startup file still runs in each nested shell.
            previous = root / "previous-startup"
            previous_calls = root / "previous-calls"
            previous.write_text("printf x >> " + shlex.quote(str(previous_calls)) + "\n")
            env["TAUCETI_PREVIOUS_BASH_ENV"] = str(previous)
            result = subprocess.run(
                ["bash", "-ec", "bash -ec true"],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(previous_calls.read_text(), "xx")
            self.assertTrue((watchdog / "bin/lean-unthrottled").exists())
            self.assertEqual(compiler.read_bytes(), snapshot)
            self.assertEqual(sorted(p.name for p in toolchain.rglob("*")), ["bin", "lean"])
            result = subprocess.run(
                [str(wrapped), "--print-prefix"], env={"PATH": os.environ["PATH"]}, text=True, capture_output=True
            )
            self.assertEqual(result.stdout.strip(), str(watchdog))
            result = subprocess.run([str(wrapped), "--githash"], capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "compiler")

    def test_ci_startup_does_not_recurse_through_a_bash_python_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            startup = root / "startup"
            startup.write_text(lean_runtime.STARTUP)
            calls = root / "calls"
            helper = root / "wrapped-python"
            helper.write_text("#!/usr/bin/env bash\nprintf x >> " + shlex.quote(str(calls)) + "\n")
            helper.chmod(0o755)
            (root / "bin").mkdir()
            (root / "bin/lean-real").write_text("compiler")
            (root / "bin/lean-real").chmod(0o755)
            result = subprocess.run(
                ["bash", "-ec", "true"],
                env={
                    **os.environ,
                    "BASH_ENV": str(startup),
                    "TAUCETI_LEAN_WRAPPER": str(helper),
                    "WATCHDOG_TOOLCHAIN": str(root),
                    "SHLVL": "1",
                },
                text=True,
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(calls.read_text(), "x")

    def test_ci_bootstrap_installs_hook_and_dotenv_without_new_base_image(self):
        commands = lean_runtime.ci_commands()
        text = "\n".join(commands)
        self.assertIn("LAKE_OVERRIDE_LEAN=true", text)
        self.assertIn("BASH_ENV=/opt/tauceti-lean-env", text)
        self.assertIn("--install-toolchain", text)
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

    def test_worker_service_prefers_workers_over_ci_for_kernel_oom(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(host, "units", return_value=Path(directory)),
            patch.object(host, "run"),
        ):
            host.start_workers()
            text = (Path(directory) / "tauceti-fleet-workers.service").read_text()
            self.assertIn("OOMScoreAdjust=500", text)


if __name__ == "__main__":
    unittest.main()
