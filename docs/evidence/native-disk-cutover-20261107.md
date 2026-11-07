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

- 437 unit tests pass, one optional test skipped. New tests cover disk scan
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
  cleaned up. [Recorded checks](../../experiments/native-disk-implementation-20261107/results/integration-checks.json)
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

## Live validation still required

Production deployment and validation have not been performed at this commit.
Complete the plan's live identity/ownership preflight, fixed-slot canary, useful
Actions completion and cleanup, two full concurrent native completions, then
expand at normal turnover. Record deployed revisions, jobs, memory, disk, operator
settings and repair of any interrupted work here. Keep worker/CI admissions and
total capacity unchanged throughout.
