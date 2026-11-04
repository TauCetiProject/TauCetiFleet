"""Staging gets CPU precedence and a smaller compiler admission RAM margin."""

from . import lean_pressure
from .process import run
from .store import FleetError


def staging(value):
    return value.get("ci_kinds") == ["staging"] or value.get("ci_job_kind") == "staging"


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


def classify(config, value, now):
    """One bounded metadata read per shared guest; failed reads back off."""
    from . import github

    if value.get("ci_kinds") == ["staging"]:
        value["ci_job_kind"] = "staging"
    elif value.get("busy") and value.get("run_id") and not value.get("ci_job_kind"):
        if now >= value.get("ci_kind_retry_at", 0):
            value["ci_kind_retry_at"] = now + 120
            try:
                run_value = github.ci_read(config, "repos/TauCetiProject/TauCeti/actions/runs/" + str(value["run_id"]))
                if run_value.get("id") != value["run_id"]:
                    raise FleetError("CI job classification returned a different run")
                value["ci_job_kind"] = ("staging" if run_value.get("event") == "repository_dispatch"
                                        and str(run_value.get("display_title", "")).startswith("bors branch=") else "pr")
                value.pop("ci_priority_error", None)
            except FleetError as error:
                value["ci_priority_error"] = str(error)
    apply(value)
