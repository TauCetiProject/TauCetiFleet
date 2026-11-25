"""A small warm CI pool; assigned and running guests survive pool changes."""

import time
import urllib.parse

from . import capacity, ci_overview
from .store import FleetError, atomic, locked, read, state_dir


def plan(config, values, inventory, observation=None, recovery_slots=None, pending_recovery=False):
    live = {v.get("slot", 0): v for v in values if v}
    busy_ids = {r["id"] for r in inventory if r.get("busy")}
    if observation:
        for value in live.values():
            own = next((r for r in inventory if r["id"] == value.get("runner_id")), {})
            labels = {label.lower() for label in (value.get("routing_labels") or
                      [label["name"] for label in own.get("labels", [])])}
            if any({label.lower() for label in queued} <= labels for queued in observation["queued"]):
                busy_ids.add(value["runner_id"])
    occupied = {i for i, v in live.items() if v.get("busy") or v.get("runner_id") in busy_ids}
    if not config["ci_enabled"]:
        return occupied
    protected = set(capacity.protected_slots(config))
    if config.get("ci_idle_pool") and observation is None:
        # Unknown queued assignments must not cause additional admissions.
        return set(live) | protected
    maximum = capacity.jobs(config)
    desired = set(occupied) | set(recovery_slots or {}) | protected
    candidates = [i for i in range(maximum) if i not in desired and not live.get(i, {}).get("withdrawing_at")]
    if config.get("ci_idle_pool"):
        reserved, _ = capacity.reservation(config)
        # Keep reserved staging guests and one general spare. Busy guests above
        # the new ceiling consume capacity until they finish, regardless of ID.
        candidates.sort(key=lambda i: (i >= reserved, i not in live, i))
        general = False
        for i in candidates:
            if len(desired) >= maximum:
                break
            if i < reserved or (not general and not pending_recovery):
                desired.add(i)
                general = general or i >= reserved
    else:
        desired.update(candidates[:max(0, maximum - len(desired))])
    return desired


def reservations(config):
    """Read only the existing cache; never initiate provider discovery."""
    base = config.get("github_state_url", "https://github-state.taucetiproject.org").rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise FleetError("CI pool observation requires HTTPS")
    value = ci_overview.fetch(base + "/v1/runner-capacity/cached?repository=TauCetiProject%2FTauCeti",
                              token_file=config.get("github_state_token_file"), timeout=3)
    observed = value.get("observed_at")
    queued = value.get("queued_fleet")
    if (value.get("read_only") is not True or value.get("complete") is not True
            or type(observed) not in (int, float) or not -5 <= time.time() - observed / 1000 <= 150
            or not isinstance(queued, list) or len(queued) > 10000
            or any(not isinstance(labels, list) or not labels or len(labels) > 100
                   or any(not isinstance(label, str) for label in labels) for labels in queued)):
        raise FleetError("Fresh complete CI reservations unavailable; idle guests retained")
    return dict(observed_at=observed / 1000, queued=[set(v) for v in queued])


def labels(value, own):
    return value.get("routing_labels") or [r["name"] for r in own.get("labels", [])]


def idle_ready(value, own, observation):
    if own.get("busy") or value.get("busy") or not observation:
        return False
    original = {label.lower() for label in labels(value, own)}
    return bool(original) and not any({label.lower() for label in queued} <= original
                                     for queued in observation["queued"])


def remember(value):
    if value.get("ci_kinds") in ([kind] for kind in capacity.PROTECTED_KINDS):
        # Protected jobs target stable group/class labels, never an instance.
        return
    # JIT labels are read-only. Retain enough identity to recreate a guest if
    # a picker races deregistration and subsequently queues on its old label.
    path = state_dir() / "ci-idle-retired.json"
    with locked(path.with_suffix(".lock")):
        records = [r for r in read(path, []) if r["at"] > time.time() - 172800
                   and r["instance"] != value["instance"]]
        records.append(dict(at=time.time(), instance=value["instance"],
                            ci_kinds=value["ci_kinds"], labels=value["routing_labels"]))
        atomic(path, records)


def recoveries(values, observation):
    if not observation:
        return []
    live = {value["instance"] for value in values if value}
    result = []
    for record in read(state_dir() / "ci-idle-retired.json", []):
        if record["at"] <= time.time() - 172800 or record["instance"] in live:
            continue
        original = {label.lower() for label in record["labels"]}
        if any({label.lower() for label in queued} <= original for queued in observation["queued"]):
            result.append(record)
    return result


def recovery_slots(config, values, inventory, observation, records):
    if not config["ci_enabled"] or not observation:
        return {}
    # Reuse the same planner without warm spares to count active assignments.
    occupied = plan(config, values, inventory, observation, pending_recovery=True)
    live_slots = {value.get("slot", 0) for value in values if value}
    reserved, _ = capacity.reservation(config)
    result = {}
    for record in records:
        if record.get("ci_kinds") in ([kind] for kind in capacity.PROTECTED_KINDS):
            continue
        if len(occupied | result.keys()) >= capacity.jobs(config):
            break
        candidates = [i for i in range(capacity.jobs(config)) if i not in live_slots and i not in result
                      and i not in capacity.protected_slots(config)
                      and (i >= reserved or record["ci_kinds"] == ["staging"])]
        if candidates:
            result[candidates[0]] = record
    return result


# Priority PR builds (TauCetiTools' picker, while the project names a priority PR label) target
# this class. The broker gives it to every PR runner and, for a priority-only registration, to
# nothing else, so only priority builds can take such a runner. GitHub otherwise gives a free
# runner to the oldest matching job, so this is how a priority build passes queued PR builds.
PRIORITY_CLASS = "tauceti-fleet-pr-priority"
# An unassigned priority-only guest this long after coming online has no priority build left
# to take (GitHub assigns a waiting job within seconds); it is retired for an ordinary one.
PRIORITY_IDLE_SECONDS = 90
# A slot that has just retired an unneeded priority-only guest registers an ordinary one next,
# so a stale observation of waiting priority builds cannot keep a slot cycling.
PRIORITY_RETRY_SECONDS = 180


def priority_observation(config):
    """Queued fleet targets for priority registration, or None when unknown or not needed.

    The cached read never starts discovery, and a priority build queues after its own picker's
    read, so ask the shared service for a refresh first. It refreshes at most every two and a
    half minutes, coalesces callers and keeps scanning after a caller stops waiting, so a short
    timeout only falls back to the cache.
    """
    if not config.get("github_state_token_file") or capacity.kinds(config) != ["pr"]:
        return None
    base = config.get("github_state_url", "https://github-state.taucetiproject.org").rstrip("/")
    if urllib.parse.urlsplit(base).scheme != "https":
        raise FleetError("CI pool observation requires HTTPS")
    try:
        value = ci_overview.fetch(base + "/v1/runner-capacity?repository=TauCetiProject%2FTauCeti&namespace_cap=1",
                                  token_file=config.get("github_state_token_file"), timeout=3)
        observed, queued = value.get("observed_at"), value.get("queued_fleet")
        if (value.get("complete") is True and value.get("fleet_queue_version") == 1
                and type(observed) in (int, float) and -5 <= time.time() - observed / 1000 <= 150
                and isinstance(queued, list) and len(queued) <= 10000
                and all(isinstance(labels, list) and len(labels) <= 100 and all(isinstance(v, str) for v in labels)
                        for labels in queued)):
            return dict(observed_at=observed / 1000, queued=[set(v) for v in queued])
    except Exception:
        pass
    return reservations(config)


def priority_waiting(observation):
    """Queued priority PR builds; ordinary builds never name the priority class."""
    if not observation:
        return 0
    return sum(PRIORITY_CLASS in {label.lower() for label in queued} for queued in observation["queued"])


def priority_wanted(values, observation):
    """Priority-only registrations to add: waiting priority builds that this host's unassigned
    priority-only guests (booting or idle) do not already cover. Unknown means none."""
    spare = sum(1 for value in values if value and value.get("ci_priority_only") and not value.get("busy"))
    return max(0, priority_waiting(observation) - spare)


def priority_slot(config, index):
    """Only PR-only slots register priority-only runners: never reserved, protected or mixed
    PR and staging slots, so no staging capacity is taken."""
    value = capacity.for_slot(config, index)
    return (value["ci_kinds"] == ["pr"] and value.get("ci_slot_runner_group_id") is None
            and not value.get("ci_staging_reserved"))


def priority_surplus(value, own, now):
    return (bool(value.get("ci_priority_only")) and not value.get("busy") and not own.get("busy")
            and own.get("status") == "online" and bool(value.get("online_observed_at"))
            and now - value["online_observed_at"] >= PRIORITY_IDLE_SECONDS)
