# Native disk implementation and live cutover, 7 November 2026

The implementation replaces the native tmpfs workspace with one `native-disk`
backend. It deletes the workspace mount/quota, 52 GiB startup threshold, 32 GiB
workspace-growth forecasts, native-only failure cooldown and VM fallback override.
The existing compiler gate and pressure protocol are unchanged. Future native
starts use the ordinary PR startup memory check and existing total slot controls.

Disk is monitored at 56 GiB per instance, with 48 GiB startup free space and the
existing 8 GiB free-space stop threshold. These are monitored budgets, not hard
quotas. A bounded background `du` at most once per minute replaces repeated
blocking scans. Cheap free-space checks continue independently. Native raw RAM,
clean cache and adjusted memory remain distinct in status/autoscaling.

## Local validation

- The implementation passed 437 unit tests with one optional skip. After the
  independent dependency-prefill deletion, the current main tree passes all 407
  remaining tests ([final log](../../experiments/native-disk-implementation-20261107/results/final-unit-tests.log.gz)). New tests cover disk scan
  scheduling/timeouts/errors, nonblocking low-space handling, monitored-budget
  failures and end-to-end native cgroup observation in the autoscaler.
- Changed production modules, the new monitor tests and the validation harness
  pass Ruff; compilation, shell syntax and diff checks pass. Existing unrelated
  lint failures in the wider repository were not changed.
- Real namespace smoke passed, including disk mount identity, isolation, receiver
  restart and the unchanged absolute OOM guard. The stock .NET Listener version
  smoke passed; actual stock runner startup reached its expected invalid-JIT
  error without registration.
- Cancellation during population, compiler waiting (confirmed by the wrapper's
  wait message) and execution released all instances, cgroups and pressure links.
  A simulated disk resource error propagated as infrastructure failure and also
  cleaned up. Killing the pressure supervisor in another unregistered instance
  terminated its job with exit 1 in four seconds and released its cgroup/root. [Recorded checks](../../experiments/native-disk-implementation-20261107/results/integration-checks.json)
  include exact instance/unit identities and raw logs.
- The supported backend passed the full sealed replay: body 106.75 seconds,
  sampled CPU 100.48 seconds, host reads 13.59 GiB, mean charge after clean-file
  discount 1.94 GiB and exact whole-unit peak 25.59 GiB. No memory-max/OOM events.
  Every consumed input matches the preceding 914,541-entry manifest and tested
  tree; zero TauCeti modules rebuilt. This is a restored-output replay, not a cold
  project rebuild or a production speedup claim.
- Real disk measurements remained successful at roughly one-minute intervals.
  Source archive bytes and local Fleet configuration are unchanged; fixture
  staging and experimental instances are released.

[Raw evidence and replay harness](../../experiments/native-disk-implementation-20261107/)
retain identities, counters, phase logs, monitoring and cleanup proof. The earlier
[disk/tmpfs comparison](../../experiments/native-disk-workspace-20261107/README.md)
provides matched storage evidence.

## Live deployment and recovery

[Implementation PR #144](https://github.com/TauCetiProject/TauCetiFleet/pull/144)
merged as `5a52f42`; all PR/push checks passed after making the registration test's
free-disk fixture explicit. Production was updated through the supported live
handover to release
`dc59297f8295fb80a282d87db8db851699a49477cdbbd9210061dee4121c59fe`.
Active processes were preserved. Worker revision
`11d86829246b7c2489e0ab07318296fd35ec6c4f` and the prepared images stayed unchanged.

There were no owned experimental native instances or pending native results before
handover. Only stale experimental backend-choice metadata was removed. CI and
worker admissions remained enabled, draining remained false and total capacity
remained four. The operator autoscaler's immutable helper was updated to the same
package, preserving its existing minimum/maximum of four.

Before the canary could start, all four existing VMs were RAM-stalled: six waiters,
zero running compiler invocations, oldest wait over 30 minutes and approximately
13 GiB headroom. The largest stalled VM in slot 3 was recovered through the
existing durable cancel/retire/rerun transaction. The original
[run 42367407411](https://github.com/TauCetiProject/TauCeti/actions/runs/42367407411)
was resubmitted as attempt 2 and subsequently **succeeded** at 12:16 UTC. Other
jobs and admissions continued; the transaction finished with no pending recovery
or error. There was no fleet drain or scheduler-policy change.

The existing autoscaler independently recovered a second stalled VM in slot 0 at
12:07 UTC ([transaction evidence](../../experiments/native-disk-implementation-20261107/results/live/automatic-recovery.json)).
Its [run 42368117729](https://github.com/TauCetiProject/TauCeti/actions/runs/42368117729)
was verified resubmitted as attempt 2. The existing workflow router selected
GitHub-hosted `ubuntu-24.04` because its Namespace capacity cap was reached; that
job remains queued outside Fleet. Its Fleet instance and durable recovery
transaction are released. This queued hosted rerun is retained explicitly; it is
not counted as a successful native job.

The first fixed-slot disk-native instance is `8f6e4f00e4be0ce6`, runner 125997,
assigned [run 42370572248 / PR #31088](https://github.com/TauCetiProject/TauCeti/actions/runs/42370572248).
Its trusted tested identity was captured from the actual Actions temporary path:

- Head: `b54e3fbcb8fd56406df2de9aea04db869b23246e`.
- Base: `4edc95abb3249c13e594cb824b4097af0f6c18e0`.
- Tested integration: `1cb8430019c41980e5ae398d7e7814e13f12847e`.
- Tested tree: `3df45fea28f464b969c7af7c73b61c278de006f8`.

It started with 37.37 GiB measured headroom, below the removed 52 GiB requirement,
and the recorded startup requirement was the ordinary 20 GiB.

An independent cleanup release, [PR #145](https://github.com/TauCetiProject/TauCetiFleet/pull/145)
(`b82910c`), was deployed during the canary. Current controller release is
`d8585dc9633def831c6d9a82b1a77327569c68bf2215617bf90daef206d718ec`.
Its native implementation, memory accounting, autoscaler and compiler helpers
are byte-identical to the initial cutover release. Its unrelated removal of unused
dependency-prefill code was preserved. The canary continued through this live
controller handover and completed successfully.

## Completed production jobs

These are ordinary assigned PR jobs with their actual tested integration identities
retained. Each completed the build, audits and lints, published successful `build`,
`scope` and `bump-guard` statuses, exited zero without runner infrastructure or
memory-max/OOM errors, and released its unit, private root and ownership.

| Run | Workflow duration | Rebuilt TauCeti modules | Exact whole-unit RAM peak | Mean computational RAM: raw / minus clean file | Sampled allocated disk peak |
| --- | --- | --- | --- | --- | --- |
| [42370572248](https://github.com/TauCetiProject/TauCeti/actions/runs/42370572248) | 28m06s | 1,081 | 33.00 GiB | 11.67 / 5.60 GiB | 33.84 GiB |
| [42373270617](https://github.com/TauCetiProject/TauCeti/actions/runs/42373270617) | 13m44s | 6 | 19.10 GiB | 10.50 / 1.73 GiB | 31.94 GiB |
| [42374155492](https://github.com/TauCetiProject/TauCeti/actions/runs/42374155492) | 7m17s | 1 | 20.02 GiB | 15.60 / 1.64 GiB | 31.65 GiB |
| [42374671959](https://github.com/TauCetiProject/TauCeti/actions/runs/42374671959) | 14m55s | 2 | 22.54 GiB | 7.25 / 1.57 GiB | 31.91 GiB |
| [42375689577](https://github.com/TauCetiProject/TauCeti/actions/runs/42375689577) | 6m46s | 1 | 22.78 GiB | 16.87 / 1.43 GiB | 31.66 GiB |
| [42375807545](https://github.com/TauCetiProject/TauCeti/actions/runs/42375807545) | 7m42s | 3 | 19.46 GiB | 13.61 / 1.67 GiB | 32.36 GiB |

Whole-unit peaks include preparation and the Listener. Computational means cover
`build` through `end`, including audits/lints, using time-weighted cgroup samples.
The clean-file discount is `max(0, file - shmem - dirty - writeback)`; it is a proxy,
not a hard unreclaimable-memory measurement. Sampled shmem peaked below 0.051 GiB
in all six jobs: the former approximately 26 GiB resident workspace floor is gone.
Real compilation still needs RAM: the large rebuild reached a sampled adjusted
peak of 30.77 GiB. Never infer a universal 2 GiB job size from the offline replay.

Preparation to the computational `build` marker took 8m01s, 5m31s and 4m45s,
respectively. This includes Actions setup/cache retrieval, not just root copying.
Sampled computational CPU was 12,582, 419 and 259 CPU-seconds; MD-device reads
were 289.16, 175.12 and 54.00 GiB. These jobs have different trees, rebuild sets,
cache states and concurrent neighbours; their elapsed times do not establish a
speedup or capacity multiplier. I/O counts use the MD aggregate once, not its
component devices again.

The unchanged gate still recorded aggregate per-process compiler waits of
19,519, 41 and 18 seconds. The first figure sums waits across 1,081 rebuilt modules
and can exceed wall time. This deployment removes persistent workspace RAM and
its startup forecast; it does not eliminate compiler memory/CPU demand or all waits.

The first pair's sandboxed workflows really overlapped for 83 seconds (units for
128.85 seconds), with preparation in one while the other finished checks/upload.
Both subsequently completed and cleaned up. Their computational bodies did not
overlap. During overlapping unit samples their combined raw peak was 19.27 GiB,
adjusted peak 2.61 GiB and minimum host available RAM 24.26 GiB. This validates
concurrent lifecycle activity; it is weak evidence for simultaneous heavy builds
and supplies no throughput benchmark.

The watchdog produced roughly minute-spaced allocated-block measurements. During
the large rebuild one scan hit its 10-second deadline. Its allocation became
explicitly unknown, cheap free-space/progress checks continued, and the next scan
succeeded. None of the following three jobs had a scan failure. No space, inode or disk-budget
failure occurred. The observed 31.65–33.84 GiB disk peaks fit the initial 48 GiB
startup estimate with the 8 GiB host margin. The 56 GiB budget remains monitored,
not a hard quota.

A separate read-only 180-second `/proc` sample captured 11 existing `du` processes
across the expanding runners, using 14.50 CPU-seconds in total; individual captured
scans used 0.12–4.20 seconds. The most populated observed runner accounted for
7.70 seconds over three scans. This supports the reduction from repeated traversal
at two-second intervals, but is not a matched benchmark of the old 32.3-second
computational-body cost. Sampling is a lower bound: allow up to the 0.112-second
maximum poll gap plus two 10ms ticks per captured process, and very short scans
can be missed. No additional scan was launched. The retained first sampling
attempt missed the nested payload cgroup and is invalid for CPU measurement;
its empty process list must not be interpreted as zero cost.

## Expansion and final verification

After both initial pair results were green on GitHub and both roots/cgroups were
verified removed, existing `next-general` selection was enabled at 13:01 UTC.
The total ceiling stayed four, staging reservations stayed zero, CI/workers stayed
enabled and draining stayed false. Healthy VMs continue to completion; replacement
at ordinary turnover uses disk-native. The existing idle pool keeps occupied
runners and one warm spare, so four is a ceiling, not a promise of four idle runners.

By 13:14 UTC all four slots owned disk-native instances through ordinary turnover
([snapshot](../../experiments/native-disk-implementation-20261107/results/live/all-native-turnover.json)).
Expanded runs 42375689577 and 42375807545 both passed all checks and publication,
exited cleanly and released their roots/cgroups/ownership. Their units overlapped
for 375.71 seconds; their computational bodies overlapped for **41.01 seconds**.
During a 35.05-second sampled subinterval they consumed 155.82 CPU-seconds
combined. This is actual concurrent computation, not queued capacity. Over their
whole unit overlap the sampled combined RAM peak was **38.82 GiB raw / 6.46 GiB
after clean-file discount**, with at least **44.25 GiB host available**. Both
performed real small rebuilds (one and three TauCeti modules). This validates
concurrent useful work and cleanup, not sustained capacity for four large rebuilds.

Their preparation-to-build intervals were 301.54 and 295.46 seconds; computational
bodies took 96.75 and 157.14 seconds. Sampled body CPU was 146.59 and 199.41 seconds;
MD reads 33.37 and 55.64 GiB. Aggregate compiler wait was 15.44 and 20.33 seconds.
The second expanded job had one nonzero disk scan during preparation, reported
unknown usage, and succeeded on the following scan. Neither job hit a memory/disk
bound or infrastructure failure.

The validated cohort produced six successful native jobs from 12:01:03 through
13:19:22 UTC: 78m19s, or 4.60 validated completions/hour over this staged observation.
The initial single-slot restriction, rollout steps and different rebuild/cache
sets are included in that interval. It is a useful-work ledger, not a steady-state
throughput estimate or a matched VM comparison.

[Final verification at 13:22 UTC](../../experiments/native-disk-implementation-20261107/results/live/final-verification.json)
confirmed the running package's content hash and critical source hashes, unchanged
Worker/image revisions, four currently owned native instances, unchanged ceiling
and admissions, no controller/CI errors, no pending operator recovery, and six
completed validation instances with stopped cgroups, removed roots, archived
results and released ownership. None of those six remains in pending CI results.
The temporary observer and scan sampler have stopped, and the temporary remote
observer directory has been removed; their filtered evidence is retained here. Normal native CI traffic continues under existing Fleet controls.

[Filtered live evidence](../../experiments/native-disk-implementation-20261107/results/live/)
includes exact releases, deployment/recovery transactions, GitHub jobs/statuses,
trusted H/B/tested/tree identities, cgroup counters, phases and cleanup. The bounded
read-only observer captures records before private roots disappear. Reproduce the
per-instance means, phase timings and overlap analysis with
`python3 experiments/native-disk-implementation-20261107/analyze-live.py`.

The [completion audit](../../experiments/native-disk-implementation-20261107/results/live/completion-audit.json)
checks all six GitHub outcomes/publications, terminal counters and identities,
unchanged compiler helpers/controls, computational overlap, expansion and cleanup.
