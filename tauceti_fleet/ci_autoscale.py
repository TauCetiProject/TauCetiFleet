"""Cached CI capacity control and durable PR-run recovery from an operator host."""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

from . import capacity, remote
from .store import FleetError, atomic, locked, read, state_dir

GIB = 1024**3
REPO = "TauCetiProject/TauCeti"
CGROUP_ROOT = Path("/sys/fs/cgroup")


def host_action(payload):
    """Authenticated SSH RPCs; no GitHub calls on the observation path."""
    from . import github, host, vm
    from .store import host_config, update_host

    config = host_config()
    mode = payload.get("mode", "snapshot")
    if mode == "limit":
        proposed = dict(config, ci_max_jobs=payload["maximum"])
        capacity.jobs(proposed)
        capacity.staging_slots(proposed)
        update_host(ci_max_jobs=proposed["ci_max_jobs"], validate=capacity.staging_slots)
        return dict(maximum=proposed["ci_max_jobs"])
    if mode == "note":
        value = payload["value"]
        if not isinstance(value, dict) or len(json.dumps(value)) > 4096:
            raise FleetError("Invalid CI autoscale observation")
        atomic(state_dir() / "ci-autoscale.json", dict(value, at=time.time()))
        return dict(saved=True)
    if mode == "cleanup":
        runner = payload["runner_id"]
        if type(runner) is not int or runner <= 0:
            raise FleetError("Invalid retired runner ID")
        owned = [read(path, {}) for path in [state_dir() / "ci-last-retired.json",
                 *(state_dir() / "ci-slots").glob("*/ci-last-retired.json")]]
        owned += read(state_dir() / "pending-ci.json", [])
        owned += read(state_dir() / "ci-autoscale-retired.json", [])
        if not any(v.get("runner_id") == runner for v in owned):
            raise FleetError("Retired runner ownership cannot be verified")
        try:
            github.retire_runner(config, runner)
        except FleetError as error:
            if "HTTP 404" not in str(error):
                raise
        return dict(removed=True)
    if mode == "retire":
        slot = payload["slot"]
        if type(slot) is not int or slot < capacity.staging_slots(config):
            raise FleetError("Reserved staging slots cannot be retired by recovery")
        slot_config = dict(config, ci_slot=slot)
        with locked(vm.slot_file(slot_config, "vm.lock")):
            value = read(vm.slot_file(slot_config, "vm.json"), {})
            if value.get("instance") != payload["instance"] or value.get("run_id") != payload["run_id"]:
                return dict(retired=False, reason="guest changed or already retired")
            # The operator-side transaction verifies the GitHub event. Bind
            # queueing and retirement to that instance under the launch lock.
            value = dict(value, operator_recovery=True)
            # A slot may finish another job before an API outage clears. Keep
            # recovery ownership separate from its replaceable last-retired file.
            ledger = state_dir() / "ci-autoscale-retired.json"
            entries = [entry for entry in read(ledger, [])
                       if entry["at"] > time.time() - 172800 and entry["runner_id"] != value["runner_id"]]
            entries.append(dict(at=time.time(), runner_id=value["runner_id"], instance=value["instance"], run_id=value["run_id"]))
            atomic(ledger, entries[-4096:])
            host.queue_ci_result(value, "healthy")
            vm._retire(slot_config, value)
        return dict(retired=True)
    if mode != "snapshot":
        raise FleetError("Unknown CI autoscale operation")
    status = read(state_dir() / "status.json", {})
    pressure = read(state_dir() / "lean-host-pressure.json", {})
    timing = read(state_dir() / "lean-statistics-report.json", {})
    # CI survives an explicit worker pause, including the worker cgroup's
    # removal. Resolve each actual CI unit instead of inferring its ancestry
    # from worker telemetry. Keep healthy units when another has just retired.
    units = {value["unit"] for value in status.get("ci_vms", []) if value.get("unit")}
    groups = {}
    if units:
        try:
            result = vm.run(["systemctl", "--user", "show", "--property=Id,ControlGroup", "--", *sorted(units)], check=False)
            for block in result.stdout.strip().split("\n\n"):
                properties = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
                group = Path(properties.get("ControlGroup", ""))
                if properties.get("Id") in units and group.is_absolute() and group != Path("/") and ".." not in group.parts:
                    groups[properties["Id"]] = CGROUP_ROOT / group.relative_to("/")
        except (FleetError, OSError):
            pass
    vms = []
    for original in status.get("ci_vms", []):
        value = dict(original)
        try:
            group = groups[value["unit"]]
            value["memory_bytes"] = int((group / "memory.current").read_text())
            value["cpu_usec"] = int(dict(line.split() for line in (group / "cpu.stat").read_text().splitlines())["usage_usec"])
        except (OSError, ValueError, KeyError):
            value["memory_bytes"] = value["cpu_usec"] = None
        vms.append(value)
    return dict(machine_id=config["machine_id"], at=time.time(), status_at=status.get("observed_at", 0),
                enabled=config["ci_enabled"], maximum=capacity.jobs(config),
                reserved=capacity.staging_slots(config), cpus=status.get("resources", {}).get("cpus", 1),
                pressure=pressure, timing_at=timing.get("at", 0), ci=timing.get("ci", {}),
                last_admission=timing.get("last_admission_seen", {}).get("ci"),
                observation_since=timing.get("admission_seen_since", timing.get("at", 0)), vms=vms)


def fresh(snapshot, now):
    return all(type(at) in (int, float) and math.isfinite(at) and -5 <= now - at <= 20
               for at in (snapshot.get("status_at"), snapshot.get("timing_at"), snapshot.get("pressure", {}).get("at")))


class Controller:
    def __init__(self, value=None, *, minimum=1, maximum=4, growth_delay=180, stall_delay=90):
        self.value = value or {}
        self.minimum, self.maximum = minimum, maximum
        self.growth_delay, self.stall_delay = growth_delay, stall_delay

    def decide(self, snapshot, now):
        from . import lean_pressure

        if not snapshot.get("enabled"):
            self.value.pop("growth_since", None)
            return dict(kind="hold", reason="CI disabled")
        if not fresh(snapshot, now):
            self.value.pop("growth_since", None)
            self.value.pop("stall_since", None)
            return dict(kind="hold", reason="waiting for fresh host telemetry")
        pressure, ci = snapshot["pressure"], snapshot["ci"]
        total, available = pressure["memory_total"], pressure["memory_available"]
        headroom = total * (1 - lean_pressure.memory_limit(total) / 100)
        limit = snapshot["maximum"]
        busy = [v for v in snapshot["vms"] if v.get("busy")]
        idle = [v for v in snapshot["vms"] if not v.get("busy")]
        general_idle = [v for v in idle if v.get("slot", 0) >= snapshot["reserved"]]
        busy_general = [v for v in busy if v.get("slot", 0) >= snapshot["reserved"]]
        waiting = ci.get("waiting_now", 0) > 0
        low = available < headroom + GIB
        prior = self.value.get("sample", {})
        elapsed = now - prior.get("at", now)
        previous = prior.get("cpu", {})
        cpu = {v["instance"]: (v["cpu_usec"] - previous[v["instance"]]) / 1e6 / elapsed
               for v in snapshot["vms"] if 0 < elapsed <= 60 and v.get("cpu_usec") is not None
               and v["instance"] in previous and v["cpu_usec"] >= previous[v["instance"]]}
        self.value["sample"] = dict(at=now, cpu={v["instance"]: v["cpu_usec"] for v in snapshot["vms"]
                                                if v.get("cpu_usec") is not None})
        last = snapshot.get("last_admission") or snapshot.get("observation_since") or now
        stalled = low and waiting and not ci.get("running_now") and now - last >= self.stall_delay
        stalled = stalled and pressure.get("cpu_percent", 100) < 2
        if stalled:
            self.value.setdefault("stall_since", now)
        else:
            self.value.pop("stall_since", None)
        floor = max(self.minimum, snapshot["reserved"] + 1)
        if limit < floor <= self.maximum and available >= headroom + 21 * GIB:
            return dict(kind="limit", target=floor, reason="restore general CI capacity alongside reserved staging")
        if low and general_idle and limit > floor:
            self.value.pop("growth_since", None)
            target = max(floor, min(limit - 1, len(busy) + len(idle) - len(general_idle)))
            if target < limit:
                return dict(kind="limit", target=target, reason="return idle guest RAM to active builds")
        if stalled and now - self.value["stall_since"] >= 30 and limit >= self.minimum:
            candidates = [v for v in busy_general if v.get("run_id") and cpu.get(v["instance"], 1) < .2]
            if candidates and now - self.value.get("last_recovery", {}).get("at", 0) >= 120:
                self.value.pop("growth_since", None)
                return dict(kind="recover", target=max(floor, limit - 1),
                            candidates=sorted(candidates, key=lambda v: (v.get("memory_bytes") or 0,
                                              v.get("job_started_at", v["started_at"])), reverse=True),
                            reason="no CI compiler admissions and idle PR guests under RAM pressure")
        # Charge unrealized preparation growth rather than assuming that a newly
        # booted 3-GiB guest will stay small. No physical memory is reserved.
        budget = max(20 * GIB, max((v.get("memory_bytes") or 0 for v in snapshot["vms"]
                                   if v.get("runtime") != "native-tmpfs"), default=0) * 1.1)
        pending = sum(max(0, (v.get("expected_memory_bytes", budget) if v.get("runtime") == "native-tmpfs" else budget)
                          - (v["memory_bytes"] if v.get("memory_bytes") is not None else budget)) for v in snapshot["vms"])
        room = available - pending >= headroom + budget + GIB
        saturated = len(busy_general) > 0 and len(busy_general) + min(1, len(general_idle)) >= limit - snapshot["reserved"]
        growth = (room and saturated and limit < self.maximum
                  and all(v.get("memory_bytes") is not None for v in snapshot["vms"]))
        if growth and pressure.get("cpu_percent", 100) < 85:
            self.value.setdefault("growth_since", now)
            if now - self.value["growth_since"] >= self.growth_delay:
                self.value.pop("growth_since", None)
                return dict(kind="limit", target=limit + 1, reason="sustained spare RAM for another guest and compiler headroom")
            return dict(kind="hold", reason="confirming sustained RAM headroom before increasing CI")
        self.value.pop("growth_since", None)
        if stalled:
            reason = "confirming a RAM admission stall"
        elif waiting and not ci.get("running_now") and pressure.get("cpu_percent", 0) >= 2:
            reason = "Lean waiting; other CI work is using CPU"
        elif ci.get("running_now"):
            reason = "Lean compilers are running"
        elif waiting:
            reason = "Lean waiting; monitoring admission headroom"
        else:
            reason = "no observed Lean waiters; monitoring CI capacity"
        return dict(kind="hold", reason=reason)


class Operator:
    def __init__(self, target, identity, *, minimum=1, maximum=4, root=None, github_account=None):
        self.target, self.identity = target, identity
        self.path = (root or state_dir()) / ("ci-autoscale-" + target + ".json")
        self.controller = Controller(read(self.path, {}), minimum=minimum, maximum=maximum)
        self.last_note = 0
        self.github_account = github_account
        self.environments = {}

    def save(self):
        atomic(self.path, self.controller.value)

    def host(self, mode, **values):
        return remote.call(self.target, dict(action="ci-autoscale", mode=mode,
                           expected_machine_id=self.identity, **values), timeout=35)

    def api(self, path, *, method="GET"):
        account = "tauceti-ai-for-science" if method == "GET" else self.github_account
        if account and account not in self.environments:
            token = subprocess.run(["gh", "auth", "token", "--hostname", "github.com", "--user", account],
                                   capture_output=True, text=True, timeout=10)
            if token.returncode or not token.stdout.strip():
                raise FleetError("CI recovery GitHub account is unavailable: " + account)
            self.environments[account] = dict(os.environ, GH_TOKEN=token.stdout.strip())
        if not account:
            raise FleetError("CI recovery requires an explicitly selected GitHub write account")
        result = subprocess.run(["gh", "api", "repos/" + REPO + "/" + path, "--method", method],
                                capture_output=True, text=True, timeout=25,
                                env=self.environments.get(account))
        if result.returncode:
            raise FleetError("GitHub recovery request failed: " + (result.stderr or result.stdout)[-350:])
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def transaction(self):
        pending = self.controller.value["pending"]
        run = self.api("actions/runs/" + str(pending["run_id"]))
        if pending["phase"] == "cancel":
            if run["run_attempt"] > pending["attempt"]:
                pending["phase"] = "retire"
            elif run["status"] != "completed":
                try:
                    self.api("actions/runs/" + str(pending["run_id"]) + "/force-cancel", method="POST")
                except (FleetError, OSError, subprocess.TimeoutExpired):
                    # A write-quota outage must not keep a verified stalled PR
                    # guest holding RAM. Its durable transaction still owns the
                    # eventual cancellation and full-workflow resubmission.
                    self.host("retire", slot=pending["slot"], instance=pending["instance"], run_id=pending["run_id"])
                    pending["retired"] = True
                    self.save()
                    raise
            elif run.get("conclusion") != "cancelled" and not pending.get("retired"):
                self.controller.value.pop("pending")
                return
            pending["phase"] = "retire"
            self.save()
        if pending["phase"] == "retire":
            self.host("retire", slot=pending["slot"], instance=pending["instance"], run_id=pending["run_id"])
            pending["retired"] = True
            pending["phase"] = "rerun"
            self.save()
        if pending["phase"] == "rerun":
            if run["run_attempt"] <= pending["attempt"]:
                if run["status"] != "completed":
                    pending["retry_at"] = time.time() + 15
                    return
                self.api("actions/runs/" + str(pending["run_id"]) + "/rerun", method="POST")
                pending["phase"] = "verify"
                self.save()
                return
            pending["phase"] = "cleanup"
            self.save()
        if pending["phase"] == "verify":
            if run["run_attempt"] <= pending["attempt"]:
                pending["phase"] = "rerun"
                return
            pending["phase"] = "cleanup"
            self.save()
        if pending["phase"] == "cleanup":
            self.host("cleanup", runner_id=pending["runner_id"])
            self.controller.value["last_recovery"] = dict(at=time.time(), run_id=pending["run_id"])
            self.controller.value.pop("stall_since", None)
            self.controller.value.pop("pending")

    def tick(self):
        now = time.time()
        value = self.controller.value
        if value.get("pending"):
            if now >= value["pending"].get("retry_at", 0):
                try:
                    self.transaction()
                except (FleetError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                    pending = value["pending"]
                    pending["failures"] = pending.get("failures", 0) + 1
                    pending["retry_at"] = now + min(300, 15 * 2 ** min(5, pending["failures"]))
                    value["error"] = str(error)[-350:]
                else:
                    value.pop("error", None)
                self.save()
        snapshot = self.host("snapshot")
        if snapshot["machine_id"] != self.identity:
            raise FleetError("CI autoscaler host identity changed")
        decision = self.controller.decide(snapshot, snapshot["at"])
        if value.get("pending") and (decision["kind"] == "recover" or
                decision["kind"] == "limit" and decision["target"] > snapshot["maximum"]):
            decision = dict(kind="hold", reason="PR resubmission pending; capacity growth deferred")
        value["target"] = snapshot["maximum"]
        if decision["kind"] == "limit":
            self.host("limit", maximum=decision["target"])
            value["target"] = decision["target"]
        elif decision["kind"] == "recover":
            candidates = decision["candidates"] if now >= value.get("probe_retry_at", 0) else []
            if candidates:
                value["probe_retry_at"] = now + 60
                self.save()
            for guest in candidates:
                run = self.api("actions/runs/" + str(guest["run_id"]))
                if run.get("event") != "pull_request_target" or run["status"] != "in_progress":
                    continue
                jobs = self.api("actions/runs/" + str(guest["run_id"]) + "/jobs?per_page=100")
                own = [job for job in jobs.get("jobs", []) if job.get("runner_id") == guest["runner_id"]
                       and job.get("status") == "in_progress"]
                if len(own) != 1:
                    continue
                value["pending"] = dict(phase="cancel", run_id=run["id"], attempt=run["run_attempt"],
                                        slot=guest.get("slot", 0), instance=guest["instance"], runner_id=guest["runner_id"])
                self.save()
                self.host("limit", maximum=decision["target"])
                value["target"] = decision["target"]
                break
        value["last_decision"] = dict(at=now, **{k:v for k,v in decision.items() if k != "candidates"})
        self.save()
        if now - self.last_note >= 60 or decision["kind"] != "hold":
            note = dict(target=value.get("target"), ceiling=self.controller.maximum,
                        reason=decision["reason"], pending_run=value.get("pending", {}).get("run_id"), error=value.get("error"))
            self.host("note", value=note)
            self.last_note = now
        return decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--minimum", type=int, default=1)
    parser.add_argument("--maximum", type=int, default=4)
    parser.add_argument("--github-account", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    remote.validate_target(args.host)
    if not 1 <= args.minimum <= args.maximum <= capacity.MAX_JOBS:
        parser.error("invalid adaptive CI bounds")
    path = state_dir() / ("ci-autoscale-" + args.host + ".json")
    with locked(path.with_suffix(".lock")):
        operator = Operator(args.host, args.machine_id, minimum=args.minimum, maximum=args.maximum, github_account=args.github_account)
        while True:
            try:
                print(json.dumps(operator.tick()), flush=True)
            except (FleetError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                print("CI autoscale: " + str(error), file=sys.stderr, flush=True)
            if args.once:
                return
            time.sleep(15)


if __name__ == "__main__":
    main()
