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
python3 scripts/setup.py invite --name chris-server --ci both --max-jobs 2
```

The invitation authorizes two jobs total, shared across PR and staging builds.
Omitting the capacity flags retains one staging slot. The host can request a
subset of these classes and fewer slots, but cannot expand its policy.

The command saves a seven-day enrollment code in a private file under
`~/.config/tauceti-ci-broker/invitations/`. Transfer that code privately to the
volunteer. No GitHub PAT or App private key goes to the host.

Use `--expires-in-days N` to choose 1–30 days. To extend an existing unredeemed
code, add `--token-file PATH`; a redeemed invitation cannot be extended or reset.
This deadline only controls enrollment. Once enrolled, the host credential
continues working until the operator revokes it.

On the volunteer's Linux x86-64 server:

```sh
uv tool install git+https://github.com/TauCetiProject/TauCetiFleet.git
tauceti-fleet enroll local
# Paste the one-time enrollment code at the hidden prompt.
tauceti-fleet prepare local
tauceti-fleet doctor local
tauceti-fleet enable local --ci both --max-jobs 2 --ci-vcpus 16 --ci-memory-gib 32
tauceti-fleet status local
```

For unattended enrollment, `--token-file PATH` reads the one-time code from a file.
Enrollment always requires an explicit machine. From your laptop, you can instead
run `tauceti-fleet enroll chris`, where `chris` is an existing SSH hostname or alias.
Fleet installs its release over SSH and redeems the code on that server. Its
machine identity and permanent credential are generated there; the credential
stays there. The laptop remembers the server's identity and SSH alias so subsequent
`prepare chris`, `doctor chris`, `enable chris --ci both`, and `status chris` operate on
the same server. `--token-file` is read on the machine running the command.
Subsequent commands check the remembered machine identity before acting, so
repointing an SSH alias does not silently activate a different server.
For an intentional replacement, revoke the old machine's broker credential first
and use `enroll HOST --replace-machine` with a fresh invitation. This explicitly
replaces the alias's inventory entry. Machine-bound CI credentials cannot be
copied through `add --profile`.

The host still needs Python 3.11+, uv, git, systemd user services with cgroup v2
resource controls, KVM access, sufficient disk/RAM/CPU, and the QEMU/cloud-init
tools (or Nix to fetch them). `doctor` reports these requirements. Workers-only
readiness failures do not block CI. Enable lingering for logout/boot persistence.

Enrollment generates and persists the host's credential before redeeming the
invitation, so retrying a lost response does not create a second identity. It
does not enable CI, enable workers, grant a budget, or authenticate any AI agent.
It preserves an existing worker profile and identity. Software updates retain
the enrolled server's credentials without copying the operator machine's profile.
`prepare` currently installs the worker package alongside the CI image, but does
not run workers. Existing hosts must drain before enrollment or reconfiguration.

Every slot has a separate GitHub registration and pending-registration record.
The service issues class, machine, and instance labels; a PR-only guest never
receives the legacy staging label. Revocation covers all slots, and reconciliation
selects one slot without releasing another slot's ambiguous registration.

A drained host clears definite, unused registration intents automatically. An
ambiguous registration still requires reconciliation before cleanup can finish.
Slot migration is one-way: drain all hosts before rolling back to a broker that
only understands the legacy single-runner record, and migrate stored records back
explicitly. Reverting broker code alone does not restore the old record format.

## Operator controls

```sh
python3 scripts/setup.py hosts
python3 scripts/setup.py revoke --machine-id HEX_ID
python3 scripts/setup.py reconcile --machine-id HEX_ID --slot 0
# After draining and retiring every slot:
python3 scripts/setup.py capacity --machine-id HEX_ID --ci both --max-jobs 2
```

Revocation immediately rejects future host requests and discards the stored JIT
configuration. It removes an idle GitHub registration when GitHub is reachable;
acquired jobs can finish. Repeat revocation to finish cleanup after an active job
or an upstream outage. It does not remotely terminate a VM or erase credentials
already delivered to a host. Re-enrolling a revoked machine requires operator recovery;
fresh invitations cannot silently undo revocation.

Each host may have one registered runner per authorized slot. The service fixes its group, labels,
and name prefix, rechecks trusted main-workflow restrictions before registration,
and filters runner inventory/deletion and job results to that host. It proxies
only specific read-only TauCeti CI metadata endpoints, not arbitrary GitHub APIs.
One-time JIT configurations reach guests; host credentials do not.

An ambiguous GitHub registration reserves capacity and schedules a durable
recovery alarm before making the GitHub request. After two minutes, recovery
checks the complete trusted-group inventory, rechecks and removes only offline
runners with that exact reserved name, and clears the reservation. The host's
existing retries then resume registration without an upgrade or operator action.
Active matches, incomplete inventories, policy errors and upstream outages keep
their reservation and retry with backoff up to fifteen minutes. Recovery runs
without incoming host requests and continues beyond the platform's six automatic
alarm retries. The operator's `reconcile` command uses the same recovery checks.
GitHub API errors never trigger blind registration retries. Definite registration
rejections release their reservation immediately.

The `hosts` command includes pending timestamps, a sanitized original failure,
recovery attempts, the latest recovery error and the last successful recovery.
Logs retain only structured recovery events, GitHub HTTP status and request IDs;
they exclude tokens, callback URLs, exception messages and upstream bodies.
Invocation logs stay disabled to avoid recording enrollment URLs.

`/healthz` returns HTTP 503 if the App is unconfigured or a registration remains
pending for ten minutes. It exposes only aggregate counts and adopts existing
reservations that predate automatic recovery. The `CI broker health` GitHub
Actions workflow checks this endpoint every ten minutes, including availability
and response shape. Normal Actions failure notifications apply; operators should
subscribe to workflow failures in their GitHub notification settings. Scheduled
runs can be delayed by GitHub. This check detects broker availability and stuck
registrations; it does not assert that every volunteer host is online or that a
CI job has completed successfully.
The host persists its runner request identity before registration and retains
local ownership when broker retirement fails, so interrupted registration and
retirement can recover without requesting extra capacity.

The service stores ownership for the latest 4096 issued runners per host, which
bounds record size. A sufficiently old deferred result can therefore age out.
The service deliberately serializes decisions in one Durable Object; revisit
sharding if fleet size makes this a throughput bottleneck.
Authenticated host requests have a burst allowance of 32 and replenish one
request per ten seconds per authorized slot. Readiness policy is cached for at most one minute;
registration and reconciliation always verify a fresh policy.

CI hosts require trusted operators. The build workflow supplies a separate CI
status App private key to its runner, and the host can also falsify build results.
Keeping Fleet administration credentials on the broker does not remove that
existing trust in the build server. Invitations should be issued only for hosts
whose operators are trusted with the CI workflow's credentials and results.

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
