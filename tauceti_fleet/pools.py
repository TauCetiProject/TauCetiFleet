"""Named worker pools owned by the host controller, with durable subscription stops.

The original pool keeps its IDs, manager and paths. Additional pools have private
managers/credentials but share the host's worker slice and memory admission.
"""

import base64
import hashlib
import json
import math
import os
import re
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .process import executable, run
from .store import FleetError, atomic, config_dir, data_dir, host_config, locked, read, state_dir

IDLE = {"waiting-capacity", "waiting-quota", "waiting-budget", "backoff", "stopped", "exited"}
FIELDS = {
    "profile",
    "agent",
    "billing",
    "github_account",
    "account",
    "max_workers",
    "max_open_prs",
    "pacing",
    "pace",
    "stop_on_exhaustion",
    "author_model",
    "author_effort",
    "github_state_url",
}
SECRETS = {"anthropic_api_key_file", "github_token_file", "codex_auth_file", "github_state_token_file"}


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", value):
        raise FleetError("Pool names must be 1–40 lowercase letters, digits or hyphens, starting with a letter")
    return value


def legacy_name(config):
    return config.get("profile", "default")


def entries(config):
    return config.get("worker_pools", {})


def paths(pool):
    name(pool)
    return config_dir() / "pools" / pool, state_dir() / "pools" / pool


def maximum(config):
    return sum(p["max_workers"] for p in entries(config).values())


def validate(value):
    if value.get("agent") not in ("claude", "codex"):
        raise FleetError("Pool agent must be claude or codex")
    if (value["agent"], value.get("billing")) not in (("claude", "api"), ("codex", "subscription")):
        raise FleetError("Supported pool billing: Claude API or Codex subscription")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", value.get("github_account", "")):
        raise FleetError("Select a valid github_account for the pool")
    for field in ("max_workers", "max_open_prs"):
        minimum = 0 if field == "max_workers" else 1
        if type(value.get(field)) is not int or not minimum <= value[field] <= 4096:
            raise FleetError(f"{field} must be an integer from {minimum} to 4096")
    if value.get("pacing") not in ("paced", "exhaust"):
        raise FleetError("Subscription pacing must be paced or exhaust")
    if type(value.get("stop_on_exhaustion")) is not bool:
        raise FleetError("stop_on_exhaustion must be a boolean")
    if value["billing"] == "api" and (value["pacing"] != "paced" or value["stop_on_exhaustion"] or value.get("pace")):
        raise FleetError("Subscription pacing and exhaustion policies cannot be applied to API billing")
    if value["billing"] == "subscription" and not str(value.get("account", "")).strip():
        raise FleetError("A subscription pool requires an explicit account email or account ID")
    if value.get("pace"):
        if value["pacing"] != "paced":
            raise FleetError("A pacing curve requires --pacing paced")
        points = []
        try:
            for point in value["pace"].split(","):
                x, y = (float(v) for v in point.split(":"))
                if not (0 <= x <= 100 and 0 <= y and math.isfinite(y)):
                    raise ValueError()
                points.append((x, y))
            if not points or any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(points, points[1:], strict=False)):
                raise ValueError()
        except (ValueError, TypeError):
            raise FleetError(
                "Invalid pacing curve; use increasing time%:budget% points, e.g. 0:10,50:70,90:90"
            ) from None


def credential(auth, account):
    """Validate identity before copying; never send the single-use refresh token."""
    try:
        value = json.loads(auth) if isinstance(auth, str) else auth
        tokens = dict(value["tokens"])
        part = tokens["access_token"].split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        identity = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
        email = claims.get("https://api.openai.com/profile", {}).get("email", claims.get("email", ""))
        if identity != tokens["account_id"] or account.lower() not in (identity.lower(), email.lower()):
            raise ValueError()
        if claims["exp"] <= time.time() + 300:
            raise ValueError()
        tokens["refresh_token"] = "rt.0.tauceti-worker-placeholder-never-a-real-refresh-token"
        return dict(auth_mode="chatgpt", tokens=tokens, last_refresh=value.get("last_refresh"))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise FleetError(
            "Codex access credential is expired, invalid or does not match the selected subscription account"
        ) from None


def install_credentials(pool, value, secrets):
    if not isinstance(secrets, dict) or set(secrets) - SECRETS:
        raise FleetError("Unknown pool credential kind")
    prepared = {}
    for key, secret in secrets.items():
        if not isinstance(secret, str) or not secret.strip() or len(secret) > 65536:
            raise FleetError("Invalid pool credential")
        prepared[key] = json.dumps(credential(secret, value["account"])) if key == "codex_auth_file" else secret.strip()
    directory, _ = paths(pool)
    for key, secret in prepared.items():
        path = directory / "codex/auth.json" if key == "codex_auth_file" else directory / "secrets" / key
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=path.parent)
        with os.fdopen(fd, "w") as stream:
            stream.write(secret + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        value[key] = str(path)


def configure(payload):
    pool = name(payload["pool"])
    operation = payload.get("operation", "set")
    if operation not in ("set", "sync", "stop", "resume"):
        raise FleetError("Unknown pool operation")
    host_config()
    path = config_dir() / "host.json"
    with locked(path.with_suffix(".lock")):
        config = read(path)
        if pool == legacy_name(config):
            # Preserve the existing pool's identity and lifecycle during adoption.
            if operation != "set" or set(payload.get("settings", {})) - {"max_workers"} or payload.get("credentials"):
                raise FleetError("The original pool supports --workers here; its existing host profile is preserved")
            maximum = payload["settings"]["max_workers"]
            if type(maximum) is not int or not 0 <= maximum <= 4096:
                raise FleetError("Worker count must be an integer from 0 to 4096")
            config["max_workers"] = maximum
            atomic(path, config)
            return dict(pool=pool, max_workers=maximum)
        existing = entries(config).get(pool)
        if operation in ("resume", "stop", "sync") and existing is None:
            raise FleetError(f"Unknown worker pool {pool!r}")
        value = dict(
            existing
            or dict(
                agent="claude", billing="api", pacing="paced", max_workers=8, max_open_prs=128, stop_on_exhaustion=False
            )
        )
        settings = payload.get("settings", {})
        if not isinstance(settings, dict) or set(settings) - FIELDS:
            raise FleetError("Unknown worker pool setting")
        if operation == "sync" and settings:
            raise FleetError("Credential sync cannot change pool settings")
        value.update(settings)
        if existing is None:
            for field in ("github_state_url", "github_state_token_file"):
                if config.get(field):
                    value.setdefault(field, config[field])
        validate(value)
        policy = config.get("subscription_pool")
        if policy is not None and value["billing"] == "subscription" and operation != "stop":
            if (
                not isinstance(policy, dict)
                or not isinstance(policy.get("name"), str)
                or type(policy.get("max_workers")) is not int
                or not 0 <= policy["max_workers"] <= 4096
            ):
                raise FleetError("Invalid single subscription pool policy")
            selected = name(policy["name"])
            if pool != selected:
                raise FleetError(f"This host uses one subscription pool: switch --profile on {selected!r} instead")
            if value["max_workers"] > policy["max_workers"]:
                raise FleetError(f"Subscription pool is limited to {policy['max_workers']} workers")
        if existing and any(value.get(k) != existing.get(k) for k in FIELDS - {"max_workers", "stop_on_exhaustion"}):
            if any(w.get("alive") for w in statuses(pool)):
                raise FleetError(
                    "Stop this pool and let its active rounds finish before changing its identity or model settings"
                )
        install_credentials(pool, value, payload.get("credentials", {}))
        required = [
            "github_token_file",
            "codex_auth_file" if value["billing"] == "subscription" else "anthropic_api_key_file",
        ]
        if any(not value.get(k) or not Path(value[k]).is_file() for k in required):
            raise FleetError("Pool profile is missing its GitHub or provider credential")
        _, state = paths(pool)
        settings_changed = existing and any(
            value.get(k) != existing.get(k) for k in FIELDS - {"max_workers", "stop_on_exhaustion"}
        )
        if settings_changed:
            value["generation"] = value.get("generation", 0) + 1
            with locked(state / "subscription.lock"):
                previous = read(state / "subscription.json", {})
                previous.pop("quota", None)
                atomic(state / "subscription.json", dict(previous, generation=value["generation"]))
        if operation in ("resume", "stop"):
            # Generation fences observations in flight across an explicit resume.
            value["generation"] = value.get("generation", 0) + 1
            with locked(state / "subscription.lock"):
                atomic(
                    state / "subscription.json",
                    dict(
                        generation=value["generation"],
                        stopped=operation == "stop",
                        reason="operator stopped pool" if operation == "stop" else None,
                    ),
                )
        config.setdefault("worker_pools", {})[pool] = value
        atomic(path, config)
    return dict(pool=pool, **{k: value[k] for k in FIELDS if k in value}, operation=operation)


def statuses(pool):
    _, state = paths(pool)
    result = []
    for path in (state / "workers").glob("*.json"):
        value = read(path, {})
        if value.get("id"):
            result.append(
                {
                    k: value.get(k)
                    for k in (
                        "id",
                        "alive",
                        "state",
                        "phase",
                        "target",
                        "detail",
                        "heartbeat_at",
                        "scope_unit",
                        "wrapper_pid",
                    )
                }
            )
    return result


def definition(value):
    fields = {
        k: value[k]
        for k in (
            "agent",
            "github_account",
            "max_open_prs",
            "author_model",
            "author_effort",
            "github_state_url",
            "github_state_token_file",
        )
        if value.get(k) is not None
    }
    if value["billing"] == "api":
        fields.update(claude_billing="api", budget=True, anthropic_api_key_file=value["anthropic_api_key_file"])
    else:
        if value["pacing"] == "exhaust":
            fields["ignore_quota"] = True
        if value.get("pace"):
            fields["pace"] = value["pace"]
    return fields


def environment_line(value):
    """Keep operator-owned worker state paths across controller reconciliation."""
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise FleetError("Worker environment must contain string names and values")
    return "env = { " + ", ".join(json.dumps(k) + " = " + json.dumps(v) for k, v in value.items()) + " }"


def write_workers(pool, value, config, *, stopped=False, workers=()):
    directory, _ = paths(pool)
    path = directory / "workers.toml"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Preserve IDs/counts so reducing admissions never terminates an active session.
    with locked(path.with_suffix(".toml.lock")):
        previous = path.read_text() if path.exists() else ""
        old = tomllib.loads(previous).get("workers", []) if previous else []
        environments = {w["id"]: w["env"] for w in old if "env" in w}
        ids = [w["id"] for w in old]
        if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", w) for w in ids):
            raise FleetError("Invalid or duplicate worker IDs in pool configuration")
        prefix = f"fleet-{config['machine_id'][:8]}-{pool}-"
        while len(ids) < value["max_workers"]:
            candidate = prefix + str(len(ids) + 1)
            if candidate in ids:
                raise FleetError("Pool worker ID collision")
            ids.append(candidate)
        observed = {w["id"]: w for w in workers}
        fields = definition(value)
        lines = ["version = 1"]
        for wid in ids:
            status = observed.get(wid, {})
            safe = not status.get("alive") or (
                status.get("state") in IDLE and time.time() - status.get("heartbeat_at", 0) < 30
            )
            enabled = not stopped or not safe
            lines += ["", "[[workers]]", "id = " + json.dumps(wid)]
            # Omit enabled=true exactly as the legacy generator/worker normalizer does.
            if not enabled:
                lines.append("enabled = false")
            for k, v in fields.items():
                lines.append(k + " = " + json.dumps(v))
            if wid in environments:
                lines.append(environment_line(environments[wid]))
        text = "\n".join(lines) + "\n"
        if text != previous:
            fd, temporary = tempfile.mkstemp(dir=directory)
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
            os.replace(temporary, path)
    return ids


def manager_identity(value):
    fields = (
        "agent",
        "billing",
        "account",
        "github_account",
        "github_token_file",
        "github_state_url",
        "github_state_token_file",
    )
    return hashlib.sha256(json.dumps({k: value.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def start_manager(pool, *, restart=False):
    from .host import RUNNING_RELEASE, command, unit_argument, units

    unit = f"tauceti-fleet-pool-{name(pool)}.service"
    argv = " ".join(unit_argument(a) for a in [*command(), "run-workers", "--pool", pool])
    environment = {"PYTHONPATH": RUNNING_RELEASE, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for key in ("TAUCETI_FLEET_CONFIG", "TAUCETI_FLEET_STATE", "TAUCETI_FLEET_DATA"):
        if key in os.environ:
            environment[key] = os.environ[key]
    (units() / unit).write_text(
        "[Unit]\nDescription=TauCeti Fleet worker pool " + pool + "\n[Service]\nType=simple\n"
        "Slice=tauceti-fleet-workers.slice\nExecStart="
        + argv
        + "\n"
        + "".join("Environment=" + unit_argument(k + "=" + v) + "\n" for k, v in environment.items())
        + "LimitNOFILE=65536\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=30\nOOMPolicy=continue\nOOMScoreAdjust=100\n"
    )
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "restart" if restart else "start", unit])


def launch(pool):
    config = host_config()
    if pool not in entries(config):
        raise FleetError(f"Unknown worker pool {pool!r}")
    value = entries(config)[pool]
    directory, state = paths(pool)
    env = os.environ
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "CODEX_HOME",
        "TAUCETI_ACCOUNT",
        "TAUCETI_CLAUDE_BILLING",
    ):
        env.pop(key, None)
    env.update(
        PATH=str(data_dir() / "bin") + os.pathsep + env.get("PATH", "/usr/bin:/bin"),
        TAUCETI_FLEET_ADMISSION_FILE=str(state / "admission.json"),
        TAUCETI_FLEET_MACHINE_ID=config["machine_id"],
        TAUCETI_FLEET_POOL=pool,
        TAUCETI_FLEET_EVENTS=str(state / "worker-events.jsonl"),
        TAUCETI_WORKERS_STATE_DIR=str(state / "workers"),
        TAUCETI_RUNTIME_DIR=str(state / "worker-runtime"),
        TAUCETI_DATA_HOME=str(data_dir() / "pools" / pool / "worker-data"),
        TAUCETI_WORKER_SLICE="tauceti-fleet-workers.slice",
        TAUCETI_WORKER_OOM_SCORE_ADJ="500",
    )
    gh = state / "gh"
    gh.mkdir(parents=True, exist_ok=True, mode=0o700)
    env.update(GH_CONFIG_DIR=str(gh), GH_TOKEN=Path(value["github_token_file"]).read_text().strip())
    for field in ("github_state_url", "github_state_token_file"):
        if value.get(field):
            env["TAUCETI_" + field.upper()] = value[field]
    if value["billing"] == "subscription":
        env.update(CODEX_HOME=str(directory / "codex"), TAUCETI_ACCOUNT=value["account"])
    env.setdefault("TAUCETI_BUDGET_DIR", str(Path.home() / ".local/state/tauceti/budget"))
    binary = executable("tauceti")
    if not binary:
        raise FleetError("Pinned worker runtime is unavailable; run prepare")
    binary = str(Path(binary).resolve())
    atomic(state / "manager.json", dict(identity=manager_identity(value), pid=os.getpid()))
    os.execv(binary, [binary, "workers", "--config", str(directory / "workers.toml"), "manager"])


def observe(pool, value):
    directory, state = paths(pool)
    binary = executable("tauceti")
    if not binary:
        raise FleetError("Pinned worker runtime unavailable")
    python = Path(binary).resolve().parent / "python"
    request = dict(codex_home=str(directory / "codex"), cache=str(state / "quota"), account=value["account"])
    result = run([python, Path(__file__).with_name("subscription.py")], input=json.dumps(request), timeout=45)
    return json.loads(result.stdout)


def exhausted(observation, now):
    # An HTTP 429, auth failure, missing/old/malformed telemetry, or pace wait
    # is never evidence that the purchased subscription quota was consumed.
    if not observation or observation.get("error") or observation.get("rate_limited"):
        return False
    if not 0 <= now - observation.get("at", 0) <= 120:
        return False
    return any(
        w.get("status") == "exhausted" and type(w.get("resets_at")) in (int, float) and w["resets_at"] > now
        for w in observation.get("windows", [])
    )


def matches_observation(value, observation):
    requested = str(value.get("account", "")).lower()
    return bool(requested) and requested in {
        str(observation.get("account_id", "")).lower(),
        str(observation.get("email", "")).lower(),
    }


def transition(prior, observation, value, now):
    result = dict(prior)
    if observation is not None:
        result["quota"] = observation
    if (
        not result.get("stopped")
        and value.get("stop_on_exhaustion")
        and matches_observation(value, result.get("quota", {}))
        and exhausted(result.get("quota"), now)
    ):
        result.update(stopped=True, reason="subscription quota exhausted; explicit resume required", stopped_at=now)
    return result


def summary(pool, value, workers, admitted, state):
    quota = state.get("quota", {})
    alive = [w for w in workers if w.get("alive")]
    return dict(
        name=pool,
        profile=value.get("profile"),
        agent=value["agent"],
        billing=value["billing"],
        github_account=value.get("github_account", "tauceti-ai-for-science"),
        account=value.get("account"),
        max_workers=value["max_workers"],
        admitted=len(admitted),
        alive=len(alive),
        running=sum(w.get("state") == "running" for w in alive),
        surveying=sum(w.get("state") == "surveying" for w in alive),
        pacing=value.get("pacing"),
        pace=value.get("pace"),
        stop_on_exhaustion=value.get("stop_on_exhaustion", False),
        lifecycle=("stopping" if alive else "stopped") if state.get("stopped") else "enabled",
        reason=state.get("reason"),
        stopped_at=state.get("stopped_at"),
        quota=quota,
        credential_file=value.get("anthropic_api_key_file")
        if value["billing"] == "api"
        else value.get("codex_auth_file"),
    )


class Controller:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subscription-usage")
        self.pending = {}
        self.next_probe = {}
        self.started = {}

    def step(self, config, *, capacity_open, reason):
        summaries, workers = [], []
        for pool, value in entries(config).items():
            try:
                found, live = self._step(
                    dict(config, worker_pools={pool: value}), capacity_open=capacity_open, reason=reason
                )
                summaries.extend(found)
                workers.extend(live)
            except (FleetError, OSError, ValueError, KeyError, TypeError) as exc:
                self.close_admissions(dict(config, worker_pools={pool: value}), "pool error; run doctor")
                result = summary(pool, value, [], [], {})
                result.update(lifecycle="error", reason=str(exc))
                summaries.append(result)
        return summaries, workers

    def _step(self, config, *, capacity_open, reason):
        summaries, all_workers = [], []
        for pool, value in entries(config).items():
            directory, state = paths(pool)
            workers = statuses(pool)
            identity = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            state_file = state / "subscription.json"
            with locked(state / "subscription.lock"):
                prior = read(state_file, {})
                if prior.get("generation", 0) != value.get("generation", 0):
                    continue  # A stop/resume committed after this controller snapshot.
                observation = None
                pending = self.pending.get(pool)
                if pending and pending[1].done():
                    self.pending.pop(pool)
                    if pending[0] == identity:
                        try:
                            observation = pending[1].result()
                        except Exception:
                            observation = dict(at=time.time(), error="Subscription observation unavailable", windows=[])
                current = transition(prior, observation, value, time.time())
                if current != prior:
                    atomic(state_file, current)
            if (
                value["billing"] == "subscription"
                and not current.get("stopped")
                and pool not in self.pending
                and time.time() >= self.next_probe.get(pool, 0)
            ):
                self.pending[pool] = (identity, self.executor.submit(observe, pool, dict(value)))
                self.next_probe[pool] = time.time() + 60
            quota = current.get("quota", {})
            identity_ok = value["billing"] == "api" or (
                not quota.get("error")
                and matches_observation(value, quota)
                and 0 <= time.time() - quota.get("at", 0) <= 120
            )
            allowed = capacity_open and not current.get("stopped") and identity_ok and not exhausted(quota, time.time())
            # Publish the closed gate BEFORE disabling idle workers. An active
            # round finishes; at its next boundary it waits behind this gate.
            old_ids = (
                [w["id"] for w in tomllib.loads((directory / "workers.toml").read_text()).get("workers", [])]
                if (directory / "workers.toml").exists()
                else []
            )
            atomic(
                state / "admission.json",
                dict(
                    version=1,
                    expires_at=time.time() + 15,
                    workers=old_ids[: value["max_workers"]] if allowed else [],
                    reason=current.get("reason")
                    or (reason if not capacity_open else "subscription quota/identity check"),
                ),
            )
            ids = write_workers(pool, value, config, stopped=current.get("stopped", False), workers=workers)
            admitted = ids[: value["max_workers"]] if allowed else []
            atomic(
                state / "admission.json",
                dict(
                    version=1,
                    expires_at=time.time() + 15,
                    workers=admitted,
                    reason=current.get("reason") or (reason if not capacity_open else "pool quota/identity check"),
                ),
            )
            if (
                config["workers_enabled"]
                and self.started.get(pool) != manager_identity(value)
                and (not current.get("stopped") or any(w.get("alive") for w in workers))
            ):
                manager = read(state / "manager.json", {})
                start_manager(pool, restart=manager.get("identity") != manager_identity(value))
                self.started[pool] = manager_identity(value)
            summaries.append(summary(pool, value, workers, admitted, current))
            all_workers.extend(dict(w, pool=pool, github_account=value["github_account"]) for w in workers)
        return summaries, all_workers

    def close_admissions(self, config, reason):
        for pool in entries(config):
            _, state = paths(pool)
            atomic(state / "admission.json", dict(version=1, expires_at=time.time() + 15, workers=[], reason=reason))

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def display(values):
    if not values:
        return
    print("Worker pools:")
    for value in values:
        billing = "Claude API" if value["billing"] == "api" else "Codex subscription " + str(value.get("account", ""))
        print(
            f"  {value['name']}: {value['alive']}/{value['max_workers']} workers alive, {value['running']} running, "
            f"{value['admitted']} admitted — GitHub {value['github_account']}; {billing}"
        )
        if value["billing"] == "subscription":
            policy = (
                "exhaust as quickly as possible"
                if value.get("pacing") == "exhaust"
                else "paced" + (" (" + value["pace"] + ")" if value.get("pace") else "")
            )
            print(
                "    "
                + policy
                + ("; stop on exhaustion" if value.get("stop_on_exhaustion") else "; wait for quota reset")
            )
            quota = value.get("quota", {})
            if quota.get("error"):
                print("    Quota: " + quota["error"])
            for window in quota.get("windows", []):
                print(f"    {window['name']}: {window.get('used', '?')}% used; {window['status']}")
            if quota.get("at"):
                print(f"    Usage observed {max(0, int(time.time() - quota['at']))}s ago")
        if value.get("credential_file") and value["billing"] == "api":
            print("    API key file: " + value["credential_file"])
        if value.get("lifecycle") != "enabled":
            print(f"    {value['lifecycle']}: {value.get('reason') or ''}")
