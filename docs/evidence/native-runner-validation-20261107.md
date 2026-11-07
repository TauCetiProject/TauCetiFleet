# Native runner validation, 7 November 2026

The optional native backend was validated unregistered on chungus2 before its
one-slot tauchonki canary. These tests post no GitHub statuses. The fixture pins
PR head `50604014370dfc4f4370dcb5fa0840e9b11c7177`, base
`afa08a3ed16fb167e35de371afb1eb03fbc5913a`, integrated tree
`fdcc264d54a81b592314cf22ec2e6ff9c29f74ec`, and stock Lean 4.36.0-rc2
(`b35cb10cf2ced51a2fd1880cfdbf75c71ab7a35a`). Lake configuration is attested
before executing the trusted cache tool; candidate computation stays offline
under the pinned bwrap boundary.

## Complete computational checks and lifetime

| Replay | Required checks | Peak charged RAM | Peak workspace | Wall time |
| --- | --- | --- | --- | --- |
| Cold preparation and body | All passed; same 165 TauCeti modules rebuilt | 30.60 GiB | 25.48 GiB | 20m14s total; 17m12s body |
| Final backend with restored fixture | All passed; normal supervisor exit and cleanup | 29.44 GiB | 25.48 GiB | 7m47s total; 4m49s body |

The first cold replay preceded moving OS cloning into the job unit; the final
replay includes that operation inside the capped unit. The restored fixture
contains warmed build outputs, so these rows are validation cases, not a timing
comparison. Other work remained active on chungus2, and real host RAM waits were
observed. Neither row establishes a tauchonki performance guarantee.

The final replay recorded zero `memory.events` max/OOM/OOM-kill events, completed
all phases through declaration-name checks, and released the complete instance.
Its exact peak was 31,614,107,648 bytes. Resource completion is durable before
optional diagnostics and filesystem teardown.

## Backing and cancellation

Mature process maps from the supported backend show:

| Process | Project/dependency artifact files | Artifact device | Toolchain device |
| --- | --- | --- | --- |
| audit-driver | 169,018 | private workspace tmpfs (`00:3c`) | host XFS (`09:7f`) |
| lint-env-driver | 169,017 | private workspace tmpfs (`00:3c`) | host XFS (`09:7f`) |

Every captured artifact mapping used the workspace device. Toolchain mappings
remained on disk. Mapping residency overlaps workspace pages; it is not another
independent allocation to add to charged RAM.

Cancellation passed during population, while the existing compiler gate waited
for host telemetry, and during full lint execution. A targeted lint cancellation
waited for the mature mapping record before stopping the unit. Each test checked
that the cgroup became unpopulated and the instance directory disappeared.
The gate-wait test used the real wrapper with a stand-in compiler and verified
that execution did not begin without host capacity information.

## Bounds, transport and real-host readiness

A reduced 64 MiB workspace test reported ENOSPC as infrastructure and cleaned up.
A separate 96 MiB cgroup test let namespace root mount its visible cgroup tree and
try to lift its limit, then forced an allocation. The capped ancestor remained
inaccessible: systemd recorded `Result=oom-kill`, `ExecMainStatus=9`, and an exact
100,663,296-byte peak. Cleanup released the instance. No host root was used.

Real namespace tests cover the actual Actions `_work`, `_actions` and `_temp`
layout, offline candidate isolation, normal host pressure discovery, receiver
restart, publisher reconnect, expected pressure-stream shutdown, and the existing
absolute OOM guard under production-style nested bwrap arguments. The sandbox
sees the 54 GiB counter view without access to writable host cgroups.

The same unregistered smoke passed on tauchonki with its prepared Actions runner
2.338.2 image, while four VM jobs and normal worker admissions stayed enabled.
It peaked at 379,027,456 bytes and released its complete instance. The real
Actions job lifecycle remains part of the bounded live canary; offline scripts
replace the listener and cannot count toward its ten useful-job requirement.

There are 389 passing unit tests. GitHub's Python 3.11/3.13 and broker checks pass;
final source changes are checked again on the PR. Compiler admission criteria,
pressure-frame semantics and stock Lean/Lake are preserved. The only helper
extension supplies the existing local guard with native cgroup counters under
the sandbox's existing read-only `/usr` mount.

## Canary boundary

Enable at most one general PR slot at normal turnover. Keep the current worker
revision, total CI ceiling and staging reservation. A native start that does not
fit returns to VM before registration. Observe ten useful native jobs across
three PRs before considering expansion. Two-native concurrent execution and its
combined footprint must be validated separately before lifting the initial
single-instance ceiling. No capacity ledger or additional compiler queue is
introduced.
