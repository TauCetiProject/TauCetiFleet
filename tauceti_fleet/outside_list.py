"""Budget pacing for authoring outside a target list.

A worker under a target list may author outside it when the list offers nothing,
up to a cutoff on open pull requests that it rereads from a file every round
(TauCetiWorker docs/target-lists.md). With the profile key `outside_list_cap`
the controller moves that cutoff so the subscription's weekly window is spent
when it ends, or when the schedule slot ends if that is sooner, after setting
aside what list work needs until then.
"""

import sys
import time

from . import capabilities, schedule, subscription_usage, targets
from .store import FleetError, atomic, read, state_dir

KEY = "outside_list_cap"
EVERY = 900
# The spending rate and the outside-list share are measured over this much recent time. Usage is
# reported in whole percent and a fleet spends under one an hour, so a short span measures the rate poorly.
WINDOW = 6 * 3600
MIN_SPAN = 2 * 3600
STALE = 1800
# Two readings of one weekly window can differ by a little in the reset time they report.
SAME_WINDOW = 600
RESERVE_MARGIN = 0.25
DEADBAND = 0.2
SESSION_LOW = 10.0
MIN_HOURS = 0.25
# TauCetiWorker's pacing curve when neither the profile's `pace` nor TAUCETI_PACE sets one.
DEFAULT_PACE = "60:40"


def settings(config):
    """The profile's bounds as dict(floor, ceiling, start), or None when the key is unset."""
    value = config.get(KEY)
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"floor", "ceiling", "start"} or any(type(v) is not int for v in value.values()):
        raise FleetError(f"{KEY} must be a table of integers: ceiling, and optionally floor and start")
    floor = value.get("floor", 0)
    start, ceiling = value.get("start", floor), value.get("ceiling", 0)
    if not 0 <= floor <= start <= ceiling <= 4096 or ceiling < 1:
        raise FleetError(f"{KEY} needs 0 <= floor <= start <= ceiling, with ceiling between 1 and 4096")
    if not subscription_usage.providers(config):
        raise FleetError(f"{KEY} follows subscription usage; select claude_billing subscription")
    return dict(floor=floor, ceiling=ceiling, start=start)


def cap_file():
    return state_dir() / "outside-list-cap"


def unsupported():
    """Why the workers cannot read the paced cap, or None when they can.

    A worker manager that does not know the keys loads no worker at all, so both
    the installed runtime and the one a running manager started from must name the feature.
    """
    if capabilities.worker_protocol().get("targets_fallback") != 1:
        return "the installed worker cannot author outside a list"
    manager = targets.running_manager()
    if manager == "":
        return "cannot tell which worker runtime the running manager uses; " + targets.RESTART_MANAGER
    if manager and capabilities.worker_protocol(manager).get("targets_fallback") != 1:
        return "the running worker manager started from a TauCetiWorker that cannot author outside a list; " + targets.RESTART_MANAGER
    return None


def worker_keys(config):
    """What every worker definition gains while the cap is paced; nothing without a list or worker support."""
    bounds = settings(config)
    if not bounds or not config.get("roadmap_targets") or unsupported():
        return {}
    # The worker rejects 0 here; the cap file carries the real figure, 0 included.
    return dict(targets_fallback_max_open=max(1, bounds["start"]), targets_fallback_cap_file=str(cap_file()))


def pace_points(config):
    """The workers' pacing curve as sorted (time %, budget %) points, with the ends the worker fills in."""
    spec = config.get("pace") or (config.get("env") or {}).get("TAUCETI_PACE") or DEFAULT_PACE
    points = {}
    for token in filter(str.strip, spec.split(",")):
        at, budget = token.split(":")
        points[float(at)] = float(budget)
    points.setdefault(0.0, 0.0)
    points.setdefault(100.0, 100.0)
    return sorted(points.items())


def budget(curve, elapsed):
    """The usage in percent below which a worker starts a round at `elapsed` percent of the window."""
    elapsed = max(0.0, min(100.0, elapsed))
    before = curve[0]
    for point in curve:
        if elapsed <= point[0]:
            return point[1] if point[0] == before[0] else before[1] + (elapsed - before[0]) / (point[0] - before[0]) * (point[1] - before[1])
        before = point
    return curve[-1][1]


def burn(readings):
    """Percent of the weekly window used per hour across [fetched_at, used] readings; None under MIN_SPAN of them."""
    if len(readings) < 2 or readings[-1][0] - readings[0][0] < MIN_SPAN:
        return None
    return max(0.0, readings[-1][1] - readings[0][1]) * 3600 / (readings[-1][0] - readings[0][0])


def step(cap, *, left, hours, listed, outside, cutting, session_left, floor, ceiling, paused=None):
    """The control law: one move of the cap toward spending `left` exactly in `hours`.

    `left` and `session_left` are percentages of their windows; `listed` and
    `outside` are what list work and outside-list work spend, in percent of the
    weekly window per hour. `cutting` says a lower cap has something to act on,
    `paused` says why the workers start no round now, so a higher one has nothing to act on.
    """
    reserve = listed * hours * (1 + RESERVE_MARGIN)
    surplus = left - reserve
    allowed = max(surplus, 0.0) / hours
    move = max(1, round(cap / 10))
    if surplus <= 0:
        reason = "list work is projected to use all that may still be spent"
        cap -= move
    elif outside > (1 + DEADBAND) * allowed:
        reason = "spending over plan"
        # Below the open pull requests the cutoff already admits nothing; stepping on only delays the way back.
        if cutting:
            cap -= move
        else:
            reason += ", no outside-list round running to cut"
    elif outside >= (1 - DEADBAND) * allowed:
        reason = "on plan"
    elif session_left is not None and session_left < SESSION_LOW:
        reason = "spending under plan, session window nearly exhausted"
    elif paused:
        reason = "spending under plan, " + paused
    else:
        reason = "spending under plan"
        cap += move
    return dict(cap=min(ceiling, max(floor, cap)), reason=reason, reserve=reserve, projected=100 - left + (listed + outside) * hours)


def advance(state, *, now, bounds, enabled, slot_seconds, reading, session_left, rounds, curve, missing="no usage reading yet", session=None,
            limit_reached=False):
    """The pacer's state after one step, from plain inputs.

    `reading` is the weekly window as dict(fetched_at, used, resets_at), or None
    with `missing` saying why,
    `slot_seconds` what `schedule.seconds_until_slot_ends` returned, `rounds` the
    average numbers of running (list, outside-list, other) rounds since the last
    step, `curve` the workers' pacing curve from `pace_points`. Only what that
    curve lets the workers spend by the horizon is planned for. `session` is the
    session window as dict(used, resets_at) or None, and `limit_reached` the
    provider's own verdict that the account is blocked. The cap holds whenever the
    inputs cannot support a decision, and is not raised while that verdict is set
    or while the curve lets no round start in either window. Rounds are recorded through every hold but the
    schedule's and a disabled host's, and the split takes those within the span
    the spending rate is measured over.
    """
    floor, ceiling = bounds["floor"], bounds["ceiling"]
    cap = min(ceiling, max(floor, state["cap"] if type(state.get("cap")) is int else bounds["start"]))
    readings, samples, resets_at = list(state.get("readings") or []), list(state.get("rounds") or []), state.get("resets_at")
    rate = hold = None
    idle = reading is not None and reading["used"] == 0 and reading["resets_at"] is None
    if not enabled or slot_seconds == 0:
        hold = "outside the weekly schedule" if enabled else "workers are not enabled"
        readings, samples = [], []
    elif reading is None:
        hold = missing
    elif idle:
        if now - reading["fetched_at"] > STALE:
            hold = f"the usage reading is {int(now - reading['fetched_at']) // 60} minutes old"
        readings, samples, resets_at = [], [], None
    elif reading["used"] is None or reading["resets_at"] is None:
        reset = reading["resets_at"] is not None and reading["resets_at"] <= now
        hold = "the weekly window has reset since the last usage reading" if reset else "the usage reading has no weekly figure"
    elif now - reading["fetched_at"] > STALE:
        hold = f"the usage reading is {int(now - reading['fetched_at']) // 60} minutes old"
    else:
        moved = resets_at is not None and abs(resets_at - reading["resets_at"]) > SAME_WINDOW
        if moved or (readings and reading["used"] < readings[-1][1]):
            readings, samples = [], []
        resets_at = reading["resets_at"]
        if not readings or reading["fetched_at"] > readings[-1][0]:
            readings.append([reading["fetched_at"], reading["used"]])
        readings = [r for r in readings if r[0] >= now - WINDOW]
        rate = burn(readings)
        if rate is None:
            hold = f"under {MIN_SPAN // 3600} h of usage readings in this weekly window"
    if enabled and slot_seconds != 0:
        samples = [s for s in [*samples, [now, *rounds]] if s[0] >= now - WINDOW]
    result = dict(version=1, at=now, cap=cap, previous=cap, floor=floor, ceiling=ceiling,
                  readings=readings, rounds=samples, resets_at=resets_at, held=hold is not None, reason=hold)
    if hold:
        return result
    # The worker starts a round only while every open window is under the curve; an unopened one does not hold it.
    session_paused = bool(session) and session["used"] is not None and session["resets_at"] is not None and (
        session["used"] >= budget(curve, 100 - (session["resets_at"] - now) / subscription_usage.SESSION * 100))
    paused = "the provider reports its limit reached" if limit_reached else ("the workers' pacing curve starts no round now" if session_paused else None)
    if idle:
        # Nothing opens the window but a model call, so with nothing spent the cap climbs until a round runs.
        ends_at, ends = now + subscription_usage.WEEK, "weekly window"
        if slot_seconds is not None and slot_seconds < subscription_usage.WEEK:
            ends_at, ends = now + slot_seconds, "slot"
        result.update(step(cap, left=100.0, hours=(ends_at - now) / 3600, listed=0.0, outside=0.0, cutting=False,
                           session_left=session_left, floor=floor, ceiling=ceiling, paused=paused),
                      left=100.0, rate=0.0, outside_share=None, ends=ends, ends_at=ends_at)
        result["reason"] = "the weekly window is not open yet, " + result["reason"]
        return result
    ends_at, ends = reading["resets_at"], "weekly window"
    if slot_seconds is not None and now + slot_seconds < ends_at:
        ends_at, ends = now + slot_seconds, "slot"
    # A sample counts the rounds since the step before it, so the split takes those after the rate's first reading.
    authoring = [sum(s[index] for s in samples if s[0] > readings[0][0]) for index in (1, 2)]
    # Rounds that report neither kind are divided as the authoring rounds are; with none of either, nothing is reserved.
    share = authoring[1] / sum(authoring) if sum(authoring) else 1.0
    left = max(0.0, 100 - reading["used"])
    elapsed = [100 - (reading["resets_at"] - at) / subscription_usage.WEEK * 100 for at in (now, ends_at)]
    spendable = max(0.0, min(100.0, budget(curve, elapsed[1])) - reading["used"])
    hours = max(MIN_HOURS, (ends_at - now) / 3600)
    result.update(step(cap, left=spendable, hours=hours, listed=rate * (1 - share), outside=rate * share,
                       cutting=rounds[1] > 0, session_left=session_left, floor=floor, ceiling=ceiling,
                       paused=paused or ("the workers' pacing curve starts no round now" if reading["used"] >= budget(curve, elapsed[0]) else None)),
                  left=left, rate=rate, outside_share=share, ends=ends, ends_at=ends_at)
    result["projected"] = reading["used"] + rate * hours
    return result


class Pacer:
    """The controller's side: counts running rounds on every pass and steps the cap every EVERY seconds."""

    def __init__(self):
        self.state = None
        self.keys = self.why = None
        self.checked = None
        self.passes, self.seen = 0, [0, 0, 0]

    def tick(self, config, statuses, now=None):
        """The block for status.json, or None when the profile does not pace the cap. Never raises."""
        if not config.get(KEY):
            self.keys = {}
            return None
        now = time.time() if now is None else now
        try:
            if self.state is None:
                saved = read(state_dir() / "outside-list-cap.json", {})
                self.state = saved if isinstance(saved, dict) and saved.get("version") == 1 else {}
            basis = (config[KEY], config.get("roadmap_targets"))
            if self.checked is None or self.checked[1:] != basis or not 0 <= now - self.checked[0] < EVERY:
                self.why = unsupported() if config.get("roadmap_targets") else "no target list is set"
                self.keys, self.checked = {} if self.why else worker_keys(config), (now, *basis)
            if not self.keys:
                return dict(active=False, reason=self.why)
            kinds = [w.get("roadmap_work") for w in statuses() if w.get("state") in targets.ACTIVE]
            self.passes += 1
            for index, kind in enumerate(("list", "outside-list")):
                self.seen[index] += kinds.count(kind)
            self.seen[2] += sum(kind not in ("list", "outside-list") for kind in kinds)
            bounds = settings(config)
            if not 0 <= now - self.state.get("at", 0) < EVERY:
                self.pace(config, now)
            elif type(self.state.get("cap")) is int and (self.state.get("floor"), self.state.get("ceiling")) != (bounds["floor"], bounds["ceiling"]):
                self.rebound(bounds)
        except (FleetError, OSError, ValueError, KeyError, TypeError) as exc:
            return dict(active=False, reason=f"pacing failed ({type(exc).__name__}: {exc})"[:200])
        return dict({k: v for k, v in self.state.items() if k not in ("readings", "rounds")}, active=True)

    def rebound(self, bounds):
        """Brings the last step's cap within changed bounds at once; the next step stays due when it was."""
        cap = min(bounds["ceiling"], max(bounds["floor"], self.state["cap"]))
        self.save(dict(self.state, cap=cap, previous=self.state["cap"], floor=bounds["floor"], ceiling=bounds["ceiling"],
                       reason=f"bounds changed to {bounds['floor']}-{bounds['ceiling']}"))

    def save(self, state):
        targets.write(cap_file(), f"{state['cap']}\n")
        atomic(state_dir() / "outside-list-cap.json", state)
        if state["cap"] != state["previous"]:
            print(f"tauceti-fleet: outside-list cap {state['previous']} -> {state['cap']} ({state['reason']})", file=sys.stderr, flush=True)
        self.state = state

    def pace(self, config, now):
        provider = subscription_usage.providers(config)[0]
        usage = subscription_usage.readings(config, now).get(provider) or {}
        windows = usage.get("windows") or {}
        several = len(usage.get("credentials") or [])
        weekly = windows.get("weekly")
        state = advance(
            self.state, now=now, bounds=settings(config), enabled=bool(config.get("workers_enabled")),
            slot_seconds=schedule.seconds_until_slot_ends(config, now),
            reading=dict(fetched_at=usage["fetched_at"], used=weekly["used"], resets_at=weekly["resets_at"]) if weekly else None,
            session_left=(windows.get("session") or {}).get("left"),
            session={key: windows["session"][key] for key in ("used", "resets_at")} if windows.get("session") else None,
            rounds=[count / self.passes for count in self.seen], curve=pace_points(config),
            limit_reached=bool(usage.get("limit_reached")),
            missing=f"{several} credentials have current usage readings and the active one cannot be told" if several else "no usage reading yet",
        )
        state["provider"] = provider
        self.save(state)
        self.passes, self.seen = 0, [0, 0, 0]


def describe(value, now=None):
    """The `status` line for a published pacer block."""
    if not value.get("active"):
        return "Outside-list cap: not paced; " + value["reason"]
    now = time.time() if now is None else now
    span = subscription_usage.span
    text = f"Outside-list cap: {value['cap']} (bounds {value['floor']}-{value['ceiling']})"
    if not value["held"]:
        end = f"the {value['ends']} ends in {span(value['ends_at'] - now)}"
        if value["projected"] > 100:
            early = (value["ends_at"] - value["at"]) - value["left"] / value["rate"] * 3600
            text += f"; at this rate the weekly window is spent {span(early)} before {end}"
        else:
            text += f"; {value['projected']:.0f}% of the weekly window projected spent when {end}"
    if value["cap"] != value["previous"]:
        move = f"{value['previous']} -> {value['cap']}, "
    else:
        move = "held, " if value["held"] else "unchanged, "
    return text + f"; last step {span(now - value['at'])} ago: {move}{value['reason']}"
