import io
import os
import signal
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from tauceti_fleet import host, pool_activity, remote, schedule, store
from tauceti_fleet.cli import main
from tauceti_fleet.status_follow import render_with_cli
from tauceti_fleet.store import FleetError

GIB = 1024**3


def at(day, hour, minute=0, *, month=1):
    """A UTC instant; 5 January 2026 is a Monday."""
    return datetime(2026, month, day, hour, minute, tzinfo=UTC).timestamp()


def config(*slots, timezone="UTC"):
    return dict(worker_schedule=dict(slots=list(slots), timezone=timezone))


def around_now(start_hours, end_hours):
    now = datetime.now(UTC)
    return " -> ".join(f"{now + timedelta(hours=h):%a %H:%M}" for h in (start_hours, end_hours))


class Slots(unittest.TestCase):
    def test_slots_are_normalized_and_malformed_ones_rejected(self):
        self.assertEqual(
            schedule.validate(["monday 23:00->thu 7:00", " Sat 09:05 -> SUN 21:00 "], "Europe/Berlin"),
            dict(slots=["Mon 23:00 -> Thu 07:00", "Sat 09:05 -> Sun 21:00"], timezone="Europe/Berlin"),
        )
        for slots in (
            [], "Mon 23:00 -> Thu 07:00", [None], ["Mon 23:00"], ["Mon 23:00 -> Thu 07:00 -> Fri 07:00"],
            ["Mo 23:00 -> Thu 07:00"], ["Mon 24:00 -> Thu 07:00"], ["Mon 23:60 -> Thu 07:00"],
            ["Mon 23 -> Thu 07:00"], ["Munday 23:00 -> Thu 07:00"], ["Mon 23:00 -> Mon 23:00"],
            ["Mon 23:00 -> Thu 07:00"] * 33,
        ):
            with self.subTest(slots=slots), self.assertRaises(FleetError):
                schedule.validate(slots)
        for timezone in ("", "Nowhere/Zone", "../UTC", 5):
            with self.subTest(timezone=timezone), self.assertRaisesRegex(FleetError, "timezone"):
                schedule.validate(["Mon 23:00 -> Thu 07:00"], timezone)

    def test_slot_boundaries_and_time_left(self):
        weekdays = config("Mon 23:00 -> Thu 07:00")
        inside = schedule.state(weekdays, at(6, 12))
        self.assertTrue(inside["active"])
        self.assertEqual(inside["slot"], "Mon 23:00 -> Thu 07:00")
        self.assertEqual((inside["boundary_at"], inside["boundary"]), (at(8, 7), "Thu 07:00 UTC"))
        self.assertNotIn("reason", inside)
        self.assertEqual(schedule.seconds_until_slot_ends(weekdays, at(6, 12)), 43 * 3600)
        self.assertTrue(schedule.state(weekdays, at(5, 23))["active"])
        for now in (at(5, 22, 59), at(8, 7), at(11, 12)):
            outside = schedule.state(weekdays, now)
            self.assertFalse(outside["active"])
            self.assertIsNone(outside["slot"])
            self.assertEqual(outside["reason"], "outside the weekly schedule")
            self.assertEqual(schedule.seconds_until_slot_ends(weekdays, now), 0)
        self.assertEqual(schedule.state(weekdays, at(8, 7))["boundary_at"], at(12, 23))

    def test_slots_wrap_the_week_join_when_adjacent_and_may_cover_everything(self):
        weekend = config("Sat 09:00 -> Mon 06:00")
        for now in (at(10, 9), at(11, 23, 59), at(12, 5, 59)):
            self.assertEqual(schedule.state(weekend, now)["boundary_at"], at(12, 6))
        self.assertEqual(schedule.state(weekend, at(12, 6))["boundary_at"], at(17, 9))
        joined = config("Mon 00:00 -> Wed 00:00", "Wed 00:00 -> Fri 12:00", "Tue 00:00 -> Tue 06:00")
        self.assertEqual(schedule.state(joined, at(6, 3))["boundary_at"], at(9, 12))
        self.assertEqual(schedule.state(joined, at(7, 0))["slot"], "Wed 00:00 -> Fri 12:00")
        always = config("Mon 00:00 -> Thu 00:00", "Thu 00:00 -> Mon 00:00")
        for now in (at(5, 0), at(8, 0), at(11, 23, 59)):
            whole = schedule.state(always, now)
            self.assertTrue(whole["active"])
            self.assertIsNone(whole["boundary_at"])
            self.assertIsNone(schedule.seconds_until_slot_ends(always, now))
            self.assertIn("cover the whole week", schedule.describe(whole, now))

    def test_no_schedule_bounds_nothing_and_an_unreadable_one_admits_nothing(self):
        for empty in ({}, dict(worker_schedule=None)):
            self.assertEqual(schedule.state(empty), {})
            self.assertIsNone(schedule.seconds_until_slot_ends(empty))
        for saved in ({}, [], "", 0, False, "Mon 23:00 -> Thu 07:00", dict(slots=[]), dict(slots=["Mon"]), dict(slots=["Mon 01:00 -> Tue 01:00"], timezone="Nowhere/Zone")):
            with self.subTest(saved=saved):
                unreadable = schedule.state(dict(worker_schedule=saved))
                self.assertFalse(unreadable["active"])
                self.assertEqual(unreadable["reason"], "weekly schedule unreadable")
                self.assertIn(unreadable["error"], schedule.describe(unreadable))
                self.assertEqual(schedule.seconds_until_slot_ends(dict(worker_schedule=saved)), 0)

    def test_times_are_wall_clock_times_in_the_named_zone(self):
        # Europe/Berlin skips 02:00-03:00 on 29 March 2026, so this slot lasts seven hours.
        night = config("Sat 22:00 -> Sun 06:00", timezone="Europe/Berlin")
        start = at(28, 21, month=3)
        self.assertFalse(schedule.state(night, start - 1)["active"])
        window = schedule.state(night, start)
        self.assertEqual(window["boundary"], "Sun 06:00 CEST")
        self.assertEqual(schedule.seconds_until_slot_ends(night, start), 7 * 3600)
        self.assertEqual(
            schedule.describe(window, start),
            "Weekly schedule (Europe/Berlin): in slot Sat 22:00 -> Sun 06:00; ends Sun 06:00 CEST (in 7h 00m)",
        )
        self.assertEqual(
            schedule.describe(schedule.state(night, at(22, 12, month=3)), at(22, 12, month=3)),
            "Weekly schedule (Europe/Berlin): outside every slot; next slot starts Sat 22:00 CET (in 6d 9h)",
        )

    def test_a_boundary_skipped_by_daylight_saving_moves_to_the_next_week(self):
        # 1 March 2026 04:00 EST; the Sunday 02:00-03:00 hour does not exist on 8 March.
        now = at(1, 9, month=3)
        gap = config("Sun 03:00 -> Sun 02:00", timezone="America/New_York")
        window = schedule.state(gap, now)
        self.assertTrue(window["active"])
        self.assertEqual((window["boundary_at"], window["boundary"]), (at(15, 6, month=3), "Sun 02:00 EDT"))
        self.assertEqual(schedule.seconds_until_slot_ends(gap, now), 1_198_800)
        self.assertNotIn("cover the whole week", schedule.describe(window, now))
        inside_gap = schedule.state(config("Sun 02:10 -> Sun 02:40", timezone="America/New_York"), now)
        self.assertFalse(inside_gap["active"])
        self.assertEqual(inside_gap["boundary_at"], at(15, 6, 10, month=3))

    def test_a_time_skipped_by_daylight_saving_is_the_end_of_the_gap(self):
        # New York skips 02:00-03:00 on 8 March 2026: 07:00 UTC is 03:00 EDT.
        whole = config("Mon 00:00 -> Sun 03:00", "Sun 02:30 -> Mon 00:00", timezone="America/New_York")
        inside = schedule.state(whole, at(8, 7, 15, month=3))
        self.assertTrue(inside["active"])
        self.assertIsNone(inside["boundary_at"])
        before = config("Sun 00:00 -> Sun 02:30", timezone="America/New_York")
        self.assertEqual(schedule.state(before, at(8, 6, 59, month=3))["boundary_at"], at(8, 7, month=3))
        self.assertFalse(schedule.state(before, at(8, 7, 15, month=3))["active"])
        after = schedule.state(config("Sun 02:30 -> Sun 04:00", timezone="America/New_York"), at(8, 6, 59, month=3))
        self.assertEqual((after["boundary_at"], after["boundary"]), (at(8, 7, month=3), "Sun 03:00 EDT"))

    def test_times_without_a_zone_are_the_hosts_local_time(self):
        self.addCleanup(time.tzset)
        with patch.dict(os.environ, TZ="America/New_York"):
            time.tzset()
            local = schedule.state(config("Mon 23:00 -> Thu 07:00", timezone=None), at(6, 12))
            named = schedule.state(config("Mon 23:00 -> Thu 07:00", timezone="America/New_York"), at(6, 12))
            self.assertEqual(local["boundary_at"], at(8, 12))
            self.assertEqual((local["boundary_at"], local["boundary"]), (named["boundary_at"], "Thu 07:00 EST"))
            self.assertIn("(host local time)", schedule.describe(local))
            # New York skips 02:00-03:00 on 8 March 2026.
            self.assertEqual(
                schedule.seconds_until_slot_ends(config("Sat 22:00 -> Sun 06:00", timezone=None), at(8, 3, month=3)),
                7 * 3600,
            )
            self.assertEqual(
                schedule.seconds_until_slot_ends(config("Sat 22:00 -> Sun 02:30", timezone=None), at(8, 3, month=3)),
                4 * 3600,
            )


class Controller(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {
            "TAUCETI_FLEET_" + key.upper(): str(Path(temporary.name) / key) for key in ("config", "state", "data")
        })
        env.start()
        self.addCleanup(env.stop)
        store.update_host(workers_enabled=True, max_workers=2)

    def controller_pass(self):
        """Run the controller loop once; return the admission and status it published."""
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
                worker_config=Mock(return_value=["w1", "w2"]),
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
        return published["admission.json"], published["status.json"]

    def test_outside_a_slot_the_ceiling_is_zero_and_inside_it_is_unchanged(self):
        def decision(admission, status):
            return {k: v for k, v in admission.items() if k != "expires_at"}, status["worker_admission"]

        unscheduled = self.controller_pass()
        self.assertEqual(unscheduled[0]["workers"], ["w1", "w2"])
        self.assertEqual(unscheduled[1]["worker_schedule"], {})

        host.rpc(dict(action="schedule", slots=[around_now(-1, 1)], timezone="UTC"))
        admission, status = self.controller_pass()
        self.assertEqual(decision(admission, status), decision(*unscheduled))
        self.assertTrue(status["worker_schedule"]["active"])

        host.rpc(dict(action="schedule", slots=[around_now(2, 4)], timezone="UTC"))
        admission, status = self.controller_pass()
        self.assertEqual(admission["workers"], [])
        self.assertEqual(admission["reason"], "outside the weekly schedule")
        self.assertEqual(status["active_worker_limit"], 0)
        self.assertEqual(status["worker_admission"]["state"], "paused")
        self.assertEqual(store.host_config()["max_workers"], 2)
        self.assertTrue(store.host_config()["workers_enabled"])

        store.update_host(worker_schedule=dict(slots=["whenever"]))
        admission, status = self.controller_pass()
        self.assertEqual((admission["workers"], admission["reason"]), ([], "weekly schedule unreadable"))

        self.assertEqual(host.rpc(dict(action="schedule", clear=True)), dict(slots=[]))
        self.assertEqual(self.controller_pass()[0]["workers"], ["w1", "w2"])

    def test_a_drained_host_keeps_its_own_reason(self):
        store.update_host(workers_enabled=False, worker_schedule=schedule.validate([around_now(2, 4)], "UTC"))
        admission, status = self.controller_pass()
        self.assertEqual(admission["reason"], "workers disabled or host draining")
        self.assertFalse(status["worker_schedule"]["active"])

    def test_saving_validates_first_and_showing_changes_nothing(self):
        saved = host.rpc(dict(action="schedule", slots=["mon 23:00->thu 7:00"], timezone="Europe/Berlin"))
        self.assertEqual(saved["slots"], ["Mon 23:00 -> Thu 07:00"])
        stored = store.host_config()["worker_schedule"]
        self.assertEqual(stored, dict(slots=["Mon 23:00 -> Thu 07:00"], timezone="Europe/Berlin"))
        for bad in (dict(slots=["Mon 23:00"]), dict(slots=[]), dict(slots=["Mon 23:00 -> Thu 07:00"], timezone="Nowhere/Zone")):
            with self.assertRaises(FleetError):
                host.rpc(dict(action="schedule", **bad))
        with patch.object(schedule, "update_host") as update:
            shown = host.rpc(dict(action="schedule"))
            update.assert_not_called()
        self.assertEqual((shown["slots"], shown["timezone"]), (stored["slots"], "Europe/Berlin"))
        self.assertEqual(store.host_config()["worker_schedule"], stored)
        # Replacing the slots without a zone returns the schedule to host local time.
        self.assertIsNone(host.rpc(dict(action="schedule", slots=["Sat 09:00 -> Sun 21:00"]))["timezone"])


class Operator(unittest.TestCase):
    def test_cli_sends_slots_zone_and_clear_and_shows_without_changing(self):
        entry = dict(target="machine", machine_id="one")
        with (
            patch.object(remote, "inventory", return_value={"test": entry}),
            patch.object(remote, "resolve", return_value=entry),
            patch.object(remote, "call", return_value=dict(slots=[])) as call,
            patch("sys.stdout", new=io.StringIO()),
            patch("sys.stderr", new=io.StringIO()),
        ):
            self.assertEqual(main(["schedule", "test", "Mon 23:00 -> Thu 07:00", "Sat 09:00 -> Sun 21:00, Sun 22:00 -> Mon 01:00"]), 0)
            self.assertEqual(call.call_args.args[1], dict(
                action="schedule", timezone=None,
                slots=["Mon 23:00 -> Thu 07:00", "Sat 09:00 -> Sun 21:00", "Sun 22:00 -> Mon 01:00"],
            ))
            self.assertEqual(main(["schedule", "test", "Mon 23:00 -> Thu 07:00", "--timezone", "Europe/Berlin"]), 0)
            self.assertEqual(call.call_args.args[1]["timezone"], "Europe/Berlin")
            self.assertEqual(main(["schedule", "test", "--clear"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="schedule", clear=True))
            self.assertEqual(main(["schedule", "test"]), 0)
            self.assertEqual(call.call_args.args[1], dict(action="schedule"))
            call.reset_mock()
            self.assertEqual(main(["schedule", "test", "Mon 23:00 -> Thu 07:00", "--clear"]), 1)
            self.assertEqual(main(["schedule", "test", "--timezone", "UTC"]), 1)
            call.assert_not_called()

    def test_status_shows_the_slot_and_the_next_boundary(self):
        entry = dict(target="local", machine_id="one")
        now = time.time()
        window = schedule.state(config(around_now(2, 4)), now)
        value = dict(machine_id="one", activity=dict(available=False),
                     status=dict(workers=[], active_worker_limit=0, worker_schedule=window))
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TAUCETI_FLEET_" + key: str(Path(directory) / key) for key in ("CONFIG", "STATE", "DATA")}),
            patch.object(remote, "inventory", return_value=dict(server1=entry)),
            patch.object(pool_activity, "cached", return_value={}),
        ):
            text = render_with_cli(["status"], dict(server1=value))
            self.assertIn(f"Weekly schedule (UTC): outside every slot; next slot starts {window['boundary']} (in ", text)
            value["status"]["worker_schedule"] = {}
            self.assertNotIn("Weekly schedule", render_with_cli(["status"], dict(server1=value)))


if __name__ == "__main__":
    unittest.main()
