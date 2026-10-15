# Validation before activation

Validated on 2026-10-15 under the existing unprivileged `kim` account on the OVH
host. No host sudo, Docker/Incus enrollment, model calls, production runner
registration, or production routing changes were used.

## Local behavior

- 24 controller tests cover inactive installation, preserved identity/permissions,
  explicit funding, admission scaling, memory pressure, drain/update behavior,
  SSH argument injection, incremental events, budget corrections/corruption,
  provenance deduplication, Bors landing evidence, incomplete reporting, pinned
  downloads, cloud-init encoding, and guest adoption after controller restart.
- Ruff, wheel/sdist builds, and deployment-archive creation from the installed
  wheel pass.
- The installed CLI successfully performs `add local`, `doctor`, `report`,
  `status`, and budget queries. Reinstallation preserves the machine ID and
  existing grant ledger. CI and worker activation remain false.
- Actual kernel cgroup values were checked, including CPU weight, memory high,
  memory maximum, and task limits. Merely accepting a systemd property is not
  treated as proof that it is enforced.
- Two sibling user services pinned to one logical CPU contended for three
  seconds. At weights 1000:10, measured CPU time was 2.9616 seconds for CI versus
  0.02807 seconds for workers (about 105:1).

## Disposable guests

User-local Nix supplied pinned QEMU and cloud-image utilities. KVM API 12 and VM
creation worked without root. A 2-vCPU / 2-GiB preparation guest produced a
checksum-verified base containing Actions runner v2.338.0.

Two fresh overlays passed execution and clean-replacement tests: a canary from
the first overlay was absent from the second, and the base digest was unchanged.
The guest smoke test uses the same AppArmor permission profile as the CI
workflow, then tests bubblewrap as an unprivileged guest user with explicit user
and network namespaces. Network access and writes to the read-only filesystem
were denied. The runner binary executed. No GitHub runner was registered.

Prepared base SHA-256:
`d65f535660656602f74bef352f2bcf35e99a4d2df76fd1b02f9eacfca2507beb`.

## Companion changes

[TauCetiWorker #270](https://github.com/TauCetiProject/TauCetiWorker/pull/270)
adds opt-in admission and provenance. Its six GitHub CI jobs passed. Targeted
local validation also covered the existing 49 API-budget tests, worker manager,
publication outbox, and SIGTERM handling using fake model receipts.

[TauCeti #16862](https://github.com/TauCetiProject/TauCeti/pull/16862) adds the
existing picker's dedicated-pool preference, bounded same-run Namespace recovery,
and attempt-fenced reporting. Local validation passed fleet tests (7), existing
picker tests (21), batch finalizer tests (20), staging/train identity tests (11),
and actionlint on changed workflows. The cancellation tests cover never-acquired
jobs remaining pending, runner-acquisition races, incomplete API observations,
and second attempts always using Namespace.

## Checks remaining for production

The initial rollout is intentionally inactive. Human review/merge and GitHub
runner-group configuration are still needed. On this account, `doctor` also
reports that logout/reboot persistence needs an administrator to run
`loginctl enable-linger kim`. This is the only privileged change currently
identified on this host. Other hosts may need KVM access or cgroup delegation;
their doctor output gives the specific request.

The default 32-vCPU / 64-GiB CI shape additionally needs available memory plus
host/OS headroom. This shared host currently has other workloads, so readiness
can block until memory is available or the operator selects a smaller shape.

Before production routing, explicitly enable a prepared host's CI role and run
the trusted `fleet-smoke.yml` while `FLEET_CI_ENABLED` remains off. Real GitHub
registration, acquisition, job completion, guest retirement, and a controlled
unacquired-job fallback must be checked at that point. Static tests and local
guests do not establish that live routing has passed.
