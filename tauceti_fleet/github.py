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
    if config.get("ci_broker_url"):
        from .broker import request as broker_request

        return broker_request(config, "/v1/runners")["runners"]
    found = []
    total = None
    for page in range(1, 21):
        data = request(
            f"orgs/TauCetiProject/actions/runners?per_page=100&page={page}",
            token_file=config.get("ci_token_file") or config.get("github_token_file"),
        )
        batch = data["runners"]
        if (not isinstance(batch, list) or type(data.get("total_count")) is not int
                or data["total_count"] < 0 or total is not None and total != data["total_count"]):
            break
        total = data["total_count"]
        found.extend(batch)
        if len(found) == total and len({r["id"] for r in found}) == total:
            return found
        if not batch or len(found) >= total:
            break
    raise FleetError("GitHub runner inventory was incomplete; CI capacity remains unavailable")


def runner_policy(config):
    if config.get("ci_broker_url"):
        from .broker import request as broker_request

        group = broker_request(config, "/v1/policy")["runner_group_id"]
        if group != config.get("runner_group_id"):
            raise FleetError("CI broker returned a different runner group")
        return group
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
    if config.get("ci_broker_url"):
        from .broker import request as broker_request
        from .capacity import kinds

        value = broker_request(config, "/v1/runners", method="POST",
                               data=dict(name=name, slot=config.get("ci_slot", 0), kinds=kinds(config)))
        return value["runner_id"], value["encoded_jit_config"]
    group = runner_policy(config)
    data = request(
        "orgs/TauCetiProject/actions/runners/generate-jitconfig",
        method="POST",
        token_file=config.get("ci_token_file") or config.get("github_token_file"),
        data=dict(
            name=name,
            runner_group_id=group,
            labels=runner_labels(config, name),
            work_folder="_work",
        ),
    )
    return data["runner"]["id"], data["encoded_jit_config"]


def runner_labels(config, name):
    from .capacity import kinds

    labels = ["self-hosted", "linux", "x64", "fleet-" + config["machine_id"],
              "fleet-instance-" + name.rsplit("-", 1)[-1]]
    allowed = kinds(config)
    # Legacy staging workflows match this label. PR-only runners never carry it.
    if "staging" in allowed:
        labels.append("tauceti-fleet")
    return labels + ["tauceti-fleet-" + kind for kind in allowed]


def capacity_policy(config):
    if config.get("ci_broker_url"):
        from .broker import request as broker_request

        return broker_request(config, "/v1/policy")
    from .capacity import jobs, kinds

    return dict(runner_group_id=runner_policy(config), max_jobs=jobs(config), kinds=kinds(config))


def retire_runner(config, runner_id):
    if config.get("ci_broker_url"):
        from .broker import request as broker_request

        return broker_request(config, f"/v1/runners/{int(runner_id)}", method="DELETE")
    return request(f"orgs/TauCetiProject/actions/runners/{int(runner_id)}", method="DELETE",
                   token_file=config.get("ci_token_file") or config.get("github_token_file"))


def ci_read(config, path):
    if config.get("ci_broker_url"):
        import re

        from .broker import request as broker_request

        prefix = "repos/TauCetiProject/TauCeti/"
        endpoint = path.split("?", 1)[0]
        if not endpoint.startswith(prefix) or not re.fullmatch(r"actions/runs/[1-9][0-9]*(?:/jobs)?|commits/[a-f0-9]{40}/statuses", endpoint[len(prefix):]):
            raise FleetError("Unsupported CI metadata endpoint")
        return broker_request(config, "/v1/github/" + endpoint[len(prefix):])
    return request(path, token_file=config.get("github_token_file"))
