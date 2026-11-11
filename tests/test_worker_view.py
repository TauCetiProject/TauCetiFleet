import contextlib
import io
import json
import os
import re
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tauceti_fleet import cli, github, host, remote, status_follow, store, worker_view

OPENED = 1_800_000_000
NOW = OPENED + 86400


def stamp(at):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at))


class WorkerView(unittest.TestCase):
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
        self.machine = store.host_config()["machine_id"]
        self.journal = store.state_dir() / "worker-events.jsonl"
        self.journal.parent.mkdir(parents=True, exist_ok=True)

    def log(self, name, *messages):
        path = self.root / (name + ".log")
        path.write_text("".join(line if at is None else f"{stamp(at)} tauceti: {line}\n" for at, line in messages))
        return str(path)

    def worker(self, name, **fields):
        store.atomic(store.state_dir() / "workers" / (name + ".json"), dict(dict(id=name, alive=True), **fields))

    def record(self, **fields):
        return json.dumps(dict(dict(version=1, machine_id=self.machine, worker="w1", kind="pr_activity",
                                    repository="TauCetiProject/TauCeti", pr=7, at=100), **fields)) + "\n"

    def test_round_in_progress_reports_pr_provider_start_and_a_terminal_safe_last_line(self):
        log = self.log("w1", (OPENED - 60, "→ REVIEW: PR #3  https://github.com/o/r/pull/3   [provider=claude]"),
                       (OPENED - 30, "round rc=1; no-progress streak=1 — backing off 60s"),
                       (OPENED, "→ FIX: PR #41  https://github.com/o/r/pull/41   [provider=codex, sandbox=host]"),
                       (OPENED + 2, "→ ROADMAP area: Analysis (auto-picked, skipping 2)"),
                       (None, "    transcript text without the log prefix\n"),
                       (OPENED + 5, "\x1b[1;31m⚠ title with\ta tab\x07 " + "x" * 400 + "\x1b[0m"))
        row = worker_view.row(dict(state="verifying", phase="fix", agent="auto", log_file=log,
                                   target="PR #41  https://github.com/o/r/pull/41",
                                   detail="provider=codex, model=m-1, effort=high, sandbox=host"))
        self.assertEqual({k: row[k] for k in ("agent", "provider", "pr", "round_started_at")},
                         dict(agent="auto", provider="codex", pr=41, round_started_at=OPENED))
        self.assertTrue(row["log_line"].startswith("⚠ title with a tab  xxx"))
        self.assertTrue(row["log_line"].isprintable())
        self.assertEqual(len(row["log_line"]), 200)

    def test_stage_kept_from_a_failed_round_is_not_reported_as_a_round_in_progress(self):
        log = self.log("w1", (OPENED, "→ FIX: PR #41  https://github.com/o/r/pull/41   [provider=codex]"),
                       (OPENED + 9, "round rc=1; no-progress streak=1 — backing off 60s"))
        for state in ("backoff", "surveying", "waiting-quota"):
            row = worker_view.row(dict(state=state, phase="fix", agent="claude", log_file=log,
                                       target="PR #41  https://github.com/o/r/pull/41", detail="provider=codex"))
            self.assertEqual((row["pr"], row["provider"], row["round_started_at"]), (41, None, None))
            self.assertIn("backing off", row["log_line"])
        idle = worker_view.row(dict(state="running", phase=None, log_file=log))
        self.assertIsNone(idle["round_started_at"])

    def test_a_round_whose_opening_line_is_not_the_latest_has_no_start(self):
        log = self.log("w1", (OPENED, "→ REVIEW: PR #3  https://github.com/o/r/pull/3   [provider=claude]"),
                       (OPENED + 3600, "  #7: closing after spent repair attempts"))
        for opened in (log, self.log("w2", (OPENED, "→ FIX: PR #7  https://github.com/o/r/pull/7   [provider=codex]"))):
            row = worker_view.row(dict(state="running", phase="fix", target="PR #7", log_file=opened,
                                       detail="closing owned PR after spent repair attempts"))
            self.assertEqual((row["pr"], row["round_started_at"]), (7, None))

    def test_only_the_end_of_a_long_log_is_read_and_an_opening_outside_it_is_unknown(self):
        log = self.log("w1", (OPENED, "→ ROADMAP: new PR (area: any)   [provider=claude]"),
                       *[(OPENED + n, "build output " + "y" * 90) for n in range(1, 1000)],
                       (OPENED + 1000, "last"))
        self.assertGreater(os.path.getsize(log), worker_view.TAIL)
        lines = worker_view.tail(log)
        self.assertLessEqual(sum(len(line) + 1 for line in lines), worker_view.TAIL)
        self.assertTrue(all(line.startswith(stamp(OPENED)[:10]) for line in lines))
        row = worker_view.row(dict(state="running", phase="roadmap", target="new PR (area: any)", log_file=log))
        self.assertEqual((row["log_line"], row["round_started_at"], row["pr"]), ("last", None, None))

    def test_malformed_status_and_log_values_yield_empty_fields_instead_of_errors(self):
        descriptor = os.open(self.root, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        bad_date = self.log("w2", (None, "2026-99-99 25:61:61 tauceti: → FIX: PR #1\n"), (None, "\xff\xfe\n"))
        empty = dict(agent=None, provider=None, pr=None, round_started_at=None, log_line=None)
        for value in (
            {},
            dict(log_file=descriptor, state="running", phase="fix"),
            dict(log_file=str(self.root / "absent.log"), state=["running"], phase={}, agent=7, detail=3, target=4),
            dict(log_file=str(self.root), target="PR #" + "9" * 5000, detail="provider=" + "p" * 5000),
            dict(log_file="\0", target=None),
        ):
            self.assertEqual(worker_view.row(value), empty)
        os.fstat(descriptor)  # still open: the integer was not used as a descriptor
        row = worker_view.row(dict(log_file=bad_date, state="running", phase="fix"))
        self.assertEqual((row["log_line"], row["round_started_at"]), ("→ FIX: PR #1", None))

    def test_events_keep_the_newest_reader_relevant_records_and_skip_what_cannot_be_stored(self):
        lines = [
            self.record(kind="pr_created", at=1, pr=5),
            self.record(activity="branch_push", at=2),
            self.record(activity="github_mutation", publication=None, at=3),
            self.record(activity="github_mutation", publication="reply", at=4),
            self.record(activity="github_mutation", publication="closure", at=5),
            self.record(activity="review", at=6, head_sha="abc", round_id="r"),
            self.record(kind="round_started", at=7),
            self.record(activity="branch_push", at=True),
            self.record(activity="branch_push", pr="7"),
            self.record(activity="branch_push", pr=10**400),
            self.record(activity="branch_push", at=10**400),
            self.record(activity=None),
            '{"kind":"pr_activity","activity":"branch_push","pr":7,"at":NaN}\n',
            '{"kind":"pr_activity","activity":"branch_push","pr":7,"at":Infinity}\n',
            "[" * 100000 + "\n",
            "not json\n",
            self.record(activity="branch_push", at=8, repository="bad repository\x1b[2J", worker="w\x1b[2J9"),
            '{"kind":"pr_created","pr":9,"at":9',
        ]
        self.journal.write_text("".join(lines))
        found = worker_view.events(self.journal)
        self.assertEqual([(e["at"], e["kind"], e["activity"], e["publication"]) for e in found], [
            (1, "pr_created", None, None), (2, "pr_activity", "branch_push", None),
            (4, "pr_activity", "github_mutation", "reply"), (5, "pr_activity", "github_mutation", "closure"),
            (6, "pr_activity", "review", None), (8, "pr_activity", "branch_push", None)])
        self.assertEqual(found[0], dict(at=1, worker="w1", kind="pr_created", activity=None, publication=None,
                                        repository="TauCetiProject/TauCeti", pr=5))
        self.assertEqual((found[-1]["repository"], found[-1]["worker"]), (None, "w [2J9"))
        store.atomic(self.root / "stored.json", found)
        self.journal.write_text("".join(self.record(activity="branch_push", at=n, pr=n) for n in range(1, 2000)))
        self.assertGreater(self.journal.stat().st_size, 2 * worker_view.TAIL)
        self.assertEqual([e["pr"] for e in worker_view.events(self.journal)],
                         list(range(2000 - worker_view.EVENTS, 2000)))
        self.journal.unlink()
        self.assertEqual(worker_view.events(self.journal), [])

    def test_rows_replace_the_log_path_and_plain_status_reads_open_no_log(self):
        self.worker("w1", state="running", phase="fix", agent="claude", target="PR #8  https://github.com/o/r/pull/8",
                    detail="provider=claude, sandbox=host",
                    log_file=self.log("w1", (OPENED, "→ FIX: PR #8  https://github.com/o/r/pull/8")))
        with patch.object(worker_view, "tail", side_effect=AssertionError("log read outside the report")):
            plain = host.worker_statuses()
        row = worker_view.rows(plain)[0]
        self.assertEqual({k: v for k, v in row.items() if k in plain[0]},
                         {k: v for k, v in plain[0].items() if k != "log_file"})
        self.assertEqual({k: v for k, v in row.items() if k not in plain[0]},
                         dict(provider="claude", pr=8, round_started_at=OPENED,
                              log_line="→ FIX: PR #8  https://github.com/o/r/pull/8"))
        self.assertNotIn(str(self.root), json.dumps(row))

    def test_controller_report_carries_rows_and_events(self):
        self.worker("w1", alive=False, state="exited", agent="codex", log_file=self.log("w1", (OPENED, "loop terminated")))
        self.journal.write_text(self.record(kind="pr_created") + '{"partial":')
        handlers = {}
        with (
            patch.object(host, "install_pressure_service"),
            patch.object(host, "resources", return_value=dict(memory_total=32 * 1024**3, memory_available=30 * 1024**3)),
            patch.object(host, "cpu_sample", return_value=(10, 9)),
            patch.object(host, "collect_ci_results"),
            patch.object(host.vm, "slots_in_use", return_value=[]),
            patch.object(host.vm, "outstanding", return_value=False),
            patch.object(host.CISlot, "step", return_value={}),
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
            patch.object(host.time, "sleep", side_effect=lambda _: handlers[signal.SIGTERM]()),
        ):
            host.daemon()
        status = host.rpc(dict(action="status"))["status"]
        self.assertIsNone(status["error"])
        self.assertEqual([(w["id"], w["agent"], w["log_line"]) for w in status["workers"]],
                         [("w1", "codex", "loop terminated")])
        self.assertEqual([(e["kind"], e["pr"]) for e in status["worker_events"]], [("pr_created", 7)])

    def render(self, argv, status):
        value = dict(machine_id=self.machine, max_workers=40, status=status, activity=dict(available=False))
        entry = dict(target="local", machine_id=self.machine)
        out = io.StringIO()
        with (
            patch.object(remote, "inventory", return_value=dict(server1=entry)),
            patch.object(remote, "call", side_effect=AssertionError("polling a host during rendering")),
            patch.object(github, "request", side_effect=AssertionError("GitHub during rendering")),
            patch.object(worker_view.time, "time", return_value=NOW),
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(cli.main(argv, _status_results=dict(server1=value)), 0)
        return out.getvalue()

    def status(self):
        now = NOW
        workers = [
            dict(id="fleet-ab-10", state="waiting-quota", detail="claude 5h window", agent="claude", provider=None,
                 pr=None, round_started_at=None, log_line="quota: rechecking"),
            dict(id="fleet-ab-2", state="running", phase="fix", target="PR #41  https://github.com/o/r/pull/41",
                 detail="provider=codex, sandbox=host", agent="auto", provider="codex", pr=41,
                 round_started_at=now - 3725, log_line="agent-codex: started " + "z" * 200),
            dict(id="fleet-ab-1", state="running", phase="roadmap", target="new PR (area: Analysis)",
                 detail="provider=claude, sandbox=host", agent="claude", provider="claude", pr=None,
                 round_started_at=now - 90, log_line=None),
            dict(id="fleet-ab-3", state="backoff", phase="review", target="PR #7  https://github.com/o/r/pull/7",
                 detail="round timed out", agent="claude", provider=None, pr=7, round_started_at=None,
                 log_line="round timed out; no-progress streak=1 — backing off 60s"),
            dict(id="fleet-ab-4", state="surveying", phase="review", target="PR #7  https://github.com/o/r/pull/7",
                 detail="selecting the next work unit", agent="claude", provider=None, pr=7, round_started_at=None,
                 log_line="survey: 12 open"),
        ]
        recent = [
            dict(at=now - 7200, worker="fleet-ab-1", kind="pr_created", activity=None, publication=None,
                 repository="TauCetiProject/TauCeti", pr=40),
            dict(at=now - 600, worker="fleet-ab-2", kind="pr_activity", activity="github_mutation", publication="reply",
                 repository="TauCetiProject/TauCeti", pr=41),
            dict(at=now - 30, worker="fleet-ab-2", kind="pr_activity", activity="review", publication=None,
                 repository=None, pr=12),
        ]
        return dict(workers=workers, worker_events=recent, active_worker_limit=3)

    def test_default_text_is_unchanged_and_the_flag_adds_sorted_rows_and_events(self):
        status = self.status()
        plain = self.render(["status"], status)
        bare = dict(status, workers=[{k: w[k] for k in ("id", "state", "detail")} for w in status["workers"]])
        del bare["worker_events"]
        self.assertEqual(plain, self.render(["status"], bare))
        text = self.render(["status", "--workers"], status)
        lines = text.splitlines()
        for line in plain.splitlines():
            self.assertIn(line, lines)
        header = next(i for i, line in enumerate(lines) if line.split()[:2] == ["Worker", "State"])
        self.assertEqual([re.split(r"\s{2,}", line.strip()) for line in lines[header:header + 6]], [
            ["Worker", "State", "Stage", "Holds", "Agent", "In round", "Last log line"],
            ["fleet-ab-1", "running", "roadmap", "new PR (area: Analysis)", "claude", "1m", "—"],
            ["fleet-ab-2", "running", "fix", "#41", "auto→codex", "1h02m", "agent-codex: started " + "z" * 79],
            ["fleet-ab-3", "backoff", "review", "#7", "claude", "—", "round timed out; no-progress streak=1 — backing off 60s"],
            ["fleet-ab-4", "surveying", "—", "—", "claude", "—", "survey: 12 open"],
            ["fleet-ab-10", "waiting-quota", "—", "—", "claude", "—", "quota: rechecking"]])
        self.assertEqual({line.index(cell) for line, cell in zip(lines[header:header + 6], (
            "Last log line", "—", "agent-codex", "round timed out", "survey:", "quota:"), strict=True)},
            {lines[header].index("Last log line")})
        self.assertEqual([" ".join(line.split()) for line in lines[header + 6:header + 10]], [
            "Recent worker events:",
            "2h00m ago fleet-ab-1 opened TauCetiProject/TauCeti#40",
            "10m ago fleet-ab-2 replied on TauCetiProject/TauCeti#41",
            "30s ago fleet-ab-2 finished review on #12"])

    def test_host_without_rows_says_so_and_a_quiet_journal_is_stated(self):
        old = dict(workers=[dict(id="w1", state="running", detail="authoring")], active_worker_limit=1)
        text = self.render(["status", "--workers"], old)
        self.assertIn("Worker rows: not reported by this host's Fleet release", text)
        self.assertNotIn("Recent worker events", text)
        self.assertNotIn("Last log line", text)
        quiet = self.render(["status", "--workers"], dict(workers=[], worker_events=[], active_worker_limit=0))
        self.assertIn("Recent worker events: none", quiet)
        self.assertNotIn("Last log line", quiet)

    def test_follow_redraws_with_the_flag(self):
        entry = dict(target="local", machine_id=self.machine)
        value = dict(machine_id=self.machine, status=self.status(), activity=dict(available=False))
        with (
            patch.object(remote, "inventory", return_value=dict(server1=entry)),
            patch.object(remote, "call", side_effect=AssertionError("polling a host during redraw")),
            patch.object(status_follow, "run", return_value=0) as run,
        ):
            self.assertEqual(cli.main(["status", "-f", "--workers"]), 0)
            self.assertIn("Last log line", run.call_args.args[1](dict(server1=value)))
            self.assertEqual(cli.main(["status", "-f"]), 0)
            self.assertNotIn("Last log line", run.call_args.args[1](dict(server1=value)))


if __name__ == "__main__":
    unittest.main()
