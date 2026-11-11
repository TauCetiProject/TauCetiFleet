import io
import json
import os
import signal
import sqlite3
import tempfile
import tomllib
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from tauceti_fleet import attention, github, host, remote, store
from tauceti_fleet.cli import main

REPOSITORY = "TauCetiProject/TauCeti"
REQUEST = github.request


class Attention(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environ = patch.dict(os.environ, {
            "TAUCETI_FLEET_CONFIG": str(self.root / "config"),
            "TAUCETI_FLEET_STATE": str(self.root / "state"),
            "TAUCETI_FLEET_DATA": str(self.root / "data"),
            "TAUCETI_ACTIVITY_DIR": str(self.root / "activity"),
        })
        environ.start()
        self.addCleanup(environ.stop)
        offline = patch.object(github, "request", side_effect=AssertionError("GitHub request"))
        self.github = offline.start()
        self.addCleanup(offline.stop)
        store.host_config()

    def on(self, **settings):
        store.update_host(incidents=True, **settings)
        return store.host_config()

    def record(self, kind, stage, pr, *, head="a" * 40, at="2026-01-15T09:30:00Z", acked=False, **fields):
        value = dict(schema="tauceti-incident/v1", kind=kind, key=f"{stage}-{pr}", worker="w1", first_at=at,
                     last_at=at, count=1, repository=REPOSITORY, stage=stage, pr=pr, head=head) | fields
        path = attention.incidents_dir() / ("acked" if acked else "") / f"{kind}-{stage}-{pr}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n")
        return path

    def observe(self, pr, state, head, observed, landed=None):
        path = self.root / "activity/activity.sqlite"
        path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS prs(repo TEXT,number INTEGER,worker TEXT,scope TEXT,created REAL,"
                       "state TEXT,landed REAL,observed REAL,checked REAL,head TEXT,source TEXT,PRIMARY KEY(repo,number))")
            db.execute("INSERT OR REPLACE INTO prs(repo,number,state,landed,observed,head) VALUES (?,?,?,?,?,?)",
                       (REPOSITORY, pr, state, landed, observed, head))

    def listed(self, **payload):
        return host.rpc(dict(action="attention", **payload))

    def cli(self, *argv, call=None):
        entry = dict(machine_id=store.host_config()["machine_id"], target="local")
        output = io.StringIO()
        with (
            patch.object(remote, "inventory", return_value={"box": entry}),
            patch.object(remote, "call", side_effect=call or (lambda target, payload, **_: host.rpc(payload))),
            patch("sys.stdout", output),
            patch("sys.stderr", output),
        ):
            code = main(list(argv))
        return code, output.getvalue()

    def test_unset_writes_no_worker_setting_and_lists_nothing(self):
        self.record("declined", "fix", 7, summary="left alone")
        host.worker_config(store.host_config())
        before = (store.config_dir() / "workers.toml").read_text()
        self.assertNotIn("env", before)
        value = self.listed()
        self.assertEqual((value["incidents"], value["items"]), (False, []))
        with patch.object(host.ci_overview, "cached", return_value={}):
            self.assertNotIn("attention", host.rpc(dict(action="status")))
        code, text = self.cli("attention", "box")
        self.assertEqual(code, 0)
        self.assertIn("Nothing needs you.", text)
        self.assertIn('profile key "incidents"', text)
        store.update_host(incidents=False, hold_declined=True)
        host.worker_config(store.host_config())
        self.assertEqual((store.config_dir() / "workers.toml").read_text(), before)

    def test_setting_reaches_every_worker_definition_and_the_hold_is_a_second_key(self):
        with patch.object(host, "install_service"):
            host.configure(dict(profile="test", ci_allowed=False, incidents=True))
            store.update_host(max_workers=2)
            host.worker_config(store.host_config())
            workers = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
            self.assertEqual([w["env"] for w in workers], [
                {"TAUCETI_INCIDENTS_DIR": str(self.root / "state/incidents"), "TAUCETI_HOLD_DECLINED": "1"}] * 2)
            host.configure(dict(hold_declined=False))
            host.worker_config(store.host_config())
            workers = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"]
            self.assertEqual(workers[0]["env"], {"TAUCETI_INCIDENTS_DIR": str(self.root / "state/incidents")})
            with self.assertRaisesRegex(store.FleetError, "incidents must be true or false"):
                host.configure(dict(incidents="yes"))
            store.update_host(workers_enabled=True)
            with self.assertRaisesRegex(store.FleetError, "Drain"):
                host.configure(dict(incidents=False))
        profiles = {"p": dict(github_account="someone", incidents=True, hold_declined=False)}
        store.atomic(store.config_dir() / "profiles.json", profiles)
        with patch.object(remote, "run") as run:
            run.return_value.returncode = 1
            sent = remote.profile("p")
        self.assertEqual((sent["incidents"], sent["hold_declined"]), (True, False))

    def test_list_shows_live_records_oldest_first_with_the_agents_words(self):
        self.on()
        self.record("declined", "rebase", 412, at="2026-01-15T09:30:00Z", reason="conflicting", count=3,
                    summary="Main already has this.\x1b[2J\nSubsumed-by: #398", subsumed_by=[398], mentions=[400],
                    transcript="/home/worker/logs/agent.log")
        self.record("budget-spent", "fix", 9, at="2026-01-14T08:00:00Z", detail="changes requested; 3 of 3 attempts used")
        self.record("declined", "fix", 5, at="2026-01-13T08:00:00Z", acked=True, summary="handled earlier")
        (attention.incidents_dir() / "declined-fix-1.json").write_text("{not json")
        value = self.listed()
        self.assertEqual([(i["id"], i["kind"]) for i in value["items"]],
                         [("declined-fix-1.json", "unreadable"), ("9", "budget-spent"), ("412", "declined")])
        declined = value["items"][-1]
        self.assertEqual(declined["text"], "Main already has this. [2J\nSubsumed-by: #398")
        self.assertEqual([(n["pr"], n["relation"]) for n in declined["named"]], [(398, "subsumed_by"), (400, "mentions")])
        code, text = self.cli("attention", "box")
        self.assertEqual(code, 0)
        self.assertIn(f"== PR #412  https://github.com/{REPOSITORY}/pull/412  "
                      "(declined · rebase · w1 · 2026-01-15T09:30:00Z · seen 3x)", text)
        self.assertIn("   Main already has this.", text)
        self.assertIn(f"   subsumed by: #398  https://github.com/{REPOSITORY}/pull/398  (--lookup for state and title)", text)
        self.assertIn("   mentions: #400", text)
        self.assertIn("   transcript: /home/worker/logs/agent.log", text)
        self.assertIn("   changes requested; 3 of 3 attempts used", text)
        self.assertIn("Acknowledge with: tauceti-fleet attention box --ack <PR number> | all", text)
        self.assertNotIn("handled earlier", text)
        self.assertNotIn("\x1b", text)
        code, text = self.cli("attention", "--json")
        self.assertEqual(len(json.loads(text)["box"]["items"]), 3)

    def test_items_clear_on_state_the_host_already_holds_and_only_when_it_is_newer_than_the_record(self):
        self.on()
        at = attention.epoch("2026-01-15T09:30:00Z")
        for pr in (1, 2, 3, 4, 5, 6):
            self.record("declined", "fix", pr, summary="no")
        self.observe(1, "MERGED", "a" * 40, at + 60)
        self.observe(2, "CLOSED", "a" * 40, at + 60, landed=at + 50)
        self.observe(3, "CLOSED", "a" * 40, at + 60)
        self.observe(4, "OPEN", "b" * 40, at + 60)
        self.observe(5, "OPEN", "b" * 40, at - 60)
        self.observe(6, "OPEN", "a" * 40, at + 60)
        value = self.listed()
        self.assertEqual({c["id"]: c["cleared"] for c in value["cleared"]},
                         {"1": "merged", "2": "merged", "3": "closed", "4": "new head"})
        self.assertEqual([(i["id"], i["observed_at"]) for i in value["items"]], [("5", None), ("6", at + 60)])
        self.assertEqual(len(list(attention.incidents_dir().glob("*.json"))), 6)
        code, text = self.cli("attention", "box")
        self.assertIn("Not listed: 4 records whose pull request merged, closed or moved to a new head.", text)
        self.assertIn("This host has no pull request state newer than the record for 1 of these", text)
        self.record("declined", "fix", 4, at="some time ago", summary="no")
        self.assertEqual(len(self.listed()["cleared"]), 3)
        (self.root / "activity/activity.sqlite").write_text("not a database")
        self.assertEqual(len(self.listed()["items"]), 6)

    def test_ack_moves_records_and_an_acked_decline_keeps_its_file_for_the_hold(self):
        self.on()
        live = self.record("declined", "fix", 7, summary="no")
        text = live.read_text()
        self.record("budget-spent", "fix", 7, detail="spent")
        self.record("declined", "rebase", 8, summary="no")
        self.record("declined", "rebase", 9, summary="no")
        self.observe(9, "MERGED", "a" * 40, attention.epoch("2026-01-15T09:30:00Z") + 1)
        value = self.listed(ack="7")
        self.assertEqual(sorted(d["record"] for d in value["acknowledged"]),
                         ["budget-spent-fix-7.json", "declined-fix-7.json"])
        self.assertEqual([i["id"] for i in value["items"]], ["8"])
        self.assertFalse(live.exists())
        self.assertEqual((attention.incidents_dir() / "acked/declined-fix-7.json").read_text(), text)
        self.assertEqual(attention.prepared()["count"], 1)
        code, output = self.cli("attention", "--ack", "all")
        self.assertEqual(code, 1)
        self.assertIn("explicit host", output)
        code, output = self.cli("attention", "box", "--ack", "all")
        self.assertIn("Acknowledged 2 items: declined-rebase-8.json, declined-rebase-9.json", output)
        self.assertIn("Nothing needs you.", output)
        self.assertEqual(sorted(p.name for p in (attention.incidents_dir() / "acked").iterdir()),
                         ["budget-spent-fix-7.json", "declined-fix-7.json", "declined-rebase-8.json", "declined-rebase-9.json"])
        self.assertEqual(list(attention.incidents_dir().glob("*.json")), [])

    def test_a_second_ack_keeps_the_earlier_archive_under_a_name_the_worker_does_not_read(self):
        self.on()
        acked = attention.incidents_dir() / "acked"
        texts = []
        for head in ("a" * 40, "b" * 40, "c" * 40):
            texts.append(self.record("declined", "fix", 7, head=head, summary=head[0]).read_text())
            self.assertEqual(len(self.listed(ack="7")["acknowledged"]), 1)
        self.assertEqual((acked / "declined-fix-7.json").read_text(), texts[2])
        self.assertEqual((acked / "declined-fix-7.json.1").read_text(), texts[0])
        self.assertEqual((acked / "declined-fix-7.json.2").read_text(), texts[1])
        self.assertEqual([p.name for p in acked.glob("*.json")], ["declined-fix-7.json"])
        self.assertEqual(self.listed(ack="7")["acknowledged"], [])
        self.assertEqual(len(list(acked.iterdir())), 3)

    def test_lookup_is_the_only_github_read_and_its_answers_stay_out_of_the_workers_record(self):
        config = self.on()
        token = self.root / "token"
        token.write_text("t")
        store.update_host(github_token_file=str(token))
        path = self.record("declined", "rebase", 412, summary="Subsumed-by: #398", subsumed_by=[398], mentions=[400, 401])
        other = self.record("declined", "fix", 413, summary="see #398", mentions=[398])
        text = path.read_text()
        self.listed()
        attention.prepare(config)
        self.assertEqual(self.github.call_count, 0)

        def answer(path, *, token_file=None):
            self.assertEqual(token_file, str(token))
            kind, number = path.split("/")[-2:]
            if number == "401":
                error = store.FleetError("GitHub GET returned HTTP 404")
                error.status = 404
                raise error
            if kind == "pulls":
                return dict(state="closed" if number == "413" else "open", merged_at=None, head=dict(sha="a" * 40),
                            title="Own", user=dict(login="author"))
            merged = "2026-01-14T10:00:00Z" if number == "398" else None
            return dict(state="closed" if merged else "open", title=f"Title {number}", user=dict(login="someone"),
                        pull_request=dict(merged_at=merged))

        self.github.side_effect = answer
        value = self.listed(lookup=True)
        self.assertEqual(sorted(c.args[0] for c in self.github.call_args_list), [
            f"repos/{REPOSITORY}/issues/{n}" for n in (398, 400, 401)] + [f"repos/{REPOSITORY}/pulls/{n}" for n in (412, 413)])
        self.assertEqual(value["lookup"], dict(asked=5, error=None))
        self.assertEqual([i["id"] for i in value["items"]], ["412"])
        self.assertEqual(value["cleared"][0]["cleared"], "closed")
        self.assertEqual([(n["pr"], n["state"]) for n in value["items"][0]["named"]],
                         [(398, "MERGED"), (400, "OPEN"), (401, "NOT FOUND")])
        self.assertEqual((path.read_text(), json.loads(other.read_text())["mentions"]), (text, [398]))
        self.assertIn(f"{REPOSITORY}#398", store.read(attention.notes_path(path.name))["prs"])
        self.github.reset_mock()
        code, output = self.cli("attention", "box", "--lookup")
        self.assertEqual(sorted(c.args[0].split("/")[-1] for c in self.github.call_args_list), ["400", "412"])
        self.assertIn("Asked GitHub about 2 pull requests", output)
        self.assertIn("   subsumed by: #398  MERGED  2026-01-14  Title 398  (someone)", output)
        self.github.side_effect = store.FleetError("GitHub request unavailable")
        value = self.listed(lookup=True)
        self.assertEqual(value["lookup"], dict(asked=1, error="GitHub request unavailable"))
        self.assertEqual(value["items"][0]["named"][0]["state"], "MERGED")

    def test_repeated_bounded_lookups_reach_every_record(self):
        self.on()
        for pr in range(1, 6):
            self.record("declined", "fix", pr, summary="no")
        self.github.side_effect = lambda path, **_: dict(state="open", merged_at=None, head=dict(sha="a" * 40))
        asked = []
        with patch.object(attention, "MAX_LOOKUPS", 2):
            for _ in range(3):
                self.github.reset_mock()
                self.assertEqual(self.listed(lookup=True)["lookup"]["asked"], 2)
                asked.append([int(c.args[0].split("/")[-1]) for c in self.github.call_args_list])
        self.assertEqual(asked, [[1, 2], [3, 4], [5, 1]])

    def test_only_a_missing_number_is_remembered_as_not_found(self):
        self.on()
        self.record("declined", "fix", 7, summary="see #8", mentions=[8])

        def answer(path, *, token_file=None):
            if "/pulls/" in path:
                return dict(state="open", merged_at=None, head=dict(sha="a" * 40))
            failure = urllib.error.HTTPError("https://api.github.com/" + path, status, "", {}, None)
            with patch("urllib.request.urlopen", side_effect=failure):
                return REQUEST(path)

        self.github.side_effect = answer
        status = 500
        value = self.listed(lookup=True)
        self.assertIn("HTTP 500", value["lookup"]["error"])
        self.assertNotIn("state", value["items"][0]["named"][0])
        status = 404
        value = self.listed(lookup=True)
        self.assertEqual((value["lookup"]["error"], value["items"][0]["named"][0]["state"]), (None, "NOT FOUND"))

    def test_status_shows_the_prepared_count_and_the_command(self):
        config = self.on()
        self.record("declined", "fix", 7, summary="no")
        self.record("declined", "rebase", 8, summary="no")
        self.record("budget-spent", "fix", 9, detail="spent")
        attention.prepare(config)
        with patch.object(host.ci_overview, "cached", return_value={}):
            value = host.rpc(dict(action="status"))
        self.assertEqual((value["attention"]["count"], value["attention"]["kinds"]), (3, {"declined": 2, "budget-spent": 1}))
        value["status"] = dict(active_worker_limit=0)
        quiet = dict(value, attention=dict(count=0, kinds={}, at=value["attention"]["at"]))
        failed = dict(value, attention=dict(error="OSError: unreadable"))
        for prepared, expected in ((value, "Needs you: 3 items (1 budget-spent, 2 declined)\n  tauceti-fleet attention box\n"),
                                   (quiet, None), (failed, "Needs you: unavailable; OSError: unreadable\n")):
            with (patch.object(main.__globals__["pool_activity"], "refresh", return_value={}),
                  patch.object(main.__globals__["activity"], "display")):
                code, text = self.cli("status", "box", call=lambda *_, value=prepared, **__: value)
            self.assertEqual(code, 0)
            if expected:
                self.assertIn(expected, text)
            else:
                self.assertNotIn("Needs you", text)
        with patch.object(attention, "listing", side_effect=OSError("unreadable")):
            attention.prepare(config)
        self.assertEqual(attention.prepared()["error"], "OSError: unreadable")

    def controller_pass(self):
        published, handlers = {}, {}

        def publish(path, value):
            published.setdefault(Path(path).name, value)
            if Path(path).name == "status.json":
                handlers[signal.SIGTERM]()

        gib = 1024**3
        with (
            patch.multiple(
                host,
                install_pressure_service=Mock(),
                resources=Mock(return_value=dict(memory_total=32 * gib, memory_available=30 * gib)),
                cpu_sample=Mock(return_value=(10, 9)),
                collect_ci_results=Mock(),
                worker_config=Mock(return_value=["w1"]),
                start_workers=Mock(),
                worker_statuses=Mock(return_value=[]),
                worker_memory=Mock(return_value=None),
                atomic=publish,
            ),
            patch.object(host.vm, "slots_in_use", return_value=[]),
            patch.object(host.vm, "outstanding", return_value=False),
            patch.object(host.CISlot, "step", return_value={}),
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
        ):
            host.daemon()
        return published["status.json"]

    def test_controller_prepares_the_count_only_when_set_and_survives_a_broken_directory(self):
        store.update_host(workers_enabled=True, max_workers=1)
        self.record("declined", "fix", 7, summary="no")
        unset = self.controller_pass()
        self.assertFalse(attention.summary_path().exists())
        self.on()
        self.assertEqual(self.controller_pass(), dict(unset, observed_at=ANY))
        self.assertEqual(attention.prepared()["count"], 1)
        with patch.object(attention, "gather", side_effect=OSError("unreadable")):
            status = self.controller_pass()
        self.assertEqual((status["error"], status["active_worker_limit"]), (None, 1))
        self.assertIn("unreadable", attention.prepared()["error"])


if __name__ == "__main__":
    unittest.main()
