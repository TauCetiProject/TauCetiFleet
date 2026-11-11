"""Slot groups: an agent and a set of work phases for each share of a host's worker slots."""

import functools
import re
from fractions import Fraction

from .store import FleetError

# The worker's work phases, in the order it lists them. An unknown phase in one
# definition stops the manager from loading any, so only these are accepted.
ROLES = ("rebase", "review", "fix-ci", "fix", "bump", "progress", "roadmap", "lint-repair", "overlap-scan", "overlap-fix")


def roles(value):
    if not isinstance(value, list) or not all(isinstance(role, str) and role in ROLES for role in value):
        raise FleetError("A slot group's roles must be a list drawn from: " + ", ".join(ROLES))
    return [role for role in ROLES if role in value]


def strategy(value):
    if value != "threshold":
        raise FleetError("A slot group's progress_strategy must be threshold")
    return value


def limit(value):
    if type(value) is not int or not 1 <= value <= 4096:
        raise FleetError("A slot group's progress_max_open must be a positive integer at most 4096")
    return value


def areas(value):
    if not isinstance(value, list) or not all(isinstance(a, str) and re.fullmatch(r"[A-Za-z0-9_-]+", a) for a in value):
        raise FleetError("A slot group's progress_skip must be a list of roadmap directory names")
    return sorted(set(value))


def names(text):
    return [name for name in text.split(",") if name]


def number(text):
    return int(text) if text.isascii() and text.isdigit() else text


# Group key -> (worker definition key, normalizer, reader of its --group text,
# the phase it configures), beside `count` and `agent`. A further per-group
# worker setting is one more entry; `roles` stays first, since validate checks
# each later key's phase against it.
KEYS = {
    "roles": ("only", roles, names, None),
    "progress_strategy": ("progress_strategy", strategy, str, "progress"),
    "progress_max_open": ("progress_max_open", limit, number, "progress"),
    "progress_skip": ("progress_skip", areas, names, "progress"),
}


def validate(raw):
    if not isinstance(raw, list) or not raw:
        raise FleetError("slots must be a non-empty list of groups")
    groups = []
    for group in raw:
        if not isinstance(group, dict) or set(group) - {"count", "agent", *KEYS}:
            raise FleetError("A slot group takes count, agent, " + ", ".join(KEYS))
        if type(group.get("count")) is not int or not 1 <= group["count"] <= 4096:
            raise FleetError("A slot group's count must be a positive integer at most 4096")
        value = dict(count=group["count"])
        if group.get("agent"):
            # Whether the profile allows this agent is for the caller's worker_fields to say.
            if not isinstance(group["agent"], str):
                raise FleetError("A slot group's agent must be claude, codex, or auto")
            value["agent"] = group["agent"]
        for key, (_, normal, _, phase) in KEYS.items():
            if group.get(key) not in (None, []):
                value[key] = normal(group[key])
                if phase and phase not in value.get("roles", ROLES):
                    raise FleetError(f"A slot group's {key} needs {phase} among its roles")
        groups.append(value)
    if sum(group["count"] for group in groups) > 4096:
        raise FleetError("slots may total at most 4096")
    return groups


def parse(text):
    """One --group value: COUNT:AGENT[:ROLE,ROLE...][:KEY=VALUE...]; an empty AGENT keeps the profile's.

    KEY is an entry of KEYS, and a list VALUE is joined by commas.
    """
    usage = FleetError(f"--group {text!r} must be COUNT:AGENT[:ROLE,ROLE...][:KEY=VALUE...], KEY one of: " + ", ".join(KEYS))
    count, agent, *fields = text.split(":") + [""] * (":" not in text)
    if not (count.isascii() and count.isdigit()):
        raise usage
    group = dict(count=int(count))
    if agent:
        group["agent"] = agent
    for position, field in enumerate(fields):
        key, named, value = field.partition("=")
        if not named and position == 0:
            key, value = "roles", field
        if key not in KEYS or key in group:
            raise usage
        if value:
            group[key] = KEYS[key][2](value)
    return group


def place(wanted, prior):
    """Group index for each worker position, None where no group has room.

    `wanted` is (count, settings) per group and `prior` the settings each
    existing position has now. A position stays in a group that still describes
    it; only then are the others reused, lowest first, and new positions added.
    """
    room = [count for count, _ in wanted]
    placed = []
    for settings in prior:
        group = next((g for g, (_, own) in enumerate(wanted) if room[g] and own == settings), None)
        if group is not None:
            room[group] -= 1
        placed.append(group)
    spare = [position for position, group in enumerate(placed) if group is None]
    for group, missing in enumerate(room):
        for _ in range(missing):
            if spare:
                placed[spare.pop(0)] = group
            else:
                placed.append(group)
    return placed


@functools.cache
def sequence(counts):
    """The group of each admission in turn, for groups of these sizes.

    Still's quota-divisor method with Webster priorities: member j of a group
    of n ranks at (2j + 1) / 2n, among the groups that stay within the upper
    quota of this prefix and leave this and every later prefix able to reach
    each group's lower quota. Every prefix then holds each group's share of it to within one
    slot, and a higher ceiling only adds.
    """
    total = sum(counts)
    taken = [0] * len(counts)

    def reachable(prefix):
        for later in range(prefix, total + 1):
            floors = [later * count // total for count in counts]
            if sum(max(t, f) for t, f in zip(taken, floors, strict=True)) > later:
                return False
            if all(t <= f for t, f in zip(taken, floors, strict=True)):
                return True
        return True

    result = []
    for prefix in range(1, total + 1):
        ranked = sorted((g for g, count in enumerate(counts) if taken[g] * total < prefix * count),
                        key=lambda g: (Fraction(2 * taken[g] + 1, counts[g]), g))
        for group in ranked:
            taken[group] += 1
            if reachable(prefix):
                break
            taken[group] -= 1
        result.append(group)
    return tuple(result)


def order(placed):
    """Placed positions in admission order, each group's in position order."""
    members = {}
    for position, group in enumerate(placed):
        if group is not None:
            members.setdefault(group, []).append(position)
    groups = sorted(members)
    taken = dict.fromkeys(groups, 0)
    result = []
    for index in sequence(tuple(len(members[g]) for g in groups)):
        group = groups[index]
        result.append(members[group][taken[group]])
        taken[group] += 1
    return result


def layout(config, old, worker_fields):
    """(definition keys, group index or None) for each worker position.

    `old` holds the existing definitions in position order. One outside every
    group keeps its agent and phases, and its other keys follow the profile.
    """
    default = config.get("agent") or "claude"
    wanted = [dict({key: group[key] for key in KEYS if key in group}, agent=group.get("agent") or default)
              for group in config["slots"]]
    prior = [dict({key: worker[name] for key, (name, *_) in KEYS.items() if worker.get(name)}, agent=worker.get("agent"))
             for worker in old]
    counts = [group["count"] for group in config["slots"]]
    result = []
    for position, group in enumerate(place(list(zip(counts, wanted, strict=True)), prior)):
        own = prior[position] if group is None else wanted[group]
        try:
            fields = worker_fields(dict(config, agent=own["agent"]))
            fields.update((name, own[key]) for key, (name, *_) in KEYS.items() if key in own)
        except FleetError:
            if group is not None:
                raise
            fields = worker_fields(config)
        result.append((fields, group))
    return result
