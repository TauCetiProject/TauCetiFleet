import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_fleet import ci_overview, cli, github, remote, status_follow as follow, store


class Follow(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {
            **{"TAUCETI_FLEET_" + key.upper(): str(self.root / key) for key in ("config", "state", "data")},
            "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
        })
        env.start()
        self.addCleanup(env.stop)
        self.config = store.host_config()
        self.machine = self.config["machine_id"]
        self.entry = dict(target="local", machine_id=self.machine)

    def test_file_changes_emit_new_prepared_reports_without_collecting_or_querying(self):
        path = store.state_dir() / "status.json"
        store.atomic(path, dict(generation=1))
        owner = self

        class Stop:
            step = 0

            def is_set(self):
                return self.step >= 3

            def wait(self, seconds):
                self.step += 1
                if self.step == 1:
                    store.atomic(path, dict(generation=2))

        frames = []
        with patch.object(github, "request", side_effect=AssertionError("GitHub in follow")), patch.object(ci_overview, "fetch", side_effect=AssertionError("API in follow")), patch.object(ci_overview, "collect", side_effect=AssertionError("collector in follow")):
            follow.watch_cached(owner.machine, frames.append, Stop())
        self.assertEqual([frame["result"]["status"]["generation"] for frame in frames], [1, 2])

    def test_identity_mismatch_does_not_replace_cached_report_and_disconnect_retains_age(self):
        value = dict(machine_id=self.machine, status=dict(generation=1))
        with patch.object(follow.time, "time", return_value=100):
            previous = follow.apply_frame(self.entry, dict(ok=True, result=value), {})
        with patch.object(follow.time, "time", return_value=200):
            offline = follow.apply_frame(self.entry, dict(ok=False, error="disconnected"), previous)
        self.assertTrue(offline["offline"])
        self.assertEqual(offline["collected_at"], 100)
        self.assertEqual(offline["status"], value["status"])
        with self.assertRaisesRegex(store.FleetError, "identity"):
            follow.apply_frame(self.entry, dict(ok=True, result=dict(machine_id="wrong")), previous)
        cached = store.read(store.config_dir() / "status-cache" / (self.machine + ".json"))
        self.assertEqual(cached["value"], value)

    def test_remote_stream_uses_one_ssh_process_for_multiple_reports_and_cleans_up(self):
        stop = threading.Event()
        frames = [dict(ok=True, result=dict(machine_id=self.machine, generation=i)) for i in (1, 2)]
        process = Mock()
        process.stdout = io.StringIO("\n".join(json.dumps(frame) for frame in frames) + "\n")
        process.poll.return_value = None
        processes = set()
        received = []

        def emit(frame):
            received.append(frame)
            if len(received) == 2:
                stop.set()

        with patch.object(follow.subprocess, "Popen", return_value=process) as start:
            follow.receive(dict(self.entry, target="server1"), emit, stop, processes, threading.Lock())
        self.assertEqual(received, frames)
        start.assert_called_once()
        self.assertIn('_watch-status', start.call_args.args[0][-1])
        self.assertEqual(json.loads(process.stdin.write.call_args.args[0])["expected_machine_id"], self.machine)
        process.terminate.assert_called_once()
        self.assertFalse(processes)

    def test_follow_aliases_and_rendering_do_not_requery_hosts(self):
        self.assertTrue(cli.parser().parse_args(["status", "-f"]).follow)
        self.assertTrue(cli.parser().parse_args(["status", "server1", "--follow", "--json"]).follow)
        value = dict(machine_id=self.machine, status=dict(workers=[], active_worker_limit=0), activity=dict(available=False))
        with patch.object(remote, "inventory", return_value=dict(server1=self.entry)), patch.object(remote, "call", side_effect=AssertionError("polling a host during redraw")), patch.object(github, "request", side_effect=AssertionError("GitHub during redraw")):
            text = follow.render_with_cli(["status"], dict(server1=value))
        self.assertIn("server1:", text)
        self.assertIn("0 running", text)


if __name__ == "__main__":
    unittest.main()
