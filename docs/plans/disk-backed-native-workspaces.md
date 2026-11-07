# Switch native CI runners to disk-backed workspaces

Status: implementation plan, 7 November 2026. This document does not deploy a
change. Implement in TauCetiFleet, then release one live general-PR canary before
expanding. The production default remains VM until the rollout steps below.

## Decision and evidence

Use disk-backed native workspaces. The fleet is continually memory constrained;
keeping approximately 26 GiB resident per job to save roughly half a minute
when RAM is plentiful is the wrong tradeoff. Spare RAM should cache workspace files and be reclaimable when
other work needs it. Do not retain tmpfs as a workspace fallback.

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

## 1. Make disk the native launch mode and preserve old receipts

In [native_runner.py](../../tauceti_fleet/native_runner.py), create the actual
`/opt/actions-runner/_work` as a private directory on the disposable root's host
filesystem. Skip its tmpfs mount before the Listener starts, so `_actions`,
`_temp`, checkouts, cache restoration and outputs all use disk from the outset.
Keep the small system and sandbox tmpfs mounts unchanged. Reuse the verified
native OS image and current materialization path; no image export is required
solely to change workspace storage. Ordinary copies remain supported when
reflinks are unavailable.

Record new instances as `runtime: native-disk`, with explicit workspace storage
and disk-budget metadata. Accept `native-tmpfs` as a legacy native runtime for
status, event reconciliation, recovery and cleanup only. An existing receipt
without a storage field keeps its old meaning; never infer its mode from the
new default or remount an active job's workspace. Future native selection always
chooses disk.

Audit literal `native-tmpfs` comparisons in
[vm.py](../../tauceti_fleet/vm.py),
[ci_autoscale.py](../../tauceti_fleet/ci_autoscale.py), status and tests. In
particular, new native receipts must continue to bypass VM image/media claims
and use native launch/retirement. Centralize runtime classification so adding the
new identifier cannot accidentally route a native instance through QEMU logic.
Existing registrations must retain their original instance and runner classes.

## 2. Replace tmpfs accounting with disk-aware accounting

Remove the disk mode's inherited **52 GiB RAM startup requirement** and **32 GiB
persistent workspace-growth forecast**. They were justified by tmpfs population;
leaving them in place would defeat the improvement before a native job starts.
Use the existing ordinary PR startup memory requirement, currently 20 GiB, and
the same host/ancestor measurement, startup lock and spacing. Missing native
prerequisites or a native cooldown select the ordinary VM path before registration.
Do not introduce another native wait or use tmpfs when disk cannot start.

Keep actual RAM usage, reclaimable cache, startup estimates and hard limits
separate. Extend the existing per-instance observation to read `memory.stat`
alongside `memory.current`. Use the existing clean-file discount for the disk
mode's memory estimates; retain raw charge and peak for diagnostics. Keep legacy
running tmpfs receipts' growth accounting until those instances retire.

Remove the tmpfs-specific growth deduction for disk receipts. Retain the existing
ordinary autoscaler growth rules for the first canary, charging anticipated
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
make host quota administration a prerequisite. Hard quotas can be a later
improvement. Report low space, observed budget exhaustion, inode exhaustion and
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
  pressure-service restart, supervisor death, registration retry and mixed old/new
  receipts. Descendants, private filesystems, pressure bridges and ownership must
  be released or retained explicitly for retry, never forgotten.

Run the normal Fleet tests and existing namespace/Listener integration checks.
Update [native-runners.md](../native-runners.md), command/status descriptions and
fixtures to distinguish disk budgets from the former tmpfs quota. Keep legacy
receipt coverage even after the new launch default changes.

## 5. Ship one live general-PR canary

After local validation, merge and install a supported Fleet release through the
live update path. Record installed Fleet/Worker and native-image identities and
the relevant operator settings; do not overwrite unrelated concurrent changes.
Keep staging on VMs and preserve the total CI ceiling. Select the next general
slot using the existing `native-runner HOST --next-general` control. Existing
healthy jobs finish normally. The first successful native selection must report
`native-disk`; a silent VM fallback is useful capacity but is not canary evidence.

Verify actual assigned head/base/tested-tree identities, normal Actions setup,
checks and result publication, phase progress, adjusted RAM, disk usage and
monitoring cost. Observe useful completions and verify cleanup after each. Include
cold preparation and realistic artifact variation through ordinary traffic.
Compare with contemporary VM jobs when workload identities allow it, without
turning an exact VM pairing into another requirement before trying the canary.
The first milestone is a real disk-native production job completed and cleaned
up with its memory benefit visible under normal fleet operation.

## 6. Expand under the existing runner controls

Use the existing canary guidance of ten useful jobs across three PRs before
broad expansion. This is rollout evidence, not a per-job admission condition.
Before raising the native ceiling beyond one, validate two disk-native full jobs
actually executing concurrently and completing under representative pressure.
Use a controlled test scope or rollout count setting; keep existing total-slot,
startup and compiler controls. A queued second job is not a concurrency pass.

Then replace general-slot VMs at normal turnover, one slot at a time. Record
useful completions per unit time, compiler RAM-wait time, population overhead,
aggregate RAM and I/O, disk peaks and cleanup. Expand on demonstrated progress
and correct lifecycle behavior; do not demand that disk beat tmpfs in an isolated
job when RAM is plentiful. If expansion exposes poor estimates or scan overhead,
fix the existing observation/startup paths rather than adding another admission
system. Removing the large workspace floor does not establish a fixed capacity
multiplier or eliminate real compiler memory demand.

## Rollback and completion

Disable future native selections with the existing `native-runner HOST --off`
control and return those future starts to VM. Let healthy disk-native jobs finish;
stop and repair only failed or unsafe instances through ordinary recovery. Keep
a controller that understands both runtime identifiers until all new receipts
are retired; selection rollback does not require a binary downgrade or a fleet
drain. Never roll back to tmpfs workspaces.

The switch is complete when new selected native jobs use disk, legacy receipts
remain recoverable, real jobs publish correct outcomes and clean up, the workspace
floor is gone, and expanded concurrency shows useful progress within measured
RAM/disk capacity. The first canary and subsequent expansion are deployable
milestones; hard quotas, shared artifacts and a redesigned memory scheduler are
not prerequisites.
