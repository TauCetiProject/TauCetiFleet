"""Slot groups: an agent and a set of work phases for each share of a host's worker slots."""

from collections import Counter
from fractions import Fraction

from .store import FleetError

# The worker's work phases, in the order it lists them. An unknown phase in one
# definition stops the manager from loading any, so only these are accepted.
ROLES = ("rebase", "review", "fix-ci", "fix", "bump", "progress", "roadmap", "lint-repair", "overlap-scan", "overlap-fix")


def roles(value):
    if not isinstance(value, list) or not all(isinstance(role, str) and role in ROLES for role in value):
        raise FleetError("A slot group's roles must be a list drawn from: " + ", ".join(ROLES))
    return [role for role in ROLES if role in value]


# Group key -> (worker definition key, normalizer), beside `count` and `agent`.
# A further per-group worker setting is one more entry.
KEYS = {"roles": ("only", roles)}


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
        for key, (_, normal) in KEYS.items():
            if group.get(key) not in (None, []):
                value[key] = normal(group[key])
        groups.append(value)
    if sum(group["count"] for group in groups) > 4096:
        raise FleetError("slots may total at most 4096")
    return groups


def parse(text):
    """One --group value: COUNT:AGENT[:ROLE,ROLE...]; an empty AGENT keeps the profile's."""
    count, _, rest = text.partition(":")
    agent, _, listed = rest.partition(":")
    if not (count.isascii() and count.isdigit()):
        raise FleetError(f"--group {text!r} must be COUNT:AGENT[:ROLE,ROLE...]")
    group = dict(count=int(count))
    if agent:
        group["agent"] = agent
    if listed:
        group["roles"] = [role for role in listed.split(",") if role]
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


def order(placed):
    """Placed positions in admission order.

    Member j of a group of n sits at (2j + 1) / 2n, so every prefix holds each
    group's share of it to within one slot and a higher ceiling only adds.
    """
    size = Counter(group for group in placed if group is not None)
    seen = Counter()
    keyed = []
    for position, group in enumerate(placed):
        if group is not None:
            keyed.append((Fraction(2 * seen[group] + 1, 2 * size[group]), group, position))
            seen[group] += 1
    return [position for _, _, position in sorted(keyed)]


def layout(config, old, worker_fields):
    """(definition keys, group index or None) for each worker position.

    `old` holds the existing definitions in position order. One outside every
    group keeps its agent and phases, and its other keys follow the profile.
    """
    default = config.get("agent") or "claude"
    wanted = [dict({key: group[key] for key in KEYS if key in group}, agent=group.get("agent") or default)
              for group in config["slots"]]
    prior = [dict({key: worker[name] for key, (name, _) in KEYS.items() if worker.get(name)}, agent=worker.get("agent"))
             for worker in old]
    counts = [group["count"] for group in config["slots"]]
    result = []
    for position, group in enumerate(place(list(zip(counts, wanted, strict=True)), prior)):
        own = prior[position] if group is None else wanted[group]
        try:
            fields = worker_fields(dict(config, agent=own["agent"]))
            fields.update((name, own[key]) for key, (name, _) in KEYS.items() if key in own)
        except FleetError:
            if group is not None:
                raise
            fields = worker_fields(config)
        result.append((fields, group))
    return result
