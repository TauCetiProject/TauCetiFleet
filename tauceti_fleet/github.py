"""Small bounded GitHub client. Privileged runner credentials stay on the host."""

import json
import urllib.error
import urllib.request
from pathlib import Path

from .store import FleetError


def request(path, *, token_file=None, method="GET", data=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "TauCetiFleet/0.1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token_file:
        token = Path(token_file).read_text().strip()
        if not token or any(c.isspace() for c in token):
            raise FleetError("GitHub credential file must contain one token")
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        "https://api.github.com/" + path.lstrip("/"), data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            if response.status == 204:
                return None
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise FleetError("GitHub response exceeds the observation limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise FleetError(
            f"GitHub {method} returned HTTP {exc.code}; check the selected account's repository/runner permissions"
        ) from None
    except (OSError, ValueError):
        raise FleetError("GitHub request unavailable; check network access and retry") from None


def runners(config):
    found = []
    for page in range(1, 21):
        data = request(
            f"orgs/TauCetiProject/actions/runners?per_page=100&page={page}",
            token_file=config.get("ci_token_file") or config.get("github_token_file"),
        )
        batch = data["runners"]
        found.extend(batch)
        if len(found) >= data["total_count"]:
            return found
        if not batch:
            break
    raise FleetError("GitHub runner inventory was incomplete; CI capacity remains unavailable")


def runner_policy(config):
    group = config.get("runner_group_id")
    if not isinstance(group, int) or group <= 0:
        raise FleetError(
            "Set runner_group_id to a GitHub runner group restricted to trusted main-defined fleet workflows"
        )
    data = request(
        f"orgs/TauCetiProject/actions/runner-groups/{group}",
        token_file=config.get("ci_token_file") or config.get("github_token_file"),
    )
    required = "TauCetiProject/TauCeti/.github/workflows/pr-build.yml@refs/heads/main"
    allowed = {required, "TauCetiProject/TauCeti/.github/workflows/fleet-smoke.yml@refs/heads/main"}
    selected = set(data.get("selected_workflows", []))
    if (
        data.get("name") != "TauCetiFleet"
        or not data.get("restricted_to_workflows")
        or required not in selected
        or not selected <= allowed
    ):
        raise FleetError("Runner group must allow only pr-build.yml and optional fleet-smoke.yml at refs/heads/main")
    return group


def jit_config(config, name):
    group = runner_policy(config)
    data = request(
        "orgs/TauCetiProject/actions/runners/generate-jitconfig",
        method="POST",
        token_file=config.get("ci_token_file") or config.get("github_token_file"),
        data=dict(
            name=name,
            runner_group_id=group,
            labels=["self-hosted", "linux", "x64", "tauceti-fleet", "fleet-" + config["machine_id"]],
            work_folder="_work",
        ),
    )
    return data["runner"]["id"], data["encoded_jit_config"]
