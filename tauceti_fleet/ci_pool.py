"""A small warm CI pool; assigned and running guests survive pool changes."""

import time
import urllib.parse

from . import capacity, ci_overview
from .store import FleetError, atomic, locked, read, state_dir


def plan(config, values, inventory, observation=None, recovery_slots=None, pending_recovery=False):
    live = {v.get("slot", 0): v for v in values if v}
    busy_ids = {r["id"] for r in inventory if r.get("busy")}
    if observation:
        for i, value in live.items():
            own = next((r for r in inventory if r["id"] == value.get("runner_id")), {})
            labels = {label.lower() for label in (value.get("routing_labels") or
                      [label["name"] for label in own.get("labels", [])])}
            if any({label.lower() for label in queued} <= labels for queued in observation["queued"]):
                busy_ids.add(value["runner_id"])
    occupied = {i for i, v in live.items() if v.get("busy") or v.get("runner_id") in busy_ids}
    if not config["ci_enabled"]:
        return occupied
    if config.get("ci_idle_pool") and observation is None:
        # Unknown queued assignments must not cause additional admissions.
        return set(live)
    maximum = capacity.jobs(config)
    desired = set(occupied) | set(recovery_slots or {})
    candidates = [i for i in range(maximum) if i not in occupied and not live.get(i, {}).get("withdrawing_at")]
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
        if len(occupied | result.keys()) >= capacity.jobs(config):
            break
        candidates = [i for i in range(capacity.jobs(config)) if i not in live_slots and i not in result
                      and (i >= reserved or record["ci_kinds"] == ["staging"])]
        if candidates:
            result[candidates[0]] = record
    return result
