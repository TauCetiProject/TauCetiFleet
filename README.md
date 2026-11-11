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
latest Worker main revision using user-local uv, and fetches pinned QEMU/cloud-init
tools through an existing Nix installation when necessary. Without Nix, provide
`qemu-system-x86_64`, `qemu-img`, and `cloud-localds` in the user's PATH. No command
automatically invokes host sudo or installs a privileged container daemon.

The first preparation downloads a checksum-verified Ubuntu image and Actions
runner plus pinned GitHub CLI, boots a 2-vCPU/2-GiB guest to prepare the base image, and tests two fresh
overlays. Smoke tests verify guest execution, the offline filesystem sandbox,
and disk replacement without registering with GitHub or calling a model.

PR-dedicated CI slots use [disk-backed native runners](docs/native-runners.md)
by default. After `prepare`, `enable HOST --ci pr` automatically prepares their
private OS image before registration; there is no separate native opt-in.
`prepare` also exports that image for an already configured PR role. Standard
live updates provision a missing native image before controller handover, leaving
healthy jobs and admissions running. The prepared image and host role persist
across restarts. Staging and mixed PR/staging registrations remain on VMs; a mixed
host with reserved staging slots uses native for its other slots.


`doctor` reports evidence, the affected role, a next step, and an administrator
request where necessary. For example, lack of `/dev/kvm` access blocks CI but
does not require a workers-only host to obtain virtualization access. Missing
lingering produces a persistence limitation and the request
`loginctl enable-linger USER`; the installer does not attempt that privileged
operation. Existing administrator resource allocations remain authoritative.
Worker readiness also checks `gh`, `git`, `uvx`, `jq`, `lake` (elan), and the
profile's agent CLI (`claude`, `codex`, or both for `auto`) in the account's PATH,
plus `bubble` and `incus` when the profile selects the bubble sandbox. Missing
tools block worker activation with a remedy.

An operator can run `python -m tauceti_fleet.ci_autoscale --host tauchonki
--machine-id MACHINE_ID --minimum 1 --maximum 4 --github-account kim-em` as a
persistent user service on the SSH control host. It reads the host's existing
telemetry every 15 seconds, trims idle general capacity during RAM pressure, and
adds at most one slot after three minutes with spare memory for both guest growth
and compiler headroom. Guest launches still obey the host's ten-second spacing and
startup memory gate. One general slot is retained alongside staging reservations, even when reducing
capacity; an existing staging-only ceiling can recover a general slot when memory
allows it. The configured maximum is an experimental ceiling, not a reservation; normal worker and CI admissions remain enabled.

A recovery requires fresh telemetry, at least 90 seconds without CI admission,
no observed executing Lean processes, very low CI CPU, low RAM, and another 30
seconds of confirmation. Only a nonreserved guest individually using less than
0.2 CPU cores is eligible. The operator verifies the current runner/job ownership
and a `pull_request_target` event before recording a durable cancel/retire/full-run
retry transaction. Staging is never selected. GitHub requests happen only during
recovery, with exponential backoff up to five minutes on failures. An unavailable
write API still allows the verified stalled PR guest's RAM to be released; its
resubmission remains pending in durable state. Fresh cached capacity observations continue while a retry is pending; additional
recovery and growth wait for its resubmission. Restarting the service resumes that
transaction and verifies the increased run attempt before cleaning up registration.
`status` reads a prepared autoscale summary and identifies stale controller reports.

Directly administered hosts can set `ci_idle_pool: true` in their private
`host.json`. This keeps the reserved staging slots and one general idle guest,
grows the pool as jobs arrive, and counts running or assigned jobs anywhere in
the pool against `ci_max_jobs`. Reducing that limit preserves existing builds.
A fresh complete cached queue observation must confirm that no job targets an
idle runner before GitHub deregistration and VM teardown. GitHub refusal to delete
a busy runner leaves its VM running. Retired identities are retained for 48 hours:
if a picker races deletion and queues against an old instance label, the controller
recreates that identity within the shared cap, preserving its original CI classes.
This avoids changing GitHub's read-only JIT labels. Missing queue observations
retain guests and block pool growth, with an error in status.
The controller reads the existing capacity cache at most once per 30 seconds;
foreground status performs no new requests. Broker-enrolled hosts retain their
existing pool behavior.

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
and effort are independent settings.
Secret files must be private (`chmod 600`). A `github_token_file` may replace a
stored gh credential; `ci_token_file` separately supplies runner administration
permissions, and `runner_group_id` selects the trusted group described below.

Profiles default to Claude API billing. These optional keys describe a fleet on
Claude or Codex subscriptions; each applies to every worker slot:

```json
{
  "subscription": {
    "github_account": "operator",
    "claude_billing": "subscription",
    "agent": "auto",
    "sandbox": "bubble",
    "pace": "0:10,50:70,90:90",
    "env": {"TAUCETI_OVERLAP_BATCH_COMMITS": "80"}
  }
}
```

| Key | Values | Default |
| --- | --- | --- |
| `claude_billing` | `api`, or `subscription`: no API key is read or transferred, and workers follow the provider's quota windows instead of [grants](#grants) | `api` |
| `agent` | `claude`, `codex`, or `auto` (the worker chooses per round); `codex` and `auto` require `subscription`, and `auto` excludes `author_model` and `author_effort` | `claude` |
| `sandbox` | `host` or `bubble`. Bubble containers run under the Incus daemon, outside the worker slice, so its memory ceiling and CPU weight do not apply to them and host headroom at admission is their only bound; `doctor` warns | `host` |
| `pace` | Subscription pacing curve as `time%:budget%` points | the worker's curve |
| `env` | Extra environment for each worker, as printable ASCII strings; names the worker or Fleet sets (`TAUCETI_FLEET_*`, `TAUCETI_WORKER_SLICE`) are refused. Not a secret store: values are written to `workers.toml` | none |

With these keys alone a subscription profile carries no model credential. Sign
in to `claude` or `codex` as the host account before enabling workers; each
worker copies that login into its own home. Like a model change, changing any of
these keys on an active host requires a drain before repeating `add`. A key
removed from the profile returns to its default.

Two more keys let a subscription profile carry the Claude sign-in, so the host
account needs none of its own:

```json
{
  "claude_token_file": "~/.config/tauceti-fleet/claude-setup-token",
  "claude_usage_credentials_file": "~/.config/tauceti-fleet/claude-usage/.credentials.json"
}
```

| Key | Holds | Lasts |
| --- | --- | --- |
| `claude_token_file` | The token `claude setup-token` prints. Every Claude agent on the host runs on it; it never rotates, so the workers share it | A year from when the file was saved |
| `claude_usage_credentials_file` | The `.credentials.json` of an ordinary sign-in to the same account, made for one host's fleet alone: `CLAUDE_CONFIG_DIR=~/.config/tauceti-fleet/claude-usage claude auth login`. A setup-token cannot read subscription usage, so workers read their quota windows through this login and renew it themselves (`auto_refresh`). Requires `claude_token_file` | About four weeks from the sign-in |

Both files must be private and are transferred like the other credentials. On
the host they are stored under `secrets/claude/` and `secrets/claude-usage/` in
the shape Claude Code reads, and `workers.toml` names only those paths
(`CLAUDE_CONFIG_DIR`, `TAUCETI_CLAUDE_USAGE_CREDENTIALS`). `doctor` and `status`
report the days left on each. The token gets a note thirty days before it
expires and the usage login four days before it stops renewing, each with the
command that replaces it; a lapsed one blocks worker activation.

Renewing a login replaces its refresh token, so once a worker has renewed the
usage login the operator's copy no longer works. A repeated `add` therefore
leaves the host's login in place unless the profile's file holds a different
sign-in. For the same reason a usage login renews on one host only: the
setup-token may go to every host, but `add` records in
`claude-usage-hosts.json` which host each usage login is sent to, before
sending it and whether or not `add` then succeeds, and refuses it to any other,
so give each host its own profile and sign-in. When the login is
due, either sign in again on the operator's machine and repeat `add`, or run
the command `doctor` prints on the host, from which `doctor` counts the four
weeks again; a profile may name a file the operator's machine does not hold
when the host is signed in that way. Adding or removing either key takes a
drained `add`; replacing a credential does not. A worker slot keeps the Claude
credential directory it first ran with, so give a host these keys before its
workers first start.

## Protected roadmap capacity

Two existing CI slots can be reserved without another scheduler. The host's
`ci_protected_slots` configuration maps slot numbers to a class and runner group:

```json
{
  "0": {"kind": "control", "runner_group_id": 5},
  "1": {"kind": "roadmap", "runner_group_id": 4}
}
```

Use the actual IDs of the `TauCetiControl` and `TauCetiRoadmap` runner groups.
Both groups allow only the TauCetiRoadmap repository. Repository isolation
keeps TauCeti out without coupling runner policy to workflow pins or filenames.
Every job runs in a disposable VM; ordinary builds select the roadmap class or
Namespace, while small automation selects the control class. This setup uses the operator host's existing
direct runner credential, not the volunteer registration broker.

The control guest has 2 CPUs and 4 GiB; the roadmap guest has 16 CPUs and 32 GiB.
Both slots stay warm independently of TauCeti's capacity discovery and cannot
be borrowed by its builds or recovery. The existing slot ceiling, launch pacing,
memory checks, and disposable guest lifecycle still apply. Use the lowest slot
numbers after any reserved staging slots so protected guests start first.
Set these reservations before routing workflows to their groups. Existing work
finishes normally; configure currently free slots or allow busy slots to finish
before relying on the reservation. Ordinary roadmap PR builds use shared
Namespace capacity; merge-group builds use the protected roadmap slot.

## Roles, resource priority, and activation

For routine production updates, the operator prefers live updates with normal
worker and CI admissions left enabled, followed by checking and repairing any
fallout. Do not drain or pause the fleet merely as a deployment precaution:
this confuses other agents monitoring the system. Use a drain only when
explicitly requested or when a concrete constraint prevents a safe live update,
and explain that constraint first. `update` performs a live controller handover.

```sh
tauceti-fleet enable big-server --ci staging
tauceti-fleet enable big-server --workers
tauceti-fleet update big-server
```

Enable is explicit and refuses missing required capabilities. A `--no-ci` host
never registers a runner. Drain prevents new rounds/CI guests and lets active
work finish. Update keeps admissions enabled, preserves configuration and
credentials on the host, and leaves CI guests and worker processes running.
It resolves TauCetiWorker `main` once and stages that exact commit in a separate
worker virtualenv before switching the launcher. Fleet releases do not pin Worker.
The installed commit is recorded in `worker-runtime.json` and returned in the
update result. Repeating an update with unchanged Fleet code still checks Worker
`main`; a lookup or installation failure leaves the selected runtimes unchanged.
A healthy status from the new controller confirms the handover;
failure restores the previous release and attempts to recover its controller.
An interrupted SSH connection is not retried automatically: inspect `status`
and the host's `current` release before repeating an update.

Already running worker managers and workers retain their existing Python runtime;
the staged worker version takes effect at a future manager start. The update
result reports that scope explicitly. Existing CI backing images are preserved;
the updater exports a missing native image from the verified base when PR slots
need it. Replacing the base image remains a separate `prepare` operation. `prepare` also selects current Worker
`main`, rather than restoring a revision recorded by an older Fleet release.
Existing releases and worker runtimes
are retained while processes may still reference them.

An update deploys the version of Fleet running the command. To upgrade an older
drain-only installation without draining, run the new CLI once from upstream:

```sh
uvx --from 'git+https://github.com/TauCetiProject/TauCetiFleet.git@main' tauceti-fleet update local
```

`update local` uses this account's host identity even without an operator
inventory. The new updater also supports SSH targets running the old controller:
it restarts only that controller's main process via its existing systemd restart
policy. Later updates use SIGHUP and preserve admission leases during re-exec.

The host uses separate user slices: CI CPU weight 1000, the complete worker
fleet weight 10. Disk-native PR jobs have a 54 GiB whole-job RAM cap and no
reserved workspace RAM. The default VM shape for staging/mixed registrations is
32 vCPUs / 64 GiB, with 2 GiB additional host overhead per job. `--max-jobs` sets one shared concurrency limit across
PR and staging jobs; it defaults to one. `--ci pr`, `--ci staging`, or `--ci both`
explicitly declares which jobs the machine accepts. Guest vCPUs are shared by
the host kernel without per-guest CPU quotas or core pinning. Their total may
exceed the host's CPU count: a quiet host lets a guest use up to its configured
vCPUs, while concurrent guests share the available CPU. More vCPUs do not
guarantee faster builds; excessive build parallelism can increase memory and
scheduling overhead. By default, CI and workers share a parent memory ceiling
that preserves host OS headroom. An explicit
`--ci-memory-budget-gib N` additionally bounds the whole CI child slice at N GiB and permits
guest RAM overcommit. Accumulated guest memory use and simultaneous peaks can OOM guests; measure useful
completions and memory events when choosing concurrency. Workers use currently
available memory under the same parent ceiling. CI receives
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
the current guests first. Each host chooses its runner count with local
`--max-jobs`; there is no separate broker or dispatcher ceiling for a machine.
Enrollment authorizes job classes, and a host can select a subset of them.
The slot format supports up to 4096 jobs per host. Each guest has its own
registration intent, state, event
socket, and startup retry. Drain, prepare, and enrollment inspect all
slots, including a guest left outside the current configured limit. A fault's
cooldown remains shared by the machine. Enabling fresh CI registers and starts the first guest;
the controller starts the remaining slots after the role is enabled, with at least
ten seconds between CI guest startup attempts across the host. Deferred starts do
not block the controller. The shared gate survives controller restarts and also
covers enable, retries, and recovered placements; existing guests are adopted
immediately without consuming another startup turn.
A draining worker pool must finish before its memory reservation can shrink.
Destination selection belongs to the
existing `pick-runner.yml` dispatcher and its shared selector/recovery scripts;
Fleet advertises and enforces each machine's capacity.

While CI builds are active, worker admission and the worker soft memory budget
leave the compiler gate's headroom plus a 1 GiB cushion. This prevents worker
preparation from consuming freed RAM before CI can resume. Existing rounds keep
running and the hard worker memory limit remains unchanged; when CI finishes,
workers return to the ordinary host headroom policy.

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
landed authored LOC, review/assist counts, budget balances and local CI history.
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
worker shutdown stops the worker slice, including detached children and workers
that outlived the manager. Live updates leave that slice running.
Status reports recent userspace OOM kills separately from kernel OOM counters.
CI VM units use `ManagedOOMPreference=avoid` and keep guest RAM
resident with `MemorySwapMax=0`. CI CPU priority remains in effect.
The common parent slice bounds actual combined RAM use. Root-owned ancestor monitors can ignore user preferences: verify
CI reclaim activity and throughput during rollout rather than treating the
preference as an unconditional exemption from OOM kills. With administrator access,
[`docs/oom-pressure-policy.nix`](docs/oom-pressure-policy.nix) is an optional NixOS
policy that moves the root-owned `user.slice` pressure monitor to each delegated
`user@.service` at the current 80% threshold. It is not an admission prerequisite.

Lean starts wait when recent CPU use exceeds 90%, or available RAM falls below
slightly more than twice the fixed worst-file RSS measured on 2026-11-03.
The memory-use threshold has a 60% floor for small machines. Retries sleep for
independent random 1–5 second intervals. Both host workers and newly started CI
guests receive `LAKE_OVERRIDE_LEAN=true` and the standalone wrapper. Workers use
a scoped PATH alias and toolchain overlay. A CI-only Bash hook wraps the prepared
watchdog entry point, so capacity waits do not spend the compiler's execution
deadline. It leaves the
official Lean installation untouched, preserving toolchain snapshot cache hits.
Metadata probes stay fast and ordinary waits stay silent for `lake build --iofail`.
Already-running guests finish with their existing setup before replacement.

CI guests use the host's CPU and memory pressure rather than their own utilization
percentages. A separate user service samples the host ten times per second, inside
the same ancestor memory limits as CI, and broadcasts a small JSON frame over
dedicated virtio serial ports. Guests send only a bounded numeric request ID for
the next compiler start. The host grants starts fairly across waiting guests,
limiting ordinary CI bursts using current free RAM and a quarter-second startup allowance.
Staging requests receive all their pending permits without that memory budget.
This is transient admission accounting, not a physical resource reservation.
It accepts no guest commands and makes no network or GitHub API requests.
A root guest service caches the frame under `/usr/local`,
readable through the existing offline sandbox's read-only `/usr` mount. Missing,
invalid, or ten-second-old observations make new compilers wait; existing
compilers continue. Guest file caches are released at most once per 30 seconds
when the host needs RAM, allowing free-page reporting to return it to the host.
Ordinary CI also requires enough guest-local absolute RAM for one measured
worst-case file; a guest's old 60% utilization threshold no longer applies.

Staging compiler starts bypass CPU-utilization sleeps and all memory admission
checks, both host pressure and the guest-local worst-file margin. Ordinary CI
retains its existing CPU, host RAM and guest RAM checks. Physical VM memory and
hard cgroup RAM caps still apply. The reserved staging guest can boot with
14 GiB of free RAM instead of 20 GiB, retaining room for guest preparation.
Staging guests receive CPUWeight=10000 versus
100 for PR guests; this is scheduling precedence, not a physical CPU reservation.

The host serves staging compiler requests first and accounts for all grants in
one shared ramp allowance. Reserved staging guests need no API classification;
shared guests are classified once per job from trusted GitHub event/title data
and a matching active runner assignment,
with a two-minute retry backoff on read failures. Guest notices cannot assert
staging priority. The active CPU/RAM policy is included in prepared status data.
Existing guests receive unrestricted host-memory admission capacity over the
serial link without restarting the guest or build. Actual RAM is retained in
`measured_memory_available`; historical gates use the compatibility fields.
An existing guest retains its installed absolute guest-local exhaustion check
until it finishes. New staging wrappers also bypass that local check. Protected
control/roadmap jobs retain their memory admission policy. Host reports always
show actual measurements. Live priority updates preserve running guests.

`status` also shows compiler throttling statistics in Workers/CI columns: observed
invocations, current waiters and executing compilers, the fraction that waited,
waiting share of elapsed time, approximate p50/p95 waits, maximum and oldest
active waits, and wait causes (CPU, RAM, missing signal, or admission queue).
Immediately below the current counts, `Last admission seen` reports how long ago
the host observed a confirmed admission counter increase, separately for workers
and CI. The timestamp survives observer restarts. Existing history seeds a baseline;
if no new admission has been observed, the cell says `none in …` for the observation
window. Replayed snapshots, archival, and permits do not count as new admissions.
`CPU active (cores)` shows the cached CPU-time rate for the complete worker and
CI slices, including native compilation and artifact preparation. A zero Lean
running count with high CPU usage therefore identifies activity outside the
instrumented compiler stream. First samples, stale samples, and counter resets
show unknown instead of inventing a rate.
Waiting share is summed capacity-wait seconds divided by summed wait plus
execution seconds, including live invocations. It is neither host CPU utilization
nor a percentage of the report's wall-clock interval. The normal CPU sampling
delay is counted separately. Quantiles cover admitted invocations that waited;
active and abandoned waits still contribute elapsed waiting time. The timing
start and CI guest coverage are explicit; pre-instrumentation worker processes
have separate observed counts and lower bounds for their current waits.

Optional Unix-socket observation survives the compiler's existing `exec`, so no
parent process is added. Closing that descriptor measures lifetime, including
the existing watchdog and descendants that inherit it. A failed collector does
not change compiler behavior. Guests send bounded cumulative histograms over the
existing serial link; retired guests are folded into one archive. The host saves
aggregate counters across publisher restarts, marking interrupted observations as
timing gaps while compiler processes continue, and prepares a small report once per second. `status` and JSON
snapshots read only that prepared file, with no process scan or API requests.
The sandbox requests starts through a Unix socket under the same read-only
`/usr` mount; its network namespace stays isolated. CPU samples exclude the
low-priority worker slice, which yields to CI. `status` reports signal freshness,
connected guests, and waiting guests using cached data only. Host and guest wall
clocks must agree within two seconds; guest monotonic time additionally expires
the local cache.

Fresh guests expose effective host CPUs (up to QEMU's 255-CPU limit) and the aggregate CI RAM
ceiling, rounded up to GiB. `ci_vcpus` and `ci_memory_gib` are minimum virtual
sizes. No CPU pinning or memory preallocation is added. The physical parent and
CI slice limits remain the shared emergency bounds. Existing guests retain
their sizes and admission setup until they complete and are replaced.

Workers and CI can use whatever RAM is currently free. Their parent
`tauceti-fleet.slice` limits combined resident memory to host RAM minus the OS
reserve and 1 GiB swap allowance. Worker hard limits no longer reserve CI's full
configured RAM in advance. CI defaults to the same shared ceiling; an explicit
`ci_memory_budget_gib` still applies to its child slice. CPU priorities are
unchanged. Concurrent compilers can still grow after passing
the gate, so the aggregate emergency bound remains necessary. Worker processes
have `OOMScoreAdjust=500`, biasing kernel OOM victim selection toward workers;
this does not guarantee protection for large CI guests.

Worker RAM includes cached file data. Estimated working memory subtracts cold
clean inactive file cache, retaining dirty/writeback data. Reclaim wait percentages
are informational. The controller refreshes worker MemoryHigh from current host headroom every five seconds,
without double-counting reclaimable artifact cache or dropping below pinned memory
plus a 1 GiB cushion (subject to the hard ceiling). Raising it allows workers to
recover when CI releases RAM. MemoryMax is independent of startup availability and
preserves at least 10% of host RAM (minimum 4 GiB) for the OS through the shared
parent ceiling. A 1 GiB swap cap is included within the worker
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
Queries can aggregate hosts. API-billed workers on a host share its budget;
subscription workers do not draw on it. Estimated
session costs reserve funds before launch; receipts settle actual costs. Credits
accumulate while idle. Grants govern admissions, not a hard billing cap, and
active sessions may settle into a deficit.

## Status and cost estimates

```sh
tauceti-fleet status
tauceti-fleet status server1
tauceti-fleet status --json
tauceti-fleet status --all-jobs
tauceti-fleet status -f
tauceti-fleet status server1 --follow
```

`status` is the single machine/fleet reporting command. Without a host argument,
it includes fleet totals when multiple distinct machines are enrolled. Host
aliases never multiply costs or output. Offline hosts retain their last small
status snapshot with an explicit age and unavailable-host error; new machines
without a snapshot contribute no invented financial totals.

The CI pool activity table uses the same public aggregate as `/ci`: running and
waiting builds, latest completion and outcome, oldest running/waiting job ages,
and finished totals for 1h/3h/24h with failure and cancellation counts. Builds are
the default; `--all-jobs` includes control jobs in the same table. JSON includes
both scopes, all completion outcomes, and exact job identities and URLs under
`_ci.pool_activity`. Coverage and observation age are explicit. An unavailable
public source retains the last cached summary with an error; missing observations
under partial coverage are unknown. Host-specific status still shows every pool.

The operator client fetches one public manifest and its jobs summary per status
invocation, independently of host count. It does not fetch job-detail pages or
scan GitHub. This table needs only the updated operator CLI; hosts need no change.

`status -f` / `--follow` opens a live, scrollable view. Use arrows or j/k,
Page Up/Down, Home/End (g/G), and q or Ctrl-C to quit. Left/right scroll wide
reports. Each host uses one persistent SSH connection and sends a new snapshot
when its prepared status, CI overview, configuration, or activity report changes.
File checks are local to that host; follow mode makes no GitHub requests and
does not start host collectors. One client background reader refreshes the public
pool aggregate every 30 seconds; redraws read its prepared cache. Disconnected hosts retain their last
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

Costs have 24h, 7 days and All time columns. All conversation costs, including reviews
and fixes for others, are amortized over landed PR output. Each window includes
every fleet-created PR that landed during that window, regardless of when it was
opened. Open PRs receive no LOC credit. Older retained creation receipts remain
eligible. Fleet totals pool costs and landed LOC before computing the ratio.
Completed costs and recorded running usage are shown separately.
Missing LOC is estimated from measured landed PRs in that window. Such estimates
show `~` and measurement coverage. With no measured sample, output contributes
zero; positive spending over zero LOC shows `$∞`. Older creation-cohort summaries
show unavailable landed output until their collectors are refreshed.

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
The allocation follows the current admitted capacity automatically. `status` reports how many
rounds in progress (running or surveying) chose a focus area, then the slot count at
the current admission limit. That limit falls to zero while new rounds are paused,
but rounds already running keep the focus they started with. Focused slots
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

## Public operations telemetry

The existing Tau Ceti `/ci` page can display approved host observations alongside
queue and GitHub jobs. `tauceti-fleet monitoring --help` runs an independent,
read-only publisher. It consumes the existing prepared status stream, never
resource probes, GitHub discovery or admission mutations.

For an operator-controlled bridge, provide a private JSON registry containing
`{alias, machine_id, target}` rows. Every permanent ID must be the full verified
identity, and every alias must be approved for publication. Duplicate identities
are rejected. Run `tauceti-fleet monitoring --registry FILE --state-file FILE
--publish-url https://ci-data.taucetiproject.org/ingest/operations/hosts
--token-file PRIVATE_GRANT`. One persistent identity-checked SSH stream serves
each host. Viewer traffic opens no connections. Fact observation age and stream
heartbeat age are distinct; disconnects retain the last facts.

An operator bridge registry may set a host's `target` to `null` when no bridge
connection is configured. It publishes that host as unobserved and opens no SSH
connection; a broker-enrolled host can independently start pushing later. The
bridge also forwards only numeric cloud-pool counts from the already-prepared
shared capacity cache, preserving its observation time and scope. It performs
no new capacity discovery.

Enrolled volunteers need only the usual package reinstall and
`tauceti-fleet enable ...` command. Enable automatically provisions the independent
telemetry sidecar. The sidecar obtains its approved alias from its own enrollment
broker; volunteers do not receive an extra token, registry file or install command.
Telemetry setup or publication failure cannot prevent enable or interrupt work.

The sidecar uses the existing host-bound enrollment credential only at that host's
own broker. The broker forwards sanitized observations with a separate grant to
CI; it never sends the host credential to CI. Monitoring/policy requests bypass
the runner-registration serial queue and do not call GitHub. Worker-only/direct
hosts can use the operator's bridge with a separate telemetry-only grant.

The broker's `OPERATIONS_PUBLIC_HOSTS` variable is the approved public
`[{machine_id,alias}]` registry. Its `OPERATIONS_INGEST_TOKEN` secret is the CI
receiver's host telemetry grant. Keep public aliases/configuration separate from
private SSH targets and credentials. Configure both readers before starting
producers; empty/unconfigured public scopes fail closed.

Public projections contain permanent host and VM/runner identities, coarse
worker states, prepared CPU/memory/compiler pressure and source freshness. They
exclude private paths, raw errors, process arguments, credentials, environment,
prompts and model output. Unsupported metrics remain unknown. Slot absence and
observer failure do not establish free capacity. Historical telemetry starts only
at activation; the earlier period is unknown.


The broker operator configures telemetry once with
`python services/ci-broker/scripts/setup.py monitoring`, after deploying the CI
reader. Its private setup directory contains `operations-hosts.json` with only
approved `{machine_id, alias}` rows and `operations-ingest-token`, copied from the
CI reader's telemetry-only host grant. The setup command validates these files,
sets the forwarding secret and deploys the public policy. It never changes host
admissions or enrollment credentials. Later ordinary broker deploys retain this
explicit policy (`keep_vars`). Removing an alias disables new publication for
that host; remove it from the CI reader's public scope to revoke public reads.


`status` includes a detailed merge-queue table from the prepared public operations
snapshot: head and sealed-successor roles, PR counts, live repair/preparation claims,
Lake build-task counters, CI phases and their times, waiting PRs, worker activity and
the last confirmed landing. Prior preparation is distinguished from later additions.
These reads use only the public snapshot and bounded immutable detail pages; they do
not query GitHub or run worker collectors. A failed refresh retains the last facts
with their original observation time and an explicit cached/stale label. Follow-mode
redraws use the same local cache, refreshed by its existing background reader.

Merge-queue timestamps default to `Australia/Sydney`, displaying AEDT or AEST as
appropriate. Use `tauceti-fleet status --timezone UTC` to override it; the option also
applies to `--follow`. JSON includes the same queue facts under `_ci.queue`.
Lake counters count build tasks, can have growing totals, and are not CI verdicts.


Current queue details and rolling history have their own caches, so an older follow
client refreshing the CI-pool cache cannot remove them. The `Queue history` table
reads the current controller's read-only `/statistics` ledger projection; the retired
Bors statistics feed is not a current queue history source. It reports distinct PR
landing receipts, landed batches, completed CI attempt outcomes and workflow-run
p50/p95 durations for 1h, 3h, 24h and 7d. Run durations include setup, reporting and
publication, and are labelled separately from build-only time. Failed refreshes keep
the original facts visibly cached/stale. Existing follow processes need a restart
to load a new client release.

The queue table labels the oldest sealed non-head batch `next head`, with later
sealed batches `sealed, waiting`. A sealed batch has fixed membership even when
its preparation has finished; only unsealed batches receive incoming PRs.
