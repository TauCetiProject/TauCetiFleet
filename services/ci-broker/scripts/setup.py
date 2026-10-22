#!/usr/bin/env python3
"""Register the Fleet App; retain all credentials in private files or Worker secrets."""

import argparse
import hashlib
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
DIRECTORY = Path.home() / ".config/tauceti-ci-broker"
ORIGIN = "https://fleet.taucetiproject.org"
PERMISSIONS = {"metadata": "read", "actions": "read", "statuses": "read", "organization_self_hosted_runners": "write"}


def private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def command(*args, input=None):
    value = subprocess.run(args, cwd=SERVICE, input=input, text=True, capture_output=True)
    if value.returncode:
        raise SystemExit(f"{args[0]} failed; raw output withheld to protect credentials")
    return value.stdout


def api(url, *, token=None, data=None, method=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "TauCetiFleet/setup", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if url.startswith("https://api.github.com/"):
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, headers=headers, data=json.dumps(data).encode() if data is not None else None, method=method)
    try:
        for attempt in range(3):
            try:
                response = urllib.request.urlopen(req, timeout=30)
                break
            except urllib.error.URLError as exc:
                if not isinstance(exc.reason, socket.gaierror) or attempt == 2:
                    raise
                time.sleep(0.2)
        with response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"API request failed: HTTP {exc.code}; response withheld") from None
    except (OSError, ValueError):
        raise SystemExit("API request unavailable; credentials withheld") from None


def admin():
    return (DIRECTORY / "admin-token").read_text().strip()


def app_jwt():
    return command("node", "--input-type=module", "-e", """
import {readFileSync} from 'node:fs'; import {sign} from 'node:crypto';
const directory=process.argv[1];
const app=JSON.parse(readFileSync(directory+'/github-app.json','utf8'));
const now=Math.floor(Date.now()/1000);
const enc=v=>Buffer.from(JSON.stringify(v)).toString('base64url');
const data=enc({alg:'RS256',typ:'JWT'})+'.'+enc({iat:now-60,exp:now+540,iss:String(app.id)});
process.stdout.write(data+'.'+sign('RSA-SHA256',Buffer.from(data),readFileSync(directory+'/github-private-key.pem')).toString('base64url'));
""", str(DIRECTORY)).strip()


def inspect():
    jwt = app_jwt()
    installations = api("https://api.github.com/app/installations?per_page=100", token=jwt)
    matches = [i for i in installations if i["account"]["login"].lower() == "taucetiproject" and not i.get("suspended_at")]
    if len(matches) != 1 or matches[0]["repository_selection"] != "selected":
        raise SystemExit("Install the Fleet App on TauCetiProject with Only select repositories → TauCeti")
    installation = matches[0]
    if installation["permissions"] != PERMISSIONS:
        raise SystemExit("Installation permissions differ from the Fleet manifest")
    grant = api(f"https://api.github.com/app/installations/{installation['id']}/access_tokens", token=jwt, data={})
    selected = api("https://api.github.com/installation/repositories?per_page=100", token=grant["token"])
    if selected["total_count"] != 1 or selected["repositories"][0]["full_name"] != "TauCetiProject/TauCeti":
        raise SystemExit("Fleet App must be installed only on TauCetiProject/TauCeti")
    # Verify the actual runner policy before enabling any host.
    group = api("https://api.github.com/orgs/TauCetiProject/actions/runner-groups/3", token=grant["token"])
    required = "TauCetiProject/TauCeti/.github/workflows/pr-build.yml@refs/heads/main"
    allowed = {required, "TauCetiProject/TauCeti/.github/workflows/fleet-smoke.yml@refs/heads/main"}
    workflows = set(group.get("selected_workflows", []))
    if group["name"] != "TauCetiFleet" or not group.get("restricted_to_workflows") or required not in workflows or not workflows <= allowed:
        raise SystemExit("Runner group 3 does not have the expected trusted-workflow restrictions")
    value = {"installation_id": installation["id"], "repository_id": selected["repositories"][0]["id"], "runner_group_id": 3}
    private(DIRECTORY / "installation.json", json.dumps(value, indent=2))
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "collect", "inspect", "activate", "invite", "hosts", "revoke", "reconcile"])
    parser.add_argument("--name", help="operator label for a new host invitation")
    parser.add_argument("--machine-id", help="machine identity to revoke")
    parser.add_argument("--expires-in-days", type=int, default=7, help="invitation lifetime, 1–30 days (default: 7)")
    parser.add_argument("--token-file", type=Path, help="extend an existing unredeemed invitation instead of creating a new code")
    args = parser.parse_args()
    DIRECTORY.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "prepare":
        if (DIRECTORY / "github-app.json").exists():
            raise SystemExit("App credentials already exist; use inspect/activate, or npm run deploy for code updates")
        path = DIRECTORY / "admin-token"
        if not path.exists():
            private(path, secrets.token_urlsafe(48) + "\n")
        command("npx", "--no-install", "wrangler", "secret", "put", "ADMIN_TOKEN", input=admin())
        command("npx", "--no-install", "wrangler", "deploy")
        state = secrets.token_urlsafe(48)
        private(DIRECTORY / "setup-state", state + "\n")
        api(ORIGIN + "/admin/setup", token=admin(), data={"state": state})
        print("Open this one-hour registration link in your signed-in GitHub browser:")
        print(ORIGIN + "/setup/register#" + state)
    elif args.action == "collect":
        path = DIRECTORY / "github-app-credentials.json"
        if path.exists():
            value = json.loads(path.read_text())
        else:
            pending = api(ORIGIN + "/admin/setup", token=admin())
            if not pending.get("code"):
                raise SystemExit("Waiting for browser registration")
            value = api("https://api.github.com/app-manifests/" + pending["code"] + "/conversions", data={})
            # Save immediately: the manifest exchange is single-use.
            private(path, json.dumps(value))
        if value.get("slug") != "tauceti-fleet" or value.get("owner", {}).get("login", "").lower() != "taucetiproject":
            raise SystemExit("Wrong App identity; credentials saved privately for recovery")
        if value.get("permissions") != PERMISSIONS:
            raise SystemExit("App permissions differ from the reviewed manifest")
        private(DIRECTORY / "github-app.json", json.dumps({k: value[k] for k in ("id", "slug", "html_url")}))
        key = command("node", "--input-type=module", "-e", """
import {createPrivateKey} from 'node:crypto';
let pem=''; for await (const chunk of process.stdin) pem+=chunk;
process.stdout.write(createPrivateKey(pem).export({type:'pkcs8',format:'pem'}));
""", input=value["pem"])
        private(DIRECTORY / "github-private-key.pem", key)
        api(ORIGIN + "/admin/setup", token=admin(), method="DELETE")
        print("App credentials saved privately under " + str(DIRECTORY))
        print("https://github.com/organizations/TauCetiProject/settings/apps/" + value["slug"] + "/installations")
    elif args.action == "inspect":
        print(json.dumps(inspect(), indent=2))
    elif args.action == "activate":
        installation = inspect()
        app = json.loads((DIRECTORY / "github-app.json").read_text())
        values = {"ADMIN_TOKEN": admin(), "GITHUB_APP_ID": str(app["id"]),
                  "GITHUB_INSTALLATION_ID": str(installation["installation_id"]),
                  "GITHUB_PRIVATE_KEY": (DIRECTORY / "github-private-key.pem").read_text()}
        command("npx", "--no-install", "wrangler", "secret", "bulk", input=json.dumps(values))
        print("App authentication configured; no volunteer hosts have been enabled")
    elif args.action == "invite":
        import re

        if not args.name or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", args.name):
            raise SystemExit("Use --name with a short host label, e.g. chris-server")
        if not 1 <= args.expires_in_days <= 30:
            raise SystemExit("Use --expires-in-days between 1 and 30")
        token = args.token_file.expanduser().read_text().strip() if args.token_file else secrets.token_urlsafe(48)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
            raise SystemExit("Enrollment codes must contain 43–128 URL-safe characters; the supplied file is invalid")
        result = api(ORIGIN + "/admin/invites", token=admin(), data={"name": args.name, "hash": hashlib.sha256(token.encode()).hexdigest(),
                     "expires_in": args.expires_in_days * 24 * 3600})
        path = args.token_file.expanduser() if args.token_file else DIRECTORY / "invitations" / (args.name + "-" + secrets.token_hex(4))
        private(path, token + "\n")
        print("Enrollment code saved privately: " + str(path))
        print("Valid until: " + datetime.fromtimestamp(result["expires_at"] / 1000, UTC).isoformat())
        print("On the volunteer host: tauceti-fleet enroll local (paste that code at the prompt)")
    elif args.action == "hosts":
        print(json.dumps(api(ORIGIN + "/admin/hosts", token=admin()), indent=2))
    elif args.action in ("revoke", "reconcile"):
        import re

        if not re.fullmatch(r"[a-f0-9]{32}", args.machine_id or ""):
            raise SystemExit("Use --machine-id from the hosts command")
        if args.action == "revoke":
            result = api(ORIGIN + "/admin/hosts/" + args.machine_id, token=admin(), method="DELETE")
            print("Host credential revoked; existing acquired jobs may finish")
            if result.get("cleanup_pending") or result.get("runner_busy"):
                print("Run revoke again after the active job finishes or GitHub connectivity recovers to finish runner cleanup")
        else:
            result = api(ORIGIN + "/admin/hosts/" + args.machine_id + "/reconcile", token=admin(), data={})
            print(json.dumps(result))


if __name__ == "__main__":
    main()
