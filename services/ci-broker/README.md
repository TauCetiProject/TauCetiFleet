# CI host enrollment

The private `tauceti-fleet` GitHub App belongs to TauCetiProject. Its installation
selects only TauCeti and grants repository Actions/read, Commit statuses/read,
Metadata/read, and organization Self-hosted runners/write. It cannot change
repository code. The organization permission can manage all organization runners;
the broker enforces the narrower host policy instead of distributing that
permission to volunteers.

The service runs at https://fleet.taucetiproject.org in the dedicated TauCeti
Cloudflare account. It keeps its App private key and admin credential in Worker
secrets. A strongly consistent Durable Object holds hashed invitation and host
credentials, host revocations, runner ownership, and pending registration records.
GitHub installation tokens refresh automatically and stay in service memory.
Read and runner-management calls use separately narrowed installation grants.

The production App ID is `5419123`; installation `173945187` selects repository
`1256881326` (TauCeti). On 2026-10-22 the live acceptance check enrolled a temporary
host, verified policy, registered runner `94772`, verified its scoped inventory,
removed it, revoked the host credential, and confirmed subsequent requests were
rejected. It booted no guest and ran no CI job or AI worker. Existing hosts retain
their current credentials and configuration.

## Add a volunteer host

From this directory, an operator with the private local admin credential runs:

```sh
python3 scripts/setup.py invite --name chris-server
```

The command saves a one-hour enrollment code in a private file under
`~/.config/tauceti-ci-broker/invitations/`. Transfer that code privately to the
volunteer. No GitHub PAT or App private key goes to the host.

On the volunteer's Linux x86-64 server:

```sh
uv tool install git+https://github.com/TauCetiProject/TauCetiFleet.git
tauceti-fleet enroll
# Paste the one-time enrollment code at the hidden prompt.
tauceti-fleet prepare local
tauceti-fleet doctor local
tauceti-fleet enable local --ci
tauceti-fleet status local
```

For unattended enrollment, `--token-file PATH` reads the one-time code from a file.
The host still needs Python 3.11+, uv, git, systemd user services with cgroup v2
resource controls, KVM access, sufficient disk/RAM/CPU, and the QEMU/cloud-init
tools (or Nix to fetch them). `doctor` reports these requirements. Workers-only
readiness failures do not block CI. Enable lingering for logout/boot persistence.

Enrollment generates and persists the host's credential before redeeming the
invitation, so retrying a lost response does not create a second identity. It
does not enable CI, enable workers, grant a budget, or authenticate any AI agent.
`prepare` currently installs the worker package alongside the CI image, but does
not run workers. Existing hosts must drain before enrollment or reconfiguration.

## Operator controls

```sh
python3 scripts/setup.py hosts
python3 scripts/setup.py revoke --machine-id HEX_ID
python3 scripts/setup.py reconcile --machine-id HEX_ID
```

Revocation immediately rejects future host requests. Acquired jobs can finish;
revocation does not remotely terminate a VM or erase a JIT configuration already
delivered to a host. Re-enrolling a revoked machine requires operator recovery;
fresh invitations cannot silently undo revocation.

Each host may have one registered runner. The service fixes its group, labels,
and name prefix, rechecks trusted main-workflow restrictions before registration,
and filters runner inventory/deletion and job results to that host. It proxies
only specific read-only TauCeti CI metadata endpoints, not arbitrary GitHub APIs.
One-time JIT configurations reach guests; host credentials do not.

An ambiguous GitHub registration reserves capacity and fails closed. After two
minutes, the operator can run `reconcile`: it checks the full trusted-group
inventory and removes only offline runners with that exact reserved name before
clearing the reservation. Active matches and incomplete inventories require
inspection instead. GitHub API errors never trigger blind registration retries.

The service stores ownership for the latest 4096 issued runners per host, which
bounds record size. A sufficiently old deferred result can therefore age out.
The service deliberately serializes decisions in one Durable Object; revisit
sharding if fleet size makes this a throughput bottleneck.

## App registration and deployment

```sh
npm ci
npm test
python3 scripts/setup.py prepare
# Open the printed one-hour link in your signed-in GitHub browser and create the App.
python3 scripts/setup.py collect
# Open the printed organization App settings link and install only on TauCeti.
python3 scripts/setup.py inspect
python3 scripts/setup.py activate
```

`prepare` deploys the service and uploads the admin credential. GitHub requires
browser creation/installation. The callback checks a one-hour random state and
stores only the one-use manifest code; `collect` exchanges it and saves credentials
privately on the operator's machine. It persists the conversion response before
converting the private key so recovery does not consume a second code. `activate`
verifies installation permissions, selected repository, and runner group 3 before
uploading the App credentials as secrets. Raw credentials never appear in command
arguments, logs, public responses, or repository files. Request logging is disabled.

For later code changes, `npm run deploy` preserves configured secrets. Run
`npm test` first. The existing PAT/SSH deployment path remains available for
previously configured hosts.
