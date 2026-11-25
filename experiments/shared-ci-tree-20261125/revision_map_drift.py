#!/usr/bin/env python3
"""How far does TauCeti `main` drift from a shared build tree made some hours earlier?

Read-only. Over a window of first-parent `main` history, compare each published Lake
revision map with the one LAG publications earlier. A module whose (input hash, output)
pair is in the newer map but not the older one has a trace a job on the newer base must
rewrite on top of a shared tree made from the older one. If its output archive is not in
the older map at all, the job must also fetch and write that output's bytes. This sizes
phase 0 of decision 211 before any build code exists.

Usage: revision_map_drift.py GIT_DIR [--hours 24] [--lags 1,2,3,4]

GIT_DIR is any clone of TauCeti (a blobless, no-checkout clone is enough). The maps are
read anonymously from the public cache; nothing is written except a private download
cache under --cache.
"""

import argparse
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://cache.taucetiproject.org/revisions"
REPOSITORY = "TauCetiProject/TauCeti"


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, text=True).stdout


def history(directory, ref, hours):
    since = time.time() - hours * 3600
    rows = []
    for line in git(directory, "log", "--first-parent", "--format=%H %ct", f"--since={int(since)}", ref).splitlines():
        sha, stamp = line.split()
        rows.append((int(stamp), sha))
    return sorted(rows)


def toolchain(directory, sha):
    return git(directory, "show", f"{sha}:lean-toolchain").strip()


def load(cache, scope, sha):
    path = cache / scope / f"{sha}.jsonl"
    missing = path.with_suffix(".missing")
    if missing.exists():
        return None
    if not path.exists():
        url = f"{ENDPOINT}/{REPOSITORY}/tc/{scope}/{sha}.jsonl"
        try:
            # The cache's CDN refuses urllib's default agent.
            request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                path.parent.mkdir(parents=True, exist_ok=True)
                missing.touch()
                return None
            raise
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    pairs = set()
    for line in path.read_text().splitlines()[1:]:
        value = json.loads(line)
        if isinstance(value, list) and len(value) == 2:
            pairs.add((value[0], value[1]))
    return pairs


def published(directory, cache, rows):
    """Every commit in the window whose map is published, oldest first."""
    found, scopes = [], {}
    for stamp, sha in rows:
        scope = scopes.setdefault(sha, toolchain(directory, sha).replace("/", "--").replace(":", "---"))
        pairs = load(cache, scope, sha)
        if pairs is not None:
            found.append((stamp, sha, scope, pairs))
    return found


def compare(older, newer, sizes=None):
    """What a job based on `newer` still lacks on top of a shared tree made from `older`.

    Counts are of map rows (one per module). With `sizes` (archive name to compressed
    bytes), also the compressed bytes of the archives each set names.
    """
    if older[2] != newer[2]:
        # A toolchain bump between the two: the job takes today's path (key miss).
        return dict(key_miss=True)
    changed = newer[3] - older[3]
    outputs = {output for _, output in older[3]}
    new = {output for _, output in changed if output not in outputs}
    extra = {}
    if sizes:
        extra = dict(input_changed_archive_bytes=sum(sizes.get(output, 0) for _, output in changed),
                     new_output_archive_bytes=sum(sizes.get(output, 0) for output in new),
                     unsized=sum(output not in sizes for _, output in changed))
    return dict(key_miss=False, modules=len(newer[3]), input_changed=len(changed), new_outputs=len(new), **extra,
                stale_inputs=len({i for i, _ in older[3]} - {i for i, _ in newer[3]}),
                age_hours=(newer[0] - older[0]) / 3600)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("git_dir")
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--lags", default="1,2,3,4", help="publication lags to compare")
    parser.add_argument("--cache", default="/tmp/tauceti-revision-maps")
    parser.add_argument("--artifact-sizes", help="file of `NAME BYTES` lines for .ltar archives, for byte estimates")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    sizes = None
    if args.artifact_sizes:
        sizes = {}
        for line in Path(args.artifact_sizes).read_text().splitlines():
            name, size = line.split()
            if name.endswith(".ltar"):
                sizes[name] = int(size)
    lags = [int(value) for value in args.lags.split(",")]
    rows = history(args.git_dir, args.ref, args.hours)
    maps = published(args.git_dir, Path(args.cache), rows)
    if len(maps) < 2:
        raise SystemExit("fewer than two published maps in the window")
    results = {lag: [dict(at=maps[i][0], newer=maps[i][1], older=maps[i - lag][1], **compare(maps[i - lag], maps[i], sizes))
                     for i in range(lag, len(maps))] for lag in lags}
    if args.json:
        json.dump(dict(commits=len(rows), published=[(stamp, sha) for stamp, sha, _, _ in maps],
                       lags={str(lag): value for lag, value in results.items()}), sys.stdout, indent=1)
        print()
        return
    span = (maps[-1][0] - maps[0][0]) / 3600
    print(f"{len(rows)} first-parent commits, {len(maps)} published maps over {span:.1f} h "
          f"(one every {span / (len(maps) - 1):.1f} h of commit time), latest {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(maps[-1][0]))}")
    for lag, values in results.items():
        hits = [v for v in values if not v["key_miss"]]
        if not hits:
            print(f"lag {lag}: no comparable pairs")
            continue
        def summary(key):
            share = sorted(100 * v[key] / v["modules"] for v in hits)
            return f"median {statistics.median(share):.1f}%, max {share[-1]:.1f}%"
        print(f"lag {lag} publication(s), median age {statistics.median(v['age_hours'] for v in hits):.1f} h, "
              f"{len(hits)} pairs, {len(values) - len(hits)} key misses: input hash changed {summary('input_changed')}; "
              f"output archive not in the older tree {summary('new_outputs')}; of about {hits[-1]['modules']} modules")
        if sizes:
            def gigabytes(key):
                return statistics.median(v[key] for v in hits) / 1e9
            print(f"  compressed archive bytes (median): input changed {gigabytes('input_changed_archive_bytes'):.2f} GB, "
                  f"new outputs {gigabytes('new_output_archive_bytes'):.3f} GB; "
                  f"{max(v['unsized'] for v in hits)} archives at most were unsized")


if __name__ == "__main__":
    main()
