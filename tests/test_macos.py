import ast
import inspect
import io
import os
import plistlib
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capabilities, host, hostos, macos, pool_activity, remote, store, update
from tauceti_fleet.cli import main

GIB = 1024**3
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              100000.
Pages active:                           3295135.
Pages inactive:                          200000.
Pages speculative:                        50000.
Pages wired down:                        546194.
Pages purgeable:                         266296.
"""


class MacOS(unittest.TestCase):
    """A Mac host against a recorded launchd; no command leaves the test on any machine."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.domain = f"gui/{os.getuid()}"
        self.calls = []
        self.loaded = {}
        self.refusals = 0
        for context in (
            patch.dict(os.environ, dict(HOME=str(self.home), **{
                "TAUCETI_FLEET_" + name.upper(): str(self.home / name) for name in ("config", "state", "data")
            })),
            patch.object(hostos, "MACOS", True),
            patch.object(macos, "run", self.command),
            *(patch.object(shared, "run", side_effect=store.FleetError("not on this host"))
              for shared in (capabilities, host, update)),
            patch.object(macos, "memory_total", return_value=32 * GIB),
            patch.object(pool_activity, "refresh", return_value={}),
        ):
            context.start()
            self.addCleanup(context.stop)

    def command(self, args, *, check=True, **_):
        args = [str(a) for a in args]
        self.calls.append(args)
        code, output = 0, ""
        if args[0] == "vm_stat":
            output = VM_STAT
        elif args[0] == "defaults":
            code = 1
        elif args[0] != "launchctl":
            raise AssertionError(f"unexpected command {args}")
        elif args[1] == "print":
            label = args[2].removeprefix(self.domain).lstrip("/")
            code = 0 if not label or label in self.loaded else 113
            output = f"\tstate = running\n\tpid = {self.loaded[label]}\n" if self.loaded.get(label) else ""
        elif args[1] == "bootstrap":
            label = plistlib.loads(Path(args[3]).read_bytes())["Label"]
            if self.refusals or label in self.loaded:
                self.refusals = max(0, self.refusals - 1)
                code = 5
            else:
                self.loaded[label] = 4242
        elif args[1] == "bootout":
            self.loaded.pop(args[2].split("/")[-1], None)
        if check and code:
            raise store.FleetError(f"{args[0]} exited {code}")
        return subprocess.CompletedProcess(args, code, stdout=output, stderr="")

    def launchctl(self, verb):
        return [c[2:] for c in self.calls if c[:2] == ["launchctl", verb]]

    def test_every_marked_function_has_a_namesake_with_the_same_parameters(self):
        package = Path(hostos.__file__).parent
        marked = []
        for path in sorted(package.glob("*.py")):
            for node in ast.parse(path.read_text()).body:
                if any(ast.unparse(d) == "hostos.linux" for d in getattr(node, "decorator_list", [])):
                    marked.append((path.stem, node))
        self.assertGreaterEqual(len(marked), 15)
        for module, node in marked:
            namesake = getattr(macos, node.name, None)
            self.assertTrue(callable(namesake), f"macos.{node.name} is missing")
            if isinstance(node, ast.FunctionDef):
                self.assertEqual([a.arg for a in node.args.args + node.args.kwonlyargs],
                                 list(inspect.signature(namesake).parameters), f"{module}.{node.name}")

    def test_the_mark_keeps_the_systemd_body_on_linux(self):
        systemd = subprocess.CompletedProcess([], 0, stdout="MainPID=7\nRestart=on-failure\n")
        with patch.object(hostos, "MACOS", False), patch.object(update, "run", return_value=systemd) as run:
            self.assertEqual(update.service(), dict(MainPID="7", Restart="on-failure"))
        self.assertEqual(run.call_args.args[0][:2], ["systemctl", "--user"])
        self.assertEqual(self.calls, [])
        self.loaded[macos.CONTROLLER] = 99
        self.assertEqual(update.service(), dict(MainPID="99", Restart="on-failure"))
        del self.loaded[macos.CONTROLLER]
        self.assertEqual(update.service()["MainPID"], "0")

    def test_install_loads_two_agents_and_restarts_only_the_controller(self):
        host.install_service()
        agents = self.home / "Library/LaunchAgents"
        controller = plistlib.loads((agents / "org.tauceti.fleet.plist").read_bytes())
        observer = plistlib.loads((agents / "org.tauceti.fleet.observe.plist").read_bytes())
        self.assertEqual(controller["ProgramArguments"], [str(self.home / ".local/bin/tauceti-fleet"), "host-daemon"])
        self.assertEqual(controller["KeepAlive"], dict(SuccessfulExit=False))
        self.assertEqual(controller["EnvironmentVariables"]["TAUCETI_FLEET_STATE"], str(self.home / "state"))
        self.assertTrue(controller["StandardErrorPath"].startswith(str(self.home / "state/logs/")))
        self.assertEqual((observer["ProgramArguments"][1], observer["StartInterval"]), ("collect-status", 300))
        self.assertEqual(set(self.loaded), {macos.CONTROLLER, macos.OBSERVER})
        self.assertFalse((self.home / ".config/systemd").exists())

        self.calls.clear()
        host.install_service(restart=False)
        self.assertEqual(self.launchctl("bootout"), [])
        self.assertEqual(len(self.launchctl("kickstart")), 2)

        self.calls.clear()
        host.install_service()
        self.assertEqual(self.launchctl("bootout"), [[f"{self.domain}/org.tauceti.fleet"]])
        self.assertEqual(self.launchctl("kickstart"), [[f"{self.domain}/org.tauceti.fleet.observe"]])

    def test_bootstrap_waits_while_launchd_still_holds_the_label(self):
        host.install_service()
        self.refusals = 2
        with patch.object(macos.time, "sleep") as sleep:
            host.install_service()
        self.assertEqual(sleep.call_count, 2)
        self.assertIn(macos.CONTROLLER, self.loaded)
        self.refusals = 10**6
        del self.loaded[macos.CONTROLLER]
        with patch.object(macos.time, "sleep"), patch.object(macos.time, "monotonic", side_effect=[0, 5, 11]):
            with self.assertRaisesRegex(store.FleetError, "did not load org.tauceti.fleet"):
                macos.start(macos.CONTROLLER)

    def test_worker_manager_starts_only_through_the_controller_and_is_never_restarted(self):
        host.start_workers()
        path = self.home / "config/launchd/org.tauceti.fleet.workers.plist"
        loaded = path.read_bytes()
        manager = plistlib.loads(loaded)
        self.assertEqual(manager["ProgramArguments"], [str(self.home / ".local/bin/tauceti-fleet"), "run-workers"])
        self.assertNotIn("PYTHONPATH", manager["EnvironmentVariables"])
        self.assertEqual(manager["EnvironmentVariables"]["TAUCETI_FLEET_CONFIG"], str(self.home / "config"))
        self.assertFalse((self.home / "Library/LaunchAgents/org.tauceti.fleet.workers.plist").exists())
        self.assertEqual(self.launchctl("bootstrap"), [[self.domain, str(path)]])
        # A controller of another release finds the manager loaded and only
        # kickstarts it, so launchd's retained definition must still be right.
        with patch.object(host, "RUNNING_RELEASE", str(self.home / "data/releases/new")), \
                patch.object(host.sys, "executable", str(self.home / "data/releases/new/python")):
            host.start_workers()
        self.assertEqual(path.read_bytes(), loaded)
        self.assertEqual(self.launchctl("bootout"), [])
        self.assertEqual(self.launchctl("kickstart"), [[f"{self.domain}/org.tauceti.fleet.workers"]])
        with self.assertRaisesRegex(store.FleetError, "no worker slice"):
            host.stop_workers()

    def test_workers_and_funding_share_one_budget_ledger(self):
        for name in ("TAUCETI_BUDGET_DIR", "XDG_STATE_HOME"):
            os.environ.pop(name, None)
        ledger = str(self.home / ".local/state/tauceti/budget")
        funded = subprocess.CompletedProcess([], 0, stdout="{}")
        with patch.object(host, "executable", return_value="/fake/tauceti"), \
                patch.object(host, "run", return_value=funded) as run:
            host.budget(dict(grant=100))
        self.assertEqual(run.call_args.args[0][-2:], ["--grant", "100"])
        self.assertEqual(run.call_args.kwargs["env"]["TAUCETI_BUDGET_DIR"], ledger)
        self.assertNotIn("TAUCETI_BUDGET_DIR", os.environ)
        secret = self.home / "token"
        secret.write_text("fake-token")
        store.update_host(github_token_file=str(secret))
        with patch.object(host, "executable", return_value="/fake/tauceti"), patch.object(host.os, "execv"):
            host.run_workers()
        self.assertEqual(os.environ["TAUCETI_BUDGET_DIR"], ledger)

    def test_resources_count_free_speculative_and_inactive_pages(self):
        caps = capabilities.resources()
        self.assertEqual(caps["memory_total"], 32 * GIB)
        self.assertEqual(caps["memory_available"], 350000 * 16384)
        self.assertGreater(caps["cpus"], 0)
        self.assertIsNone(capabilities.worker_memory())
        with patch.object(macos, "run", side_effect=store.FleetError("vm_stat is missing")):
            self.assertEqual(capabilities.resources()["memory_total"], 0)

    def test_cpu_sample_survives_a_wrapped_mach_counter(self):
        samples = iter([(10, 20, 2**32 - 5, 0), (30, 40, 15, 0)])

        class Mach:
            @staticmethod
            def host_statistics(port, flavor, raw, count):
                raw[:] = next(samples)
                return 0

        with patch.object(macos, "mach_host", return_value=(Mach, 0)), \
                patch.dict(macos.CPU, dict(raw=None, total=0, idle=0)):
            first = host.cpu_sample()
            second = host.cpu_sample()
        self.assertEqual((second[0] - first[0], second[1] - first[1]), (60, 20))

    def test_admission_counts_workers_against_free_memory(self):
        reserve = 4 * GIB
        ids = [f"w{n}" for n in range(1, 9)]
        workers = []
        scaler = host.Scaler()
        self.assertIsInstance(scaler, macos.Scaler)

        def step(available_gib, maximum=8, **extra):
            with patch.object(host, "worker_statuses", return_value=workers):
                return scaler.step(now=0, ci_busy=False, idle=0, enabled=True, available=available_gib * GIB,
                                   reserve=reserve, maximum=maximum, workers=ids, **extra)

        self.assertEqual(step(13), (2, "2 of 8 workers fit in free memory at 4 GiB each"))
        self.assertEqual(scaler.decision["policy"], "memory-count")
        workers[:] = [dict(id="w1", state="running"), dict(id="w2", state="surveying"), dict(id="w3", state="idle")]
        self.assertEqual(step(13)[0], 4)
        self.assertEqual(step(5)[0], 2)
        # Admission is a prefix of the worker list: idle workers ahead of the
        # running ones would each be a new round.
        workers[:] = [dict(id="w3", state="running"), dict(id="w4", state="running")]
        self.assertEqual(step(5, maximum=4)[0], 0)
        self.assertEqual(step(9, maximum=4)[0], 1)
        self.assertEqual(step(13, maximum=4)[0], 4)
        workers[:] = [dict(id="w1", state="running"), dict(id="w2", state="surveying"), dict(id="w3", state="idle")]
        # 32 GiB less the reserve holds seven, however much is free.
        self.assertEqual(step(31)[0], 7)
        self.assertEqual(step(31, maximum=3), (3, "all configured workers fit in free memory at 4 GiB each"))
        self.assertEqual(step(3), (0, "host memory headroom below reserve"))
        self.assertEqual(scaler.decision["state"], "paused")
        self.assertEqual(step(31, maximum=0, pause_reason="worker admission lease expired")[1],
                         "worker admission lease expired")
        with patch.object(host, "worker_statuses", return_value=[]):
            self.assertEqual(scaler.step(enabled=False, available=31 * GIB, reserve=reserve, maximum=8)[0], 0)

    def test_controller_admits_no_idle_worker_ahead_of_running_ones_beyond_free_memory(self):
        machine = store.update_host(ci_allowed=False, workers_enabled=True, max_workers=4)["machine_id"]
        ids = [f"fleet-{machine[:8]}-{n}" for n in range(1, 5)]
        running = [dict(id=wid, state="running", alive=True) for wid in ids[2:]]
        admitted = []
        caps = dict(cpus=8.0, memory_total=32 * GIB, memory_available=5 * GIB, cgroup=None)

        def sleep(_):
            admitted.append(store.read(store.state_dir() / "admission.json")["workers"])
            host.signal.getsignal(host.signal.SIGTERM)()

        handlers = {s: host.signal.getsignal(s) for s in (host.signal.SIGTERM, host.signal.SIGINT, host.signal.SIGHUP)}
        self.addCleanup(lambda: [host.signal.signal(s, h) for s, h in handlers.items()])
        with (
            patch.object(host, "worker_statuses", return_value=running),
            patch.object(host, "resources", return_value=caps),
            patch.object(host, "worker_headroom", return_value=4 * GIB),
            patch.object(host, "cpu_sample", return_value=(0, 0)),
            patch.object(host, "collect_ci_results"),
            patch.object(host.time, "sleep", sleep),
        ):
            host.daemon()
        self.assertEqual(admitted, [[]])
        status = store.read(store.state_dir() / "status.json")
        self.assertIsNone(status["error"])
        self.assertEqual(status["active_worker_limit"], 0)
        self.assertIn(macos.WORKERS, self.loaded)

    def test_doctor_reports_only_the_workers_role(self):
        config = store.update_host(ci_allowed=False)
        result = capabilities.doctor(config)
        checks = {c["name"]: c for c in result["checks"]}
        self.assertTrue(all(c["roles"] == ["workers"] for c in checks.values()))
        self.assertFalse({"kvm", "ci-shape", "ci-image", "storage", "qemu-img"} & set(checks))
        self.assertEqual(checks["platform"]["status"], "ok")
        self.assertEqual(checks["user-services"]["status"], "ok")
        self.assertEqual(checks["persistence"]["status"], "warning")
        self.assertEqual(checks["resource-controls"]["status"], "warning")
        self.assertIn("4 GiB", checks["resource-controls"]["evidence"])
        self.assertIn("32.0 GiB RAM", checks["capacity"]["evidence"])
        self.assertEqual(result["readiness"]["ci"], "disabled-by-configuration")
        self.assertEqual(self.launchctl("print"), [[self.domain]])

    def test_ci_is_refused_before_anything_is_stored_or_installed(self):
        with self.assertRaisesRegex(store.FleetError, "no CI runners; repeat add with --no-ci"):
            host.configure(dict(profile="test", ci_allowed=True, credentials={"github_token_file": "secret"}))
        self.assertFalse((self.home / "config/secrets").exists())
        self.assertEqual(self.calls, [])
        for payload in (dict(action="enroll"), dict(action="native-runner"), dict(action="reserve-staging", maximum=1),
                        dict(action="ci-autoscale"), dict(action="doctor", smoke=True)):
            with self.assertRaisesRegex(store.FleetError, "no CI runners"):
                host.rpc(payload)
        result = host.configure(dict(profile="test", ci_allowed=False))
        self.assertIsNone(result["service_error"])
        self.assertIn(macos.CONTROLLER, self.loaded)
        with self.assertRaisesRegex(store.FleetError, "--no-ci"):
            host.enable(ci="pr")
        self.assertIsNone(host.rpc(dict(action="status"))["lean_host_pressure"])

    def test_enabling_workers_writes_no_slices_and_leaves_the_controller_running(self):
        host.configure(dict(profile="test", ci_allowed=False, max_workers=3))
        self.calls.clear()
        ready = dict(readiness=dict(workers="ready-with-limitations", ci="disabled-by-configuration"), checks=[])
        with patch.object(host, "doctor", return_value=ready):
            result = host.enable(workers=True)
        self.assertTrue(result["workers_enabled"])
        self.assertEqual((self.home / "config/workers.toml").read_text().count("[[workers]]"), 3)
        self.assertFalse((self.home / ".config/systemd").exists())
        self.assertEqual(self.launchctl("bootout"), [])
        self.assertEqual(self.launchctl("bootstrap"), [])
        self.assertEqual(host.drain()["state"], "draining")
        self.assertFalse(store.host_config()["workers_enabled"])

    def test_live_handover_signals_the_running_launchd_job(self):
        release = self.home / "data/releases/new"
        pid = 4242
        self.loaded[macos.CONTROLLER] = pid
        store.atomic(store.state_dir() / "status.json", dict(live_update_protocol=1, controller_pid=pid))
        def launchd(args, **kwargs):
            if list(args[:2]) == ["launchctl", "kill"]:
                store.atomic(store.state_dir() / "status.json", dict(
                    live_update_protocol=1, controller_pid=pid, controller_release=str(release),
                    observed_at=time.time() + 5))
            return self.command(args, **kwargs)

        with patch.object(macos, "run", launchd):
            current = update.handover(release)
        self.assertEqual(current["controller_release"], str(release))
        self.assertEqual(self.launchctl("kill"), [["SIGHUP", f"{self.domain}/org.tauceti.fleet"]])
        self.assertEqual(self.launchctl("bootout") + self.launchctl("bootstrap"), [])

    def test_status_explains_the_memory_count_and_omits_ci_admission(self):
        machine = store.host_config()["machine_id"]
        decision = dict(policy="memory-count", cpu_idle_required=0, next_increase_at=None, next_worker_limit=None)
        value = dict(
            machine_id=machine, max_workers=8, lean_host_pressure=None,
            status=dict(active_worker_limit=3, cpu_idle_fraction=0.25, worker_admission=decision, worker_memory=None,
                        scaling_reason="3 of 8 workers fit in free memory at 4 GiB each", workers=[]),
        )
        output = io.StringIO()
        with patch.object(remote, "call", return_value=value), patch("sys.stdout", output):
            self.assertEqual(main(["status", "local"]), 0)
        text = output.getvalue()
        self.assertIn("New-round admission limit: 3 / 8", text)
        self.assertIn("up to 3 workers allowed; 3 of 8 workers fit in free memory at 4 GiB each", text)
        self.assertIn("CPU idle: 25.0%\n", text)
        self.assertNotIn("Lean host admission", text)
        self.assertNotIn("Worker RAM", text)


if __name__ == "__main__":
    unittest.main()
