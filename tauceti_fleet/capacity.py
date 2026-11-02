"""CI job classes and a shared, machine-wide build limit."""

from .store import FleetError

KINDS = {"pr": ["pr"], "staging": ["staging"], "both": ["pr", "staging"]}
MAX_JOBS = 4096


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


def memory_budget_gib(config):
    """Physical CI slice ceiling, independent of overcommitted guest RAM."""
    value = config.get("ci_memory_budget_gib")
    if value is None:
        return jobs(config) * (config["ci_memory_gib"] + 2)
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
    if value not in KINDS.values():
        raise FleetError("ci_kinds must explicitly select pr, staging, or both")
    return value


def staging_slots(config):
    """Slots whose future guests accept staging work exclusively."""
    count = config.get("ci_staging_slots", 0)
    if type(count) is not int or not 0 <= count <= jobs(config):
        raise FleetError("ci_staging_slots must be an integer between zero and ci_max_jobs")
    if count and "staging" not in kinds(config):
        raise FleetError("Staging reservations require staging in the machine's CI classes")
    return count


def reservation(config):
    """Report a corrupt reservation without stopping unrelated worker admissions."""
    try:
        return staging_slots(config), None
    except FleetError as exc:
        return 0, str(exc)


def for_slot(config, index):
    reserved, _ = reservation(config)
    return dict(config, ci_slot=index, ci_kinds=["staging"] if index < reserved else kinds(config))


def authorize(config, policy):
    if jobs(config) > policy.get("max_jobs", 1) or not set(kinds(config)) <= set(policy.get("kinds", ["staging"])):
        raise FleetError("Requested CI capacity exceeds the enrollment policy; ask the operator to update this machine's CI policy")
