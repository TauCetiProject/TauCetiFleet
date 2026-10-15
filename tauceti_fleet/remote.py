"""SSH deployment and a JSON stdin protocol. No shell interpolation of payloads."""

import io
import json
import re
import shlex
import zipfile
from pathlib import Path

from .process import run
from .store import FleetError, atomic, config_dir, locked, read


def validate_target(target):
    if not re.fullmatch(r"(?:[a-zA-Z0-9_][a-zA-Z0-9_.-]*@)?[a-zA-Z0-9_][a-zA-Z0-9_.-]*", target):
        raise FleetError("Use an SSH hostname/alias or user@hostname; configure ports and jump hosts in ~/.ssh/config")
    return target


def ssh(target, remote_command, *, input=None, timeout=60):
    validate_target(target)
    # Existing known-host verification remains enabled. Never accept new host
    # keys silently, disable verification, or retry an ambiguous mutation.
    return run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", target, remote_command],
        input=input,
        timeout=timeout,
    )


def call(target, payload, *, timeout=90):
    if target == "local":
        from .host import rpc

        return rpc(payload)
    result = ssh(target, '"$HOME/.local/bin/tauceti-fleet" _host', input=json.dumps(payload), timeout=timeout)
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        raise FleetError(
            "Remote host did not return a fleet response; check its installation and SSH login shell"
        ) from None
    if not reply.get("ok"):
        raise FleetError(reply.get("error", "Remote operation failed"))
    return reply["result"]


BOOTSTRAP = r"""import hashlib,io,json,os,pathlib,shlex,sys,tempfile,zipfile
raw=sys.stdin.buffer.read(32*1024*1024+1)
if len(raw)>32*1024*1024: raise SystemExit('Fleet release exceeds deployment limit')
digest=hashlib.sha256(raw).hexdigest()
base=pathlib.Path.home()/'.local/share/tauceti-fleet'
base.mkdir(parents=True,exist_ok=True,mode=0o700)
release=base/'releases'/digest
release.parent.mkdir(exist_ok=True,mode=0o700)
with zipfile.ZipFile(io.BytesIO(raw)) as archive:
    for entry in archive.infolist():
        p=pathlib.PurePosixPath(entry.filename)
        if p.is_absolute() or '..' in p.parts or not (entry.filename.startswith('tauceti_fleet/') or entry.filename=='release.json'):
            raise SystemExit('Invalid release archive member')
        if entry.file_size>4*1024*1024: raise SystemExit('Release member too large')
    if not release.exists():
        temporary=pathlib.Path(tempfile.mkdtemp(dir=release.parent))
        archive.extractall(temporary)
        temporary.rename(release)
current=base/'current'
temporary=base/('.current-'+str(os.getpid()))
temporary.symlink_to(release)
os.replace(temporary,current)
metadata=json.loads((release/'release.json').read_text())
(base/'release.json').write_text(json.dumps(metadata))
binary=pathlib.Path.home()/'.local/bin/tauceti-fleet'
binary.parent.mkdir(parents=True,exist_ok=True)
launcher='import sys; sys.path.insert(0,'+repr(str(current))+'); from tauceti_fleet.cli import main; raise SystemExit(main())'
binary.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -c '+shlex.quote(launcher)+' "$@"\n')
binary.chmod(0o755)
print(json.dumps(dict(installed=True,release=digest)))
"""


def archive():
    root = Path(__file__).resolve().parent
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(root.glob("*.py")):
            zipped.write(path, "tauceti_fleet/" + path.name)
        manifest = read(root / "release.json", read(root.parent / "release.json", {}))
        if not manifest.get("worker_revision"):
            raise FleetError("Release manifest has no pinned worker revision")
        zipped.writestr("release.json", json.dumps(manifest))
    return stream.getvalue()


def profile(name):
    profiles = read(config_dir() / "profiles.json", {})
    if name in profiles:
        value = dict(profiles[name])
    elif name == "ai-for-science":
        value = dict(
            github_account="tauceti-ai-for-science",
            anthropic_api_key_file=str(Path.home() / ".config/tauceti/secrets/TAUCETI_AI_FOR_SCIENCE_API_KEY"),
        )
        # Reuse the configured science profile's account/model/read-service
        # choices without copying a whole credential directory or raw config.
        import tomllib

        path = Path.home() / ".config/tauceti/workers.toml"
        if path.exists():
            workers = tomllib.loads(path.read_text()).get("workers", [])
            source = next((w for w in workers if w.get("id") == "tauceti-ai-for-science"), {})
            for key in (
                "github_account",
                "max_open_prs",
                "anthropic_api_key_file",
                "author_model",
                "author_effort",
                "github_state_url",
                "github_state_token_file",
            ):
                if source.get(key):
                    value[key] = source[key]
    else:
        raise FleetError(f"Profile {name!r} does not exist; define it in {config_dir() / 'profiles.json'}")
    allowed = {"github_account", "max_open_prs", "author_model", "author_effort", "runner_group_id", "github_state_url"}
    result = {k: v for k, v in value.items() if k in allowed}
    credentials = {}
    for name in ("anthropic_api_key_file", "github_token_file", "ci_token_file", "github_state_token_file"):
        if value.get(name):
            path = Path(value[name]).expanduser()
            if path.exists():
                if path.stat().st_mode & 0o077:
                    raise FleetError(f"Credential file {path} is not private; use chmod 600")
                credentials[name] = path.read_text().strip()
    if "github_token_file" not in credentials and result.get("github_account"):
        token = run(["gh", "auth", "token", "--user", result["github_account"]], check=False)
        if token.returncode == 0:
            credentials["github_token_file"] = token.stdout.strip()
    result["credentials"] = credentials
    return result


def inventory():
    return read(config_dir() / "inventory.json", {})


def resolve(name):
    entries = inventory()
    if name in entries:
        return entries[name]
    for entry in entries.values():
        if name in (entry["target"], entry["machine_id"]) or name in entry.get("aliases", []):
            return entry
    raise FleetError(f"Unknown fleet host {name!r}; use add first")


def add(target, *, profile_name="ai-for-science", no_ci=False):
    if target != "local":
        result = ssh(target, "python3 -c " + shlex.quote(BOOTSTRAP), input=archive(), timeout=120)
        if not json.loads(result.stdout).get("installed"):
            raise FleetError("Remote deployment did not complete")
    else:
        import sys

        run([sys.executable, "-c", BOOTSTRAP], input=archive(), timeout=120)
    payload = dict(action="configure", profile=profile_name, ci_allowed=not no_ci, **profile(profile_name))
    status = call(target, payload)
    name = target.split("@")[-1]
    path = config_dir() / "inventory.json"
    with locked(path.with_suffix(".lock")):
        entries = inventory()
        matching = next((k for k, v in entries.items() if v["machine_id"] == status["machine_id"]), name)
        prior = entries.get(matching, {})
        aliases = sorted(set(prior.get("aliases", []) + [target, name]))
        entries[matching] = dict(target=target, machine_id=status["machine_id"], aliases=aliases, profile=profile_name)
        atomic(path, entries)
    readiness = call(target, dict(action="doctor"))
    return dict(host=matching, installed=status, doctor=readiness)
