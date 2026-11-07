# Native PR runner canary on tauchonki

The backend from [Fleet PR #136](https://github.com/TauCetiProject/TauCetiFleet/pull/136)
(`b360de9`) and opportunistic selection from
[PR #138](https://github.com/TauCetiProject/TauCetiFleet/pull/138) (`72344a1`)
were merged and deployed live on 7 November 2026. The running Fleet release is
`ec413eee517ab1d46c5305db4c880a776a8b7ad4ebf2010a8b398a1d15c4fa6d`.
The initial backend deployment used release
`96f987f04969167212f18356ba6ab543f2f523181911d054c6aab62be9f1589d`.
The Fleet-only update preserved worker revision
`f1bbc941696791028fdc523df5cb4e8a10ac25b6` and left CI/worker admissions enabled.
No fleet drain or healthy-job interruption was used. An existing VM job completed
successfully across handover, with normal replacement continuing.

At approximately 03:40 UTC, general PR slot 3 was initially selected for native
at its next normal turnover. At 04:16 UTC, after the fallbacks below, the selection
changed to `next-general`: the single canary may use any suitable general-slot
turnover. Total CI ceiling remains four, staging reservation remains zero,
and jobs remain PR-only. The initial native ceiling is one. The 32 GiB workspace,
54 GiB whole-job cap and zero swap are bounds, not a reservation ledger.

The native OS image is
`c58f3a69e9fa41e0d2f62dbbc4f852f0c5997f772c608ed060b0aad23e3ab721`, derived from
prepared VM image `2f469b19af5efa27572e0ade3dc524aeb7fc905d3e06cd43a9fbfe75e8b5e619`
with Actions runner 2.338.2. A start below the native footprint estimate uses VM
before registration. Normal compiler admission criteria and pressure semantics
remain unchanged.

Rollback changes only future native admissions:

```sh
tauceti-fleet native-runner tauchonki --off --json
```

Let healthy work finish. Keep the native ceiling at one until ten useful native
completions across three PRs and separate concurrent-job validation justify
expansion. Offline tests and unregistered replays do not count as useful live
canary jobs. See [offline validation](native-runner-validation-20261107.md).

## Observation

At 03:52:01 UTC, the first selected-slot turnover found 27,769,438,208 bytes
(25.86 GiB) of available memory, below the existing startup check's native input
of 52 GiB. It selected VM before registration and started replacement instance
`efad857177fb07ed` (runner `125104`). No partial native workspace or native
registration was created. At 03:53 UTC the next-instance selector was moved to slot 0, the oldest
remaining VM job, to try at the next likely turnover. The one-native ceiling,
existing jobs and startup estimate were unchanged.
The preceding VM's workflow had been cancelled; this was not a native result.

The live result collector recorded VM run
[42333818431](https://github.com/TauCetiProject/TauCeti/actions/runs/42333818431)
finishing successfully after the live update, with 1,198 seconds run time and
3 seconds initial queue wait. Its slot was normally replaced. This validates
continuity of existing VM work, not native production performance.

At 03:59:26 UTC, slot 0 also chose VM with 34,546,229,248 bytes (32.17 GiB)
available. Slots 0 and 1's previous jobs both completed successfully. Although
available memory had briefly reached about 52.2 GiB during their retirement,
slot 2's VM grew to approximately 40 GiB before the normal startup spacing
allowed another launch. At approximately 04:00 UTC the selector was moved to
slot 2, so its normal retirement could release that footprint. No job was
interrupted and the threshold was unchanged.

At 04:04:54 UTC, slot 2 chose VM with 38,098,534,400 bytes (35.48 GiB)
available after its previous job completed successfully. There was no native
registration or workspace allocation. These three fallbacks expose a live
rollout constraint: a selected slot can miss a short memory window in a different
slot, and all selected startups so far have been below 52 GiB.

[PR #138](https://github.com/TauCetiProject/TauCetiFleet/pull/138) supplies the deployed optional
`--next-general` selection. Each general slot can use its ordinary startup decision
for the canary, while the existing global startup lock and owned receipts keep
native concurrency at one, including cleanup. Reserved staging slots, memory
threshold, total ceiling, startup spacing and compiler admission are unchanged.

The selector update passed 395 unit tests, compileall, targeted Ruff, both CI
Python versions and broker checks. Mixed-class idle-pool spares without reserved
staging capacity and existing non-PR registration intents fall back to VM to
preserve routing classes. Its Fleet-only handover preserved the same four running
VM instances and the independent worker runtime. The running controller reports
the new release, fresh status and no error.

Before any native production job, ancestor monitoring started at 04:01:41 UTC.
Baseline `memory.events:oom_kill` was 65 in the CI slice and 200 in the Fleet
slice and its ancestors. These are historical counts. No increase had appeared
in the observation window at the time this record was written.

Useful existing VM jobs observed after deployment:

| Run | PR | Conclusion | Runtime | Initial wait |
| --- | --- | --- | --- | --- |
| [42333818431](https://github.com/TauCetiProject/TauCeti/actions/runs/42333818431) | — | success | 1,198 s | 3 s |
| [42334274044](https://github.com/TauCetiProject/TauCeti/actions/runs/42334274044) | 30004 | success | 1,622 s | 2 s |
| [42334429132](https://github.com/TauCetiProject/TauCeti/actions/runs/42334429132) | 31387 | success | 1,513 s | 3 s |
| [42335481964](https://github.com/TauCetiProject/TauCeti/actions/runs/42335481964) | 31405 | success | 1,014 s | 2 s |

These are different workloads under varying shared-host load; they establish
continuity and provide context, not a controlled speed comparison.

Observation remains in progress. There have been **zero native production jobs**
in this observation period so far. Native admission, actual GitHub job
conclusions, footprint, Actions scratch and resource release must be recorded
before claiming the ten-job threshold or expanding.
