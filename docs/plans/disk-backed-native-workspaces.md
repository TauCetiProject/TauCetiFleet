# Switch native CI runners to disk-backed workspaces

Status: implemented and deployed, 7 November 2026. [PR #144](https://github.com/TauCetiProject/TauCetiFleet/pull/144)
ships the single disk-backed native implementation. The canary and initial pair
completed successfully; all four general slots have now turned over to native.
The expanded pair also completed with overlapping computation, correct result
publication and cleanup. See the [cutover evidence](../evidence/native-disk-cutover-20261107.md)
for exact revisions, job outcomes, memory, disk and cleanup. The sections below
record the implementation and validation requirements.

## Decision and evidence

Use disk-backed native workspaces. The fleet is continually memory constrained;
keeping approximately 26 GiB resident per job to save roughly half a minute
when RAM is plentiful is the wrong tradeoff. Spare RAM should cache workspace
files and be reclaimable when other work needs it.

The [matched disk/tmpfs experiment](../../experiments/native-disk-workspace-20261107/README.md)
used identical restored inputs and the unchanged compiler gate:

| Measurement | Disk-backed native | Tmpfs native |
| --- | --- | --- |
| Completed computational bodies | 1m57s, 2m06s | 1m34s, 1m30s |
| Mean charge after clean-file discount | 1.92–1.95 GiB | 27.02–27.09 GiB |
| Charge after an own-cgroup reclaim probe | 0.05–0.50 GiB | 26.40 GiB |
| RAM-gate waits / OOM events | None | None |

Disk's total mean charge was about 20.5 GiB, mostly reclaimable file cache. It is
not a 2 GiB total-memory promise. The offline probe did not run the Actions
Listener, and the workload restored existing outputs rather than rebuilding
TauCeti. Use these results to choose storage, not to predict runner counts or
fleet throughput. The experiment also identified at least 32.3 CPU-seconds of
`du` in the final disk body; remove that repeated traversal cost.

## Operating constraints

- Keep the existing compiler gate, pressure protocol and priority behavior.
  Add no reservation ledger, compiler queue, per-check permission or workspace
  RAM deduction. Use stock Lean/Lake with no upstream patches.
- Keep the initial 54 GiB whole-job cgroup cap, zero job swap, normal PR CPU
  priority and unlimited `LimitFSIZE` needed by the stock .NET Listener.
- Require no host root, quota provisioning, FUSE installation or host sysctl
  changes. Use ordinary writable local storage and the supported namespaces.
- Preserve broker/JIT ownership, hooks, GitHub conclusions, staging reservations,
  startup locking/spacing and total runner count. Candidate computation retains
  its existing offline bwrap boundary.
- Deploy live with normal CI admissions enabled. Preserve current worker settings
  and unrelated work. Do not drain or wait for the whole fleet to become idle.

## 1. Replace the native workspace implementation

In [native_runner.py](../../tauceti_fleet/native_runner.py), create the actual
`/opt/actions-runner/_work` as a private directory on the disposable root's host
filesystem. Delete the workspace tmpfs mount. Before the Listener starts,
`_actions`, `_temp`, checkouts, cache restoration and outputs all use disk.
Keep the small system and sandbox tmpfs mounts unchanged. Reuse the verified
native OS image and current materialization path; no image export is required
solely to change workspace storage. Ordinary copies remain supported when
reflinks are unavailable.

Use one native runtime identifier, `native-disk`, and one workspace implementation.
Delete `native-tmpfs`, its launch branches, quota-specific fields and tests that
exist only to support that implementation. Add no storage-mode flag, dual-runtime
reader, compatibility alias or receipt-version migration.

Replace the literal runtime comparisons in
[vm.py](../../tauceti_fleet/vm.py),
[ci_autoscale.py](../../tauceti_fleet/ci_autoscale.py), status and tests with the
single current native classification. Native instances must bypass VM image/media
claims and use native launch, event handling and retirement. VM remains a supported
backend for staging and slots not yet switched; it is not a recovery version of
the native workspace implementation.

Perform the one-time handover in section 5 before installing this code. There
must be no old native instance or owned receipt left for the new controller to
interpret. Clean up that state operationally rather than shipping an old reader.

## 2. Replace tmpfs accounting with disk-aware accounting

Delete the native **52 GiB RAM startup requirement** and **32 GiB
persistent workspace-growth forecast**. They were justified by tmpfs population;
leaving them in place would defeat the improvement before a native job starts.
Use the existing ordinary PR startup memory requirement, currently 20 GiB, and
the same host/ancestor measurement, startup lock and spacing. A selected native
slot launches disk-backed native. Surface startup failures explicitly and repair
them through the existing retry/ownership path; do not silently switch its backend
or introduce another wait in front of active compilers.

Keep actual RAM usage, reclaimable cache, startup estimates and hard limits
separate. Extend the existing per-instance observation to read `memory.stat`
alongside `memory.current`. Use the existing clean-file discount for the disk
runner's memory estimates; retain raw charge and peak for diagnostics. Delete the
old workspace-growth bookkeeping rather than retaining it behind a mode check.

Remove the tmpfs-specific growth deduction. Retain the existing ordinary
autoscaler growth rules for the first canary, charging anticipated
startup growth once. Do not let a disk-native clean-cache peak inflate the common
VM footprint estimate, or count its workspace bytes as unreclaimable RAM. Audit
both startup and autoscaler paths for leftover special cases. A 54 GiB cap is
never a reservation, and the measured 1.9 GiB proxy is never a universal job cap.
No forecast is subtracted from compiler credits for already-running jobs.

## 3. Bound disk usage without an expensive monitoring loop

Adapt the existing native watchdog in
[native_inside.py](../../tauceti_fleet/native_inside.py), using these initial
settings:

| Setting | Initial implementation |
| --- | --- |
| Whole-instance monitored disk budget | 56 GiB: existing 24 GiB root allowance plus 32 GiB workspace allowance |
| Host free-space stop threshold | Existing 8 GiB |
| Cheap host free-space check | Every 2 seconds, independent of recursive usage scans |
| Recursive whole-instance usage scan | At most once per 60 seconds; one scan at a time, bounded timeout |
| Disk startup check | Recalibrate the existing native disk check for the disk workspace; initial estimate 48 GiB free |

The 48 GiB startup estimate allows the observed approximately 30–35 GiB populated
instance, the existing 8 GiB host margin and some preparation variation. It is a
disk-space check, not a RAM reservation or another compiler admission rule. Keep
it in the existing pre-registration storage decision; record actual cold-setup
peaks during the canary and adjust that estimate if needed. Never reserve the
entire 56 GiB budget through a new capacity ledger.

These are **monitored disk budgets, not hard filesystem quotas**. Usage can
overshoot between scans or grow faster than the free-space monitor can react.
Retain that explicit limitation of the existing rootless canary model; do not
make host quota administration a prerequisite. Report low space, observed budget
exhaustion, inode exhaustion and
ENOSPC as infrastructure failures and stop only the affected instance.

Separate cheap space checks from the scanning task so a slow `du` cannot suspend
them. Count allocated blocks, preserve hard-link deduplication within the private
instance, and include root, toolchain and workspace once. Avoid overlapping
scans, repeated phase-triggered walks and a separate full scan of each workspace
subdirectory. Cache completed measurements with timestamps; a stale or timed-out
scan is explicitly unknown rather than zero or proof of budget compliance. Keep
bounded retries and fail closed through existing infrastructure recovery if the
watchdog cannot maintain its bounds.

`statvfs` on the disk workspace describes the shared host filesystem. Expose
those values as host free space, never as per-job allocation or quota. Report
workspace storage, whole-instance allocated disk, monitored budget, measurement
age, raw/adjusted RAM, OOM events and compiler-wait reasons separately. Retain the
fast trustworthy RAM/CPU/I/O samples. No production cache flush or periodic
`memory.reclaim` request is part of this design.

## 4. Validate the supported implementation locally

Use focused lifecycle/accounting tests and the existing unregistered integration
harness; no GitHub registration is needed for these checks.

- Verify the real Actions `_work` mount is disk-backed before setup, the stock
  Listener starts, and its scratch/toolchain paths work. Preserve instance
  isolation and avoid writable cross-job hard links or shared cache changes.
- Repeat the frozen full computational replay with the same consumed manifest,
  tested tree, checks and rebuild set. Retain all failed or incomplete attempts.
  Record setup separately from computational time; do not require a second broad
  VM benchmark before shipping the one-runner canary.
- Confirm the workspace has no large shmem footprint and clean cache is reclaimable.
  An optional own-cgroup post-job probe is test-only. Measure active-job charge,
  dirty/writeback pages, kernel memory and I/O rather than relying on RSS alone.
- Exercise startup at headroom that fits the existing ordinary PR threshold but
  not the old 52 GiB threshold. Verify no residual 32 GiB workspace promise blocks
  the start and no new native-specific compiler wait appears.
- Verify scan cadence, no overlap, continued cheap checks during a slow scan,
  stale/error reporting, allocated-block accounting and infrastructure cleanup
  after simulated space/budget exhaustion. Check that repeated traversal CPU is
  substantially below the experiment's 32.3-second observation.
- Cancel during population, compiler waiting and execution; test controller and
  pressure-service restart, supervisor death and registration retry. Descendants, private filesystems, pressure bridges and ownership must
  be released or retained explicitly for retry, never forgotten.

Run the normal Fleet tests and existing namespace/Listener integration checks.
Update [native-runners.md](../native-runners.md), command/status descriptions and
fixtures for the single disk-backed native implementation. Remove obsolete tmpfs
fixtures and tests; keep the lifecycle, resource-bound and ownership tests.

## 5. Ship one live general-PR canary

After local validation, merge the implementation. Record Fleet/Worker and
native-image identities and the relevant operator settings; do not overwrite
unrelated concurrent changes. Complete the handover below, then install the
supported release through the live update path.

Before replacing the controller, inventory experimental native instances and
receipts. If none remain, proceed directly. Otherwise disable only future native
selection under the existing release while ordinary VM admissions continue. Let
the specific healthy native job finish, or repair an actually failed instance,
and verify deregistration, stopped descendants, removed filesystems/bridges and
released ownership. Resolve abandoned receipts with that release's normal cleanup
before installing the replacement. This is a one-time handover; leave no migration
command, old schema reader or temporary cleanup branch in the shipped code.
Never drain the fleet or wait for unrelated jobs to finish.

Keep staging on VMs and preserve the total CI ceiling. Select one existing general
PR slot using `native-runner HOST --slot N` for its next ordinary turnover. Existing
healthy jobs continue. Verify the assigned job actually runs as `native-disk`.

Verify actual assigned head/base/tested-tree identities, normal Actions setup,
checks and result publication, phase progress, adjusted RAM, disk usage and
monitoring cost. Observe useful completions and verify cleanup after each. Include
cold preparation and realistic artifact variation through ordinary traffic.
Compare with contemporary VM jobs when workload identities allow it, without
turning an exact VM pairing into another requirement before trying the canary.
The first milestone is a real disk-native production job completed and cleaned
up with its memory benefit visible under normal fleet operation.

## 6. Expand under the existing runner controls

After useful canary completions confirm correctness and cleanup, validate two
disk-native full jobs actually executing concurrently and completing under
representative pressure. Use the existing total-slot, startup and compiler
controls. A queued second job is not a concurrency pass. Adapt the initial
one-native canary restriction for this test and remove that restriction when
expanding; do not add another permanent capacity-control mechanism.

Then replace general-slot VMs at normal turnover, one slot at a time. Record
useful completions per unit time, compiler RAM-wait time, population overhead,
aggregate RAM and I/O, disk peaks and cleanup. Expand on demonstrated progress
and correct lifecycle behavior; do not demand that disk beat tmpfs in an isolated
job when RAM is plentiful. If expansion exposes poor estimates or scan overhead,
fix the existing observation/startup paths rather than adding another admission
system. Removing the large workspace floor does not establish a fixed capacity
multiplier or eliminate real compiler memory demand.

## Finish the cutover and repair forward

Correct failures in the disk-backed implementation and retry affected jobs through
ordinary Fleet ownership and recovery. Keep unaffected jobs and admissions running.
The plan includes no automatic backend reversion, retained tmpfs implementation,
release-downgrade procedure or rollback switch added for this change.

The switch is complete when selected native jobs use disk, the old implementation
and its special accounting are deleted, real jobs publish correct outcomes and
clean up, and concurrent native jobs make useful progress within measured RAM and
disk capacity. Keep the implementation focused on that result: one native code
path, ordinary runner controls and the existing compiler gate.
