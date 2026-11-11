import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tauceti_fleet import capabilities, cli, host, remote, shared_host, store
from tauceti_fleet.capabilities import GIB


class SharedHost(unittest.TestCase):
    caps = dict(cpus=32, memory_total=125 * GIB, memory_available=70 * GIB)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"TAUCETI_FLEET_" + key.upper(): str(self.root / key)
                                      for key in ("config", "state", "data")})
        env.start()
        self.addCleanup(env.stop)
        self.machine = store.host_config()["machine_id"]
        self.directory = self.root / "shared"
        self.directory.mkdir(mode=0o1777)

    def claim(self, name="peer", now=1000, **fields):
        value = dict(dict(version=1, expires_at=now + shared_host.LEASE, workers=24, ci_busy=False), **fields)
        (self.directory / (name + ".json")).write_text(json.dumps(value))

    def step(self, machine="own", **fields):
        return shared_host.step(str(self.directory), machine, **dict(dict(workers=8, ci_busy=False, now=1000), **fields))

    def test_installs_divide_spare_ram_by_the_workers_each_publishes(self):
        alone = self.step()
        self.assertEqual((alone["peers"], alone["share"], alone["error"]), (0, 1, None))
        target = self.directory / "own.json"
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(json.loads(target.read_text()), dict(version=1, expires_at=1015, workers=8, ci_busy=False))
        other = self.step("other", workers=24, ci_busy=True)
        own = self.step()
        self.assertEqual((other["share"], other["peer_workers"], other["ci_busy"]), (0.75, 8, False))
        self.assertEqual((own["share"], own["peers"], own["peer_workers"], own["ci_busy"]), (0.25, 1, 24, True))
        spare = self.caps["memory_available"] - host.worker_headroom(self.caps)
        highs = [host.worker_memory_limits({}, self.caps, 4 * GIB, share=value["share"])[0] for value in (own, other)]
        self.assertEqual(sum(highs), 8 * GIB + spare)
        self.assertEqual(highs[1] - 4 * GIB, 3 * (highs[0] - 4 * GIB))
        # An install with no workers leaves the whole of the spare RAM to the others.
        self.assertEqual(self.step("idle", workers=0)["share"], 0)
        self.assertEqual(self.step()["share"], 0.25)

    def test_expired_and_malformed_claims_count_for_nothing(self):
        self.claim("expired", expires_at=1000)
        self.claim("distant", expires_at=1000 + 2 * shared_host.LEASE + 1)
        self.claim("unversioned", version=2)
        self.claim("negative", workers=-1)
        self.claim("text", workers="8")
        self.claim("flag", workers=True)
        self.claim("number", ci_busy=1)
        self.claim("nan", expires_at=float("nan"))
        (self.directory / "list.json").write_text("[]")
        (self.directory / "binary.json").write_bytes(b"\xff\x00")
        (self.directory / "nested.json").write_text("[" * 4000)
        (self.directory / "directory.json").mkdir()
        os.mkfifo(self.directory / "fifo.json")
        self.claim("real.target")
        (self.directory / "real.target.json").rename(self.directory / "real.target")
        (self.directory / "link.json").symlink_to(self.directory / "real.target")
        value = self.step()
        self.assertEqual((value["peers"], value["share"], value["error"]), (0, 1, None))
        self.claim("live")
        self.assertEqual(self.step()["peers"], 1)
        (self.directory / "distant.json").unlink()
        self.assertEqual(self.step(now=1000 + shared_host.LEASE)["peers"], 0)

    def test_an_unusable_directory_is_reported_and_the_install_sizes_itself_alone(self):
        for directory in (str(self.root / "missing"), "relative", 7):
            value = shared_host.step(directory, "own", workers=8, ci_busy=False)
            self.assertIn("existing directory", value["error"])
            self.assertEqual((value["share"], value["peers"], value["ci_busy"]), (1, 0, False))
        self.assertFalse((self.root / "missing").exists())
        self.claim()
        with patch.object(store.tempfile, "mkstemp", side_effect=PermissionError(13, "Permission denied")):
            value = self.step()
        self.assertIn("cannot publish", value["error"])
        self.assertEqual(value["share"], 0.25)
        self.assertFalse((self.directory / "own.json").exists())
        if os.geteuid():
            self.directory.chmod(0o500)
            self.addCleanup(self.directory.chmod, 0o700)
            self.assertIn("cannot read and write", self.step()["error"])
            # Beneath another account's private home the directory cannot even be inspected.
            parent = self.root / "private"
            (parent / "shared").mkdir(parents=True)
            parent.chmod(0)
            self.addCleanup(parent.chmod, 0o700)
            value = shared_host.step(str(parent / "shared"), "own", workers=8, ci_busy=False)
            self.assertEqual((value["error"], value["share"], value["peers"]),
                             (f"this account cannot reach {parent / 'shared'}: PermissionError", 1, 0))

    def run_controller_once(self, available, cgroup):
        handlers = {}
        # Unlike a cgroup file, a regular file keeps the tail of a longer previous value.
        (cgroup / "memory.high").write_text("0")
        memory = dict(cgroup=str(cgroup), pinned=4 * GIB, working_set=4 * GIB, swap=0, maximum=110 * GIB)
        with (
            patch.object(host, "install_pressure_service"),
            patch.object(host, "resources", return_value=dict(self.caps, memory_available=available)),
            patch.object(host, "cpu_sample", return_value=(10, 9)),
            patch.object(host, "collect_ci_results"),
            patch.object(host, "start_workers"),
            patch.object(host, "worker_memory", return_value=memory),
            patch.object(host.vm, "slots_in_use", return_value=[]),
            patch.object(host.vm, "outstanding", return_value=False),
            patch.object(host.CISlot, "step", return_value={}),
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
            patch.object(host.time, "sleep", side_effect=lambda _: handlers[signal.SIGTERM]()),
        ):
            host.daemon()
        return store.read(store.state_dir() / "status.json"), int((cgroup / "memory.high").read_text())

    def test_controller_takes_its_share_and_yields_to_ci_building_in_another_install(self):
        cgroup = self.root / "cgroup"
        cgroup.mkdir()
        store.update_host(workers_enabled=True, max_workers=8)
        status, alone = self.run_controller_once(70 * GIB, cgroup)
        self.assertNotIn("shared_host", status)
        self.assertEqual(list(self.directory.iterdir()), [])
        spare = 70 * GIB - host.worker_headroom(self.caps)
        self.assertEqual(alone, 4 * GIB + spare)

        store.update_host(shared_host_dir=str(self.directory))
        now = host.time.time()
        self.claim(now=now)
        status, high = self.run_controller_once(70 * GIB, cgroup)
        self.assertEqual(high, 4 * GIB + spare // 4)
        self.assertEqual(status["shared_host"], dict(directory=str(self.directory), workers=8, peers=1, peer_workers=24,
                                                      share=0.25, ci_busy=False, error=None))
        self.assertEqual(json.loads((self.directory / (self.machine + ".json")).read_text())["workers"], 8)
        self.assertEqual(status["active_worker_limit"], 8)

        # 18 GiB is above the ordinary reserve and below the one kept while CI compiles.
        status, _ = self.run_controller_once(18 * GIB, cgroup)
        self.assertEqual(status["active_worker_limit"], 8)
        self.claim(now=now, ci_busy=True)
        status, high = self.run_controller_once(18 * GIB, cgroup)
        self.assertEqual((status["active_worker_limit"], status["memory_pressure"]), (0, ["host memory headroom low"]))
        self.assertEqual((status["ci_busy"], status["shared_host"]["ci_busy"]), (False, True))
        self.assertEqual(high, 5 * GIB)

        # Lowering the ceiling does not give up the part of a pool whose workers are still alive.
        store.update_host(max_workers=2)
        alive = [dict(id=str(n), alive=True) for n in range(6)]
        with patch.object(host, "worker_statuses", return_value=alive):
            status, _ = self.run_controller_once(70 * GIB, cgroup)
        self.assertEqual((status["shared_host"]["workers"], status["shared_host"]["share"]), (6, 0.2))

    def test_doctor_reports_the_directory_only_when_the_setting_is_present(self):
        def check(config):
            with (
                patch.object(capabilities, "resources", return_value=self.caps),
                patch.object(capabilities, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")),
                patch.object(capabilities, "executable", return_value=None),
                patch.object(capabilities.fcntl, "ioctl", side_effect=PermissionError),
                patch.object(capabilities.shutil, "disk_usage", return_value=SimpleNamespace(free=100 * GIB)),
            ):
                checks = capabilities.doctor(config, probe=False)["checks"]
            return next((c for c in checks if c["name"] == "shared-host"), None)

        config = store.host_config()
        self.assertIsNone(check(config))
        self.claim(now=host.time.time())
        found = check(dict(config, shared_host_dir=str(self.directory)))
        self.assertEqual((found["status"], found["roles"]), ("ok", ["workers"]))
        self.assertIn("1 other installs publish", found["evidence"])
        found = check(dict(config, shared_host_dir=str(self.root / "missing")))
        self.assertEqual(found["status"], "warning")
        self.assertIn("existing directory", found["evidence"])

    def test_status_names_the_share_and_the_reason_it_is_unavailable(self):
        def render(**shared):
            shared = dict(dict(directory="/shared", workers=8, peers=3, peer_workers=24, share=0.25, ci_busy=False,
                               error=None), **shared)
            results = dict(one=dict(machine_id="one", status=dict(workers=[], cpu_idle_fraction=0.5, shared_host=shared)))
            with (
                patch.object(remote, "inventory", return_value=dict(one=dict(target="one", machine_id="one"))),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                cli.main(["status"], _status_results=results)
            return next(line for line in output.getvalue().splitlines() if line.startswith("Shared machine"))

        self.assertEqual(render(), "Shared machine: 3 other installs with 24 workers; this pool may take 25% of spare RAM")
        self.assertTrue(render(ci_busy=True).endswith("; CI is building in another install"))
        self.assertTrue(render(peers=0, peer_workers=0, share=1, error="cannot publish in /shared: PermissionError")
                        .endswith("100% of spare RAM; cannot publish in /shared: PermissionError"))


if __name__ == "__main__":
    unittest.main()
