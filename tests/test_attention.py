import fcntl
import http.client
import io
import json
import os
import signal
import sqlite3
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from tauceti_fleet import attention, claude_credentials, forgejo, github, host, remote, store
from tauceti_fleet.cli import main

REPOSITORY = "TauCetiProject/TauCeti"
REQUEST = github.request
FORGEJO_REQUEST = forgejo.request
DAY = 86400


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
        forge = patch.object(forgejo, "request", side_effect=AssertionError("Forgejo request"))
        self.forgejo = forge.start()
        self.addCleanup(forge.stop)
        store.host_config()

    def on(self, **settings):
        store.update_host(incidents=True, **settings)
        return store.host_config()

    def record(self, kind, stage, pr, *, head="a" * 40, at="2026-01-15T09:30:00Z", acked=False, suffix="", **fields):
        value = dict(schema="tauceti-incident/v1", kind=kind, key=f"{stage}-{pr}{suffix}", worker="w1", first_at=at,
                     last_at=at, count=1, repository=REPOSITORY, stage=stage, pr=pr, head=head) | fields
        path = attention.incidents_dir() / ("acked" if acked else "") / f"{kind}-{stage}-{pr}{suffix}.json"
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
        self.assertIn("Acknowledge with: tauceti-fleet attention box --ack <PR number> | <ack id> | all", text)
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

    def test_an_answer_counts_from_when_its_request_started(self):
        self.on()
        at = attention.epoch("2026-01-15T09:30:00Z")
        self.record("declined", "fix", 7, head="a" * 40, summary="A")
        clock = [at + 10]

        def answer(path, **_):
            clock[0] = at + 15
            self.record("declined", "fix", 7, head="b" * 40, summary="B",
                        at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at + 15)))
            clock[0] = at + 20
            return dict(state="open", merged_at=None, head=dict(sha="a" * 40))

        self.github.side_effect = answer
        with patch.object(attention.time, "time", side_effect=lambda: clock[0]):
            value = self.listed(lookup=True)
        self.assertEqual([(i["id"], i["head"]) for i in value["items"]], [("7", "b" * 40)])
        self.assertEqual(store.read(attention.notes_path("declined-fix-7.json"))["prs"][f"{REPOSITORY}#7"]["observed"],
                         at + 10)

    def test_an_answer_from_an_earlier_request_does_not_replace_a_later_one(self):
        self.on()
        at = attention.epoch("2026-01-15T09:30:00Z")
        self.record("declined", "fix", 7, summary="A")
        clock = [at + 10]

        def answer(path, **_):
            if clock[0] == at + 10:
                clock[0] = at + 20
                self.listed(lookup=True)
                return dict(state="closed", merged_at=None, head=dict(sha="a" * 40))
            return dict(state="open", merged_at=None, head=dict(sha="a" * 40))

        self.github.side_effect = answer
        with patch.object(attention.time, "time", side_effect=lambda: clock[0]):
            value = self.listed(lookup=True)
        self.assertEqual([i["id"] for i in value["items"]], ["7"])
        kept = store.read(attention.notes_path("declined-fix-7.json"))["prs"][f"{REPOSITORY}#7"]
        self.assertEqual((kept["state"], kept["observed"]), ("OPEN", at + 20))

    def test_overlapping_lookups_merge_their_answers_one_after_the_other(self):
        self.on()
        at = attention.epoch("2026-01-15T09:30:00Z")
        self.record("declined", "fix", 7, summary="A")
        clock, write, done = [at + 10], attention.atomic, {}

        def answer(path, **_):
            state = "closed" if clock[0] == at + 10 else "open"
            return dict(state=state, merged_at=None, head=dict(sha="a" * 40))

        def paused(path, value):
            if Path(path).parent == attention.notes_path("x").parent and "thread" not in done:
                clock[0] = at + 20
                done["thread"] = threading.Thread(target=lambda: self.listed(lookup=True))
                done["thread"].start()
                done["thread"].join(timeout=1)
            write(path, value)

        self.github.side_effect = answer
        with (patch.object(attention.time, "time", side_effect=lambda: clock[0]),
              patch.object(attention, "atomic", side_effect=paused)):
            self.listed(lookup=True)
            done["thread"].join()
        kept = store.read(attention.notes_path("declined-fix-7.json"))["prs"][f"{REPOSITORY}#7"]
        self.assertEqual((kept["state"], kept["observed"]), ("OPEN", at + 20))

    def test_an_observation_inside_the_records_write_second_is_not_newer(self):
        self.on()
        at = attention.epoch("2026-01-15T09:30:00Z")
        for pr, observed in ((1, at + 0.1), (2, at + 0.999), (3, at + 1), (4, at - 1)):
            self.record("declined", "fix", pr, head="b" * 40, summary="no")
            self.observe(pr, "OPEN", "a" * 40, observed)
        value = self.listed()
        self.assertEqual({c["id"]: c["cleared"] for c in value["cleared"]}, {"3": "new head"})
        self.assertEqual([(i["id"], i["observed_at"]) for i in value["items"]], [("1", None), ("2", None), ("4", None)])

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

    def test_the_sweep_waits_for_an_acknowledgement_holding_the_records_lock(self):
        config = self.on()
        path = self.record("declined", "fix", 1, summary="no")
        self.observe(1, "MERGED", "a" * 40, attention.epoch("2026-01-15T09:30:00Z") + 60)
        with attention.records_lock():
            sweeping = threading.Thread(target=attention.prepare, args=(config,))
            sweeping.start()
            sweeping.join(0.5)
            self.assertTrue(sweeping.is_alive())
            self.assertTrue(path.exists())
        sweeping.join(10)
        self.assertFalse(path.exists())
        self.assertEqual(json.loads((attention.incidents_dir() / "acked" / path.name).read_text())["overtaken"]["by"], "merged")

    def test_overlapping_acks_never_overwrite_an_archived_record(self):
        self.on()
        acked = attention.incidents_dir() / "acked"
        self.record("declined", "fix", 7, head="a" * 40, acked=True, summary="A")
        self.record("declined", "fix", 7, head="b" * 40, summary="B")
        link, done = os.link, {}

        def second():
            done["acknowledged"] = self.listed(ack="7")["acknowledged"]

        def paused(source, target):
            link(source, target)
            if "first" not in done:
                done["first"] = True
                overlapping = threading.Thread(target=second)
                overlapping.start()
                overlapping.join(timeout=1)
                self.record("declined", "fix", 7, head="c" * 40, summary="C")
                done["thread"] = overlapping

        with patch.object(attention.os, "link", side_effect=paused):
            first = self.listed(ack="7")["acknowledged"]
            done["thread"].join()
        archived = [json.loads(p.read_text())["summary"] for p in sorted(acked.iterdir())]
        self.assertEqual(len(archived), len(set(archived)), archived)
        self.assertEqual(len(first) + len(done["acknowledged"]), len(archived) - 1)
        self.assertEqual(json.loads((acked / "declined-fix-7.json").read_text())["summary"], archived[0])

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
        self.assertEqual(sorted(c.args[0].split("/")[-1] for c in self.github.call_args_list), ["400", "412", "413"])
        self.assertIn("Looked up 3 pull requests", output)
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

    def test_repeated_bounded_lookups_reach_every_named_pull_request(self):
        self.on()
        for pr in (1, 2, 3):
            self.record("declined", "fix", pr, summary=f"see #{pr + 10}", mentions=[pr + 10])

        def answer(path, **_):
            if "/pulls/" in path:
                return dict(state="open", merged_at=None, head=dict(sha="a" * 40))
            return dict(state="open", pull_request=dict(merged_at=None))

        self.github.side_effect = answer
        asked = []
        with patch.object(attention, "MAX_LOOKUPS", 3):
            for _ in range(3):
                self.github.reset_mock()
                self.listed(lookup=True)
                asked.append([c.args[0].split("/", 3)[-1] for c in self.github.call_args_list])
        self.assertEqual(asked, [["pulls/1", "issues/11", "pulls/2"], ["issues/12", "pulls/3", "issues/13"],
                                 ["pulls/1", "issues/11", "pulls/2"]])

    def test_bounded_lookups_resume_inside_an_item_that_names_more_than_one_call_can_ask(self):
        self.on()
        self.record("declined", "fix", 7, summary="see #11 #12 #13", mentions=[11, 12, 13])
        self.record("declined", "fix", 8, summary="no", at="2026-01-15T09:31:00Z")

        def answer(path, **_):
            if "/pulls/" in path:
                return dict(state="open", merged_at=None, head=dict(sha="a" * 40))
            return dict(state="open", pull_request=dict(merged_at=None))

        self.github.side_effect = answer
        asked = []
        with patch.object(attention, "MAX_LOOKUPS", 3):
            for _ in range(3):
                self.github.reset_mock()
                self.listed(lookup=True)
                asked.append([c.args[0].split("/", 3)[-1] for c in self.github.call_args_list])
        self.assertEqual(asked, [["pulls/7", "issues/11", "issues/12"], ["issues/13", "pulls/8", "pulls/7"],
                                 ["issues/11", "issues/12", "issues/13"]])
        self.assertEqual(sorted(store.read(attention.notes_path("declined-fix-7.json"))["prs"]),
                         [f"{REPOSITORY}#{n}" for n in (11, 12, 13, 7)])

    def test_an_unreadable_incident_directory_is_reported_and_not_counted_as_empty(self):
        config = self.on()
        self.record("declined", "fix", 7, summary="no")
        self.observe(7, "MERGED", "a" * 40, attention.epoch("2026-01-15T09:30:00Z") + 60)
        attention.incidents_dir().chmod(0o300)
        self.addCleanup(lambda: attention.incidents_dir().exists() and attention.incidents_dir().chmod(0o700))
        if os.access(attention.incidents_dir(), os.R_OK):
            self.skipTest("permissions do not apply to this user")
        with self.assertRaises(store.FleetError):
            self.listed()
        attention.prepare(config)
        self.assertIn("could not be listed", attention.prepared()["error"])
        attention.incidents_dir().chmod(0o700)
        self.assertTrue((attention.incidents_dir() / "declined-fix-7.json").exists())
        attention.incidents_dir().rename(self.root / "elsewhere")
        self.assertEqual(self.listed()["items"], [])

    def test_lookup_asks_again_about_a_closed_record_until_it_is_acknowledged(self):
        self.on()
        self.record("declined", "fix", 7, summary="no")
        self.record("declined", "fix", 8, summary="no")
        states = {"7": "closed", "8": "closed"}

        def answer(path, **_):
            number = path.split("/")[-1]
            return dict(state=states[number], merged_at="2026-01-16T00:00:00Z" if number == "8" else None,
                        head=dict(sha="a" * 40))

        self.github.side_effect = answer
        value = self.listed(lookup=True)
        self.assertEqual((value["lookup"]["asked"], value["items"]), (2, []))
        self.assertEqual(sorted(c["cleared"] for c in value["cleared"]), ["closed", "merged"])
        states["7"] = "open"
        self.github.reset_mock()
        value = self.listed(lookup=True)
        self.assertEqual([c.args[0].split("/")[-1] for c in self.github.call_args_list], ["7"])
        self.assertEqual([i["id"] for i in value["items"]], ["7"])

    def test_a_forgejo_record_beside_a_github_one_of_the_same_number_is_read_from_its_own_forge(self):
        self.on()
        origin = "https://forge.example"
        on_forgejo = dict(forge=dict(kind="forgejo", url=origin), repository_id=1, suffix=".0123456789ab")
        self.record("declined", "fix", 412, forge=dict(kind="github", url="https://github.com"), summary="github")
        path = self.record("declined", "fix", 412, summary="see #398", mentions=[398], **on_forgejo)
        self.record("declined", "fix", 413, forge=dict(kind="gitlab", url="https://gitlab.example"), suffix=".abc")
        self.observe(412, "MERGED", "a" * 40, attention.epoch("2026-01-15T09:30:00Z") + 1)
        value = self.listed()
        self.assertEqual([(c["id"], c["cleared"]) for c in value["cleared"]], [("412", "merged")])
        self.assertEqual([i["id"] for i in value["items"]], ["forge.example/TauCetiProject/TauCeti#412", "fix-413.abc"])
        code, output = self.cli("attention", "box")
        self.assertIn(f"== PR #412 on Forgejo {origin}  {origin}/TauCetiProject/TauCeti/pulls/412  (declined", output)
        self.assertIn("   ack id: forge.example/TauCetiProject/TauCeti#412", output)
        self.assertIn(f"   mentions: #398  {origin}/TauCetiProject/TauCeti/pulls/398  (--lookup", output)

        states = {"412": dict(state="closed", merged=False, merged_at=None, head=dict(sha="a" * 40))}

        def answer(at, path):
            self.assertEqual(at, origin)
            if path.endswith("issues/398"):
                return dict(state="closed", title="Replacement", user=dict(login="someone"),
                            pull_request=dict(merged=True, merged_at="2026-01-14T10:00:00Z"))
            return states[path.split("/")[-1]]

        self.forgejo.side_effect = answer
        value = self.listed(lookup=True)
        self.assertEqual([c.args for c in self.forgejo.call_args_list],
                         [(origin, f"repos/{REPOSITORY}/pulls/412"), (origin, f"repos/{REPOSITORY}/issues/398")])
        self.assertEqual(self.github.call_count, 0)
        self.assertEqual(sorted((c["id"], c["cleared"]) for c in value["cleared"]),
                         [("412", "merged"), ("forge.example/TauCetiProject/TauCeti#412", "closed")])
        states["412"] = dict(state="open", merged=False, merged_at=None, head=dict(sha="a" * 40))
        self.forgejo.reset_mock()
        value = self.listed(lookup=True)
        self.assertEqual([c.args[1] for c in self.forgejo.call_args_list], [f"repos/{REPOSITORY}/pulls/412"])
        item = value["items"][0]
        self.assertEqual((item["id"], item["named"][0]["state"]), ("forge.example/TauCetiProject/TauCeti#412", "MERGED"))

        self.assertEqual([d["record"] for d in self.listed(ack="412")["acknowledged"]], ["declined-fix-412.json"])
        text = path.read_text()
        self.listed(ack="forge.example/TauCetiProject/TauCeti#412")
        self.record("declined", "fix", 412, summary="again", **on_forgejo)
        self.listed(ack="forge.example/TauCetiProject/TauCeti#412")
        acked = attention.incidents_dir() / "acked"
        self.assertEqual((acked / f"{path.name}.1").read_text(), text)
        self.assertEqual(json.loads((acked / path.name).read_text())["summary"], "again")

    def test_the_sweep_archives_a_forgejo_record_merged_on_its_own_forge_and_not_its_github_namesake(self):
        config = self.on()
        origin = "https://forge.example"
        github_record = self.record("declined", "fix", 412, summary="github")
        on_forgejo = self.record("declined", "fix", 412, forge=dict(kind="forgejo", url=origin), repository_id=1,
                                 suffix=".0123456789ab", summary="forgejo")
        self.record("declined", "fix", 412, forge=dict(kind="forgejo", url=origin), repository_id=1,
                    suffix=".0123456789ab", acked=True, summary="earlier")
        self.observe(412, "OPEN", "a" * 40, attention.epoch("2026-01-15T09:30:00Z") + 60)
        self.forgejo.side_effect = lambda at, path: dict(state="closed", merged=True, merged_at="2026-01-16T00:00:00Z",
                                                         head=dict(sha="a" * 40))
        self.github.side_effect = lambda path, **_: dict(state="open", merged_at=None, head=dict(sha="a" * 40))
        self.listed(lookup=True)
        attention.prepare(config)
        acked = attention.incidents_dir() / "acked"
        self.assertTrue(github_record.exists())
        self.assertFalse(on_forgejo.exists())
        swept = json.loads((acked / on_forgejo.name).read_text())
        self.assertEqual((swept["summary"], swept["overtaken"]["by"]), ("forgejo", "merged"))
        self.assertEqual(json.loads((acked / f"{on_forgejo.name}.1").read_text())["summary"], "earlier")
        self.assertFalse((acked / github_record.name).exists())

    def test_a_failing_forge_leaves_the_other_forges_lookups_to_go_ahead(self):
        self.on()
        self.record("declined", "fix", 7, forge=dict(kind="forgejo", url="https://forge.example"), suffix=".0123456789ab")
        self.record("declined", "fix", 8, summary="no")
        self.forgejo.side_effect = store.FleetError("https://forge.example request unavailable")
        self.github.side_effect = lambda path, **_: dict(state="closed", merged_at=None, head=dict(sha="a" * 40))
        value = self.listed(lookup=True)
        self.assertEqual(value["lookup"], dict(asked=2, error="https://forge.example request unavailable"))
        self.assertEqual([c["id"] for c in value["cleared"]], ["8"])

    def test_forges_that_time_out_move_behind_the_requests_not_yet_asked(self):
        self.on()
        for pr, name in ((5, "a.example"), (6, "b.example")):
            self.record("declined", "fix", pr, at="2026-01-15T09:00:00Z", suffix=f".{pr:012d}",
                        forge=dict(kind="forgejo", url=f"https://{name}"))
        self.record("declined", "fix", 8, summary="no")
        clock = [0.0]

        def timeout(at, path):
            clock[0] += 20
            raise store.FleetError(f"{at} request unavailable; check network access and retry")

        self.forgejo.side_effect = timeout
        self.github.side_effect = lambda path, **_: dict(state="closed", merged_at=None, head=dict(sha="a" * 40))
        asked = []
        with patch.object(attention.time, "monotonic", side_effect=lambda: clock[0]):
            for _ in range(2):
                self.forgejo.reset_mock()
                self.github.reset_mock()
                value = self.listed(lookup=True)
                asked.append(([c.args[0] for c in self.forgejo.call_args_list], self.github.call_count))
        self.assertEqual(asked, [(["https://a.example", "https://b.example"], 0),
                                 (["https://a.example", "https://b.example"], 1)])
        self.assertEqual([c["id"] for c in value["cleared"]], ["8"])
        self.assertEqual([i["id"] for i in value["items"]], ["a.example/TauCetiProject/TauCeti#5",
                                                            "b.example/TauCetiProject/TauCeti#6"])
        self.assertEqual(sorted(attention.failed_origins()), ["https://a.example", "https://b.example"])
        self.forgejo.side_effect = lambda at, path: dict(state="open", merged=False, merged_at=None,
                                                         head=dict(sha="a" * 40))
        self.listed(lookup=True)
        self.assertEqual(attention.failed_origins(), {})

    def test_forgejo_reads_are_anonymous_and_keep_the_http_status(self):
        seen = []

        def opened(request, timeout):
            seen.append((request.full_url, request.headers))
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        with patch("urllib.request.urlopen", side_effect=opened):
            with self.assertRaises(store.FleetError) as raised:
                FORGEJO_REQUEST("https://forge.example", "repos/O/R/issues/9")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(seen[0][0], "https://forge.example/api/v1/repos/O/R/issues/9")
        self.assertNotIn("Authorization", seen[0][1])
        with self.assertRaises(store.FleetError):
            FORGEJO_REQUEST("https://forge.example/evil?", "x")

    def test_a_repository_its_forge_will_not_show_leaves_the_rest_of_that_forge_to_be_asked(self):
        self.on()
        origin = "https://forge.example"
        for pr, repository in ((1, "private/project"), (2, "public/project")):
            self.record("declined", "fix", pr, repository=repository, forge=dict(kind="forgejo", url=origin),
                        suffix=f".{pr:012d}")

        def answer(at, path):
            if path.startswith("repos/private/"):
                error = store.FleetError(f"{origin} GET returned HTTP 404")
                error.status = 404
                raise error
            return dict(state="closed", merged=False, merged_at=None, head=dict(sha="a" * 40))

        self.forgejo.side_effect = answer
        for _ in range(3):
            self.forgejo.reset_mock()
            value = self.listed(lookup=True)
            self.assertEqual(sorted(c.args[1] for c in self.forgejo.call_args_list),
                             ["repos/private/project/pulls/1", "repos/public/project/pulls/2"])
        self.assertEqual(value["lookup"]["error"], f"{origin} answered HTTP 404 for private/project#1")
        self.assertEqual([i["id"] for i in value["items"]], ["forge.example/private/project#1"])
        self.assertEqual(value["items"][0]["observed_at"], None)
        self.assertEqual([c["id"] for c in value["cleared"]], ["forge.example/public/project#2"])
        code, output = self.cli("attention", "box")
        self.assertIn("   its forge answered HTTP 404 for this pull request; --lookup reads Forgejo anonymously", output)

    def test_an_interrupted_response_is_a_fleet_error_on_both_forges(self):
        class Interrupted:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _):
                raise http.client.IncompleteRead(b"{")

        with patch("urllib.request.urlopen", return_value=Interrupted()):
            for call in (lambda: FORGEJO_REQUEST("https://forge.example", "repos/O/R/pulls/1"),
                         lambda: REQUEST("repos/O/R/pulls/1")):
                with self.assertRaises(store.FleetError):
                    call()

    def test_forgejo_origins_are_the_ones_the_worker_writes(self):
        self.on()
        for pr, url in ((5, "http://[::1]:3000"), (6, "http://forge.example"), (7, "https://Forge.example:443")):
            self.record("declined", "fix", pr, forge=dict(kind="forgejo", url=url), suffix=f".{pr:012d}")
        value = self.listed()
        self.assertEqual([i["id"] for i in value["items"]], ["[::1]:3000/TauCetiProject/TauCeti#5", "fix-6.000000000006",
                                                            "fix-7.000000000007"])
        self.forgejo.side_effect = lambda at, path: dict(state="open", merged=False, merged_at=None,
                                                         head=dict(sha="a" * 40))
        self.listed(lookup=True)
        self.assertEqual([c.args for c in self.forgejo.call_args_list],
                         [("http://[::1]:3000", f"repos/{REPOSITORY}/pulls/5")])
        seen = []

        def opened(request, timeout):
            seen.append(request.full_url)
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=opened), self.assertRaises(store.FleetError):
            FORGEJO_REQUEST("http://[::1]:3000", "repos/O/R/pulls/5")
        self.assertEqual(seen, ["http://[::1]:3000/api/v1/repos/O/R/pulls/5"])

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

    def controller_pass(self, workers=()):
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
                worker_statuses=Mock(return_value=list(workers)),
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

    def test_a_forgejo_halt_is_listed_with_its_forge_and_the_forgejo_remedy(self):
        store.update_host(workers_enabled=True, max_workers=1)
        store.atomic(store.state_dir() / "workers/w1.json", dict(
            id="w1", state="halted", alive=False, halt_reason="forgejo:invalid-credentials",
            failure_reason="Forgejo https://forge.example rejected the credential for user 3 (HTTP 401); issue a new token"))
        self.assertEqual([(i["id"], i["forge"]) for i in self.listed()["items"]], [("w1", "forgejo")])
        code, text = self.cli("attention", "box")
        self.assertIn("== w1  (halted)\n   Forgejo invalid-credentials: Forgejo https://forge.example rejected", text)
        self.assertIn("   Issue a new Forgejo token for the worker's user and write it to the file "
                      "TAUCETI_FORGEJO_TOKEN_FILE names on the host, then clear the halt", text)
        self.assertIn("   Next step: tauceti-fleet clear-halt box w1\n", text)
        self.assertNotIn("gh auth", text)

    def test_a_halted_worker_is_listed_with_its_reason_and_command_on_a_host_with_nothing_set(self):
        store.update_host(workers_enabled=True, max_workers=2)
        halted = dict(id="w2", state="halted", alive=False, detail="rc=84", halt_reason="account-halt",
                      failure_reason="GitHub reports the account suspended\x1b[2J")
        for row in (dict(id="w1", state="running", alive=True), halted):
            store.atomic(store.state_dir() / "workers" / f"{row['id']}.json", row)
        self.record("declined", "fix", 7, at="2026-01-15T09:30:00Z", summary="no")
        value = self.listed()
        self.assertEqual([(i["source"], i["id"], i["kind"], i["command"]) for i in value["items"]],
                         [("halt", "w2", "halted", ["clear-halt", "w2"])])
        code, text = self.cli("attention", "box")
        self.assertIn("== w2  (halted)\n   GitHub account-halt: GitHub reports the account suspended [2J\n"
                      "   Have GitHub lift the suspension or lock on the worker's account, then clear the halt", text)
        self.assertIn("   Next step: tauceti-fleet clear-halt box w2\n", text)
        self.assertNotIn("Acknowledge with", text)
        self.assertEqual(self.listed(ack="all")["acknowledged"], [])
        self.on()
        self.assertEqual([i["id"] for i in self.listed()["items"]], ["w2", "7"])
        self.assertIn("Acknowledge with", self.cli("attention", "box")[1])
        store.update_host(incidents=None)

        self.controller_pass(host.worker_statuses())
        with patch.object(host.ci_overview, "cached", return_value={}):
            self.assertEqual(host.rpc(dict(action="status"))["attention"], dict(at=ANY, count=1, kinds={"halted": 1}))
            self.controller_pass()
            self.assertNotIn("attention", host.rpc(dict(action="status")))
        self.assertFalse(attention.summary_path().exists())

    def test_claude_credentials_are_listed_when_due_or_lapsed_with_the_sign_in(self):
        def login(refresh):
            return json.dumps({"claudeAiOauth": dict(accessToken="access", refreshToken=refresh)})

        now = time.time()
        for kind in claude_credentials.KINDS:
            store.update_host(claude_billing="subscription", **{kind: str(claude_credentials.path(kind))})
        claude_credentials.install(claude_credentials.USAGE, login("one"), now - 10 * DAY)
        claude_credentials.install(claude_credentials.TOKEN, "sk-ant-oat01-token", now - 100 * DAY)
        self.assertEqual(self.listed()["items"], [])
        claude_credentials.install(claude_credentials.USAGE, login("two"), now - 26 * DAY)
        claude_credentials.install(claude_credentials.TOKEN, "sk-ant-oat01-token", now - 366 * DAY)
        items = self.listed()["items"]
        self.assertEqual([(i["source"], i["id"], i["kind"], i["text"]) for i in items], [
            ("credential", "claude-setup-token", "credential", "setup-token expired 1 days ago"),
            ("credential", "claude-usage-login", "credential", "usage login renews for another 2 days")])
        code, text = self.cli("attention", "box")
        self.assertIn("== claude-setup-token  (credential)\n   setup-token expired 1 days ago\n   Next step: Run claude setup-token", text)
        self.assertIn("   Next step: Sign in on this host: tauceti-fleet claude-usage-login;", text)
        attention.prepare(store.host_config())
        self.assertEqual(attention.prepared()["kinds"], {"credential": 2})
        claude_credentials.install(claude_credentials.TOKEN, "sk-ant-oat01-token", now)
        store.update_host(claude_usage_credentials_file=None)
        self.assertEqual([e["status"] for e in claude_credentials.report(store.host_config())], ["ok", "warning"])
        self.assertEqual(self.listed()["items"], [])

    def test_credentials_are_listed_without_waiting_for_a_renewal_in_progress(self):
        login = json.dumps({"claudeAiOauth": dict(accessToken="access", refreshToken="one")})
        store.update_host(claude_billing="subscription", **{k: str(claude_credentials.path(k)) for k in claude_credentials.KINDS})
        claude_credentials.install(claude_credentials.USAGE, login, time.time() - 26 * DAY)
        claude_credentials.install(claude_credentials.TOKEN, "sk-ant-oat01-token", time.time())
        held = claude_credentials.path(claude_credentials.USAGE)
        # The worker's lock name, held as a renewal holds it across its token exchange.
        with held.with_name(".credentials.json.refresh.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            passing = threading.Thread(target=attention.prepare, args=(store.host_config(),))
            passing.start()
            passing.join(3)
            self.assertFalse(passing.is_alive())
            self.assertEqual(attention.prepared()["kinds"], {"credential": 1})
            self.assertEqual([i["id"] for i in self.listed()["items"]], ["claude-usage-login"])

    def test_controller_archives_overtaken_records_as_overtaken_without_asking_github(self):
        config = self.on(workers_enabled=True, max_workers=1)
        at = attention.epoch("2026-01-15T09:30:00Z")
        for pr in (1, 2, 3, 4, 5):
            self.record("declined", "fix", pr, summary="no")
        text = (attention.incidents_dir() / "declined-fix-1.json").read_text()
        self.observe(1, "MERGED", "a" * 40, at + 60)
        self.observe(2, "CLOSED", "a" * 40, at + 60)
        self.observe(3, "OPEN", "b" * 40, at + 60)
        self.observe(4, "OPEN", "a" * 40, at + 60)
        store.atomic(attention.notes_path("declined-fix-1.json"), dict(version=1, prs={}))
        self.listed(ack="5")
        self.controller_pass()
        acked = attention.incidents_dir() / "acked"
        self.assertEqual(sorted(p.name for p in attention.incidents_dir().glob("*.json")),
                         ["declined-fix-2.json", "declined-fix-3.json", "declined-fix-4.json"])
        self.assertEqual({p.name: json.loads(p.read_text()).get("overtaken", {}).get("by") for p in acked.iterdir()},
                         {"declined-fix-1.json": "merged", "declined-fix-5.json": None})
        swept = json.loads((acked / "declined-fix-1.json").read_text())
        self.assertEqual(dict(swept, overtaken=None), dict(json.loads(text), overtaken=None))
        self.assertIsNotNone(attention.epoch(swept["overtaken"]["at"]))
        self.assertFalse(attention.notes_path("declined-fix-1.json").exists())
        self.assertEqual((attention.prepared().get("error"), attention.prepared()["count"]), (None, 1))
        self.assertEqual(sorted(c["cleared"] for c in self.listed()["cleared"]), ["closed", "new head"])

        self.observe(4, "MERGED", "a" * 40, at + 120)
        judged = [i for i in attention.incident_items(config) if i.get("cleared")]
        later = self.record("declined", "fix", 4, at="2026-01-15T12:00:00Z", summary="again")
        attention.incident_sweep(config, judged)
        self.assertEqual(json.loads(later.read_text())["summary"], "again")
        self.assertFalse((acked / "declined-fix-4.json").exists())
        self.assertEqual(self.github.call_count, 0)

    def test_sweep_keeps_a_record_the_worker_rewrites_while_the_old_one_is_archived(self):
        config = self.on()
        at = attention.epoch("2026-01-15T09:30:00Z")
        self.record("declined", "fix", 1, summary="old")
        self.record("declined", "fix", 1, acked=True, summary="earlier")
        self.observe(1, "MERGED", "a" * 40, at + 60)
        judged = [i for i in attention.incident_items(config) if i.get("cleared")]
        write = store.atomic

        def worker_writes_first(path, value):
            self.record("declined", "fix", 1, head="b" * 40, at="2026-01-15T09:32:00Z", summary="new")
            write(path, value)

        with patch.object(attention, "atomic", worker_writes_first):
            attention.incident_sweep(config, judged)
        acked = attention.incidents_dir() / "acked"
        live = json.loads((attention.incidents_dir() / "declined-fix-1.json").read_text())
        self.assertEqual((live["summary"], live["head"]), ("new", "b" * 40))
        self.assertEqual(json.loads((acked / "declined-fix-1.json").read_text())["overtaken"]["by"], "merged")
        self.assertEqual(json.loads((acked / "declined-fix-1.json.1").read_text())["summary"], "earlier")

    def test_a_closed_record_stays_live_through_the_sweep_and_is_listed_when_it_reopens(self):
        config = self.on()
        self.record("declined", "fix", 7, summary="no")
        state = dict(value="closed")
        self.github.side_effect = lambda path, **_: dict(state=state["value"], merged_at=None, head=dict(sha="a" * 40))
        self.assertEqual(self.listed(lookup=True)["items"], [])
        attention.prepare(config)
        self.assertTrue((attention.incidents_dir() / "declined-fix-7.json").exists())
        state["value"] = "open"
        self.assertEqual([i["id"] for i in self.listed(lookup=True)["items"]], ["7"])

    def test_sweep_puts_back_a_record_rewritten_before_it_moved_and_restores_the_earlier_one(self):
        config = self.on()
        at = attention.epoch("2026-01-15T09:30:00Z")
        self.record("declined", "fix", 1, summary="old")
        self.record("declined", "fix", 1, acked=True, summary="earlier")
        self.observe(1, "MERGED", "a" * 40, at + 60)
        judged = [i for i in attention.incident_items(config) if i.get("cleared")]
        self.record("declined", "fix", 1, head="b" * 40, at="2026-01-15T09:32:00Z", summary="new")
        attention.incident_sweep(config, judged)
        acked = attention.incidents_dir() / "acked"
        self.assertEqual(json.loads((attention.incidents_dir() / "declined-fix-1.json").read_text())["summary"], "new")
        self.assertEqual(sorted(p.name for p in acked.iterdir()), ["declined-fix-1.json"])
        self.assertEqual(json.loads((acked / "declined-fix-1.json").read_text())["summary"], "earlier")

    def test_incident_and_credential_variables_share_one_env_table(self):
        with patch.object(host, "install_service"):
            host.configure(dict(profile="test", ci_allowed=False, incidents=True, claude_billing="subscription",
                                env={"A": "1"}, credentials=dict(claude_token_file="sk-ant-oat01-token")))
            host.worker_config(store.host_config())
            worker = tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"][0]
            self.assertEqual(sorted(worker["env"]), ["A", "CLAUDE_CONFIG_DIR", "TAUCETI_HOLD_DECLINED", "TAUCETI_INCIDENTS_DIR"])
            with self.assertRaisesRegex(store.FleetError, "env must not set TAUCETI_HOLD_DECLINED or TAUCETI_INCIDENTS_DIR"):
                host.configure(dict(env={"TAUCETI_INCIDENTS_DIR": "/elsewhere"}))
            host.configure(dict(incidents=False, env={"TAUCETI_INCIDENTS_DIR": "/elsewhere"}))


if __name__ == "__main__":
    unittest.main()
