# Disk-backed native PR runners

The `native-disk` backend runs general PR jobs in a private rootless namespace
with a disk-backed Actions workspace. Spare RAM caches files and remains
reclaimable by the host. VM remains the backend for staging and general slots
not selected for native execution. There is one native implementation.

Native instances use the existing broker/JIT registration, durable ownership,
job hooks, slot recovery, idle retirement, priority, startup lock and spacing.
Population, execution and cleanup count against the ordinary total runner ceiling.
The compiler gate, pressure protocol and stock Lean/Lake are unchanged. There is
no workspace RAM reservation or extra compiler admission rule.

## Prepare and select

Prepare the supported native OS image and select an existing general PR slot:

```sh
tauceti-fleet native-runner tauchonki --prepare --json
tauceti-fleet native-runner tauchonki --json
tauceti-fleet native-runner tauchonki --slot 3 --json
```

`3` is an example; choose a general slot from the current inventory. Selection
changes its next instance at normal turnover. Existing healthy jobs and worker
admissions continue. No drain is needed. Use a fixed slot for the first canary.
After concurrent-job validation, `--next-general` selects disk-native for future
general PR starts across slots. Multiple native jobs can run within the existing
total slot limit; there is no separate one-native capacity restriction. Reserved
staging slots remain VMs. `--off` clears the operator's native selection.

Already-owned registration classes and a mixed PR/staging idle-pool spare retain
their original classes. A selected native startup failure is reported explicitly
and retried through normal slot handling; it does not silently select another
backend. Native preparation, launch and cleanup use no host root or sysctl changes.
The host needs the existing unprivileged namespaces, subordinate UID/GID mappings
and `vm.max_map_count >= 1048576`.

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

`native-runner HOST --json` reports selected slots, image and backend-choice
reason. Each instance reports raw `memory_bytes`, `memory_working_bytes` after
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

Before the one-time cutover, inventory old experimental native units/receipts.
Retire any such instance with its existing release before updating; keep normal
VM admissions running. The disk implementation has no old-runtime reader or
migration branch. Install live, select one fixed general slot, and verify a real
Actions job's checks, identity, memory, disk and cleanup. Then prove two full
native jobs run concurrently and complete under representative pressure before
expanding native selection at normal turnover. Preserve total capacity, staging
reservations and worker settings. Record running Fleet/Worker revisions, useful
completions and repair any interrupted work.

The [implementation plan](plans/disk-backed-native-workspaces.md) and
[matched workspace evidence](../experiments/native-disk-workspace-20261107/README.md)
explain the storage choice and experimental limits. Disk has extra I/O cost;
removing the approximately 26 GiB persistent workspace floor is the reason to use
it on the memory-constrained fleet. Do not promise a runner-count multiplier from
a single-job benchmark.
