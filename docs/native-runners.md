# Disk-backed native PR runners

The `native-disk` backend runs general PR jobs in a private rootless namespace
with a disk-backed Actions workspace. Spare RAM caches files and remains
reclaimable by the host. This is the standard backend for PR-dedicated slots.
Staging and mixed PR/staging registrations use VMs. There is one native implementation.

Native instances use the existing broker/JIT registration, durable ownership,
job hooks, slot recovery, idle retirement, priority, startup lock and spacing.
Population, execution and cleanup count against the ordinary total runner ceiling.
The compiler gate, pressure protocol and stock Lean/Lake are unchanged. There is
no workspace RAM reservation or extra compiler admission rule.

## Standard setup

Use the ordinary preparation and activation commands:

```sh
tauceti-fleet prepare HOST
tauceti-fleet enable HOST --ci pr --max-jobs 4
tauceti-fleet native-runner HOST --json
```

`--max-jobs 4` is an example ceiling, not a reservation. `enable --ci pr` prepares
the native OS image from the verified base before registration. `prepare` also
exports it when the host already has a PR role. No backend selection setting or
extra activation command is needed. The image and role configuration persist
across controller restarts and reboots.

The standard live `update` prepares a missing native image before handing over
to the new controller. It uses the existing verified VM base; healthy jobs and
normal worker/CI admissions continue. Existing VM jobs retain their ownership
and finish normally, and later PR-dedicated starts use disk-native. Repeating
setup/update reuses a valid image. Image export uses a small unregistered VM and
requires the existing QEMU/KVM preparation tools. Neither image preparation nor
execution uses host root or changes host sysctls.

A PR-only host uses native in all its slots. A host accepting both classes with
reserved staging slots uses VMs in those reserved slots and native in the others.
A host accepting both classes without a reservation retains mixed VM runners so
staging work remains eligible. `reserve-staging` prepares any newly needed native
image before changing live slot classes; it preserves running jobs. Existing
registration intents retain their original classes until resolved.

The native host needs unprivileged namespaces, subordinate UID/GID mappings and
`vm.max_map_count >= 1048576`. `doctor` reports these requirements for native PR
slots. An image, namespace or mapping failure is explicit; it does not silently
select a different backend. The compiler gate and shared slot/startup controls
are unchanged. There are no canary slot selectors or native enable/disable flags.

`native-runner HOST --prepare` remains available for an explicit image refresh;
it is not required to enable PR CI through the standard setup.

## Memory and disk

Each job gets a private OS clone and the real `/opt/actions-runner/_work`
directory on local disk before checkout or cache restoration. This covers Actions
scratch, toolchains, source and build outputs. Reflinks are optional; ordinary
copies work. Writable job files are never shared with another job.

The systemd unit caps whole-job RAM at 54 GiB, disables job swap and preserves
normal PR CPU priority. `LimitFSIZE` is unlimited because .NET's RAM-backed
executable mapper otherwise fails. The existing ordinary PR startup RAM check
(currently 20 GiB), lock and spacing apply to native jobs too. Clean host file
cache is discounted in native memory estimates. Raw charge and peaks remain
visible; the cap is not a reservation. The ordinary autoscaler forecasts future
growth once and does not inflate VM estimates from native clean cache.

Native startup checks for 48 GiB free on the instance filesystem before JIT
registration. This is an estimate for population plus the host margin, not a disk
reservation. A 56 GiB whole-instance allocated-disk budget and 8 GiB host free-space
threshold are monitored. These are **not hard filesystem quotas**; bursts and
other writers can exceed a budget between observations. No quota provisioning or
FUSE is required.

Cheap free-space/inode checks and phase observations run every two seconds.
Allocated disk is measured by one bounded whole-instance scan at most every
60 seconds; slow scans do not block the cheap checks. Failed/timed-out scans are
unknown rather than zero usage, and repeated failures stop the affected instance.
A budget breach, low space, inode exhaustion or resource-monitor failure is an
infrastructure failure. Only the affected instance is stopped and cleaned up.

The runner root has no host home, credentials or writable host cgroups mounted.
Trusted setup has root inside its disposable user namespace and shares host
networking. Candidate computation retains the workflow's offline bwrap boundary.
The existing read-only cgroup-counter view supplies the absolute local OOM guard.

## Observation and recovery

`native-runner HOST --json` reports the standard PR backend, image readiness
and last backend-choice reason. Each instance reports raw `memory_bytes`, `memory_working_bytes` after
clean-file discount, clean-cache bytes, the 54 GiB cap and disk budget. Progress
includes storage kind, host free space/inodes, whole-instance allocated disk,
measurement timestamp/age, scan state/errors and phases. Shared filesystem free
space is not labelled as a per-job workspace quota.

The instance directory holds `native.json`, `native-progress.json`,
`native-samples.jsonl`, `native-result.json`, hooks and runner output. Completion
is durable before optional diagnostics are copied. Controller restart recovers
job ownership from hook history. The pressure receiver can restart without
reusing compiler permits; a failed receiver supervisor fails the instance.
Native infrastructure uses ordinary slot failure/retry handling.

Retirement stops the whole unit, archives bounded diagnostics (latest 50
instances), removes the mapped-UID root asynchronously, then releases ownership.
Failed cleanup retains ownership for retry. Correct failures forward while
unaffected jobs continue. GitHub conclusions remain owned by the workflow and
usual result collector.

## Validation and deployment

Run the Fleet tests and supported unregistered namespace checks:

```sh
python3 -m unittest discover -s tests
python3 tests/integration/native_runner.py tests/integration/native-smoke.sh
python3 tests/integration/native_runner.py tests/integration/native-listener-smoke.sh
python3 tests/integration/native_runner.py SCRIPT --cancel-after 5
```

The smoke check exercises the real workspace filesystem, private hard links,
stock namespace sudo, candidate isolation, receiver restart and the pressure/guard
transport. The Listener smoke starts the real .NET executable without registration.
Full frozen CI must preserve consumed inputs, tested tree, checks and rebuild set.
Exercise cancellation while populating, waiting and computing, plus resource
failure and ownership recovery.

Use the supported live update path for existing hosts. Verify running
Fleet/Worker and image revisions, useful completions, native disk/RAM observations
and instance cleanup. Preserve total capacity, staging reservations and worker
settings. Repair failures forward while unrelated jobs continue. The standard
setup retains the validated namespace, offline-build sandbox, resource bounds
and lifecycle behavior from the original cutover.

The [implementation plan](plans/disk-backed-native-workspaces.md) and
[matched workspace evidence](../experiments/native-disk-workspace-20261107/README.md)
explain the storage choice and experimental limits. Disk has extra I/O cost;
removing the approximately 26 GiB persistent workspace floor is the reason to use
it on the memory-constrained fleet. Do not promise a runner-count multiplier from
a single-job benchmark.

The [7 November production cutover](evidence/native-disk-cutover-20261107.md) records
six successful real jobs, overlapping computation, four-slot turnover, exact
revisions, resource measurements and cleanup.
