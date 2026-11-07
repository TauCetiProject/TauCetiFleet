# Native runner production trial, 7 November 2026

## Matched follow-up

The later [matched-input replay comparison](../../experiments/native-vm-matched-20261107/README.md)
qualifies the different-PR timings below. It confirms lower native CPU/disk-read
cost, but does not establish a reliable elapsed-time or capacity win: native
passes experienced RAM-gate waits, one was stopped after a ten-minute stall, and
much of the raw peak-RAM difference is reclaimable host file cache. Keep those
results separate from this successful Actions compatibility trial.

## Result

The supported native backend completed a real GitHub Actions PR job successfully
on tauchonki, including normal setup, offline candidate computation, required
status publication, ephemeral deregistration and verified cleanup.
[Run 42344835665](https://github.com/TauCetiProject/TauCeti/actions/runs/42344835665)
for [PR 31161](https://github.com/TauCetiProject/TauCeti/pull/31161), attempt 1,
ran on native runner `125334`, instance `fc4ac1a10c8a0c4a` in slot 0.
GitHub and the Fleet result collector both recorded success. The head's `build`,
`bump-guard` and `scope` statuses all link to this successful run.

| Measurement | Native production job |
| --- | --- |
| Actions job | 05:57:11–06:02:36 UTC: **5m25s** |
| Full offline build/audit/lint step | **1m40s**, including its setup |
| Inner computation phase timestamps | **70.57 s**, build through declaration names |
| Native supervisor lifetime | **6m04s**, including registration wait and shutdown |
| Exact charged RAM peak | **32,941,740,032 bytes = 30.68 GiB** |
| Sampled workspace peak | **28,196,089,856 bytes = 26.26 GiB** |
| Workspace quota remaining at sampled peak | **17.94%** of 32 GiB |
| Sampled `_actions` peak | **64.11 MiB** |
| Sampled `_temp` peak | **432.21 MiB** |
| CPU time | **1,001.89 CPU seconds** across the job unit |
| Sampled memory-time integral | Approximately **5,049 GiB-seconds** |
| Native memory max/OOM/OOM-kill events | **0 / 0 / 0** |
| Native swap | Disabled by `MemorySwapMax=0` |

The native instance started at 05:56:36 UTC with about 62.47 GiB available,
passing the unchanged 52 GiB startup estimate. The guard then disabled future
native selection and restored the trial ceiling to two. A VM companion,
[run 42344900084](https://github.com/TauCetiProject/TauCeti/actions/runs/42344900084),
ran concurrently. Workers remained enabled. The native job started from a fresh
disposable OS/workspace, downloaded stock Lean 4.36.0-rc2, and used the workflow's
normal network artifact/cache restoration. No prepared dependency image was
used by the native runner.

## Workload identity and limits of the comparison

- Head H: `199d0bc9ec5e1310b716912094474a60767b0ca5`.
- Captured base B: `686d0d7f4390d0e8d6185114e99e90b74c599656`.
- Tested integration commit T: `4800bef9e34b03b5df27990c6081d810641742ff`.
- Tested tree: `0aa6e4f98952fbbe10847ff435de644e8c626dce`.
- Stock Lean 4.36.0-rc2: `b35cb10cf2ced51a2fd1880cfdbf75c71ab7a35a`.
- Native OS image: `c58f3a69e9fa41e0d2f62dbbc4f852f0c5997f772c608ed060b0aad23e3ab721`.
- Native Fleet release: `2b73f05677bf4f77b33decc4eb46df25183c8eca1cf009f0e361af00d770e7ff`.

The ordinary workflow rebuilt two changed TauCeti modules. Environment lint and
axiom auditing used their normal two-module scope; duplicate-declaration checks
covered 27,169 TauCeti modules and 9,203 dependency modules. Other project outputs
were restored from the normal public cache. This is a valid useful PR job, but
it is not the frozen 165-module experimental workload and not a whole-library
lint case. The companion VM has a different PR/workload; do not infer a backend
speedup factor from its timing. This trial establishes production feasibility,
real Actions scratch sizing for this workload, and successful cleanup. It does
not establish a universal footprint, fleet throughput or an expanded native
concurrency limit.

## One focused repair

The first native launch, `5b40c57fd84770e2`, exited before accepting a job at
05:41:18 UTC. The stock listener reported signal 153 (file-size limit exceeded)
at a 297.71 MiB memory peak, with no OOM events. The 4 GiB `LimitFSIZE` also
limited .NET's anonymous W^X code-mapping file. The instance was deregistered
and cleaned up; no PR job or math status was stranded.

[PR #139](https://github.com/TauCetiProject/TauCetiFleet/pull/139) removes that
incompatible limit with `LimitFSIZE=infinity`. The cgroup RAM bound, zero swap,
workspace quota, disabled core dumps and monitored OS-disk bounds remain.
The old properties reproduce signal 153 in an unregistered namespace test;
the fixed properties start the stock 2.338.2 listener successfully. The
invalid-JIT integration test now requires the intentional managed parse error,
so an earlier bootstrap crash cannot count as a pass. The unit suite ran 423
tests with one skipped; both Python CI jobs and the broker checks passed.

That repair was merged as `39ff3a0` and deployed Fleet-only, preserving the
independent worker runtime `f1bbc941696791028fdc523df5cb4e8a10ac25b6` and existing
jobs. Only the failed instance's verified cooldown was cleared before retry.
No Lean/Lake patch or compiler admission change was made.

## Lifetime and trial operations

At 05:17 UTC the pre-existing operator capacity controller was stopped temporarily,
with no pending recovery transaction. Its normal four-runner setting otherwise
would undo the trial ceiling and could cancel work for memory recovery.
The temporary ceiling was two, then one for the native-first handover, then two
immediately after native launch. Existing VM jobs finished normally; no job was
cancelled to create headroom. CI and worker admissions remained enabled and
`draining=false` throughout. The transient one-runner phase allowed both remaining
VM jobs to finish; it was used specifically to obtain the memory needed for this
bounded native trial, not as a routine deployment precaution.

The successful listener removed `.credentials` and `.runner`, reported success,
and exited zero without an infrastructure error. At 06:03 UTC the host showed:

- native service inactive/dead, `MainPID=0`, with no remaining service cgroup;
- native directory, slot receipt and host mounts released;
- no native infrastructure cooldown;
- a durable result, completed hook, samples, phase timings and bounded runner
  diagnostics archived under `native-results/fc4ac1a10c8a0c4a`;
- the normal result collector recorded `runtime=native-tmpfs`, conclusion success,
  325 seconds run time and 2 seconds initial queue wait.

Native samples include zero max/OOM/OOM-kill events, and ancestor monitoring
recorded no increase from its baseline of 65 CI-slice and 200 Fleet-slice OOM
kills during the native job. The baseline counts are historical, not trial
failures. Resource samples are every five seconds (native) and ten seconds
(host); workspace and scratch peaks are sampled, while cgroup peak is exact.

## Decision

The backend has now served one useful production job within its initial bounds.
Continue evaluation rather than abandon it. Expansion remains disabled: the
trial is one success for one PR, below ten useful successes across three PRs,
and two-native contention has not been validated. A bounded follow-on should
keep one native plus one VM, gather varied workloads and compare matching inputs
before making throughput or speedup claims. It should not reintroduce a capacity
ledger or change the running compiler gate.

## Companion and restoration

The companion [VM run 42344900084](https://github.com/TauCetiProject/TauCeti/actions/runs/42344900084)
for [PR 30254](https://github.com/TauCetiProject/TauCeti/pull/30254) completed
successfully from 05:58:15 to 06:09:23 UTC: **11m08s** total. Its full offline
build/audit/lint step took **4m33s**, and its observed systemd `MemoryPeak` was
37,903,515,648 bytes (**35.30 GiB**). It rebuilt one changed TauCeti module and
used the ordinary one-module lint/axiom scope. Its captured base was the same
`686d0d7f4390d0e8d6185114e99e90b74c599656`; its head was
`4c45f18ec55415346e451df3b8adbed76a361afc` and tested tree was
`a5fb217a6ba3df7cbf2ac7eb9b982a92825cd127`.

| Observed real PR job | Native | VM companion |
| --- | --- | --- |
| Conclusion | success | success |
| Actions job time | 5m25s | 11m08s |
| Full offline step | 1m40s | 4m33s |
| Charged RAM peak | 30.68 GiB | 35.30 GiB |
| Changed TauCeti modules rebuilt | 2 | 1 |

These observations are encouraging, but the different source changes, job start
times, artifact/cache behavior and shared-host load prevent a causal backend
speedup or memory-saving claim from this pair alone.

At **06:10:37 UTC**, after observing the companion's successful completion:

- `ci_max_jobs` was restored to **4** and the pre-existing operator controller
  `tauceti-ci-autoscale.service` was restarted with its original 4/4 bounds;
- future native selection remained **off**, so this trial cannot silently turn
  into additional native production jobs;
- CI and worker admissions remained enabled, staging reservation stayed zero,
  worker runtime stayed at the independent revision, and no drain was used;
- the Fleet-only listener repair remained deployed for a subsequent canary.

The native result is one useful success for one PR. This trial did not authorize
or perform expansion. Trial-specific guards have exited, and the unregistered
validation instances were fully released. Small local evidence contains the
native receipt/result/samples, workflow/job/status JSON, official Actions logs,
cleanup proof, temporary-setting receipts, and restoration receipt.

