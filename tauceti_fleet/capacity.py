"""CI job classes and a shared, machine-wide build limit."""

from .store import FleetError

KINDS = {"pr": ["pr"], "staging": ["staging"], "both": ["pr", "staging"]}


def settings(config, *, ci, maximum=None, vcpus=None, memory_gib=None, disk_gib=None):
    if not isinstance(ci, str) or ci not in KINDS:
        raise FleetError("Select CI jobs explicitly: --ci pr, --ci staging, or --ci both")
    result = dict(config, ci_kinds=KINDS[ci], ci_max_jobs=jobs(config))
    for name, value, upper in (("ci_max_jobs", maximum, 16), ("ci_vcpus", vcpus, 4096),
                               ("ci_memory_gib", memory_gib, 4096), ("ci_disk_gib", disk_gib, 4096)):
        if value is None:
            continue
        if type(value) is not int or not 1 <= value <= upper:
            raise FleetError(f"{name} must be an integer between 1 and {upper}")
        result[name] = value
    return result


def jobs(config):
    count = config.get("ci_max_jobs", 1)
    if type(count) is not int or not 1 <= count <= 16:
        raise FleetError("ci_max_jobs must be an integer between 1 and 16")
    return count


def kinds(config):
    value = config.get("ci_kinds", ["staging"])
    if value not in KINDS.values():
        raise FleetError("ci_kinds must explicitly select pr, staging, or both")
    return value


def authorize(config, policy):
    if jobs(config) > policy.get("max_jobs", 1) or not set(kinds(config)) <= set(policy.get("kinds", ["staging"])):
        raise FleetError("Requested CI capacity exceeds the enrollment policy; ask the operator to update this machine's CI policy")
