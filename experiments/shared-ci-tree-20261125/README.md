# Shared CI build tree: phase 0 measurement

Phase 0 of TauCetiWorker decision 211 (a read-only per-host `.lake` with a private
overlay per job). Nothing here changes production; the Fleet change in the same PR only
adds telemetry (`io`, `files` and `io_after_removal` in `native-result.json`, and
`drive_io` in the controller's status).

## Drift between published `main` maps

`revision_map_drift.py` compares each published TauCeti Lake revision map with the one
LAG publications earlier. It reads the public cache anonymously and writes only a local
download cache.

```sh
git clone --filter=blob:none --no-checkout https://github.com/TauCetiProject/TauCeti /tmp/tauceti
python3 revision_map_drift.py /tmp/tauceti --hours 26
```

Result on 25 Nov 2026 (1,248 first-parent commits, 15 published maps over 17.2 hours,
one map about every 1.2 hours, 41,325 modules):

| Shared tree age | Median age | Input hash changed (median, max) | Output archive not in the older tree (median, max) |
| --- | ---: | ---: | ---: |
| 1 publication | 1.2 h | 27.4%, 35.8% | 1.0%, 1.5% |
| 2 publications | 2.4 h | 41.1%, 46.9% | 2.1%, 2.7% |
| 3 publications | 3.7 h | 48.9%, 53.3% | 3.0%, 3.9% |
| 4 publications | 4.9 h | 54.2%, 57.1% | 4.0%, 4.8% |

Two numbers matter. Only about 1% of output archives are new per publication, so the
bytes a job must fetch on top of a tree one publication old are small (about 0.4 GB of
the 34 GB). But a quarter to a third of modules get a new input hash every publication,
because one change near the root rehashes everything downstream. For each of those a job
on the newer base rewrites the trace and hash files and relinks the build outputs. On an
overlay, linking or rewriting a lower-layer file copies it up, so whether a job writes
about 1% or about 30% of the tree depends on whether Lake rewrites unchanged outputs in
place. Phase 2 measures that. The refresh interval should follow publications (about
every 1.2 hours), not a fixed clock.

## Per-job writes and drive latency on chungus5

From the existing five-second `io.stat` samples of seven archived PR builds on chungus5
(25 Nov 2026, 03:16 to 03:54 UTC), on the md array that holds the instance filesystem:
48.2 to 52.5 GB written in 1.50 to 1.60 million write requests per job, 76 to 137 GB
read, peak allocated disk 47 to 49 GB, over 20 to 30 minutes.

Ten one-minute `/proc/diskstats` samples on chungus5 (04:07 to 04:17 UTC, 25 Nov):
each NVMe drive (Micron 7450, md RAID 0) wrote 195 to 323 MiB/s at 13,500 to 18,100
writes/s, with a mean write latency of 4.9 to 12.0 ms (median about 7.5 ms). The array
wrote 390 to 645 MiB/s.
