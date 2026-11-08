"""CI job classes and a shared, machine-wide build limit."""

import math

from .store import FleetError

KINDS = {"pr": ["pr"], "staging": ["staging"], "both": ["pr", "staging"]}
MAX_JOBS = 4096


def guest_shape(config, caps=None):
    """Expose shared host capacity, not a fixed fraction reserved per guest."""
    if config.get("ci_kinds") == ["control"]:
        return dict(vcpus=2, memory_gib=4)
    if config.get("ci_kinds") == ["roadmap"]:
        return dict(vcpus=16, memory_gib=32)
    if caps is None:
        from .capabilities import resources

        caps = resources()
    return dict(vcpus=min(255, max(config["ci_vcpus"], math.ceil(caps["cpus"]))),
                memory_gib=max(config["ci_memory_gib"], math.ceil(memory_budget_gib(config, caps))))


def settings(config, *, ci, maximum=None, vcpus=None, memory_gib=None, disk_gib=None, memory_budget_gib=None):
    if not isinstance(ci, str) or ci not in KINDS:
        raise FleetError("Select CI jobs explicitly: --ci pr, --ci staging, or --ci both")
    result = dict(config, ci_kinds=KINDS[ci], ci_max_jobs=jobs(config))
    for name, value, upper in (("ci_max_jobs", maximum, MAX_JOBS), ("ci_vcpus", vcpus, 4096),
                               ("ci_memory_gib", memory_gib, 4096), ("ci_disk_gib", disk_gib, 4096),
                               ("ci_memory_budget_gib", memory_budget_gib, 1048576)):
        if value is None:
            continue
        if type(value) is not int or not 1 <= value <= upper:
            raise FleetError(f"{name} must be an integer between 1 and {upper}")
        result[name] = value
    staging_slots(result)
    return result


def shared_memory_bytes(caps):
    gib = 1024**3
    reserve = max(4 * gib, int(caps["memory_total"] * 0.1))
    return max(16 * 1024**2, caps["memory_total"] - reserve - gib)


def memory_budget_gib(config, caps=None):
    """Physical CI slice ceiling, independent of overcommitted guest RAM."""
    value = config.get("ci_memory_budget_gib")
    if value is None:
        if caps is None:
            from .capabilities import resources

            caps = resources()
        return shared_memory_bytes(caps) / 1024**3
    if type(value) is not int or not 1 <= value <= 1048576:
        raise FleetError("ci_memory_budget_gib must be a positive integer at most 1048576")
    return value


def disk_startup_gib(config):
    """QCOW2 disks allocate on write; virtual size is not reserved space."""
    return min(4, config["ci_disk_gib"])


def disk_root():
    from .store import state_dir

    root = state_dir() / "vms"
    while not root.exists():
        root = root.parent
    return root


def jobs(config):
    count = config.get("ci_max_jobs", 1)
    if type(count) is not int or not 1 <= count <= MAX_JOBS:
        raise FleetError(f"ci_max_jobs must be an integer between 1 and {MAX_JOBS}")
    return count


def kinds(config):
    value = config.get("ci_kinds", ["staging"])
    if value not in [*KINDS.values(), ["control"], ["roadmap"]]:
        raise FleetError("ci_kinds must explicitly select pr, staging, or both")
    return value


def staging_slots(config):
    """Slots whose future guests accept staging work exclusively."""
    count = config.get("ci_staging_slots", 0)
    if type(count) is not int or not 0 <= count <= jobs(config):
        raise FleetError("ci_staging_slots must be an integer between zero and ci_max_jobs")
    if count and "staging" not in kinds(config):
        raise FleetError("Staging reservations require staging in the machine's CI classes")
    protected_slots(config)
    return count


def protected_slots(config):
    """Explicit slots in separate runner groups; never borrowed by PR recovery."""
    values = config.get("ci_protected_slots", {})
    if not isinstance(values, dict):
        raise FleetError("ci_protected_slots must map slot numbers to kind and runner_group_id")
    result = {}
    for key, value in values.items():
        if (not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key
                or not config.get("ci_staging_slots", 0) <= int(key) < jobs(config)
                or not isinstance(value, dict) or set(value) != {"kind", "runner_group_id"}
                or value["kind"] not in ("control", "roadmap")
                or type(value["runner_group_id"]) is not int or value["runner_group_id"] <= 0
                or value["runner_group_id"] == config.get("runner_group_id")
                or config.get("ci_broker_url")):
            raise FleetError("Protected slots require a separate runner group, a valid slot, and direct runner credentials")
        result[int(key)] = value
    return result


def reservation(config):
    """Report a corrupt reservation without stopping unrelated worker admissions."""
    try:
        return staging_slots(config), None
    except FleetError as exc:
        return 0, str(exc)


def for_slot(config, index):
    protected = protected_slots(config).get(index)
    if protected:
        return dict(config, ci_slot=index, ci_kinds=[protected["kind"]],
                    ci_slot_runner_group_id=protected["runner_group_id"])
    reserved, _ = reservation(config)
    return dict(config, ci_slot=index, ci_kinds=["staging"] if index < reserved else kinds(config))


def authorize(config, policy):
    jobs(config)  # Validate the locally chosen count, independently of broker policy.
    if not set(kinds(config)) <= set(policy.get("kinds", ["staging"])):
        raise FleetError("Requested CI job classes exceed the enrollment policy; ask the operator to update this machine's CI classes")
