"""Anonymous reads from a Forgejo instance's REST API. Fleet holds no Forgejo credential."""

import http.client
import json
import re
import urllib.error
import urllib.request

from .store import FleetError

ORIGIN = re.compile(r"https?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?")
LIMIT = 4 * 1024 * 1024


def request(origin, path):
    if not ORIGIN.fullmatch(origin):
        raise FleetError(f"{origin!r} is not a Forgejo origin")
    req = urllib.request.Request(f"{origin}/api/v1/{path.lstrip('/')}",
                                 headers={"Accept": "application/json", "User-Agent": "TauCetiFleet/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise FleetError(f"{origin} response exceeds the observation limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        error = FleetError(f"{origin} GET returned HTTP {exc.code}")
        error.status = exc.code
        raise error from None
    except (OSError, ValueError, http.client.HTTPException):
        raise FleetError(f"{origin} request unavailable; check network access and retry") from None
