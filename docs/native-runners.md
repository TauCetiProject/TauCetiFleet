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

Protected `infra` slots (`ci_protected_slots` entries with kind `infra`, in the
TauCetiInfra group) also use native-disk. They run our infrastructure
repositories' CI, never take PR or staging work, and run at CPU weight 1000,
between PR builds' 100 and staging's 10000: infra jobs are rare and someone is
usually waiting on them, while nobody waits on any particular PR build. Once the broker
authorizes the `infra` class for the host, set them live with:

```sh
tauceti-fleet infra-slots HOST --slots 8
```

Infra slots use their own native image, exported from the same verified VM base
with what other repositories' ordinary GitHub workflows expect of GitHub's runner
image: current Git (the git-core PPA), Node.js, and a runner-owned
`/opt/hostedtoolcache` set as the tool cache, so `actions/setup-python` interpreters
find their libraries. PR and staging slots keep the minimal image. Both native
images include the system C compiler (`gcc`, as `cc`), which Lake uses for
packages with C sources such as doc-gen4's dependencies; an export without a
working `cc` fails rather than producing an image. Setup and live
`update` prepare the infra image when the host has infra slots; an infra slot
whose image is missing fails explicitly rather than falling back. Infra never
runs in a VM: a slot converted to infra that still holds another class's
pending registration cancels it and starts native.

This places the slots directly above the PR slots and raises `ci_max_jobs` by
their count, so PR slots and running guests are untouched. `--slots 0` removes
them; removed slots finish their current job. The CI autoscaler sizes only the
PR slots below them. On a host with infra slots it keeps that PR count fixed,
because changing it would move the infra slot numbers.

`--pr-slots N` sets that PR count in the same step, moving the infra slots to sit
directly above the new count; `--pr-slots 0` leaves the host running only its
infra slots:

```sh
tauceti-fleet infra-slots HOST --slots 12 --pr-slots 0
```

Busy guests in slots that change class or fall above the new count finish their
job; idle guests in slots that move between PR and infra retire and restart in
their new class.

`native-runner HOST --prepare` remains available for an explicit image refresh;
it is not required to enable PR CI through the standard setup.

## Memory and disk

Each job gets a private OS clone and the real `/opt/actions-runner/_work`
directory on local disk before checkout or cache restoration. This covers Actions
scratch, toolchains, source and build outputs. Reflinks are optional; ordinary
copies work. Writable job files are never shared with another job.

The systemd unit caps whole-job RAM at 54 GiB, disables job swap and preserves
normal PR CPU priority. `LimitFSIZE` is unlimited because .NET's RAM-backed
executable mapper otherwise fails. Unlike a VM's, every thread of a native
job counts against the host's CI slice, so each job may hold 16,384 tasks and
the slice 262,144; the old shared 16,384 starved Lean and leantar of threads
once a few cache restores overlapped. The existing ordinary PR startup RAM check
(currently 20 GiB), lock and spacing apply to native jobs too. Native infra jobs
start two seconds after the previous start, like small control VMs, rather than
ten: they have no VM boot to settle, and their short jobs would otherwise queue
behind the gap. Clean host file cache is discounted in native memory estimates.
Raw charge and peaks remain
visible; the cap is not a reservation. The ordinary autoscaler forecasts future
growth once and does not inflate VM estimates from native clean cache.

Readiness checks startup headroom without reserving the aggregate CI memory
budget or the sum of native per-job caps, even when those caps fit that budget.

Native startup checks for 48 GiB free on the instance filesystem before JIT
registration. This is an estimate for population plus the host margin, not a disk
reservation. A 128 GiB whole-instance allocated-disk budget and 8 GiB host free-space
threshold are monitored. The budget must admit a from-source build of the whole
library, which the nightly verification runs on these slots. These are **not hard filesystem quotas**; bursts and
other writers can exceed a budget between observations. No quota provisioning or
FUSE is required.

Cheap free-space/inode checks and phase observations run every two seconds.
Allocated disk is measured by one bounded whole-instance scan at most every
five minutes, with a two-minute scan deadline; slow scans do not block the cheap
checks. Scanner diagnostics cannot fill an unread pipe. A failed scan retains
the last verified value and its original timestamp, with its error and age
reported explicitly. A scan that reports a total after `du` met vanishing or unreadable files (exit 1)
counts as a measurement, not a failure. Telemetry counts such partial scans
(`disk_scan_partial_total`, and `disk_scan_partial_streak` among consecutive accepted
measurements; a failed scan neither extends nor resets it), so a subtree `du` never reads
stays visible. Three consecutive failed scans still stop
the instance.
Telemetry includes scan duration, deadline and interval for live verification.
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
`native-samples.jsonl`, `native-result.json`, hooks and runner output.
`native-result.json` records the job unit's cumulative block I/O at job end (`io`:
the unit's `io.stat` row for the instance filesystem's device, so an md array's writes
are counted once, plus every row for the record), the inodes in the instance root
just before it is removed (`files`, one `du --inodes` bounded at 60 seconds), and
`io_before_removal`, `removal_seconds` and `io_after_removal`. The difference of the
last two I/O snapshots is the interval's accounting: removal, plus any of the job's
writeback that landed then. The core result is written before any of these
measurements, and a requested stop skips them. Unit-level `io.stat` includes the writeback the kernel attributes
to the job, but not XFS log writes or the asynchronous cleanup unit's unlinks.

The controller samples `/proc/diskstats` once a minute and publishes each whole drive's
and array's read and write rates, mean write latency (write milliseconds over writes
completed) and utilization as `drive_io` in `status.json` and the monitoring stream. It
keeps at least a week of those minutes in `drive-io.jsonl` (16 MiB, halved when full) in the state directory. Completion
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

## Shared read-only build tree

A host can keep one trusted, read-only copy of the `.lake` a TauCeti PR build restores
(TauCetiWorker decision 211). It is off by default and switched per host:

```sh
tauceti-fleet ci-tree HOST           # pool and builder state
tauceti-fleet ci-tree HOST --on      # build entries and offer them to new PR guests
tauceti-fleet ci-tree HOST --off     # stop building and offering; leased entries stay until released
tauceti-fleet ci-tree HOST --slots even   # offer entries only to even-numbered PR slots (all, even or odd)
```

Only PR-class slots are offered an entry; staging, infra and the protected special-purpose slots
keep today's path. `--slots even` (or `odd`) leaves the other half of the PR slots unbound, so
they build the same PRs on the same drives at the same time without the tree: a control group
for measurement (TauCetiWorker decision 216). The default is `all`. A guest leases when it
starts, so a change reaches each slot at its next guest.

The split needs this revision on both ends. `--slots` sets the split on its own first and
refuses to go on unless the host echoes it back. The controller reports the split it applies
as `ci_shared_tree.slots` in `status.json`, so check that field after an update before relying
on the control group. An older controller ignores the setting and offers the tree to every PR
slot, so switch the tree off before rolling a host back past this revision.

Tools' `pr-build.yml` mounts the leased entry as the lower layer of a private overlay at
`pr/.lake` in ordinary PR builds (Tools `scripts/shared_tree.py`), with today's path on any key
mismatch. It writes a trusted record of each job (`hit` or `miss:<reason>`, the upper layer's
bytes and files, the verdict) to the control link's `telemetry/shared-tree.json`, which native
cleanup keeps with the job's other evidence under `native-results/<instance>/telemetry/`.

The pool lives in `ci-tree/v1` under Fleet's data directory. An entry is built only from
CI's published `main` outputs, never from a job. The builder is a Fleet-owned native
instance in offline mode at PR CPU priority, with no runner registration. It checks out
the newest first-parent `main` commit whose Lake revision map is published, with Tools
`main` materialized over the project's scripts. It runs `pr-build.yml`'s trusted steps at
the same absolute paths and in the same environment: `lake exe cache get`,
`lake-cache-get.sh` for that exact revision, and a restore-only
`lake build --iofail --no-build`, which fails rather than compile anything. Its `.lake`
is written directly into the pool's staging directory.

The guest verifier (`ci_tree_guest.py`, installed in every native guest as
`/usr/local/bin/tauceti_ci_tree.py`) then prunes job scratch, other revisions' maps, and
mappings and artifacts nothing references. It fails closed unless all of the following
hold:

- every published module is in the tree, with a trace and mapping naming exactly the
  published archive;
- the checkout and every package are at their manifest revisions, with clean tracked files;
- every output any root or package trace names exists, and no output-named file exists
  that no trace or mapping accounts for;
- every trace and mapping uses a Lake schema the verifier knows, with its three oleans and
  ilean;
- every retained artifact, every traced output and every `.hash` record in any `.lake`
  matches its content hash, computed by the toolchain's own Lake hash.

The host then checks structure as the mapped root. Only directories, regular files and
in-tree relative symlinks are allowed; every file must belong to the guest runner, with
no set-ID, sticky or world-writable modes. Every hard link must have all its names
inside the entry. The tree walk fails on any unreadable directory, and a symlink must
resolve, through its whole chain, inside the entry. The host records a stat inventory after the final permission state,
then renames the entry into `entries/` under the collection lock. Modes stay as Lake
left them: read-only comes from the bind, not from write bits.

The entry's key (`ready.json`) names everything that decides whether a job's traces match:

- the toolchain, and Lean's Git hash and binary;
- the Lake version;
- the manifest and package revisions;
- the lakefile;
- the architecture;
- the content of the `tauceti-lean` wrapper and the watchdog toolchain.

`tauceti_ci_tree.py key` computes the same key in a job.

When a PR guest is staged, Fleet leases the newest entry under the collection lock and
records it in `native.json`. The guest's namespace then binds that one entry read-only
(`ro,nosuid,nodev`) at `/opt/tauceti-shared-tree`, never the whole pool. Native cleanup
releases the lease only once the unit, and so every process that could hold a mount, has
stopped. Collection keeps every leased entry and the newest two. It retires the rest by
renaming them out of `entries/` under the lock, and removes them in a background unit.
The guest's disk scan excludes the bind.

Builds run at most once per host at a time. The controller checks every 30 minutes and
skips an entry younger than 45 minutes. It also skips when the commit and the key are
both unchanged; a new wrapper or watchdog alone does trigger a rebuild. It backs off two
hours after a failure. It never builds with four entries present, or with less than
164 GiB free. That covers the builder's 60 GiB peak (entry, rootfs, toolchain and
download scratch), two guests' 48 GiB start checks and the 8 GiB margin. Each entry is
about 38 GB and 1.3 million files. `status.json` reports `ci_shared_tree`, and
`builds.jsonl` in the pool keeps recent build outcomes.

Validate with:

```sh
python3 tests/integration/ci_tree_build.py [--keep]
python3 tests/integration/native_runner.py tests/integration/native-shared-tree.sh --shared-tree
```

The first builds one entry in an isolated pool. The second needs `TAUCETI_FLEET_DATA`
pointing at a pool with an entry. It checks that the leased bind is read-only even to
the guest's root, that overlay writes and deletions stay in the job's upper layer, and
that the lease is released after cleanup.
