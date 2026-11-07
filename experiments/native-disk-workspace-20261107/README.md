# Disk-backed native workspace experiment, chungus2, 7 November 2026

Predeclared protocol: four serial fresh native instances in disk-1, tmpfs-1,
tmpfs-2, disk-2 order, replaying the sealed fixture from the
[matched native/VM experiment](../native-vm-matched-20261107/README.md).
No production changes, runner registration, compiler changes or new compiler
admission conditions. Same 54 GiB cgroup cap, zero job swap, CPU weight 100 and
40 GiB offline start threshold. Other host work stays enabled.

The runtime copy in task state differs in exactly two places: disk instances
skip mounting tmpfs over the workspace, and the existing root-filesystem usage
watchdog permits the original 24 GiB root budget plus 32 GiB for that workspace.
The fixed trusted replay uses no per-directory disk quota; disk headroom and
whole-unit RAM remain bounded. Production package files are untouched.

The timed computational script, fixture and exhaustive input manifest are the
same as before. Storage identity is recorded before timing. No explicit workspace
cache flush is performed before a body. After the timed job and hook complete,
the job waits while the harness requests `32G swappiness=0` reclaim in only that
experimental cgroup. Before/after counters, any EAGAIN/error and the request's
elapsed time are recorded. This probe is outside timing and is a diagnostic,
not a production mechanism or a substitute for a concurrency measurement.

Stop and mark an attempt censored after 600 seconds without phase progress when
the existing compiler RAM gate is confirmed stalled. Retain every attempted
result, including failures and stalls; do not select only favorable runs.

Results pending. The VM passes from the preceding experiment are historical
references, not contemporaneous controls for this workspace comparison.
