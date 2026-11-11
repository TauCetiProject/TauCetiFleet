"""Claude subscription credentials a profile transfers: a `claude setup-token` token and a usage login.

The worker takes its Claude credential from `$CLAUDE_CONFIG_DIR/.credentials.json` and leaves one
that carries no refresh token alone, so the token is stored in that shape in a directory of its
own. The token's scope cannot read subscription usage; the worker reads usage through the login
named by `TAUCETI_CLAUDE_USAGE_CREDENTIALS` and renews that login under `auto_refresh`.
"""

import hashlib
import json
import shlex
import time
from pathlib import Path

from .store import FleetError, atomic, config_dir, locked, read

TOKEN = "claude_token_file"
USAGE = "claude_usage_credentials_file"
KINDS = (TOKEN, USAGE)
DAY = 86400
# A setup-token is valid for a year. A login is renewable for about four weeks after its sign-in;
# the credential's own refreshTokenExpiresAt is used when it has one.
LIFETIME = {TOKEN: 365 * DAY, USAGE: 28 * DAY}
NOTICE = {TOKEN: 30 * DAY, USAGE: 4 * DAY}


def path(kind):
    return config_dir() / "secrets" / ("claude" if kind == TOKEN else "claude-usage") / ".credentials.json"


def selected(payload):
    """Host paths for the kinds a profile names and None for those it omits; empty without a profile.

    A profile may name a file the operator's machine does not hold: the credential is then expected
    on the host, where the usage login can also be signed in directly.
    """
    updates = {}
    for kind in KINDS:
        if payload.get(kind) not in (None, True):
            raise FleetError(f"{kind} is transferred as a credential, never as a path")
        named = payload.get(kind) or kind in payload["credentials"]
        if named or kind in payload:
            updates[kind] = str(path(kind)) if named else None
    return updates


def refresh_lock(target):
    """The lock the worker holds while it renews the login in `target`."""
    return locked(target.with_name(f".{target.name.lstrip('.')}.refresh.lock"))


def block(secret):
    try:
        value = json.loads(secret)["claudeAiOauth"]
    except (ValueError, KeyError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def validate(kind, secret):
    token = secret.strip()
    if kind == TOKEN and not (token.startswith("sk-ant-oat") and token.isascii() and len(token.split()) == 1):
        raise FleetError("claude_token_file does not hold a setup-token (sk-ant-oat...); run claude setup-token again")
    if kind == USAGE and not all(isinstance(block(secret).get(k), str) for k in ("accessToken", "refreshToken")):
        raise FleetError("claude_usage_credentials_file is not the .credentials.json of a claude auth login")


def install(kind, secret, since=None):
    """Store a validated credential; `since` is when the operator saved or signed in to it."""
    now = time.time()
    since = since if type(since) in (int, float) and 0 < since < now else now
    target = path(kind)
    for directory in (target.parent.parent, target.parent):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if kind == TOKEN:
        value = dict(accessToken=secret.strip(), expiresAt=int((since + LIFETIME[TOKEN]) * 1000), scopes=["user:inference"])
        login = block(path(USAGE).read_text()) if path(USAGE).is_file() else {}
        value.update({k: login[k] for k in ("subscriptionType", "rateLimitTier") if login.get(k)})
        atomic(target, {"claudeAiOauth": value})
        return
    # Refresh tokens are single-use: once a worker has renewed this login, the operator's copy is
    # dead, and writing it again on a repeated add would end the chain the host now holds.
    digest = hashlib.sha256(secret.strip().encode()).hexdigest()
    marker = target.with_name("transferred.json")
    # A worker renewing under this lock writes back a rotation of the login it read before the wait.
    with refresh_lock(target):
        if target.is_file() and read(marker, {}).get("sha256") == digest:
            return
        atomic(target, json.loads(secret))
        atomic(marker, dict(sha256=digest, signed_in=since))


def signed_in(target):
    """When the stored login was signed in: at the transfer, or later on the host itself.

    A login newer than both the transfer record and the worker's last renewal was written by
    neither, so it is a sign-in on the host. Its time is recorded before a renewal hides it.
    """
    marker = target.with_name("transferred.json")
    renewed = target.with_name(f".{target.name.lstrip('.')}.refresh.last-success")
    try:
        with refresh_lock(target):
            recorded = read(marker, {})
            written = target.stat().st_mtime
            if all(written > p.stat().st_mtime for p in (marker, renewed) if p.exists()):
                recorded = dict(recorded, signed_in=written)
                atomic(marker, recorded)
    except (OSError, FleetError):
        try:
            recorded = read(marker, {})
        except FleetError:
            return None
    since = recorded.get("signed_in") if isinstance(recorded, dict) else None
    return since if type(since) in (int, float) else None


def worker_env(config):
    """Environment pointing each worker at the stored credentials; empty when the profile names none."""
    token, usage = config.get(TOKEN), config.get(USAGE)
    if (token or usage) and config.get("claude_billing") != "subscription":
        raise FleetError("claude_token_file and claude_usage_credentials_file require claude_billing subscription")
    if usage and not token:
        raise FleetError("claude_usage_credentials_file requires claude_token_file")
    env = {}
    if token:
        env["CLAUDE_CONFIG_DIR"] = str(Path(token).parent)
    if usage:
        env["TAUCETI_CLAUDE_USAGE_CREDENTIALS"] = usage
    if set(env) & set(config.get("env") or {}):
        raise FleetError("env must not set " + " or ".join(sorted(env)) + " beside the profile's Claude credential files")
    return env


def report(config, *, now=None):
    """One entry per expected credential for doctor and status: time left, and the sign-in when due."""
    now = time.time() if now is None else now
    token, usage = config.get(TOKEN), config.get(USAGE)
    entries = []

    def add(kind, status, evidence, remedy=None):
        if remedy is None and kind == TOKEN:
            remedy = "Run claude setup-token, save the token in the profile's claude_token_file (chmod 600), and repeat add"
        elif remedy is None:
            remedy = (f"Sign in on this host: CLAUDE_CONFIG_DIR={shlex.quote(str(path(USAGE).parent))} claude auth login; "
                      "or sign in where the profile's claude_usage_credentials_file is kept and repeat add")
        name = "claude-setup-token" if kind == TOKEN else "claude-usage-login"
        entries.append(dict(name=name, status=status, evidence=evidence, remedy="" if status == "ok" else remedy))

    def stored(file):
        try:
            return {} if file.stat().st_mode & 0o077 else block(file.read_text())
        except OSError:
            return {}

    def left(kind, expiry, ahead, behind):
        seconds = expiry - now
        if seconds <= 0:
            add(kind, "blocked", f"{behind} {-seconds / DAY:.0f} days ago")
        else:
            add(kind, "warning" if seconds < NOTICE[kind] else "ok", f"{ahead} {seconds / DAY:.0f} days")

    if token:
        expiry = stored(Path(token)).get("expiresAt")
        if type(expiry) not in (int, float):
            add(TOKEN, "blocked", "setup-token missing or readable by other users")
        else:
            left(TOKEN, expiry / 1000, "setup-token expires in", "setup-token expired")
    if usage:
        login = stored(Path(usage))
        expiry = login.get("refreshTokenExpiresAt")
        if type(expiry) in (int, float) and expiry > 0:
            expiry /= 1000
        elif login.get("refreshToken"):
            since = signed_in(Path(usage))
            expiry = None if since is None else since + LIFETIME[USAGE]
        if not login.get("refreshToken"):
            add(USAGE, "blocked", "usage login missing, readable by other users, or without a refresh token")
        elif expiry is None:
            add(USAGE, "ok", "usage login present, signed in on this host at an unrecorded time")
        else:
            left(USAGE, expiry, "usage login renews for another", "usage login stopped renewing")
    elif token:
        add(USAGE, "warning", "no usage login, and a setup-token cannot read subscription usage",
            "Add claude_usage_credentials_file to the profile and repeat add")
    return entries


def display(entries):
    if entries:
        print("Claude credentials: " + "; ".join(e["evidence"] for e in entries))
        for entry in entries:
            if entry["remedy"]:
                print("  Next step: " + entry["remedy"])
