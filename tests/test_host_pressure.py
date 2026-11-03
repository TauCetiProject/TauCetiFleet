import io
import json
import math
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import capacity, host, lean_guest, lean_host, lean_pressure, lean_runtime, store, vm

GIB = 1024**3


class HostPressure(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.target = self.root / "pressure.json"
        self.required = self.root / "required"
        self.required.touch()
        for key, value in (("HOST_PRESSURE", self.target), ("HOST_REQUIRED", self.required)):
            p = patch.object(lean_pressure, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(lean_pressure.time, "monotonic", return_value=100)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(lean_pressure.time, "time", return_value=100)
        p.start()
        self.addCleanup(p.stop)
        self.value = dict(version=1, at=100, received_monotonic=100, cpu_percent=20,
                          memory_total=112 * GIB, memory_available=50 * GIB)

    def sample(self, **changes):
        self.target.write_text(json.dumps(dict(self.value, **changes)))

    def test_busy_guest_with_spare_host_capacity_can_start(self):
        # Former 60% guest threshold rejects this, despite 50 GiB free on host.
        self.sample()
        with patch.object(lean_pressure, "memory", return_value=(32 * GIB, 10 * GIB)):
            self.assertTrue(lean_pressure.capacity_available())

    def test_idle_guest_does_not_admit_work_when_host_is_full(self):
        with patch.object(lean_pressure, "memory", return_value=(112 * GIB, 100 * GIB)):
            for changes in (dict(memory_available=GIB), dict(cpu_percent=95)):
                self.sample(**changes)
                self.assertFalse(lean_pressure.capacity_available())

    def test_guest_absolute_exhaustion_still_waits(self):
        self.sample()
        with patch.object(lean_pressure, "memory", return_value=(32 * GIB, GIB)):
            self.assertFalse(lean_pressure.capacity_available())

    def test_missing_stale_or_corrupt_host_data_never_falls_back_to_guest_load(self):
        self.assertFalse(lean_pressure.capacity_available())
        for value in ({}, dict(self.value, received_monotonic=89), dict(self.value, received_monotonic=101),
                      dict(self.value, cpu_percent=math.nan), dict(self.value, memory_total=True),
                      dict(self.value, memory_available=113 * GIB)):
            self.target.write_text(json.dumps(value))
            self.assertFalse(lean_pressure.capacity_available())
        self.target.write_text('{"bad')
        self.assertFalse(lean_pressure.capacity_available())
        self.required.unlink()
        self.assertIsNone(lean_pressure.capacity_available())

    def test_buffered_old_host_frames_do_not_become_fresh_when_guest_resumes(self):
        self.sample(at=89, received_monotonic=100)
        self.assertFalse(lean_pressure.capacity_available())
        self.sample(at=103, received_monotonic=100)
        self.assertFalse(lean_pressure.capacity_available())

    def test_receiver_cache_is_readable_in_sandbox_without_network_or_environment(self):
        frame = json.dumps(dict(self.value, received_monotonic=-100)).encode() + b"\n"
        with patch.object(lean_guest, "reclaim"):
            lean_guest.receive(io.BytesIO(b'invalid\n' + b'x' * 5000 + b'\n' + frame), self.target)
        self.assertEqual(lean_pressure.host_pressure(), (20, 112 * GIB, 50 * GIB))
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o644)

    def test_guest_file_cache_reclaim_is_bounded_and_only_on_host_memory_pressure(self):
        with patch.object(Path, "write_text") as write:
            self.assertEqual(lean_guest.reclaim(dict(self.value), 0), 0)
            write.assert_not_called()
            low = dict(self.value, memory_available=GIB)
            self.assertEqual(lean_guest.reclaim(low, 0), 100)
            self.assertEqual(lean_guest.reclaim(low, 99), 99)
            write.assert_called_once_with("1\n")

    def test_host_snapshot_reaches_guest_over_serial_socket(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)):
            directory = store.state_dir() / "vms/guest"
            directory.mkdir(parents=True)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(directory / "pressure.sock"))
            listener.listen()
            listener.settimeout(1)
            with (patch.object(lean_pressure, "cpu_sample", side_effect=[(100, 80), (200, 160)]),
                  patch.object(lean_pressure, "memory", return_value=(112 * GIB, 50 * GIB))):
                publisher = lean_host.Publisher()
                self.addCleanup(publisher.close)
                publisher.step()
            stream, _ = listener.accept()
            with stream, stream.makefile("rb") as frames, patch.object(lean_guest, "reclaim"):
                frame = frames.readline()
                lean_guest.receive(io.BytesIO(frame), self.target)
            cpu, total, available = lean_pressure.host_pressure()
            self.assertAlmostEqual(cpu, 20)
            self.assertEqual((total, available), (112 * GIB, 50 * GIB))

    def test_cache_reclaim_rate_limit_survives_host_reconnection(self):
        frame = json.dumps(dict(self.value, memory_available=GIB)).encode() + b"\n"
        with patch.object(lean_guest, "reclaim", side_effect=[100, 100]) as reclaim:
            last = lean_guest.receive(io.BytesIO(frame), self.target)
            lean_guest.receive(io.BytesIO(frame), self.target, last)
        self.assertEqual(reclaim.call_args_list[1].args[1], 100)

    def test_fresh_guests_can_use_all_shared_host_capacity_without_preallocation(self):
        config = dict(ci_vcpus=16, ci_memory_gib=32)
        caps = dict(cpus=96, memory_total=125 * GIB)
        shape = capacity.guest_shape(config, caps)
        self.assertEqual(shape, dict(vcpus=96, memory_gib=112))
        with patch.object(capacity, "guest_shape", return_value=shape), patch.object(vm, "executable", side_effect=lambda x: x):
            args = vm.qemu_args(config, self.root, self.root / "disk", self.root / "seed")
        self.assertEqual(args[args.index("-m") + 1], str(112 * 1024))
        self.assertEqual(args[args.index("-smp") + 1], "96")
        self.assertIn("virtserialport,chardev=pressure,name=org.tauceti.pressure", args)
        self.assertFalse(any("prealloc" in x for x in args))

    def test_publisher_service_samples_the_same_memory_ceiling_as_ci(self):
        with patch.object(host, "units", return_value=self.root), patch.object(host, "run"):
            host.install_service(restart=False)
        self.assertIn("Slice=tauceti-fleet-ci.slice", (self.root / "tauceti-fleet-pressure.service").read_text())
        self.assertNotIn("WorkingDirectory", (self.root / "tauceti-fleet-pressure.service").read_text())
        self.assertIn("PYTHONPATH=", (self.root / "tauceti-fleet-pressure.service").read_text())
        commands = "\n".join(lean_runtime.ci_commands())
        self.assertIn("host-pressure.required", commands)
        self.assertIn("systemctl enable --now tauceti-host-pressure.service", commands)
        subprocess.run(["bash", "-n"], input=commands, text=True, check=True)

    def test_capacity_wait_does_not_spend_compiler_watchdog_deadline(self):
        compiler = self.root / "compiler"
        compiler.write_text('#!/bin/sh\nprintf compiled\n')
        compiler.chmod(0o755)
        binary = self.root / "watchdog/bin/lean"
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexec timeout 0.05 "$(dirname "$0")/lean-real" "$@"\n')
        binary.chmod(0o755)
        (binary.parent / "lean-real").symlink_to(compiler)
        lean_pressure.install_watchdog(binary.parent / "lean-real")
        code = ("import sys,time; from tauceti_fleet import lean_pressure as p; "
                "p.sys.argv[0]=sys.argv[1]; p.wait_for_capacity=lambda:time.sleep(0.2); p.main(['file.lean'])")
        result = subprocess.run(["python3", "-c", code, str(binary)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "compiled")

    def test_shared_host_start_permissions_are_bounded_and_demand_driven(self):
        with patch.dict(os.environ, TAUCETI_FLEET_STATE=str(self.root)):
            peers = []
            for n in range(8):
                directory = store.state_dir() / f"vms/guest-{n}"
                directory.mkdir(parents=True)
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.addCleanup(listener.close)
                listener.bind(str(directory / "pressure.sock"))
                listener.listen()
                peers.append(listener)
            with (patch.object(lean_pressure, "cpu_sample", side_effect=[(100, 80), (200, 160), (300, 240), (400, 320), (500, 400)]),
                  patch.object(lean_host, "worker_cpu", return_value=0),
                  patch.object(lean_pressure, "memory", return_value=(112 * GIB, 50 * GIB))):
                publisher = lean_host.Publisher()
                self.addCleanup(publisher.close)
                publisher.step()
                clients = []
                for listener in peers:
                    client, _ = listener.accept()
                    self.addCleanup(client.close)
                    client.recv(4096)
                    client.sendall(b'1\n')
                    clients.append(client)
                publisher.step()
                frames = [json.loads(c.recv(4096)) for c in clients]
                admitted = sum(v['permit'] == 1 for v in frames)
                self.assertGreater(admitted, 0)
                self.assertLess(admitted, 8)
                publisher.step()
                frames = [json.loads(c.recv(4096)) for c in clients]
                self.assertEqual(sum(bool(v['permit']) for v in frames), 0)
                # After the short ramp window, other requesting guests get a
                # turn; a request that was already granted is never replayed.
                with patch.object(lean_host.time, 'monotonic', return_value=103):
                    publisher.step()
                later = [json.loads(c.recv(4096)) for c in clients]
                self.assertGreater(sum(bool(v['permit']) for v in later), 0)

    def test_guest_rejects_replayed_or_stale_permissions_and_serves_only_one_waiter(self):
        broker = lean_guest.Broker()
        request = broker.request
        a, b = socket.socketpair()
        c, d = socket.socketpair()
        for s in (a, b, c, d):
            self.addCleanup(s.close)
            s.settimeout(0.01)
        broker.clients.extend([a, c])
        broker.grant(dict(self.value, permit=request))
        self.assertEqual(b.recv(1), b'1')
        broker.grant(dict(self.value, permit=request))
        with self.assertRaises(TimeoutError):
            d.recv(1)
        broker.grant(dict(self.value, permit=request + 1, at=80))
        with self.assertRaises(TimeoutError):
            d.recv(1)
        broker.grant(dict(self.value, permit=request + 1))
        with self.assertRaises(TimeoutError):
            d.recv(1)
        broker.grant(dict(self.value, permit=request + 2))
        self.assertEqual(d.recv(1), b'1')

    def test_guest_serves_a_batch_of_waiters_from_one_host_grant(self):
        broker = lean_guest.Broker()
        peers = []
        for _ in range(4):
            a, b = socket.socketpair()
            self.addCleanup(a.close)
            self.addCleanup(b.close)
            b.settimeout(.1)
            broker.clients.append(a)
            peers.append(b)
        broker.grant(dict(self.value, permit=broker.request, starts=4))
        self.assertEqual([p.recv(1) for p in peers], [b'1'] * 4)
        self.assertFalse(broker.clients)


if __name__ == "__main__":
    unittest.main()
