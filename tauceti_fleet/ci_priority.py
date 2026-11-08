"""Staging gets CPU precedence and a smaller compiler admission RAM margin."""

from . import lean_pressure
from .process import run
from .store import FleetError, atomic, locked, read, state_dir

FIELDS = ("ci_job_kind", "ci_kind_retry_at", "ci_priority_error", "cpu_weight")


def staging(value):
    return value.get("ci_kinds") in (["staging"], ["roadmap"], ["control"]) or value.get("ci_job_kind") == "staging"


def headroom(total, *, staging=False):
    return total * (1 - lean_pressure.memory_limit(total, staging=staging) / 100)


def frame(observation, *, staging=False):
    """Keep live older guests compatible, with measured values explicit.

    Old wrappers only know the two-file CPU/RAM gate. For them the legacy
    fields express admission capacity. New wrappers use the measured fields and
    explicit priority. Host status always reports the original measurements.
    """
    total = observation["memory_total"]
    delta = headroom(total) - headroom(total, staging=True) if staging else 0
    return dict(observation, priority="staging" if staging else "normal",
                observed_cpu_percent=observation["cpu_percent"],
                observed_memory_available=observation["memory_available"],
                cpu_percent=0 if staging else observation["cpu_percent"],
                memory_available=min(total, observation["memory_available"] + delta))


def apply(value):
    weight = 10000 if staging(value) else 100
    if value.get("cpu_weight") != weight:
        run(["systemctl", "--user", "set-property", "--runtime", value["unit"], f"CPUWeight={weight}"])
        value["cpu_weight"] = weight


def receipts(status):
    if isinstance(status.get("ci_vms"), list):
        return status["ci_vms"]
    # Controller errors can omit ci_vms. Guest ownership and reserved staging
    # classes remain durable; a failed inventory must not revoke precedence.
    values = []
    for path in [state_dir() / "vm.json", *(state_dir() / "ci-slots").glob("*/vm.json")]:
        try:
            value = read(path, {})
            if value:
                values.append(value)
        except FleetError:
            continue
    return values


def persist(config, before, value):
    from . import vm

    changed = [key for key in FIELDS if (key in before) != (key in value) or before.get(key) != value.get(key)]
    if not changed:
        return value
    with locked(vm.slot_file(config, "vm.lock")):
        path = vm.slot_file(config, "vm.json")
        current = read(path, {})
        if current.get("instance") != value.get("instance"):
            return {}
        for key in changed:
            if key in value:
                current[key] = value[key]
            else:
                current.pop(key, None)
        atomic(path, current)
        return current


def classify(config, value, now):
    """One bounded metadata read per shared guest; failed reads back off."""
    from . import github

    if value.get("ci_kinds") == ["staging"]:
        value["ci_job_kind"] = "staging"
    elif value.get("ci_kinds") in (["roadmap"], ["control"]):
        value["ci_job_kind"] = value["ci_kinds"][0]
    elif value.get("ci_kinds") == ["pr"]:
        value["ci_job_kind"] = "pr"
    elif value.get("busy") and value.get("run_id") and not value.get("ci_job_kind"):
        if now >= value.get("ci_kind_retry_at", 0):
            value["ci_kind_retry_at"] = now + 120
            try:
                run_value = github.ci_read(config, "repos/TauCetiProject/TauCeti/actions/runs/" + str(value["run_id"]))
                if run_value.get("id") != value["run_id"]:
                    raise FleetError("CI job classification returned a different run")
                is_staging = (run_value.get("event") == "repository_dispatch"
                              and str(run_value.get("display_title", "")).startswith("bors branch="))
                if is_staging:
                    jobs = github.ci_read(config, "repos/TauCetiProject/TauCeti/actions/runs/" + str(value["run_id"]) + "/jobs?per_page=100")
                    matches = [job for job in jobs.get("jobs", []) if job.get("runner_id") == value["runner_id"]
                               and job.get("status") == "in_progress"]
                    if len(matches) != 1:
                        raise FleetError("Staging run is not assigned to this guest")
                value["ci_job_kind"] = "staging" if is_staging else "pr"
                value.pop("ci_priority_error", None)
            except FleetError as error:
                value["ci_priority_error"] = str(error)
    apply(value)
