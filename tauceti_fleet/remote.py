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


def call(target, payload, *, timeout=90, expected_machine_id=None):
    if expected_machine_id is not None:
        payload = dict(payload, expected_machine_id=expected_machine_id)
        if payload.get("action") != "status":
            # Older hosts ignore the new RPC field. Read their stable identity
            # before sending a mutation or replacing their installed release.
            call(target, dict(action="status"), timeout=min(timeout, 90), expected_machine_id=expected_machine_id)
    if target == "local":
        from .host import rpc

        value = rpc(payload)
    else:
        result = ssh(target, '"$HOME/.local/bin/tauceti-fleet" _host', input=json.dumps(payload), timeout=timeout)
        try:
            reply = json.loads(result.stdout)
        except ValueError:
            raise FleetError(
                "Remote host did not return a fleet response; check its installation and SSH login shell"
            ) from None
        if not reply.get("ok"):
            raise FleetError(reply.get("error", "Remote operation failed"))
        value = reply["result"]
    if expected_machine_id is not None and payload.get("action") == "status" and value.get("machine_id") != expected_machine_id:
        raise FleetError("Host identity differs from inventory; verify the SSH target before acting")
    return value


BOOTSTRAP = r"""import hashlib,io,json,os,pathlib,shlex,sys,tempfile,zipfile
raw=sys.stdin.buffer.read(32*1024*1024+1)
if len(raw)>32*1024*1024: raise SystemExit('Fleet release exceeds deployment limit')
digest=hashlib.sha256(raw).hexdigest()
base=pathlib.Path(os.environ.get('TAUCETI_FLEET_DATA',pathlib.Path.home()/'.local/share/tauceti-fleet')).expanduser()
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
if len(sys.argv)>1 and sys.argv[1] in ('live','fleet-only'):
    sys.path.insert(0,str(release))
    from tauceti_fleet.update import install
    from tauceti_fleet.store import FleetError
    try:
        result=install(release,expected_machine_id=sys.argv[2],preserve_worker=True) if sys.argv[1]=='fleet-only' else install(release,expected_machine_id=sys.argv[2])
    except FleetError as exc:
        result=dict(installed=False,error=str(exc))
    print(json.dumps(result))
    raise SystemExit(0)
current=base/'current'
temporary=base/('.current-'+str(os.getpid()))
temporary.symlink_to(release)
os.replace(temporary,current)
metadata=json.loads((release/'release.json').read_text())
(base/'release.json').write_text(json.dumps(metadata))
binary=pathlib.Path.home()/'.local/bin/tauceti-fleet'
binary.parent.mkdir(parents=True,exist_ok=True)
launcher='import os,sys; sys.path.insert(0,os.path.realpath('+repr(str(current))+')); from tauceti_fleet.cli import main; raise SystemExit(main())'
python=getattr(sys,'_base_executable',sys.executable)
binary.write_text('#!/bin/sh\nexec '+shlex.quote(python)+' -c '+shlex.quote(launcher)+' "$@"\n')
binary.chmod(0o755)
print(json.dumps(dict(installed=True,release=digest)))
"""


def archive():
    root = Path(__file__).resolve().parent
    stream = io.BytesIO()
    def member(zipped, name, contents):
        # A release is identified by contents, including when a host packages
        # its own extracted installation. Extraction timestamps are incidental.
        entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        zipped.writestr(entry, contents)
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(root.glob("*.py")):
            member(zipped, "tauceti_fleet/" + path.name, path.read_bytes())
        manifest = read(root / "release.json", read(root.parent / "release.json", {}))
        if not manifest.get("worker_revision"):
            raise FleetError("Release manifest has no pinned worker revision")
        member(zipped, "release.json", json.dumps(manifest, sort_keys=True))
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
    if value.get("ci_broker_url") or value.get("ci_broker_token_file"):
        raise FleetError("Machine-bound CI credentials cannot be deployed as a profile; use enroll HOST")
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
    raise FleetError(f"Unknown fleet host {name!r}; use add or enroll first")


def deploy(target, *, live=False, expected_machine_id=None, fleet_only=False):
    """Install this release on the explicitly selected machine."""
    if fleet_only and not live:
        raise FleetError("A Fleet-only update requires the live handover path")
    args = ["fleet-only" if fleet_only else "live", expected_machine_id] if live else []
    if live:
        if not expected_machine_id:
            raise FleetError("Live update requires the installed host identity")
        call(target, dict(action="status"), expected_machine_id=expected_machine_id)
    if target != "local":
        command = "python3 -c " + shlex.quote(BOOTSTRAP)
        command += "".join(" " + shlex.quote(a) for a in args)
        result = ssh(target, command, input=archive(), timeout=1800 if live else 120)
    else:
        import sys

        python = getattr(sys, "_base_executable", sys.executable)
        result = run([python, "-c", BOOTSTRAP, *args], input=archive(), timeout=1800 if live else 120)
    try:
        reply = json.loads(result.stdout)
    except ValueError:
        raise FleetError("Deployment did not return a Fleet response; inspect the installed release before retrying") from None
    if not reply.get("installed"):
        raise FleetError(reply.get("error", "Deployment did not complete"))
    return reply


def remember(target, status, *, profile_name):
    name = target.split("@")[-1]
    path = config_dir() / "inventory.json"
    with locked(path.with_suffix(".lock")):
        entries = inventory()
        matching = next((k for k, v in entries.items() if v["machine_id"] == status["machine_id"]), name)
        prior = entries.get(matching, {})
        if prior.get("machine_id") != status["machine_id"]:
            prior = {}
        aliases = sorted(set(prior.get("aliases", []) + [target, name]))
        entries[matching] = dict(target=target, machine_id=status["machine_id"], aliases=aliases,
                                 profile=profile_name, ci_enrolled=bool(status.get("ci_enrolled")))
        atomic(path, entries)
    return matching


def add(target, *, profile_name="ai-for-science", no_ci=False, preserve_profile=False, expected_machine_id=None):
    if expected_machine_id:
        call(target, dict(action="status"), expected_machine_id=expected_machine_id)
    deploy(target)
    # Enrolled hosts generate their credential on the server. Updating the
    # release must not require (or copy) that credential onto the SSH client.
    payload = dict(action="configure") if preserve_profile else dict(
        action="configure", profile=profile_name, ci_allowed=not no_ci, **profile(profile_name)
    )
    status = call(target, payload, expected_machine_id=expected_machine_id)
    matching = remember(target, status, profile_name=status["profile"])
    readiness = call(target, dict(action="doctor"), expected_machine_id=status["machine_id"])
    return dict(host=matching, installed=status, doctor=readiness)


def enroll(target, *, url, token_file=None, replace_machine=False):
    from . import broker

    if target != "local":
        validate_target(target)
    url = broker.validate_url(url)
    entries = inventory()
    known = next((entry for name, entry in entries.items() if target in
                  (name, entry["target"], entry["machine_id"], *entry.get("aliases", []))), None)
    expected = known["machine_id"] if known and not replace_machine else None
    if expected:
        call(target, dict(action="status"), expected_machine_id=expected)
    invitation = broker.invitation(token_file, target=target)
    deploy(target)
    # Only the invitation crosses SSH stdin. The long-lived credential is
    # created and retained by the selected machine, and never returned here.
    value = call(target, dict(action="enroll", broker=url, invitation=invitation), timeout=180,
                 expected_machine_id=expected)
    value["host"] = remember(target, value["installed"], profile_name=value["installed"]["profile"])
    return value
