# Validation before activation

Validated on 2026-10-15 under the existing unprivileged `kim` account on the OVH
host. No host sudo, Docker/Incus enrollment, model calls, production runner
registration, or production routing changes were used.

## Local behavior

- 27 controller tests cover inactive installation, preserved identity/permissions,
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
were denied. The runner binary and pinned gh 2.102.0 executed, along with git, jq,
sudo/dpkg and the AppArmor tools. A failed cloud-init prerequisite prevents subsequent success markers; the
regression test executes that failure path. No GitHub runner was registered.

Prepared base SHA-256:
`9a62c95749f36af3c1445c3475fef3e4ae5da2ea2c87ca93877c4b273a6f6fe0`.

## Companion changes

[TauCetiWorker #270](https://github.com/TauCetiProject/TauCetiWorker/pull/270)
adds opt-in admission and provenance. Its six GitHub CI jobs passed. Targeted
local validation also covered the existing 49 API-budget tests, worker manager,
publication outbox, and SIGTERM handling using fake model receipts.

[TauCeti #16862](https://github.com/TauCetiProject/TauCeti/pull/16862) adds the
existing picker's dedicated-pool preference, bounded same-run Namespace recovery,
and attempt-fenced reporting. Local validation passed fleet tests (14), existing
picker tests (22), batch finalizer tests (20), staging/train identity tests (11),
and actionlint on changed workflows. The cancellation tests cover force-cancel and same-run retry, terminal failure on
failed handoff, lost rerun responses, runner-acquisition races, unknown labels, delayed dependency completion, transient read failures,
incomplete API observations, and second attempts always using Namespace.

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

## Verified cancellation classification (#17125)

Fleet exempts a cancellation from its 900-second circuit only when **all** of
these host/GitHub facts agree:

- The disposable QEMU service exited successfully. New units retain their
  systemd result with `RemainAfterExit=yes` until retirement; a failed/missing
  read, older pending record, or previously collected unit is unknown.
- GitHub binds the registered runner's completed `sandboxed-build` to attempt 1
  of the Bors bot's `repository_dispatch` of main's `pr-build.yml`. Run name,
  base SHA, candidate SHA and batch are checked together. Reruns stay conservative.
- Setup and trusted-tooling checkout succeeded, the sandboxed build was cancelled,
  and `Complete job` succeeded. No setup/host/timeout or unknown failed step is
  excused. Candidate source compilation failure remains a candidate result.
- The latest Bors-bot `bors` commit status on that candidate is `Canceled`, links
  to the exact batch, and was created during the sandboxed build before it ended.
  This status represents coordinator withdrawal/supersession, not runner failure.
  A cancelled conclusion or absence of failed steps alone grants no exemption.
- The trusted report step intentionally exits 1 when its build was cancelled.
  That specific failure is allowed only with both successful `scope` and
  `bump-guard` GitHub status writes, tied to this run and reporting interval.
  Missing, stale or failed writes retain cooldown.

Metadata comes from bounded GitHub API reads using the existing host read
credential. No logs, guest hook claims, candidate artifacts or new credentials
establish health. Unknown/changed workflow shapes retain cooldown. Classification
is advisory for host capacity only; it never grants candidate approval or cache
publication. A healthy result does not clear another job's cooldown.

`ci_result` events include runner/run/job identity, a fixed classification reason,
and bounded evidence (candidate SHA, batch, status ID/time, attempt, host service
health). Cooldown expiry is anchored to job completion plus 900 seconds; replayed
collection cannot renew it, shorten a newer fault's cooldown, or duplicate its
result event. Historical result events are not reclassified on deployment.

`ci_vm_retired` and `ci_runner_online` record the previous runner ID and elapsed
retirement-to-first-observed-online time. Online observation uses the existing
30-second inventory refresh; this is an upper bound at that observation, not an
exact listener timestamp. Failed API reads/controller delays can widen it. No
extra production watcher is introduced, and the capacity/guest isolation is
unchanged. This corrects the cooldown after an eligible cancellation; it cannot
move a picker decision made while the previous guest was still busy.

Local validation: 41 unit tests, Ruff, compileall and wheel/sdist build pass.
A local disposable user-service probe confirmed the retained successful exit is
reported `(False, 'healthy')`; it registered no runner and was removed. Production
acceptance remains pending required review, merged deployment identity, and the
next naturally occurring eligible cancellation/replacement. Do not manufacture
one or launch a hardware benchmark. Existing root-owned staging watch supplies
production observations; genuine fault cooldown is verified live only if a fault
occurs naturally.
