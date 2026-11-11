"""Slot groups: an agent and a set of work phases for each share of a host's worker slots."""

import functools
import heapq
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


class _Slack:
    """Range add and range maximum over prefixes 0..size."""

    def __init__(self, values):
        self.size = len(values)
        self.top = [0] * (4 * self.size)
        self.add = [0] * (4 * self.size)
        self._build(1, 0, self.size - 1, values)

    def _build(self, node, low, high, values):
        if low == high:
            self.top[node] = values[low]
            return
        middle = (low + high) // 2
        self._build(2 * node, low, middle, values)
        self._build(2 * node + 1, middle + 1, high, values)
        self.top[node] = max(self.top[2 * node], self.top[2 * node + 1])

    def update(self, first, last, delta, node=1, low=0, high=None):
        high = self.size - 1 if high is None else high
        if last < low or high < first or first > last:
            return
        if first <= low and high <= last:
            self.top[node] += delta
            self.add[node] += delta
            return
        middle = (low + high) // 2
        self.update(first, last, delta, 2 * node, low, middle)
        self.update(first, last, delta, 2 * node + 1, middle + 1, high)
        self.top[node] = self.add[node] + max(self.top[2 * node], self.top[2 * node + 1])

    def maximum(self, first, node=1, low=0, high=None):
        high = self.size - 1 if high is None else high
        if high < first:
            return None
        if first <= low:
            return self.top[node]
        middle = (low + high) // 2
        values = [v for v in (self.maximum(first, 2 * node, low, middle),
                              self.maximum(first, 2 * node + 1, middle + 1, high)) if v is not None]
        return self.add[node] + max(values)


@functools.cache
def sequence(counts):
    """The group of each admission in turn, for groups of these sizes.

    Still's quota-divisor method with Webster priorities: member j of a group
    of n ranks at (2j + 1) / 2n, among the groups that stay within the upper
    quota of this prefix and leave this and every later prefix able to reach
    each group's lower quota. Every prefix then holds each group's share of it
    to within one slot, and a higher ceiling only adds.

    A group holding t is ahead of its lower quota at prefixes below
    ceil(t * total / n), and a prefix L can carry as many groups ahead as the
    sum of its fractional shares, r(L). `slack` keeps (groups ahead - r) per
    prefix, so trying a group is a range add and one range maximum.
    """
    total = sum(counts)
    sizes = Counter(counts)
    slack = _Slack([sum(m * (later * n // total) for n, m in sizes.items()) - later for later in range(total + 1)])
    # Groups of one size holding as many are interchangeable but for index, so
    # each (size, held) class is tried once per prefix, by its lowest index.
    members = {}
    for group, count in enumerate(counts):
        members.setdefault((count, 0), []).append(group)
    heap = [(Fraction(1, count), groups[0], count, 0) for (count, _), groups in members.items()]
    heapq.heapify(heap)
    result = []
    for prefix in range(1, total + 1):
        skipped = []
        while True:
            entry = heapq.heappop(heap)
            _, lowest, count, held = entry
            if members.get((count, held), [None])[0] != lowest:
                continue
            if held * total < prefix * count:
                first, last = -(-held * total // count), -(-(held + 1) * total // count) - 1
                slack.update(first, last, 1)
                if slack.maximum(prefix) <= 0:
                    break
                slack.update(first, last, -1)
            skipped.append(entry)
        heapq.heappop(members[count, held])
        if members[count, held]:
            group = members[count, held][0]
            heapq.heappush(heap, (Fraction(2 * held + 1, count), group, count, held))
        else:
            del members[count, held]
        if held + 1 < count:
            moved = members.setdefault((count, held + 1), [])
            heapq.heappush(moved, lowest)
            if moved[0] == lowest:
                heapq.heappush(heap, (Fraction(2 * held + 3, count), lowest, count, held + 1))
        for entry in skipped:
            heapq.heappush(heap, entry)
        result.append(lowest)
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
