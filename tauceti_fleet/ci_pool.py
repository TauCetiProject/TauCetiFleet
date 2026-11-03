"""A small warm CI pool; assigned and running guests survive pool changes."""

import time
import urllib.parse

from . import capacity, ci_overview, github
from .store import FleetError, atomic

# Pickers accept capacity observations up to 150s old. Allow outstanding
# placements to appear before deleting a runner whose routing labels changed.
WITHDRAWAL_SECONDS = 180


def plan(config, values, inventory, observation=None):
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
    desired = set(occupied)
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
            if i < reserved or not general:
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


def withdraw(config, value, own, observation, path):
    """Withdraw routing, preserve queued assignments, then permit idle deletion."""
    if own.get("busy") or value.get("busy") or not observation:
        return False
    original = value.get("routing_labels") or [r["name"] for r in own.get("labels", [])]
    if not original:
        return False
    labels = {label.lower() for label in original}
    assigned = any({label.lower() for label in queued} <= labels for queued in observation["queued"])
    if assigned:
        if value.get("withdrawing_at"):
            github.set_runner_labels(config, value["runner_id"], original)
            value.pop("withdrawing_at", None)
            value.pop("routing_labels", None)
            value.pop("routing_withdrawn", None)
            atomic(path, value)
        return False
    if not value.get("withdrawing_at"):
        # Persist the intent before mutation so a controller restart cannot
        # forget that this guest has been withdrawn from future selection.
        value.update(withdrawing_at=time.time(), routing_labels=original)
        atomic(path, value)
        github.set_runner_labels(config, value["runner_id"], [label for label in original
                                 if label != "tauceti-fleet" and not label.startswith("tauceti-fleet-")])
        value["routing_withdrawn"] = True
        atomic(path, value)
        return False
    if not value.get("routing_withdrawn"):
        github.set_runner_labels(config, value["runner_id"], [label for label in original
                                 if label != "tauceti-fleet" and not label.startswith("tauceti-fleet-")])
        value["routing_withdrawn"] = True
        atomic(path, value)
    return observation["observed_at"] > value["withdrawing_at"] + WITHDRAWAL_SECONDS
