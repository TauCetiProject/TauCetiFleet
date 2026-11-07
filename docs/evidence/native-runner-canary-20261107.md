# Native PR runner canary on tauchonki

The backend from [Fleet PR #136](https://github.com/TauCetiProject/TauCetiFleet/pull/136)
was merged as `b360de9` and deployed live on 7 November 2026. The running Fleet
release is `96f987f04969167212f18356ba6ab543f2f523181911d054c6aab62be9f1589d`.
The Fleet-only update preserved worker revision
`f1bbc941696791028fdc523df5cb4e8a10ac25b6` and left CI/worker admissions enabled.
No fleet drain or healthy-job interruption was used. An existing VM job completed
successfully across handover, with normal replacement continuing.

At approximately 03:40 UTC, general PR slot 3 was selected for native at its next
normal turnover. Total CI ceiling remains four, staging reservation remains zero,
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

Observation remains in progress. There have been **zero native production jobs**
in this observation period so far. Native admission, actual GitHub job
conclusions, footprint, Actions scratch and resource release must be recorded
before claiming the ten-job threshold or expanding.
