import http.client
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

from tauceti_fleet import attention, forgejo, github, host, remote, store
from tauceti_fleet.cli import main

REPOSITORY = "TauCetiProject/TauCeti"
REQUEST = github.request
FORGEJO_REQUEST = forgejo.request


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
        attention.incidents_dir().chmod(0o300)
        self.addCleanup(lambda: attention.incidents_dir().exists() and attention.incidents_dir().chmod(0o700))
        if os.access(attention.incidents_dir(), os.R_OK):
            self.skipTest("permissions do not apply to this user")
        with self.assertRaises(store.FleetError):
            self.listed()
        attention.prepare(config)
        self.assertIn("could not be listed", attention.prepared()["error"])
        attention.incidents_dir().chmod(0o700)
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

    def test_a_failing_forge_leaves_the_other_forges_lookups_to_go_ahead(self):
        self.on()
        self.record("declined", "fix", 7, forge=dict(kind="forgejo", url="https://forge.example"), suffix=".0123456789ab")
        self.record("declined", "fix", 8, summary="no")
        self.forgejo.side_effect = store.FleetError("https://forge.example request unavailable")
        self.github.side_effect = lambda path, **_: dict(state="closed", merged_at=None, head=dict(sha="a" * 40))
        value = self.listed(lookup=True)
        self.assertEqual(value["lookup"], dict(asked=2, error="https://forge.example request unavailable"))
        self.assertEqual([c["id"] for c in value["cleared"]], ["8"])

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
