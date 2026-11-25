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
python3 revision_map_drift.py /tmp/tauceti --hours 26 [--artifact-sizes SIZES]
```

`SIZES` lists `NAME BYTES` per cached file, for example from `find .lake/cache/artifacts
-type f -printf '%f %s\n'` in a finished job; only `.ltar` rows are used, and archives it
lacks count as zero bytes.

Result on 25 Nov 2026: 1,248 first-parent commits, 15 published maps over 17.2 hours of
commit time (one per 1.2 hours of commit time; the script does not see publication
times), 41,325 modules. Byte figures use the archive sizes of one chungus5 job's cache,
which sized all but at most about 1,500 of each set. In that cache the unpacked
artifacts were 21 times the compressed archives (30.1 GB against 1.43 GB).

| Shared tree age | Median age | Input hash changed: rows (median, max), compressed bytes | Output archive not in the older tree: rows (median, max), compressed bytes |
| --- | ---: | ---: | ---: |
| 1 publication | 1.2 h | 27.4%, 35.8%; 0.43 GB | 1.0%, 1.5%; 0.017 GB |
| 2 publications | 2.4 h | 41.1%, 46.9%; 0.63 GB | 2.1%, 2.7%; 0.034 GB |
| 3 publications | 3.7 h | 48.9%, 53.3%; 0.75 GB | 3.0%, 3.9%; 0.049 GB |
| 4 publications | 4.9 h | 54.2%, 57.1%; 0.81 GB | 4.0%, 4.8%; 0.068 GB |

Two numbers matter. Few output archives are new per publication: at the 21 times ratio,
about 0.4 GB unpacked on top of a tree one publication old. But a quarter to a third of
modules get a new input hash every publication, because one change near the root
rehashes everything downstream; their archives unpack to about 9 GB. For each of those a
job on the newer base rewrites the trace and hash files and relinks the build outputs.
On an overlay, linking or rewriting a lower-layer file copies it up, so whether a job
writes about 0.4 GB or about 9 GB of restored outputs depends on whether Lake rewrites
unchanged outputs in place. Phase 2 measures that. Either way it is well below the 39 GB
restored today. The refresh should follow publications, not a fixed clock.

## Per-job writes and drive latency on chungus5

From the existing five-second `io.stat` samples of seven archived PR builds on chungus5
(25 Nov 2026, 03:16 to 03:54 UTC), on the md array that holds the instance filesystem:
48.2 to 52.5 GB written in 1.50 to 1.60 million write requests per job, 76 to 137 GB
read, peak allocated disk 47 to 49 GB, over 20 to 30 minutes.

Ten one-minute `/proc/diskstats` samples on chungus5 (04:07 to 04:17 UTC, 25 Nov):
each NVMe drive (Micron 7450, md RAID 0) wrote 195 to 323 MiB/s at 13,500 to 18,100
writes/s, with a mean write latency of 4.9 to 12.0 ms (median about 7.5 ms). The array
wrote 390 to 645 MiB/s.
