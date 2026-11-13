import contextlib
import io
import json
import os
import signal
import subprocess
import tempfile
import tomllib
import unittest
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_fleet import capabilities, host, outside_list, pool_activity, remote, store, subscription_usage, targets
from tauceti_fleet.status_follow import render_with_cli
from tauceti_fleet.store import FleetError

GIB = 1024**3
HOUR = 3600
BOUNDS = dict(floor=0, ceiling=40, start=4)


def simulate(*, demand, cost=0.07, hours=168, begin=0, slot=None, used=0.0, blind=(0, 0), session=lambda t: 60.0, bounds=BOUNDS,
             pace="0:0,100:100"):
    """A toy fleet under the pacer, one row per 15-minute step from hour `begin` of a weekly window.

    Twelve workers. `demand(t)` of them have list work at hour t; an outside-list
    round takes 1.5 h and leaves a pull request open for a day, authoring stops
    at the cap as a worker's cutoff does, and every six open pull requests keep
    one worker on maintenance. Each running round costs `cost` percent of the
    week per hour, usage is read in whole percent five minutes late and not at
    all during the `blind` hours, no round starts while usage is at the `pace`
    curve's budget, and no round runs once the week is spent.
    """
    state, opened, authoring, rows, reading = {}, [], [], [], None
    curve = outside_list.pace_points(dict(pace=pace))
    end = hours if slot is None else slot
    for index in range(begin * 4, end * 4):
        t = index / 4
        opened = [merged for merged in opened if merged > t] + [t + 24 for done in authoring if done <= t]
        authoring = [done for done in authoring if done > t]
        starting = used < outside_list.budget(curve, t / hours * 100)
        listed = min(12, demand(t)) if starting else 0
        other = min(12 - listed, round(len(opened) / 6)) if starting else 0
        cap = state.get("cap", bounds["start"])
        while starting and len(authoring) + len(opened) < cap and listed + other + len(authoring) < 12:
            authoring.append(t + 1.5)
        running = (listed, len(authoring), other) if used < 100 else (0, 0, 0)
        used = min(100.0, used + sum(running) * cost / 4)
        if not blind[0] <= t < blind[1]:
            reading = dict(fetched_at=t * HOUR - 300, used=float(int(used)), resets_at=hours * HOUR)
        state = outside_list.advance(
            state, now=t * HOUR, bounds=bounds, enabled=True, slot_seconds=None if slot is None else (slot - t) * HOUR,
            reading=reading, session_left=session(t), rounds=running, curve=curve,
        )
        rows.append(dict(t=t, used=used, cap=state["cap"], outside=running[1], reason=state["reason"]))
    return rows


# name, simulation, hour from which no outside-list round may run (None: the week must be spent, and not early).
# Workers paced on the line used% < elapsed%, except where the list alone would outrun the week, which a pacing curve forbids.
WEEKS = [
    ("a list that keeps three workers busy", dict(demand=lambda t: 3), None),
    ("no list work at all", dict(demand=lambda t: 0), None),
    ("list work that dries up on the third day", dict(demand=lambda t: 8 if t < 72 else 1), None),
    ("a slot ending four hours before the window", dict(demand=lambda t: 3, cost=0.12, begin=100, slot=164, used=30.0), None),
    ("no reading for ten hours", dict(demand=lambda t: 3, blind=(30, 40)), None),
    ("list work that alone outruns the week", dict(demand=lambda t: 10, pace="0:100"), 12),
    ("list work that arrives on the third day", dict(demand=lambda t: 0 if t < 72 else 9, pace="0:100"), 80),
]


class ControlLaw(unittest.TestCase):
    def test_one_step_moves_a_tenth_toward_the_plan(self):
        # 100 h left; list work spends 0.2 %/h, so 25 % is reserved and the plan for outside-list work is (left - 25) / 100.
        def step(cap, outside, **changes):
            values = dict(left=65.0, hours=100.0, listed=0.2, outside=outside, cutting=True, session_left=60.0, floor=2, ceiling=40)
            return outside_list.step(cap, **dict(values, **changes))

        for cap, outside, changes, expected, reason in [
            (20, 0.2, {}, 22, "spending under plan"),
            (3, 0.2, {}, 4, "spending under plan"),
            (39, 0.2, {}, 40, "spending under plan"),
            (20, 0.2, dict(session_left=9.0), 20, "spending under plan, session window nearly exhausted"),
            (20, 0.2, dict(session_left=None), 22, "spending under plan"),
            (20, 0.33, {}, 20, "on plan"),
            (20, 0.47, {}, 20, "on plan"),
            (20, 0.6, {}, 18, "spending over plan"),
            (20, 0.6, dict(session_left=9.0), 18, "spending over plan"),
            (20, 0.6, dict(cutting=False), 20, "spending over plan, no outside-list round running to cut"),
            (3, 0.6, {}, 2, "spending over plan"),
            (2, 0.6, {}, 2, "spending over plan"),
            (20, 0.0, dict(left=25.0), 18, "list work is projected to use all that may still be spent"),
            (20, 0.0, dict(left=20.0, cutting=False), 18, "list work is projected to use all that may still be spent"),
        ]:
            with self.subTest(cap=cap, outside=outside, **changes):
                result = step(cap, outside, **changes)
                self.assertEqual((result["cap"], result["reason"]), (expected, reason))
        result = step(20, 0.3)
        self.assertEqual((result["reserve"], result["projected"]), (25.0, 85.0))

    def test_cap_holds_without_a_usable_reading(self):
        now = 50 * HOUR
        fresh = dict(fetched_at=now - 60, used=30.0, resets_at=168 * HOUR)
        history = dict(cap=10, resets_at=168 * HOUR, readings=[[now - 3 * HOUR, 29.4], [now - 900, 29.9]],
                       rounds=[[now - 900, 3, 1, 2]])

        def advance(state=history, **changes):
            values = dict(now=now, bounds=BOUNDS, enabled=True, slot_seconds=None, reading=fresh, session_left=60.0, rounds=(3, 1, 2),
                          curve=outside_list.pace_points(dict(pace="0:100")))
            return outside_list.advance(state, **dict(values, **changes))

        moving = advance()
        self.assertEqual((moving["held"], moving["cap"], moving["previous"], moving["reason"]), (False, 11, 10, "spending under plan"))
        self.assertEqual((moving["ends"], moving["ends_at"], moving["outside_share"]), ("weekly window", 168 * HOUR, 0.25))
        self.assertAlmostEqual(moving["rate"], 0.6 / (3 - 1 / 60))
        self.assertEqual(advance(slot_seconds=10 * HOUR)["ends"], "slot")
        self.assertEqual(advance(slot_seconds=200 * HOUR)["ends"], "weekly window")
        for changes, reason in [
            (dict(reading=None), "no usage reading yet"),
            (dict(reading=dict(fresh, fetched_at=now - 1900)), "the usage reading is 31 minutes old"),
            (dict(reading=dict(fresh, used=None)), "the usage reading has no weekly figure"),
            (dict(reading=dict(fresh, used=None, resets_at=now - 60)), "the weekly window has reset since the last usage reading"),
            (dict(reading=dict(fresh, resets_at=None)), "the usage reading has no weekly figure"),
            (dict(reading=dict(fresh, used=2.0, resets_at=336 * HOUR)), "under 2 h of usage readings in this weekly window"),
            (dict(reading=dict(fresh, used=2.0)), "under 2 h of usage readings in this weekly window"),
            (dict(state=dict(history, readings=history["readings"][1:])), "under 2 h of usage readings in this weekly window"),
            (dict(state={}), "under 2 h of usage readings in this weekly window"),
            (dict(slot_seconds=0.0), "outside the weekly schedule"),
            (dict(enabled=False), "workers are not enabled"),
        ]:
            with self.subTest(reason=reason, **changes):
                held = advance(**changes)
                self.assertEqual((held["held"], held["reason"]), (True, reason))
                self.assertEqual(held["cap"], 10 if changes.get("state", history) else BOUNDS["start"])
        # A reset, a new slot or a disabled host starts the measurement again; a late reading keeps it.
        self.assertEqual(advance(reading=dict(fresh, used=2.0))["readings"], [[now - 60, 2.0]])
        self.assertEqual(advance(reading=dict(fresh, used=2.0, resets_at=336 * HOUR))["rounds"], [[now, 3, 1, 2]])
        self.assertEqual(advance(slot_seconds=0.0)["readings"], [])
        self.assertEqual(advance(reading=dict(fresh, fetched_at=now - 1900))["readings"], history["readings"])
        # 40% used 60 h into the window: the default curve allows 23.8% and the slot ends before it allows 40%.
        late = dict(fresh, used=40.0, fetched_at=now + 10 * HOUR - 60)
        state = dict(history, readings=[[now + 7 * HOUR, 39.4], [now + 10 * HOUR - 900, 39.9]], resets_at=168 * HOUR)
        curved = advance(state=state, now=now + 10 * HOUR, reading=late, slot_seconds=HOUR, curve=outside_list.pace_points({}))
        self.assertEqual((curved["cap"], curved["reason"]), (9, "list work is projected to use all that may still be spent"))
        self.assertEqual(outside_list.budget(outside_list.pace_points(dict(pace="0:10,50:70")), 25), 40)
        self.assertEqual(advance(state=dict(history, cap=90))["previous"], 40)
        idle = dict(fresh, used=0.0, resets_at=None)
        opening = advance(reading=idle)
        self.assertEqual((opening["held"], opening["cap"], opening["readings"], opening["reason"]),
                         (False, 11, [], "the weekly window is not open yet, spending under plan"))
        self.assertEqual(advance(reading=idle, slot_seconds=10 * HOUR)["ends_at"], now + 10 * HOUR)
        self.assertEqual(advance(reading=idle, session_left=5.0)["cap"], 10)
        self.assertEqual(advance(reading=dict(idle, fetched_at=now - 1900))["reason"], "the usage reading is 31 minutes old")

    def test_session_window_over_its_pacing_curve_does_not_raise_the_cap(self):
        now, resets = 50 * HOUR, 70 * HOUR
        state = dict(cap=12, resets_at=resets, readings=[[now - 3 * HOUR, 69.4], [now - 900, 69.9]], rounds=[[now - 900, 0, 0, 0]])
        values = dict(now=now, bounds=BOUNDS, enabled=True, slot_seconds=None, reading=dict(fetched_at=now - 60, used=70.0, resets_at=resets),
                      session_left=20.0, rounds=(0, 0, 0), curve=outside_list.pace_points({}))
        self.assertEqual(outside_list.advance(state, **values)["cap"], 13)
        # One hour into a five-hour session at 80% used, the default curve allows 13.3%.
        session = dict(used=80.0, resets_at=now + 4 * HOUR)
        held = outside_list.advance(state, **values, session=session)
        self.assertEqual((held["cap"], held["reason"]), (12, "spending under plan, the workers' pacing curve starts no round now"))
        self.assertEqual(outside_list.advance(state, **values, session=dict(used=0.0, resets_at=None))["cap"], 13)
        opening = outside_list.advance(state, **dict(values, reading=dict(fetched_at=now - 60, used=0.0, resets_at=None)), session=session)
        self.assertEqual(opening["cap"], 12)

    def test_rounds_are_kept_while_the_reading_is_held(self):
        now = 50 * HOUR
        state = dict(cap=14, resets_at=100 * HOUR, readings=[[now - 6 * HOUR, 0.0], [now - 4 * HOUR, 2.0]],
                     rounds=[[now - 6 * HOUR + 900 * (n + 1), 1, 0, 0] for n in range(8)])
        values = dict(now=now, bounds=BOUNDS, enabled=True, slot_seconds=None, session_left=60.0, curve=outside_list.pace_points(dict(pace="0:100")))
        for n in range(15):
            at = now - 4 * HOUR + 900 * (n + 1)
            state = outside_list.advance(state, **dict(values, now=at), reading=None, rounds=(0, 1, 0))
            self.assertEqual(state["reason"], "no usage reading yet")
        self.assertEqual(len(state["rounds"]), 23)
        stepped = outside_list.advance(state, **values, reading=dict(fetched_at=now - 60, used=6.0, resets_at=100 * HOUR), rounds=(0, 1, 0))
        self.assertAlmostEqual(stepped["outside_share"], 16 / 24)
        # Two hours of list work and four of outside-list work at 1%/h, 50 h left: a third of 50%, plus a quarter, reserved.
        self.assertAlmostEqual(stepped["reserve"], 50 / 3 * 1.25, delta=0.1)

    def test_rounds_before_the_first_reading_of_the_rate_are_not_split(self):
        # Four hours of list work under a held reading, then two of outside-list work measured from a new first reading.
        now, resets = 50 * HOUR, 123 * HOUR
        rounds = [[now - 6 * HOUR + 900 * (n + 1), 1, 0, 0] for n in range(16)] + [[now - 2 * HOUR + 900 * (n + 1), 0, 2, 0] for n in range(7)]
        state = dict(cap=40, resets_at=resets, readings=[[now - 2 * HOUR, 25.0]], rounds=rounds)
        stepped = outside_list.advance(state, now=now, bounds=BOUNDS, enabled=True, slot_seconds=None, session_left=60.0, rounds=(0, 2, 0),
                                       curve=outside_list.pace_points({}), reading=dict(fetched_at=now, used=27.0, resets_at=resets))
        self.assertEqual((stepped["outside_share"], stepped["reserve"], stepped["cap"]), (1.0, 0.0, 40))
        self.assertEqual(len(stepped["rounds"]), 24)

    def test_simulated_weeks(self):
        for name, simulation, quiet_from in WEEKS:
            with self.subTest(name):
                rows = simulate(**simulation)
                self.assertTrue(all(BOUNDS["floor"] <= row["cap"] <= BOUNDS["ceiling"] for row in rows))
                self.assertTrue(all(abs(after["cap"] - before["cap"]) <= max(1, round(before["cap"] / 10)) for before, after in pairwise(rows)))
                if quiet_from is None:
                    self.assertGreaterEqual(rows[-1]["used"], 95)
                    self.assertLess(rows[-16]["used"], 100)
                else:
                    self.assertEqual({row["outside"] for row in rows if row["t"] >= quiet_from}, {0})
                    self.assertEqual(rows[-1]["cap"], BOUNDS["floor"])

    def test_simulated_week_under_the_default_pacing_curve_does_not_raise_a_cap_it_cannot_use(self):
        rows = simulate(demand=lambda t: 3, pace=outside_list.DEFAULT_PACE)
        paused = "spending under plan, the workers' pacing curve starts no round now"
        self.assertIn(paused, {row["reason"] for row in rows})
        self.assertTrue(all(after["cap"] == before["cap"] for before, after in pairwise(rows) if after["reason"] == paused))
        # The curve allows 40% by hour 100.8, so the rest is spent at the fleet's full rate and the week is not quite used up.
        self.assertGreater(rows[-1]["used"], 90)

    def test_simulated_week_holds_through_a_blind_spell_and_a_spent_session(self):
        blind = simulate(demand=lambda t: 3, blind=(30, 40))
        self.assertEqual({row["cap"] for row in blind if 30 <= row["t"] < 40}, {blind[4 * 30]["cap"]})
        self.assertEqual({row["reason"] for row in blind if 30.25 <= row["t"] < 40},
                         {f"the usage reading is {minutes} minutes old" for minutes in range(35, 606, 15)})
        spent = simulate(demand=lambda t: 3, session=lambda t: 5.0 if 50 <= t < 60 else 60.0)
        during = [row["cap"] for row in spent if 49.75 <= row["t"] < 60]
        self.assertEqual(during, sorted(during, reverse=True))
        self.assertIn("spending under plan, session window nearly exhausted", {row["reason"] for row in spent})


class Wiring(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environ = patch.dict(os.environ, {"HOME": str(self.root / "home"), **{
            "TAUCETI_FLEET_" + key.upper(): str(self.root / key) for key in ("config", "state", "data")}})
        environ.start()
        self.addCleanup(environ.stop)
        self.protocol = dict(version=1, roadmap_targets=1, targets_fallback=1)
        self.manager, self.manager_protocol = None, None
        probe = patch.object(capabilities, "worker_protocol",
                             side_effect=lambda worker=None: self.protocol if worker is None else self.manager_protocol)
        probe.start()
        self.addCleanup(probe.stop)
        manager = patch.object(targets, "running_manager", side_effect=lambda: self.manager)
        manager.start()
        self.addCleanup(manager.stop)
        store.update_host(github_account="operator", ci_allowed=False, max_workers=2, claude_billing="subscription")

    def pace(self, **bounds):
        store.update_host(outside_list_cap=dict(dict(ceiling=40, start=4), **bounds), roadmap_targets="/srv/list.md")

    def definitions(self):
        host.worker_config(store.host_config())
        return (store.config_dir() / "workers.toml").read_text()

    def controller_pass(self, readings, workers=()):
        published, handlers = {}, {}

        def publish(path, value):
            published.setdefault(Path(path).name, value)
            if Path(path).name == "status.json":
                handlers[signal.SIGTERM]()

        with (
            patch.multiple(
                host,
                install_pressure_service=Mock(),
                resources=Mock(return_value=dict(memory_total=32 * GIB, memory_available=30 * GIB)),
                cpu_sample=Mock(return_value=(10, 9)),
                collect_ci_results=Mock(),
                start_workers=Mock(),
                worker_statuses=Mock(return_value=list(workers)),
                worker_memory=Mock(return_value=None),
                atomic=publish,
            ),
            patch.object(host.vm, "slots_in_use", return_value=[]),
            patch.object(host.vm, "outstanding", return_value=False),
            patch.object(host.CISlot, "step", return_value={}),
            patch.object(host.signal, "signal", side_effect=handlers.__setitem__),
            patch.object(host.targets, "status", return_value=None),
            patch.object(subscription_usage, "readings", return_value=readings),
            contextlib.redirect_stderr(io.StringIO()) as log,
        ):
            host.daemon()
        return published["status.json"]["outside_list_cap"], log.getvalue()

    def test_profile_key_is_validated_and_needs_a_subscription(self):
        store.host_config()
        with patch.object(host, "install_service"):
            for value in (
                {}, 12, dict(floor=1), dict(ceiling=0), dict(ceiling=5000), dict(ceiling=8, floor=9), dict(ceiling=8, start=9),
                dict(ceiling=8, floor=2, start=1), dict(ceiling=8, floor=-1), dict(ceiling=8.0), dict(ceiling=True), dict(ceiling=8, step=2),
            ):
                with self.subTest(value=value), self.assertRaisesRegex(FleetError, "outside_list_cap"):
                    host.configure(dict(outside_list_cap=value))
            with self.assertRaisesRegex(FleetError, "subscription"):
                host.configure(dict(outside_list_cap=dict(ceiling=8), claude_billing=None))
            self.assertNotIn("outside_list_cap", store.host_config())
            host.configure(dict(outside_list_cap=dict(ceiling=8)))
            self.assertEqual(outside_list.settings(store.host_config()), dict(floor=0, ceiling=8, start=0))
            store.update_host(workers_enabled=True)
            host.configure(dict(outside_list_cap=dict(ceiling=8)))
            with self.assertRaisesRegex(FleetError, "Drain"):
                host.configure(dict(outside_list_cap=dict(ceiling=9)))
        store.atomic(store.config_dir() / "profiles.json", {"paced": dict(github_account="operator", outside_list_cap=dict(ceiling=8)), "plain": {}})
        with patch.object(remote, "run", return_value=subprocess.CompletedProcess([], 1, stdout="")):
            self.assertEqual(remote.profile("paced")["outside_list_cap"], dict(ceiling=8))
            self.assertIsNone(remote.profile("plain")["outside_list_cap"])

    def test_definitions_gain_the_cap_only_with_a_list_and_a_worker_that_knows_it(self):
        plain = self.definitions()
        self.assertNotIn("targets_fallback", plain)
        store.update_host(outside_list_cap=dict(ceiling=40))
        self.assertEqual(self.definitions(), plain)
        self.pace()
        workers = tomllib.loads(self.definitions())["workers"]
        self.assertEqual(len(workers), 2)
        for worker in workers:
            self.assertEqual((worker["targets_fallback_max_open"], worker["targets_fallback_cap_file"], worker["roadmap_targets"]),
                             (4, str(self.root / "state/outside-list-cap"), "/srv/list.md"))
        self.pace(start=0)
        self.assertEqual(tomllib.loads(self.definitions())["workers"][0]["targets_fallback_max_open"], 1)
        self.protocol = dict(version=1, roadmap_targets=1)
        self.assertNotIn("targets_fallback", self.definitions())
        with (
            patch.object(capabilities, "resources", return_value=dict(cpus=8, memory_total=64 * GIB, memory_available=64 * GIB)),
            patch.object(capabilities, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")),
            patch.object(capabilities, "executable", return_value=None),
            patch.object(capabilities.os, "open", side_effect=PermissionError),
        ):
            check = {c["name"]: c for c in capabilities.doctor(store.host_config(), probe=False)["checks"]}["outside-list-cap"]
            self.protocol["targets_fallback"] = 1
            ready = {c["name"]: c for c in capabilities.doctor(store.host_config(), probe=False)["checks"]}["outside-list-cap"]
        self.assertEqual((check["status"], ready["status"]), ("warning", "ok"))

    def test_a_running_manager_without_fallback_support_gets_no_cap_keys(self):
        self.pace()
        self.manager, self.manager_protocol = "/old/bin/tauceti", dict(version=1, roadmap_targets=1)
        store.update_host(outside_list_cap=dict(ceiling=40, start=4))
        self.assertNotIn("targets_fallback", self.definitions())
        pacer = outside_list.Pacer()
        block = pacer.tick(store.host_config(), list, now=1000.0)
        self.assertEqual(block, dict(active=False, reason=(
            "the running worker manager started from a TauCetiWorker that cannot author outside a list; " + targets.RESTART_MANAGER)))
        self.assertFalse(outside_list.cap_file().exists())
        with (
            patch.object(capabilities, "resources", return_value=dict(cpus=8, memory_total=64 * GIB, memory_available=64 * GIB)),
            patch.object(capabilities, "run", return_value=subprocess.CompletedProcess([], 0, stdout="0")),
            patch.object(capabilities, "executable", return_value=None),
            patch.object(capabilities.os, "open", side_effect=PermissionError),
        ):
            check = {c["name"]: c for c in capabilities.doctor(store.host_config(), probe=False)["checks"]}["outside-list-cap"]
        self.assertEqual(check["status"], "warning")
        self.assertTrue(check["evidence"].startswith("The running worker manager started from a TauCetiWorker that cannot"))
        self.manager = ""
        self.assertTrue(outside_list.unsupported().startswith("cannot tell which worker runtime the running manager uses; on the host run"))
        self.assertNotIn("targets_fallback", self.definitions())
        # Restarted on the installed runtime, the manager takes the keys from the next check on.
        self.manager, self.manager_protocol = "/new/bin/tauceti", self.protocol
        self.assertIn("targets_fallback_cap_file", self.definitions())
        self.assertFalse(pacer.tick(store.host_config(), list, now=1500.0)["active"])
        self.assertTrue(pacer.tick(store.host_config(), list, now=1000.0 + outside_list.EVERY)["active"])

    def test_controller_writes_the_cap_file_and_publishes_each_step(self):
        import time

        store.update_host(workers_enabled=True)
        block, _ = self.controller_pass({"claude": None})
        self.assertIsNone(block)
        self.assertFalse(outside_list.cap_file().exists())
        store.update_host(outside_list_cap=dict(ceiling=40, start=4))
        self.assertEqual(self.controller_pass({"claude": None})[0], dict(active=False, reason="no target list is set"))
        self.pace()
        self.protocol = dict(version=1, roadmap_targets=1)
        self.assertEqual(self.controller_pass({"claude": None})[0]["reason"], "the installed worker cannot author outside a list")
        self.assertFalse(outside_list.cap_file().exists())

        self.protocol["targets_fallback"] = 1
        block, _ = self.controller_pass({"claude": None})
        self.assertEqual((block["active"], block["held"], block["cap"], block["reason"]), (True, True, 4, "no usage reading yet"))
        self.assertEqual(outside_list.cap_file().read_text(), "4\n")
        self.assertEqual(tomllib.loads((store.config_dir() / "workers.toml").read_text())["workers"][0]["targets_fallback_max_open"], 4)

        now = time.time()
        reading = {"claude": dict(fetched_at=now - 60, windows=dict(
            session=dict(seconds=18000.0, used=40.0, left=60.0, resets_at=now + HOUR),
            weekly=dict(seconds=604800.0, used=20.0, left=80.0, resets_at=now + 100 * HOUR),
        ))}
        running = [dict(id="w1", state="running", roadmap_work="list"), dict(id="w2", state="running", roadmap_work="outside-list"),
                   dict(id="w3", state="running"), dict(id="w4", state="waiting-capacity", roadmap_work="list")]
        saved = store.read(store.state_dir() / "outside-list-cap.json")
        self.assertEqual(self.controller_pass(reading, running)[0]["at"], saved["at"])
        store.atomic(store.state_dir() / "outside-list-cap.json", dict(
            saved, at=now - 901, cap=20, resets_at=now + 100 * HOUR, readings=[[now - 3 * HOUR, 19.0]], rounds=[]))
        block, log = self.controller_pass(reading, running)
        self.assertEqual((block["held"], block["previous"], block["cap"], block["reason"]), (False, 20, 22, "spending under plan"))
        self.assertEqual((block["provider"], block["ends"], block["outside_share"]), ("claude", "weekly window", 0.5))
        self.assertFalse({"readings", "rounds"} & set(block))
        self.assertEqual(outside_list.cap_file().read_text(), "22\n")
        self.assertIn("outside-list cap 20 -> 22 (spending under plan)", log)
        self.assertEqual(store.read(store.state_dir() / "outside-list-cap.json")["rounds"][-1][1:], [1.0, 1.0, 1.0])

        with patch.object(outside_list.targets, "write", side_effect=OSError("read-only")):
            store.atomic(store.state_dir() / "outside-list-cap.json", dict(saved, at=now - 901))
            block, _ = self.controller_pass(reading, running)
        self.assertEqual((block["active"], block["reason"]), (False, "pacing failed (OSError: read-only)"))

    def test_rounds_are_averaged_over_the_passes_between_steps(self):
        self.pace()
        config = dict(store.host_config(), workers_enabled=True)
        pacer = outside_list.Pacer()
        with patch.object(subscription_usage, "readings", return_value={"claude": None}), contextlib.redirect_stderr(io.StringIO()):
            pacer.tick(config, lambda: [], now=1000.0)
            steps = Mock(wraps=pacer.pace)
            pacer.pace = steps
            for offset, kinds in enumerate((["list", "list"], ["list", "outside-list"], [None, "outside-list"], ["list", "list"])):
                states = ("running", "verifying") if offset % 2 else ("working", "surveying")
                pacer.tick(config, lambda kinds=kinds, states=states: [
                    dict(state=state, roadmap_work=kind, next_action_at=None) for state, kind in zip(states, kinds, strict=True)
                ] + [dict(state="waiting-capacity", roadmap_work="list"),
                     dict(state="verification-pending", roadmap_work="outside-list", next_action_at=2000.0)], now=1300.0 + offset)
            steps.assert_not_called()
            self.assertEqual((pacer.passes, pacer.seen), (4, [5, 2, 1]))
            pacer.tick(config, lambda: [], now=1900.0)
            steps.assert_called_once()
            self.assertEqual((pacer.passes, pacer.seen), (0, [0, 0, 0]))

    def test_worker_statuses_carry_the_retry_time_of_outside_list_work(self):
        workers = store.state_dir() / "workers"
        workers.mkdir(parents=True)
        (workers / "w1.json").write_text(json.dumps(dict(id="w1", state="verifying", roadmap_work="outside-list", next_action_at=2000.0)))
        (workers / "w2.json").write_text(json.dumps(dict(id="w2", state="running", next_action_at=2000.0)))
        found = {w["id"]: w for w in host.worker_statuses()}
        self.assertEqual(found["w1"]["next_action_at"], 2000.0)
        self.assertNotIn("next_action_at", found["w2"])

    def test_a_list_set_while_the_controller_runs_is_paced_from_the_next_pass(self):
        store.update_host(outside_list_cap=dict(ceiling=40, start=4))
        pacer = outside_list.Pacer()
        with patch.object(subscription_usage, "readings", return_value={"claude": None}):
            self.assertEqual(pacer.tick(store.host_config(), list, now=1000.0), dict(active=False, reason="no target list is set"))
            self.assertEqual(pacer.keys, {})
            self.pace()
            self.assertTrue(pacer.tick(store.host_config(), list, now=1005.0)["active"])
        self.assertEqual(pacer.keys, outside_list.worker_keys(store.host_config()))
        self.assertEqual(outside_list.cap_file().read_text(), "4\n")

    def test_a_reached_codex_limit_does_not_raise_the_cap(self):
        store.update_host(agent="codex")
        self.pace()
        now = 1_800_000_000.0
        cache = self.root / "data/worker-runtimes" / ("a" * 40) / "lib/python3.12/site-packages/tauceti_worker/state/w1/cache/quota-codex.json"
        cache.parent.mkdir(parents=True)
        config = dict(store.host_config(), workers_enabled=True)
        caps = {}
        for reached in (False, True):
            limits = dict(limit_reached=reached,
                          primary_window=dict(used_percent=5, limit_window_seconds=5 * HOUR, reset_after_seconds=4 * HOUR),
                          secondary_window=dict(used_percent=10, limit_window_seconds=7 * 24 * HOUR, reset_after_seconds=80 * HOUR))
            cache.write_text(json.dumps(dict(fetched_at=now - 60, payload=dict(rate_limit=limits))))
            store.atomic(store.state_dir() / "outside-list-cap.json", dict(
                version=1, at=now - 901, cap=12, previous=12, floor=0, ceiling=40, resets_at=now - 60 + 80 * HOUR,
                readings=[[now - 3 * HOUR, 9.4]], rounds=[], held=True, reason="no usage reading yet"))
            with contextlib.redirect_stderr(io.StringIO()):
                caps[reached] = outside_list.Pacer().tick(config, list, now=now)
        self.assertEqual((caps[False]["cap"], caps[False]["reason"]), (13, "spending under plan"))
        self.assertEqual((caps[True]["cap"], caps[True]["reason"]), (12, "spending under plan, the provider reports its limit reached"))

    def test_step_reads_the_weekly_and_session_windows_the_workers_cached(self):
        self.pace()
        now = 1_800_000_000.0
        windows = {key: dict(utilization=used, resets_at=datetime.fromtimestamp(now + after, UTC).isoformat())
                   for key, used, after in (("five_hour", 95, HOUR), ("seven_day", 30, 100 * HOUR))}
        cache = self.root / "home/.claude/.tauceti-quota/usage/quota-claude-account.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps(dict(fetched_at=now - 120, payload=windows)))
        pacer = outside_list.Pacer()
        config = dict(store.host_config(), workers_enabled=True)
        with patch.object(outside_list, "advance", wraps=outside_list.advance) as advance:
            block = pacer.tick(config, list, now=now)
        self.assertEqual(block["reason"], "under 2 h of usage readings in this weekly window")
        self.assertEqual(pacer.state["readings"], [[now - 120, 30.0]])
        seen = advance.call_args.kwargs
        self.assertEqual((seen["reading"], seen["session_left"], seen["slot_seconds"], seen["rounds"]),
                         (dict(fetched_at=now - 120, used=30.0, resets_at=now + 100 * HOUR), 5.0, None, [0.0, 0.0, 0.0]))
        self.assertEqual(seen["curve"], [(0.0, 0.0), (60.0, 40.0), (100.0, 100.0)])
        self.assertEqual(seen["session"], dict(used=95.0, resets_at=now + HOUR))
        # A credential the workers no longer serve leaves the reading in force.
        other = cache.with_name("quota-claude-other.json")
        other.write_text(json.dumps(dict(fetched_at=now - 2 * HOUR, fp="other", payload=windows)))
        pacer.state["at"] = now - outside_list.EVERY
        self.assertEqual(pacer.tick(config, list, now=now)["reason"], "under 2 h of usage readings in this weekly window")
        other.write_text(json.dumps(dict(fetched_at=now - 60, fp="other", payload=windows)))
        pacer.state["at"] = now - outside_list.EVERY
        block = pacer.tick(config, list, now=now)
        self.assertEqual((block["held"], block["reason"]),
                         (True, "2 credentials have current usage readings and the active one cannot be told"))
        self.assertTrue(outside_list.describe(block, now).endswith("held, " + block["reason"]))
        store.update_host(pace="0:10,50:70")
        self.assertEqual(outside_list.pace_points(store.host_config()), [(0.0, 10.0), (50.0, 70.0), (100.0, 100.0)])

    def test_the_curve_follows_the_pace_the_running_manager_hands_its_workers(self):
        self.pace()
        now = 1_800_000_000.0
        config = dict(store.host_config(), workers_enabled=True)
        manager = (["/runtime/bin/tauceti", "workers"], dict(TAUCETI_PACE="60:20"))
        curves = []
        with (
            patch.object(outside_list, "advance", wraps=outside_list.advance) as advance,
            patch.object(subscription_usage, "readings", return_value={"claude": None}),
        ):
            for process, environ, profile in [(manager, {}, {}), (None, dict(TAUCETI_PACE="0:5"), {}),
                                              (manager, {}, dict(env=dict(TAUCETI_PACE="0:30"))), (manager, {}, dict(pace="0:50"))]:
                pacer = outside_list.Pacer()
                pacer.passes, pacer.state = 1, {}
                with patch.object(host, "manager_process", return_value=process), patch.dict(os.environ, environ):
                    pacer.pace(dict(config, **profile), now)
                curves.append(advance.call_args.kwargs["curve"][:2])
        self.assertEqual(curves, [[(0.0, 0.0), (60.0, 20.0)], [(0.0, 5.0), (100.0, 100.0)],
                                  [(0.0, 30.0), (100.0, 100.0)], [(0.0, 50.0), (100.0, 100.0)]])

    def test_changed_bounds_reach_the_cap_file_before_the_next_step(self):
        self.pace()
        now = 1_800_000_000.0
        store.atomic(store.state_dir() / "outside-list-cap.json", dict(
            version=1, at=now - 60, cap=40, previous=40, floor=0, ceiling=40, held=True, reason="no usage reading yet"))
        self.pace(ceiling=5, start=0)
        pacer = outside_list.Pacer()
        with contextlib.redirect_stderr(io.StringIO()) as log:
            block = pacer.tick(store.host_config(), list, now=now)
        self.assertEqual((block["cap"], block["previous"], block["ceiling"], block["at"]), (5, 40, 5, now - 60))
        self.assertEqual(outside_list.cap_file().read_text(), "5\n")
        self.assertIn("outside-list cap 40 -> 5 (bounds changed to 0-5)", log.getvalue())
        self.assertTrue(outside_list.describe(block, now).endswith("last step 1m ago: 40 -> 5, bounds changed to 0-5"))
        # Unchanged bounds leave the last step alone until the next one is due.
        self.assertEqual(pacer.tick(store.host_config(), list, now=now + 60)["previous"], 40)

    def test_an_unopened_weekly_window_raises_a_zero_cap_until_a_round_opens_it(self):
        self.pace(start=0)
        now = 1_800_000_000.0
        cache = self.root / "home/.claude/.tauceti-quota/usage/quota-claude-account.json"
        cache.parent.mkdir(parents=True)
        pacer = outside_list.Pacer()
        config = dict(store.host_config(), workers_enabled=True)
        caps = []
        for _ in range(4):
            cache.write_text(json.dumps(dict(fetched_at=now - 60, payload=dict(five_hour=None, seven_day=None))))
            with contextlib.redirect_stderr(io.StringIO()):
                caps.append(pacer.tick(config, list, now=now)["cap"])
            now += outside_list.EVERY
        self.assertEqual(caps, [1, 2, 3, 4])
        self.assertEqual(outside_list.cap_file().read_text(), "4\n")
        self.assertEqual(pacer.state["reason"], "the weekly window is not open yet, spending under plan")
        # Once a round opens it, the window is measured from its first reading.
        opened = dict(seven_day=dict(utilization=1, resets_at=datetime.fromtimestamp(now + 167 * HOUR, UTC).isoformat()))
        cache.write_text(json.dumps(dict(fetched_at=now - 60, payload=opened)))
        block = pacer.tick(config, list, now=now)
        self.assertEqual((block["cap"], block["reason"]), (4, "under 2 h of usage readings in this weekly window"))

    def test_status_line_for_a_weekly_window_already_over_its_limit(self):
        now, resets = 50 * HOUR, 80 * HOUR
        state = dict(cap=12, resets_at=resets, readings=[[now - 3 * HOUR, 101.0]], rounds=[])
        stepped = outside_list.advance(state, now=now, bounds=BOUNDS, enabled=True, slot_seconds=None, session_left=60.0, rounds=(0, 0, 0),
                                       curve=outside_list.pace_points({}), reading=dict(fetched_at=now - 60, used=101.0, resets_at=resets))
        self.assertEqual((stepped["held"], stepped["left"], stepped["rate"]), (False, 0.0, 0.0))
        self.assertIn("already spent until it resets in 30h00m", outside_list.describe(dict(stepped, active=True, ends="slot", ends_at=now + HOUR), now))
        self.assertEqual(outside_list.describe(dict(stepped, active=True), now),
                         "Outside-list cap: 11 (bounds 0-40); the weekly window is already spent until it resets in 30h00m; "
                         "last step 0m ago: 12 -> 11, list work is projected to use all that may still be spent")

    def test_status_line(self):
        now = 1_000_000.0
        moving = dict(active=True, held=False, cap=14, previous=13, floor=0, ceiling=40, at=now - 720, reason="spending under plan",
                      projected=91.4, ends="slot", ends_at=now + 14 * HOUR, left=20.0, rate=2.0)
        self.assertEqual(
            outside_list.describe(moving, now),
            "Outside-list cap: 14 (bounds 0-40); 91% of the weekly window projected spent when the slot ends in 14h00m; "
            "last step 12m ago: 13 -> 14, spending under plan",
        )
        short = dict(moving, cap=13, projected=130.0, ends="weekly window", reason="list work is projected to use all that may still be spent")
        self.assertEqual(
            outside_list.describe(short, now),
            "Outside-list cap: 13 (bounds 0-40); at this rate the weekly window is spent 4h12m before the weekly window ends in 14h00m; "
            "last step 12m ago: unchanged, list work is projected to use all that may still be spent",
        )
        held = dict(active=True, held=True, cap=14, previous=14, floor=0, ceiling=40, at=now - 180, reason="no usage reading yet")
        self.assertEqual(outside_list.describe(held, now), "Outside-list cap: 14 (bounds 0-40); last step 3m ago: held, no usage reading yet")
        self.assertEqual(outside_list.describe(dict(active=False, reason="no target list is set")),
                         "Outside-list cap: not paced; no target list is set")
        entry = dict(target="local", machine_id="one")
        value = dict(machine_id="one", activity=dict(available=False), status=dict(workers=[], active_worker_limit=0, outside_list_cap=held))
        with patch.object(remote, "inventory", return_value=dict(server1=entry)), patch.object(pool_activity, "cached", return_value={}):
            self.assertIn("Outside-list cap: 14 (bounds 0-40); last step ", render_with_cli(["status"], dict(server1=value)))
            value["status"]["outside_list_cap"] = None
            self.assertNotIn("Outside-list cap", render_with_cli(["status"], dict(server1=value)))


if __name__ == "__main__":
    unittest.main()
