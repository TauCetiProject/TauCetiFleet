# TauCetiFleet

Deploy TauCeti workers and disposable CI runners under an ordinary Linux account.
Each host manages its own resources. GitHub assigns CI jobs; there is no fleet
server, enrollment token, or SSH connection from CI to the hosts.

Installation is inactive by default. It does not enable production CI, start paid
workers, change existing worker definitions, or copy budget grants.

## Install and inspect

Install with `uv tool install git+https://github.com/TauCetiProject/TauCetiFleet.git`
(or `uv tool install .` from a checkout). From the operator's
machine, use existing SSH aliases, authentication, and known-host verification:

```sh
tauceti-fleet add kim@big-server
tauceti-fleet add kim@small-server --no-ci
tauceti-fleet doctor big-server
tauceti-fleet prepare big-server
tauceti-fleet doctor big-server --smoke
tauceti-fleet status
```

`add local` installs on the current account. Hosts require Python 3.11+, Linux
x86-64, systemd user services, and usable CPU/memory cgroup controls. The operator
needs `gh` to reuse a selected stored GitHub credential. `prepare` installs the
pinned worker revision using user-local uv, and fetches pinned QEMU/cloud-init
tools through an existing Nix installation when necessary. Without Nix, provide
`qemu-system-x86_64`, `qemu-img`, and `cloud-localds` in the user's PATH. No command
automatically invokes host sudo or installs a privileged container daemon.

The first preparation downloads a checksum-verified Ubuntu image and Actions
runner plus pinned GitHub CLI, boots a 2-vCPU/2-GiB guest to prepare the base image, and tests two fresh
overlays. Smoke tests verify guest execution, the offline filesystem sandbox,
and disk replacement without registering with GitHub or calling a model.

`doctor` reports evidence, the affected role, a next step, and an administrator
request where necessary. For example, lack of `/dev/kvm` access blocks CI but
does not require a workers-only host to obtain virtualization access. Missing
lingering produces a persistence limitation and the request
`loginctl enable-linger USER`; the installer does not attempt that privileged
operation. Existing administrator resource allocations remain authoritative.
Worker readiness also checks `gh`, `git`, `uvx`, `jq`, `lake` (elan), and `claude`
in the account's PATH. Missing tools block worker activation with a remedy.

## Profiles

The default `ai-for-science` profile reuses the existing science worker's key-file
path, GitHub identity, model settings, and shared GitHub-state service settings.
It transfers only the selected credentials, over SSH, into private host files.
It does not copy a whole home/config directory or embed secrets in process
arguments, unit files, logs, or repository configuration.

Override the default or add profiles in `~/.config/tauceti-fleet/profiles.json`:

```json
{
  "personal": {
    "anthropic_api_key_file": "/home/operator/.config/secrets/personal-anthropic",
    "github_account": "operator",
    "author_model": "YOUR_CLAUDE_MODEL"
  }
}
```

Then deploy with `--profile personal`. Model credentials, GitHub identity, model,
and effort are independent settings. Version 1 profiles use Claude API billing.
Secret files must be private (`chmod 600`). A `github_token_file` may replace a
stored gh credential; `ci_token_file` separately supplies runner administration
permissions, and `runner_group_id` selects the trusted group described below.

## Roles, resource priority, and activation

```sh
tauceti-fleet enable big-server --ci
tauceti-fleet enable big-server --workers
tauceti-fleet drain big-server
tauceti-fleet update big-server
```

Enable is explicit and refuses missing required capabilities. A `--no-ci` host
never registers a runner. Drain prevents new rounds/CI guests and lets active
work finish. Update requires the host to have drained, and preserves ledgers,
machine identity, and historical reporting records.

The host uses separate user slices: CI CPU weight 1000, the complete worker
fleet weight 10. The default CI shape is 32 vCPUs / 64 GiB, with 2 GiB additional
host overhead. Workers receive bounded remaining memory after CI and OS
headroom, considering actual available memory on a shared host. CI receives
priority over this fleet, not an entitlement over other users' allocations.

Workers start one at a time, at most one per minute, up to eight by default.
Memory pressure stops new admissions; an active CI build limits fresh worker
rounds. Already-admitted paid sessions finish. A stale/missing controller policy
blocks new rounds and paid launches instead of accidentally admitting a full
fleet. Runtime decisions and reasons appear in status output. Host settings are
in `~/.config/tauceti-fleet/host.json`; role/resource changes should be made while
drained. Fleet worker state is separate from the account's existing manager.

## Worker controls and memory observations

`status` also reads the worker installation's prepared activity summary: finished
rounds by type, authored/landed PRs over 3h/24h/7d and recorded totals, and current
open PRs. Preparation happens in `tauceti`, not during status. The summary shows
its age, recorded coverage and unknown PR states/landing evidence. Full worker
history and PR inventories are not transferred by status.

After upgrading `tauceti`, prepare existing structured history once:

```sh
tauceti activity refresh --config ~/.config/tauceti-fleet/workers.toml --backfill
```

The worker manager maintains the cache in background. `tauceti activity
install-timer` is optional for installations without a running manager. Shared
server reporting reads stored facts only; missing/stale data never triggers
GitHub fallback or reconciliation. The fleet `report` command also uses prepared
PR evidence and no longer refreshes GitHub/Bors archives on request. Its legacy
spending and CI sections may still scan local ledgers; status does not.

`tauceti-fleet workers HOST --max N` sets the worker admission ceiling while CI
stays active. Zero pauses new work; admitted paid sessions finish. Existing live
worker definitions are retained when lowering the ceiling.

`tauceti-fleet status HOST` reports the configured ceiling, current admissions,
worker states and resource measurements. "Starting new work" shows how many workers
may start new rounds and why; existing paid sessions finish even if that allowance
falls. Budget checks still apply before paid work starts.
The explanation identifies the current constraint: rebuilding the allowance after
controller start or a pause, waiting for CPU headroom, memory pressure, an expired
admission lease, disabled workers, or the configured ceiling. When only the interval
between increases remains, status shows the next allowance, a countdown and UTC
time; that increase remains conditional on CPU and memory checks. JSON status
includes the same decision under `worker_admission`.

Starting a CI job preserves the current worker allowance. Increases remain limited
to one worker per minute and require at least 35% idle CPU while CI is busy (25%
otherwise), alongside the memory checks below.

Worker RAM is the total memory counted toward the worker group's limits, including
cached file data. Estimated working memory subtracts likely reclaimable file cache
from that total; it is not an additional allocation. Memory reporting also includes
one-minute averages of time spent waiting for RAM, both with at least one task
waiting and with all active worker-group tasks waiting simultaneously. The display
marks these stalls as high when either admission threshold is reached. Cold clean cache is estimated as inactive
file cache minus dirty/writeback data; the estimate is diagnostic, not a promise
that every subtracted page is immediately reclaimable. Limits still include cache.

New worker admissions pause on low host headroom, working set reaching the soft
threshold, or partial/full memory stalls of at least 5%/1%. Missing memory evidence
fails closed. External automation can renew a short admission lease; an expired
lease pauses new work. A manual `workers --max` command clears the lease.

Ad hoc scheduling, capacity experiments and their reports belong to external
operator scripts. The dated script under `experiments/` records the October 2026
capacity investigation and is excluded from installed releases.

## GitHub CI setup

The [companion TauCeti PR](https://github.com/TauCetiProject/TauCeti/pull/16862)
extends the existing runner picker. Worker integration is in
[TauCetiWorker #270](https://github.com/TauCetiProject/TauCetiWorker/pull/270). With
`FLEET_CI_ENABLED` unset/false, production routing is unchanged. Before enabling:

1. Configure an organization runner group named `TauCetiFleet`, restricted to
   `TauCetiProject/TauCeti/.github/workflows/pr-build.yml@refs/heads/main` and,
   optionally, `fleet-smoke.yml` at the same trusted branch. The host verifies
   this policy before registration.
2. Supply the host profile's `runner_group_id` and a credential with organization
   self-hosted-runner write permission. Worker GitHub permissions alone may not
   provide this. Only an encoded, single-job JIT configuration reaches a guest. Use a fine-grained or App token
   scoped to Self-hosted runners; do not use a broadly scoped admin token for CI reads.
3. Keep production routing disabled while enabling a prepared host's CI role and
   manually dispatching `fleet-smoke.yml` on main. This registers a disposable
   runner but starts no paid agent. Verify runner acquisition and completion.
4. Configure TauCeti's `FLEET_RUNNER_GROUP_ID` variable and `FLEET_READ_TOKEN`
   secret (runner read permission), then explicitly set `FLEET_CI_ENABLED=true`.

Initial bors attempts prefer online, idle fleet runners. Incomplete inventories,
API errors, disabled routing, and busy/offline hosts select the existing
Namespace configuration. GitHub makes the actual assignment within the trusted
group. An unacquired fleet job is bounded to two minutes, then force-canceled to bypass always-running finalizers and rerun under
the **same workflow run ID** once cancellation is confirmed. Failed handoff posts
a verified staging failure so bors can recover instead of waiting indefinitely. Second attempts
always use Namespace. This preserves bors's unique staging-run identity;
run-attempt fencing protects status publication. Acquired builds and ordinary
validation failures remain with the existing failure/recovery pipeline. After a
fleet infrastructure failure, its host stops registering capacity for 15 minutes, allowing
fresh bors retries to choose Namespace. Candidate proof failures keep the host
available. This cooldown survives controller restart.

Guests have private writable overlays and unprivileged networking, with no
shared host directory or host administrative credentials. Each handles one job
and shuts down. Host-side cache publications are not introduced; the existing
trusted publication workflow remains separate. Worker-built artifacts never
seed CI's trusted caches. QEMU user networking provides outbound connectivity;
it also exposes the host loopback through its virtual gateway, so host-local
services must authenticate callers. The workflow filesystem/network sandbox
remains the boundary for candidate code inside a guest.

## Grants

```sh
tauceti-fleet budget server1 --grant 100
tauceti-fleet budget server1 --grant-rate 10
tauceti-fleet budget server2 --grant-rate 10
tauceti-fleet budget
```

These wrap the existing local grant ledger. New hosts start with zero credit
and replenishment; existing budgets are retained. Grants require one explicit
host and are never automatically retried after an ambiguous SSH response.
Queries can aggregate hosts. Workers on a host share its budget. Estimated
session costs reserve funds before launch; receipts settle actual costs. Credits
accumulate while idle. Grants govern admissions, not a hard billing cap, and
active sessions may settle into a deficit.

## Reports

```sh
tauceti-fleet report
tauceti-fleet report server1
tauceti-fleet report --since 24h --json
```

Default rolling UTC windows: 3h, 6h, 24h, 7d. Reports include originated PRs
landed, other PRs assisted, landed Lean additions/deletions/net LOC, settled API
spending, pending estimates, CI attempts/outcomes/runtime, and current resources
and worker limits. Open originating PRs are a current workload count.

Machine IDs and mutation receipts establish attribution even when hosts share a
GitHub login. One origin receives primary credit; assistance is separate and
fleet totals deduplicate PRs. Running CI alone earns no authorship credit.
GitHub merge evidence and the existing TauCetiCI database establish landings;
closed bors PRs are not assumed merged. LOC uses the observed final PR diff and
excludes dependency pins and generated files. Missing LOC/evidence is explicit.

Spending is recognized at settlement; reconciliation restates that receipt.
Pending/unresolved estimates are separate. Subscription equivalents and hosting
invoices are not invented or added to API expenditure. Offline hosts use cached
observations with refresh timestamps and incomplete coverage. Historical machine
attribution is unavailable before instrumentation; each affected window says so.
Unknown PR metadata and incomplete fleet spending remain explicit. Local grant receipts may
provide earlier expenditure history.

## Development

```sh
python -m unittest discover -s tests -v
uvx ruff check tauceti_fleet tests
uv build
# Optional measured user-service contention test; one logical CPU for 3 seconds:
python tests/host_priority.py
```

Configuration: `~/.config/tauceti-fleet`; state: `~/.local/state/tauceti-fleet`;
runtime/images: `~/.local/share/tauceti-fleet`. These roots support corresponding
`TAUCETI_FLEET_CONFIG`, `TAUCETI_FLEET_STATE`, and `TAUCETI_FLEET_DATA` overrides
for isolated tests. Credentials and runtime state are never part of release
archives. `release.json` pins the required TauCetiWorker commit.

See [local validation](docs/validation.md) for measured rootless tests and the
remaining activation checks.
