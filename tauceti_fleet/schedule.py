"""Weekly slots for new worker rounds; outside every slot the controller admits none."""

import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import FleetError, host_config, update_host

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def minute(text):
    match = re.fullmatch(r"\s*([A-Za-z]{3,})\s+(\d{1,2}):(\d{2})\s*", text)
    day = next((n for n, name in enumerate(DAYS) if match and name.startswith(match[1].lower())), None)
    if day is None or int(match[2]) > 23 or int(match[3]) > 59:
        raise FleetError(f"Schedule time {text.strip()!r} is not 'Day HH:MM', for example 'Thu 07:00'")
    return day * 1440 + int(match[2]) * 60 + int(match[3])


def parse(slot):
    """'Mon 23:00 -> Thu 07:00' as (start, end) minutes from Monday 00:00; an earlier end is in the next week."""
    ends = slot.split("->") if isinstance(slot, str) else []
    if len(ends) != 2:
        raise FleetError(f"Schedule slot {slot!r} is not 'Day HH:MM -> Day HH:MM'")
    start, end = minute(ends[0]), minute(ends[1])
    if start == end:
        raise FleetError(f"Schedule slot {slot!r} is empty")
    return start, end


def zone(name):
    try:
        return ZoneInfo(name)
    except (TypeError, ValueError, OSError, ZoneInfoNotFoundError):
        raise FleetError(f"Unknown timezone: {name}") from None


def clock(at, tz):
    local = datetime.fromtimestamp(at).astimezone(tz)
    return f"{DAYS[local.weekday()][:3].title()} {local:%H:%M %Z}"


def validate(slots, timezone=None):
    if not isinstance(slots, list) or not 1 <= len(slots) <= 32:
        raise FleetError("A schedule is a list of 1 to 32 slots")
    if timezone is not None:
        zone(timezone)
    names = [" -> ".join(f"{DAYS[m // 1440][:3].title()} {m % 1440 // 60:02d}:{m % 60:02d}" for m in parse(s)) for s in slots]
    return dict(slots=names, timezone=timezone)


def occurrences(slots, tz, now):
    """Epoch (start, end, slot) for each slot in the weeks around `now`.

    Slot times are wall-clock times: a naive datetime converts by the host's
    local rules and an aware one by its zone's, daylight saving included.
    """
    local = datetime.fromtimestamp(now, tz)
    monday = datetime(local.year, local.month, local.day) - timedelta(days=local.weekday())
    for week in range(-1, 3):
        for slot in slots:
            start, end = parse(slot)
            end += 7 * 1440 if end < start else 0
            yield (
                *((monday + timedelta(weeks=week, minutes=m)).replace(tzinfo=tz).timestamp() for m in (start, end)),
                slot,
            )


def state(config, now=None):
    """What the controller enforces at `now`; empty when the host has no schedule."""
    saved = config.get("worker_schedule")
    if not saved:
        return {}
    now = time.time() if now is None else now
    try:
        if not isinstance(saved, dict):
            raise FleetError("Schedule configuration must be an object")
        value = validate(saved.get("slots"), saved.get("timezone"))
        tz = zone(value["timezone"]) if value["timezone"] else None
        spans = list(occurrences(value["slots"], tz, now))
    except FleetError as exc:
        return dict(active=False, reason="weekly schedule unreadable", error=str(exc))

    def inside(at):
        return next((slot for start, end, slot in spans if start <= at < end), None)

    slot = inside(now)
    # Slots covering the whole week never flip. Their generated occurrences run
    # out more than two weeks ahead, which the eight-day horizon excludes.
    edges = sorted(e for start, end, _ in spans for e in (start, end) if now < e < now + 8 * 86400)
    boundary = next((e for e in edges if bool(inside(e)) != bool(slot)), None)
    result = dict(
        value,
        active=bool(slot),
        slot=slot,
        boundary_at=boundary,
        boundary=None if boundary is None else clock(boundary, tz),
    )
    if not slot:
        result["reason"] = "outside the weekly schedule"
    return result


def seconds_until_slot_ends(config, now=None):
    """Admission time left: None when unbounded (no schedule, or slots covering the week), 0 outside every slot."""
    now = time.time() if now is None else now
    window = state(config, now)
    if not window or (window["active"] and window["boundary_at"] is None):
        return None
    return window["boundary_at"] - now if window["active"] else 0.0


def describe(window, now=None):
    """The `status` line for a published schedule state."""
    if window.get("error"):
        return "Weekly schedule unreadable; new worker rounds paused: " + window["error"]
    text = f"Weekly schedule ({window.get('timezone') or 'host local time'}): "
    text += f"in slot {window['slot']}" if window["active"] else "outside every slot"
    if window.get("boundary_at") is None:
        return text + "; the slots cover the whole week"
    left = max(0, int(window["boundary_at"] - (time.time() if now is None else now)))
    left = f"{left // 86400}d {left % 86400 // 3600}h" if left >= 86400 else f"{left // 3600}h {left % 3600 // 60:02d}m"
    return text + ("; ends " if window["active"] else "; next slot starts ") + f"{window['boundary']} (in {left})"


def configure(payload):
    if payload.get("clear"):
        update_host(worker_schedule=None)
    elif payload.get("slots") is not None:
        update_host(worker_schedule=validate(payload["slots"], payload.get("timezone")))
    return state(host_config()) or dict(slots=[])
