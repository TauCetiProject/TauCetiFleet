"""Host-scoped CI enrollment. GitHub credentials never enter volunteer hosts."""

import getpass
import json
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .store import FleetError, atomic, config_dir, host_config, read, state_dir

DEFAULT_URL = "https://fleet.taucetiproject.org"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FleetError("CI broker redirected an authenticated request; check its configured HTTPS origin")


def open_request(req):
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        try:
            return opener.open(req, timeout=60)
        except urllib.error.URLError as exc:
            # DNS resolution fails before a request can be transmitted. Other
            # transport failures may have mutated state and must not be retried.
            if not isinstance(exc.reason, socket.gaierror) or attempt == 2:
                raise
            time.sleep(0.2)


def validate_url(value):
    url = urllib.parse.urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise FleetError("CI broker must be an HTTPS origin without a path, credentials, query, or fragment")
    return value.rstrip("/")


def request(config, path, *, method="GET", data=None, token=None):
    origin = validate_url(config["ci_broker_url"])
    if not path.startswith("/v1/") or "?" in path or "#" in path:
        raise FleetError("Invalid CI broker endpoint")
    if token is None:
        file = Path(config["ci_broker_token_file"])
        if file.stat().st_mode & 0o077:
            raise FleetError("CI enrollment credential must be private; use chmod 600")
        token = file.read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise FleetError("Invalid CI enrollment credential")
    req = urllib.request.Request(
        origin + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + token, "X-Fleet-Machine": config["machine_id"],
                 "Content-Type": "application/json", "User-Agent": "TauCetiFleet/host"},
    )
    try:
        with open_request(req) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise FleetError("CI broker response exceeds observation limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise FleetError(f"CI broker returned HTTP {exc.code}; check enrollment, host capacity, and operator service status") from None
    except (OSError, ValueError):
        raise FleetError("CI broker unavailable; retry after checking connectivity") from None


def enroll(url=DEFAULT_URL, *, token_file=None):
    from . import remote

    url = validate_url(url)
    config = host_config()
    if config["ci_enabled"] or config["workers_enabled"]:
        raise FleetError("Drain this host before enrolling it for CI")
    if (state_dir() / "vm.json").exists() or (state_dir() / "ci-registration.json").exists():
        raise FleetError("Wait for this host's CI guest and runner registration to retire before enrolling it")
    path = config_dir() / "enrollment.json"
    saved = read(path)
    if saved and saved["url"] != url:
        raise FleetError("This host has an enrollment for another broker; preserve it before reconfiguring")
    if not saved:
        # Persist before redemption, allowing an exact retry after a lost response.
        saved = dict(url=url, host_token=secrets.token_urlsafe(48))
        atomic(path, saved)
    invitation = Path(token_file).expanduser().read_text().strip() if token_file else getpass.getpass("One-time enrollment code: ").strip()
    result = request(dict(config, ci_broker_url=url), "/v1/enroll", method="POST", token=invitation,
                     data=dict(machine_id=config["machine_id"], host_token=saved["host_token"]))
    if result.get("machine_id") != config["machine_id"] or result.get("runner_group_id") != 3:
        raise FleetError("CI broker returned an unexpected host identity or runner group")
    credential = config_dir() / "secrets" / "ci_broker_token_file"
    credential.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    import os
    import tempfile

    fd, temporary = tempfile.mkstemp(dir=credential.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(saved["host_token"] + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, credential)
    finally:
        Path(temporary).unlink(missing_ok=True)
    profiles_path = config_dir() / "profiles.json"
    profiles = read(profiles_path, {})
    profiles["ci-enrolled"] = dict(ci_broker_url=url, ci_broker_token_file=str(credential), runner_group_id=3)
    atomic(profiles_path, profiles)
    return remote.add("local", profile_name="ci-enrolled")
