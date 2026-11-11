"""Anonymous reads from a Forgejo instance's REST API. Fleet holds no Forgejo credential."""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from .store import FleetError

LIMIT = 4 * 1024 * 1024
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def origin(url):
    """`url` when it is an origin in the canonical form the worker writes (TauCetiTools' `Forge`): HTTPS, or
    HTTP on loopback, a lower-case host with IPv6 in brackets, and a port only when it is not the default."""
    if not isinstance(url, str) or any(c.isspace() for c in url):
        return None
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if (not host or parsed.scheme not in ("https", "http") or parsed.scheme == "http" and host not in LOOPBACK
            or parsed.username is not None or parsed.password is not None or parsed.path or parsed.query
            or parsed.fragment):
        return None
    authority = (f"[{host}]" if ":" in host else host) + (
        f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else "")
    return url if url == f"{parsed.scheme}://{authority}" else None


def request(at, path):
    if origin(at) is None:
        raise FleetError(f"{at!r} is not a Forgejo origin")
    req = urllib.request.Request(f"{at}/api/v1/{path.lstrip('/')}",
                                 headers={"Accept": "application/json", "User-Agent": "TauCetiFleet/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise FleetError(f"{at} response exceeds the observation limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        error = FleetError(f"{at} GET returned HTTP {exc.code}")
        error.status = exc.code
        raise error from None
    except (OSError, ValueError, http.client.HTTPException):
        raise FleetError(f"{at} request unavailable; check network access and retry") from None
