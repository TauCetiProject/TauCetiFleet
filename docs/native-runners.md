# Native tmpfs PR runner canary

The optional `native-tmpfs` backend replaces the next instance of one general PR
slot. VM is the default. It uses the existing broker/JIT registration, durable
instance and registration receipts, job hooks, slot recovery, idle retirement,
priority, startup lock and spacing. Native instances count toward the existing
runner ceiling throughout population, execution and cleanup. Reserved staging
slots remain VMs.

There is no workspace reservation ledger or additional compiler admission rule.
Lean/Lake, compiler admission criteria and the pressure protocol are unchanged.
An inherited pressure stream keeps host sockets outside the job. A read-only
cgroup-counter view under the existing `/usr` sandbox mount supplies the existing
absolute local OOM guard without exposing writable host cgroups. The existing startup check
uses a 52 GiB initial-headroom threshold (32 GiB expected complete-job footprint
plus the existing 20 GiB compiler headroom). Autoscaling includes unrealized
native growth once when considering additional runners, separately from measured
RAM. It never deducts that forecast from compiler credits for active jobs.
Insufficient startup headroom, a missing image, another owned native instance or
a native failure cooldown selects VM before registration, without a native wait.

## Prepare and activate

Use the current supported Fleet release on the host. Preserve the host's current
worker revision, admission settings, total CI ceiling and staging reservation
when deploying a canary; a Fleet-only rollout must not accidentally replace an
independently deployed worker runtime with an older release-manifest pin.
`update --fleet-only` leaves the worker binary and revision untouched, including
on rollback while another operator updates them independently.

```sh
tauceti-fleet update tauchonki --fleet-only --json
tauceti-fleet native-runner tauchonki --prepare --json
tauceti-fleet native-runner tauchonki --json
tauceti-fleet native-runner tauchonki --slot 3 --json
```

Choose an existing general PR slot from the current host inventory; `3` is an
example. Selection affects its next instance at normal turnover. Healthy existing
VMs keep running. No drain or global admissions change is needed. Online native
runners that receive no job for five minutes enter the ordinary deregistration
path, which checks GitHub ownership before stopping the unit.

Preparation verifies the existing prepared VM image and exports its Ubuntu OS
through a short-lived, unregistered 2-CPU/2-GiB guest with a 3-GiB scope cap. It
records the export content identity, backing-image SHA and Actions runner version.
No host root or host sysctl changes are used. The host needs unprivileged user,
mount and PID namespaces with subordinate UID/GID mappings and
`vm.max_map_count >= 1048576`; hosts without these should continue using VMs.

Each native job gets a private OS clone and a systemd cgroup with 54 GiB
`MemoryMax`, zero swap and normal PR CPU priority. A 4 GiB hard per-file limit and monitored 24 GiB OS-disk budget bound ordinary
trusted setup writes; the monitor also stops a job below 8 GiB free disk. This is
not a filesystem quota. A private 32 GiB tmpfs is
mounted at the actual Actions runner `_work` before checkout or restoration.
This includes `_actions`, `_temp`, checkout, downloaded caches and generated
outputs. Toolchains live on the disposable OS disk. The trusted workflow retains
its offline candidate bwrap sandbox, pinned tool installation and stock compiler.
The runner root has no host home, credentials or host cgroups mounted.
Trusted setup has root inside the disposable user namespace and shares host
networking. Candidate computation keeps the trusted workflow’s offline bwrap
network boundary. This canary deliberately provides less isolation for trusted
setup than the VM backend.

## Observe and recover

`native-runner HOST --json` reports the selected slot, image, last backend-choice
reason, measured instance RAM, expected footprint, startup forecast growth and
workspace allocation/quota. Ordinary host status retains native runtime identity
in its existing `ci_vms` collection; the collection is shared by both backends.

The ordinary instance directory contains `native.json`, `native-progress.json`,
`native-samples.jsonl`, `native-result.json`, durable hook events and runner output.
Completion is recorded before optional telemetry copying. Controller restart
recovers the run ID from hook history before retiring an exited unit. Native
samples include cgroup RAM/peak/events, CPU, I/O and workspace/phase progress.
OOM and quota exhaustion are infrastructure failures. The pressure receiver restarts after a transient failure; a failed receiver
supervisor fails the instance instead of leaving compiler waiters indefinitely blocked.
A native infrastructure failure triggers a 15-minute native-only cooldown; normal
VM capacity remains available through the existing start and recovery paths.

Retirement stops the whole unit first, preserves bounded diagnostics under
`STATE/native-results/INSTANCE` (the latest 50 instances), removes the mapped-UID OS clone asynchronously, and only then
releases ordinary slot ownership. Failed cleanup retains ownership for retry.
Useful job conclusions and status semantics remain owned by the existing
workflow/GitHub result collector.

Rollback affects only future native admissions:

```sh
tauceti-fleet native-runner tauchonki --off --json
```

Let healthy work finish. Stop only an actual failed or unsafe instance and use
ordinary job/registration recovery. Verify both backend and worker revisions,
continued useful completions and any interrupted/stranded work after handover.

## Validation and expansion

Run the targeted contracts with the ordinary test suite. The optional real
namespace harness does not register with GitHub or post statuses:

```sh
python3 -m unittest discover -s tests
python3 tests/integration/native_runner.py tests/integration/native-smoke.sh
python3 tests/integration/native_runner.py SCRIPT --cancel-after 5
```

`native-frozen-ci.sh` repeats the experiment's frozen H/B/tree and complete
computational checks, including cold toolchain/cache setup and the actual Actions
workspace layout. Supply `--input DIR` containing a `source.bundle` with the
`paired-head` and `paired-base` refs. This fixture is test-only; production uses
its assigned workflow inputs. The harness records private evidence and checks
that the unit is unpopulated and the disposable filesystem is gone after cleanup.

Keep the live native ceiling at one until at least ten useful native jobs across
three PRs complete, including cold setup and varied artifact footprints, with no
OOM, leaks, stranded ownership or pressure deadlocks. Compare actual tauchonki
workload identities and phase timing with contemporary VMs. The earlier serial
chungus2 results are feasibility evidence, not a production latency promise.
Before expansion, require two full native jobs to execute concurrently under the
existing count/capacity controls and demonstrate combined headroom and useful
completions. A queued second job is not a concurrency pass. Expand one slot at a
time only after that evidence; no additional capacity ledger is a prerequisite.
