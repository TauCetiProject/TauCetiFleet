# TauCetiFleet

Deploy TauCeti workers and disposable CI runners under an ordinary Linux account.
Each host manages its own resources. GitHub assigns CI jobs; there is no fleet
server, enrollment token, or SSH connection from CI to the hosts.

Installation is inactive by default. It does not enable production CI, start paid
workers, change existing worker definitions, or copy budget grants.

## Install and inspect

For a volunteer contributing CI without AI workers, use [host enrollment](services/ci-broker/README.md):
an operator supplies a one-time code, and the volunteer runs `tauceti-fleet enroll local`
on the server, or `tauceti-fleet enroll SSH_HOST` from a machine with SSH access.
The host needs no GitHub login, model credentials, or budget. It receives only a
revocable credential for its own disposable runner; the GitHub App key stays on
the enrollment service. Enrollment installs inactive services; `prepare` and
`enable local --ci staging` remain explicit.

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

For routine production updates, the operator prefers live updates with normal
worker and CI admissions left enabled, followed by checking and repairing any
fallout. Do not drain or pause the fleet merely as a deployment precaution:
this confuses other agents monitoring the system. Use a drain only when
explicitly requested or when a concrete constraint prevents a safe live update,
and explain that constraint first. The `update` command below requires a drain;
that restriction does not make draining the default rollout procedure.

```sh
tauceti-fleet enable big-server --ci staging
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
host overhead per job. `--max-jobs` sets one shared concurrency limit across
PR and staging jobs; it defaults to one. `--ci pr`, `--ci staging`, or `--ci both`
explicitly declares which jobs the machine accepts. Guest vCPUs are shared by
the host kernel without per-guest CPU quotas or core pinning. Their total may
exceed the host's CPU count: a quiet host lets a guest use up to its configured
vCPUs, while concurrent guests share the available CPU. More vCPUs do not
guarantee faster builds; excessive build parallelism can increase memory and
scheduling overhead. By default, the physical CI memory budget covers guest RAM
and 2 GiB of overhead for every configured job. An explicit
`--ci-memory-budget-gib N` instead bounds the whole CI slice at N GiB and permits
guest RAM overcommit. Accumulated guest memory use and simultaneous peaks can OOM guests; measure useful
completions and memory events when choosing concurrency. Workers receive bounded remaining memory after CI and OS
headroom, considering actual available memory on a shared host. CI receives
priority over this fleet, not an entitlement over other users' allocations.

Example CI configurations for dedicated volunteer hosts:

```sh
# 64 cores / 500 GB RAM: 12 jobs, 408 GiB including guest overhead.
tauceti-fleet enable local --ci both --max-jobs 12 --ci-vcpus 16 --ci-memory-gib 32
# 88 cores / 1 TB RAM: 24 jobs, 816 GiB including guest overhead.
tauceti-fleet enable local --ci both --max-jobs 24 --ci-vcpus 16 --ci-memory-gib 32
# 32 cores / 128 GB RAM: 3 jobs, 102 GiB including guest overhead.
tauceti-fleet enable local --ci both --max-jobs 3 --ci-vcpus 16 --ci-memory-gib 32
# Explicit memory-overcommit trial; 384 GiB virtual RAM shares an 80 GiB CI ceiling.
tauceti-fleet enable local --ci both --max-jobs 12 --ci-vcpus 16 --ci-memory-gib 32 --ci-memory-budget-gib 80
```

Before enabling memory overcommit on an existing installation, update and load
the new controller first, then verify its revision, and only then raise the slot
count with the explicit budget. Older controllers do not understand that budget.
For rollback, restore the prior slot count and budget configuration before
restoring an older controller. Keep busy guest services and worker admissions
enabled during a live controller handover. In explicit-budget mode, a confirmed
QEMU OOM is recorded without triggering the host-wide CI cooldown; other host
faults retain that cooldown. New guests enable virtio free-page reporting.

These are starting points for otherwise available machines, not measured
optima. Keep the existing OS headroom and check actual RAM and disk availability
with `doctor`. The default virtual disk limit is 100 GiB per slot. QCOW2 overlays
allocate on write, so doctor requires up to 4 GiB of startup space per slot on
the guest overlay filesystem, rather than the sum of virtual disk limits. Each launch
also checks that headroom before registering a runner. Monitor actual overlay
growth: the startup check does not guarantee that every guest can fill its disk.
Guests report disk I/O errors instead of silently pausing on a full host disk.

`--ci-memory-pause-gib P` adds a soft admission limit below the CI memory budget.
While the CI slice uses at least P GiB, the controller starts no new guests and
retires idle ones; busy guests finish normally. Admissions resume once usage falls
to `--ci-memory-resume-gib` (default 85% of P). The thresholds apply live without a
drain, `status` shows the guard's state, and the events log records
`ci_memory_paused`/`ci_memory_resumed`. `--ci-memory-pause-gib 0` turns the guard off.
Usage grows after admission, mostly during guest setup, so the guard is a safety
margin for overlapping large jobs, not a substitute for sizing `--max-jobs`: every
admitted guest must still fit within the budget at its peak.

The guest overlay directory is `$TAUCETI_FLEET_STATE/vms`. On a host with spare
RAM and a slow or wear-limited disk, it may live on tmpfs. Each guest's setup
writes tens of GiB that are discarded when it retires, so a tmpfs overlay removes
that disk I/O. Overlay pages on tmpfs are charged to the CI slice, so set
`--ci-memory-budget-gib` to cover guest RAM plus overlay growth, and pair it with
the memory guard:

```sh
# 1 TB host, guest overlays on /dev/shm: ~40 GiB guest RAM + ~35 GiB overlay per typical job.
tauceti-fleet enable local --ci both --max-jobs 10 --ci-vcpus 28 --ci-memory-gib 48 \
  --ci-memory-budget-gib 880 --ci-memory-pause-gib 760 --ci-memory-resume-gib 650
```

tmpfs is emptied on reboot. Recreate the overlay directory before the controller
starts, for example with a user service ordered before `tauceti-fleet.service`.
Increase or reduce
concurrency based on completed jobs per hour and job latency under load, rather
than treating virtual CPU counts as physical CPU reservations.

After upgrading an already running installation, restart its controller with
`systemctl --user restart tauceti-fleet.service` before raising capacity above
16 slots. This loads the new slot validation; upgrading the CLI alone leaves
the old Python controller running. Existing CI guests run in separate transient
services and survive the controller restart. No drain or guest restart is needed.

Reserve staging capacity on a mixed host with
`tauceti-fleet reserve-staging HOST --slots 1`. The first slot's future guests
advertise staging labels only; other slots retain the host's configured classes.
The shared job ceiling and worker admissions stay enabled. Existing guests finish
with their original classes, and the command reports slots awaiting replacement.
Use `--slots 0` to remove the reservation as guests are replaced.

Increase `--max-jobs` live when the guest shape and accepted job classes are
unchanged: existing builds keep running and the controller starts the added
slots independently. Enabling does not restart the controller. Changes to guest
shape or job classes, and reductions in the slot count, still require retiring
the current guests first. Enrollment also imposes an
operator-controlled ceiling on classes and shared capacity. A host can request
less capacity, but cannot exceed its enrollment. Legacy installations retain
one staging slot. Each guest has its own registration intent, state, event
socket, and startup retry. Drain, prepare, enrollment, and update inspect all
slots, including a guest left outside the current configured limit. A fault's
cooldown remains shared by the machine. Enabling fresh CI registers and starts the first guest;
the controller starts the remaining slots independently after the role is enabled.
A draining worker pool must finish before its memory reservation can shrink.
Destination selection belongs to the
existing `pick-runner.yml` dispatcher and its shared selector/recovery scripts;
Fleet advertises and enforces each machine's capacity.

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
its age, the activity start time and current PR state/landing evidence. Full worker
history and PR inventories are not transferred by status.

Activity starts with native round tracking. To import PR creation receipts from
that point onward:

```sh
tauceti activity refresh --config ~/.config/tauceti-fleet/workers.toml --backfill
```

The worker manager maintains the cache in background. `tauceti activity
install-timer` is optional for installations without a running manager. Shared
server reporting reads stored facts only; missing/stale data never triggers
GitHub fallback or reconciliation. `status` includes cached cost estimates,
weighted authored LOC, review/assist counts, budget balances and local CI history.
It never scans ledgers, opens reporting databases or invokes the budget command.
The shared presentation/aggregation module is vendored from the pinned worker.
Missing finish records show counts by round start time over 1h and 24h, the
retained total, and the latest affected worker and round for checking logs. Exit
times and causes are unknown; legacy summaries mark recent timing unavailable.
PR freshness stays silent within the normal hourly reconciliation cycle. It
warns only when an unresolved PR's evidence, or its wait for a first observation,
exceeds two hours. Closed and verified landed PRs do not produce age warnings.
Activity tables use 1h, 3h, 24h, 7d and All time columns; cost/output uses 24h,
7 days and All time. The cost total follows its additive components; settled and
recovered estimate subsets remain available in JSON.

`tauceti-fleet workers HOST --max N` sets the worker admission ceiling while CI
stays active. Zero pauses new work; admitted paid sessions finish. Existing live
worker definitions are retained when lowering the ceiling.

`tauceti-fleet status HOST` reports the configured ceiling, current admissions,
worker states and resource measurements. "Starting new work" shows how many workers
may start new rounds and why; existing paid sessions finish even if that allowance
falls. Budget checks still apply before paid work starts.
All configured workers may start work immediately when memory is safe. CPU idle
and memory-reclaim stalls do not reduce admissions: mostly network-bound API
sessions can keep running while CI builds. CI has CPUWeight 1000 and workers 10. This prioritizes CPU contention; I/O and
memory-bandwidth isolation are not provided. CI throughput must be measured
under representative load; scheduling weights alone do not guarantee a runtime.

Each worker runs in an independent systemd scope inside the same worker slice;
memory remains pooled. A manager-service restart preserves scoped workers. Scope
setup failures report degraded isolation and continue the same process. Whole-host
worker shutdown for an update stops the worker slice, including detached children
and workers that outlived the manager; draining still allows active rounds to finish.
Status reports recent userspace OOM kills separately from kernel OOM counters.
CI VM units use `ManagedOOMPreference=avoid` and keep their already-reserved RAM
resident with `MemorySwapMax=0`. Worker limits and CI CPU priority are unchanged.
This avoids swapping the guest's memory while physical RAM is reserved for it,
reduces reclaim activity, and bounds CI's memory consumption within its existing
RAM reservation. Root-owned ancestor monitors can ignore user preferences: verify
CI reclaim activity and throughput during rollout rather than treating the
preference as an unconditional exemption from OOM kills. With administrator access,
[`docs/oom-pressure-policy.nix`](docs/oom-pressure-policy.nix) is an optional NixOS
policy that moves the root-owned `user.slice` pressure monitor to each delegated
`user@.service` at the current 80% threshold. It is not an admission prerequisite.

Worker RAM includes cached file data. Estimated working memory subtracts cold
clean inactive file cache, retaining dirty/writeback data. Reclaim wait percentages
are informational. The controller refreshes worker MemoryHigh from current host headroom every five seconds,
without double-counting reclaimable artifact cache or dropping below pinned memory
plus a 1 GiB cushion (subject to the hard ceiling). Raising it allows workers to
recover when CI releases RAM. MemoryMax is independent of startup availability and still
reserves the aggregate CI memory budget (guest RAM plus overhead by default) and at least 10% of host
RAM (minimum 4 GiB) for the OS. A 1 GiB swap cap is included within the worker
reservation, deducted from its resident RAM limit; swapped bytes also count in
the estimated working set. This bounds zram use outside resident accounting.

New work pauses when host available memory falls below the OS reserve or worker
working memory reaches 90% of its hard RAM limit. After a memory pause it must
fall below 85% before resuming, preventing repeated start/pause cycles. Missing
memory evidence fails closed. Existing paid sessions finish. When safe memory
returns, the entire configured allowance is restored immediately. External admission leases and manual pauses
retain their existing semantics. JSON status includes the decision under
`worker_admission`.

Ad hoc scheduling, capacity experiments and their reports belong to external
operator scripts. The dated script under `experiments/` records the October 2026
capacity investigation and is excluded from installed releases.
When the experiment reaches a CPU, memory-stall, budget, or configured-ceiling
limit, it stops increasing capacity and releases its admission lease. The current
worker ceiling remains under the ordinary controller and budget checks. Queue
overload, an OOM kill, disabled workers, or lost observations can explicitly
pause new work. Stopping the experiment itself preserves the current ceiling.

## GitHub CI setup

The controller reconciles each guest with the existing GitHub runner inventory.
JIT runners serve one job and disappear after completion. If a guest remains
alive after its registration disappears, fresh complete inventories must confirm
the absence across ten minutes before the controller retires it and provisions
a replacement. The result identity is retained for normal result collection.
Registered runners, including offline runners, are preserved: neither VM age
nor a guest completion message authorizes stopping a build. Failed or incomplete
inventory reads do not authorize cleanup. Job timeouts remain the workflow's
responsibility. Orphan retirements appear as `ci_orphan_retired` in local events.
An offline runner that remains registered waits for GitHub's ephemeral-runner
cleanup; the controller does not infer job completion from lost connectivity.
For broker-enrolled hosts, deploy the accompanying broker change before updating
hosts: inventory observations must retain ownership until explicit retirement,
so a runner omitted once can appear again on the next observation.

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
available. An explicitly identified GitHub API quota failure also keeps a
healthy host available: moving the same request to Namespace cannot repair a
shared API quota. This exemption requires the trusted workflow's dedicated quota
step and a clean host service exit; unrelated setup failures still cool down.
A confirmed Bors cancellation keeps a healthy host available in any job phase,
including setup and cache publication. Host faults and actual infrastructure
step failures still take precedence. Cancellation classification reuses the
existing host receipt, successful runner completion and bounded GitHub metadata reads.
The cooldown survives controller restart.

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

## Status and cost estimates

```sh
tauceti-fleet status
tauceti-fleet status server1
tauceti-fleet status --json
tauceti-fleet status -f
tauceti-fleet status server1 --follow
```

`status` is the single machine/fleet reporting command. Without a host argument,
it includes fleet totals when multiple distinct machines are enrolled. Host
aliases never multiply costs or output. Offline hosts retain their last small
status snapshot with an explicit age and unavailable-host error; new machines
without a snapshot contribute no invented financial totals.

`status -f` / `--follow` opens a live, scrollable view. Use arrows or j/k,
Page Up/Down, Home/End (g/G), and q or Ctrl-C to quit. Left/right scroll wide
reports. Each host uses one persistent SSH connection and sends a new snapshot
when its prepared status, CI overview, configuration, or activity report changes.
File checks are local to that host; follow mode makes no GitHub requests and
does not start background collectors. Disconnected hosts retain their last
snapshot while the stream reconnects with backoff. Hosts need the updated Fleet
CLI for streaming. Without a terminal, follow prints snapshots as they change;
`status --follow --json` emits newline-delimited JSON snapshots.

The first lines report running jobs by runner pool,
the queued PR-build and total workflow backlogs, and account authoring pressure
against its configured PR limit. Running-job counts cover all organization
workflows, including small control jobs. Backlog counts are queued workflow runs,
not individual jobs. Authoring pressure uses the workers' complete cached survey:
non-draft roadmap PRs, with the same selected-area and exclusion scope. A focused
worker can have spare authoring capacity even when the account's all-roadmap
limit is reached. Missing facts are unknown; old observations retain their age.

Bors batch sizes and history follow the spending/budget tables. Per-host OOM
events and storage/cache diagnostics appear later, after those tables; the fleet
total does not repeat each host's storage statistics. Storage figures describe
the last background cleanup, and artifact reuse/time counters sum the latest
preparation report per worker rather than measuring current work.
Saved recovery work gets one compact line per host, with nonzero incomplete,
corrupt or error counts. Per-round retry reasons, internal repair states and
cleanup instrumentation remain in `status --json`.

`status` only reads a small prepared file for these lines. The independent
`tauceti-fleet-observe.timer` prepares observations about every five minutes,
with a randomized delay. A nonblocking lock and a persisted attempt deadline
also bound concurrent and failed refreshes. Each refresh uses one Bors snapshot,
two `per_page=1` GitHub queued-run counts, the shared service's **cached-only**
running-job snapshot, and existing local worker survey responses. It does not
scan GitHub runs or jobs. The shared snapshot adds no discovery requests and can
remain stale when no scheduler has refreshed it. Existing host-service setup
installs the observation timer; for a live code update, install/start only that
timer while leaving the controller, workers, and CI guests running. JSON status
includes the combined observations under `_ci` and each host's `ci_overview`.

Costs have 7 days and All time columns. All conversation costs, including reviews
and fixes for others, are amortized over initiated PR output: landed Lean lines
added plus half of currently open Lean lines added. The seven-day output cohort
contains only PRs initiated in that window. Completed costs and recorded running usage
are shown separately, and ratios pool cost/output before division.
Missing LOC is estimated from measured PRs in that window, preserving half credit
for open PRs. Such estimates show `~` and measurement coverage. Unknown PR states
and LOC with no measured sample contribute zero; positive spending over zero LOC
shows `$∞`, so missing PR data never hides the rate.

Spending has 3h, 24h, 7d and All time columns: worker-hours recorded, API dollars
per worker-hour and API dollars per elapsed hour, using accrued usage from running, completed and interrupted conversations.
Native final totals correct token-price estimates at the original usage timestamps;
legacy costs without timestamps are spread over the conversation duration.
Budget reservations are excluded from both rates and cost/LOC. A compact quality
note identifies incomplete usage. New machines use elapsed
recorded time; fleet totals pool spending over the longest covered wall-clock
duration rather than averaging machine rates or adding machine-hours.
`Worker-hours recorded` sums recorded round durations across workers, clipped to
each window; capacity/budget/quota waiting does not inflate it. The worker-hour
rate pools spending over summed worker time; the elapsed-hour rate pools spending
over the longest covered wall-clock duration. These values come from the same
prepared worker summary, with no extra collector or GitHub requests.

Native host coding rounds can share immutable extracted Mathlib snapshots with
private copy-on-write builds. The native activity collector prepares the sharing
summary as well; Fleet only renders it. See the worker
[shared Mathlib documentation](https://github.com/TauCetiProject/TauCetiWorker/blob/main/docs/shared-mathlib.md)
for `auto|off|required` modes, capability diagnostics, bounded canaries and rollback.

Bors squash-merges are counted using exact-head promotion receipts imported into
the shared state server's cache by its existing cron, at most once per five
minutes for the whole fleet. This reads Bors's stored journal without GitHub API
calls or changes to merge-queue state. Ordinary closed PRs do not produce a
missing-landing warning.

`tauceti` prepares accounting, LOC observations, review counts and local CI
history in background. Fleet only collects the small prepared summaries.
Neither status nor LOC preparation issues new GitHub API requests. The `budget`
command remains the explicit interface for grants and replenishment changes.

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

## Roadmap focus

Steer a configurable share of worker slots toward any set of roadmap directories:

```sh
tauceti-fleet focus big-server --percent 20 --areas PDE,SymplecticGeometry,DGFloer
tauceti-fleet focus big-server --percent 35  # keep the same areas
tauceti-fleet focus big-server             # show the saved setting
tauceti-fleet focus big-server --clear     # return every slot to ordinary selection
```

The host saves this setting in `host.json` and publishes it in its live admission
policy. Changes take effect on subsequent roadmap rounds without rewriting worker
definitions, restarting workers, or changing worker/CI admissions. Focused slots select an area before their survey and run the usual maintenance,
review, and authoring cascade within that area. The remaining slots keep the
existing global maintenance behavior. An explicit
worker `--roadmap-only` still wins, and `--roadmap-skip` exclusions still apply.
Workers require the corresponding TauCetiWorker focus support; older workers
ignore the additional policy field.

At 40 admitted workers, 20% designates eight slots. These slots draw uniformly
from the eligible focus areas at the start of each round. The other
32 draw from the entire eligible roadmap list for new authoring, including focus areas. The share
therefore reserves attention rather than capping the total attention those areas
receive. It describes slots, not a guaranteed percentage of runtime or completed
PRs. Whole slots are spread through admission order; any fractional remainder
uses one additional slot with a per-round probability. One worker with 20% focus
therefore draws from the focus list on 20% of its rounds on average.
The allocation follows the current admitted capacity automatically. Focused slots
retain the account-wide open-PR limit. When capped with no available maintenance
in the chosen area, they fall back to global maintenance instead of exceeding
that limit. Changing only the areas preserves the saved percentage; the first
configuration defaults to 20%. Invalid saved focus disables only focus, with a
diagnostic in `status`; worker and CI admissions continue.

For an initial PDE and symplectic-topology focus:

```sh
tauceti-fleet focus big-server --percent 20 --preset pde-symplectic
```

The preset contains exactly six roadmaps: PDE, SymplecticGeometry,
HeegaardFloer, DGFloer, HamiltonianFloerOperations, and TaubesGromov.
Supporting analytic and geometric roadmaps remain available to the ordinary pool.
The concrete directory names live in [`focus.py`](tauceti_fleet/focus.py).
Use `--areas` to replace it with any list. Names are case-sensitive top-level
roadmap directories. Unknown or skipped areas are filtered against
the worker's current roadmap inventory; when none remain, it logs the condition
and uses ordinary selection so focus cannot strand a worker. Future roadmap
additions require updating the configured list or reapplying an updated preset.
`status` reports the effective whole and fractional slot allocation.
