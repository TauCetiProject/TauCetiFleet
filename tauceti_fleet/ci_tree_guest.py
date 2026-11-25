#!/usr/bin/env python3
"""Shared CI build tree: the parts that run inside a native guest (TauCetiWorker decision 211).

Standalone: Fleet copies this file into every native guest as /usr/local/bin/tauceti_ci_tree.py,
so it imports nothing from Fleet.

    tauceti_ci_tree.py key --project DIR --watchdog DIR [--wrapper FILE]
        Print the entry key: everything that decides whether a job's traces match the tree.
    tauceti_ci_tree.py newest-published --repo DIR --ref REF --map-out FILE
        Print the newest first-parent commit of REF whose Lake revision map is published.
    tauceti_ci_tree.py verify --project DIR --commit SHA --map FILE --hasher FILE --watchdog DIR --report FILE
        Prune the builder's `.lake` to what the entry needs and check all of it, failing closed.

`verify` runs in the builder guest after a restore-only `lake build`. It executes no project
code: it reads files, asks Git about package checkouts and hashes outputs with the toolchain's
own Lake hash (Tools' CacheHash.lean, run by the real `lean`). It checks more than Tools'
lake_output_check.py: every package build, every retained artifact and mapping, the published
map's provenance of every root module, required-file completeness and `.hash` records.
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SCHEMA = 1
ROOT_PACKAGE = "TauCeti"
REPOSITORY = "TauCetiProject/TauCeti"
REVISIONS = "https://cache.taucetiproject.org/revisions"
NAME = re.compile(r"[0-9a-f]{16}")
EXTENSIONS = ("olean", "olean.server", "olean.private", "ilean", "ir", "ir.sig", "c", "bc", "ltar")
ARTIFACT = re.compile(r"([0-9a-f]{16})\.(" + "|".join(re.escape(e) for e in EXTENSIONS) + r")")
# Lake's module trace output keys and their extensions (`ModuleOutputDescrs.toJson`).
DESCRIPTORS = {"o": ("olean", "olean.server", "olean.private"), "i": ("ilean",), "rs": ("ir.sig",),
               "r": ("ir",), "c": ("c",), "b": ("bc",), "l": ("ltar",)}
# Outputs Lake keeps beside the module in build/ir rather than build/lib/lean.
IR_EXTENSIONS = ("c", "bc", "ltar")
# Root traces with no row in the published map (the empty root module, say). Any more means the
# tree is not main's publication.
MAX_UNMAPPED_ROOT = 16
# The Lake schemas this verifier understands; anything else fails until it is taught.
TRACE_SCHEMAS = ("2025-09-10",)
MAPPING_SCHEMAS = ("2026-02-25",)
OUTPUT_SUFFIXES = tuple("." + e for e in EXTENSIONS)


class Invalid(Exception):
    """The tree must not become an entry."""


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(root, watchdog=False):
    """Content digest of a small directory: names, types, modes, link targets and file bytes.

    With `watchdog`, an installed watchdog digests as prepared (see watchdog_digest).
    """
    root = Path(root)
    digest = hashlib.sha256()
    for directory, directories, files in os.walk(root):
        directories.sort()
        for name in sorted(directories + files):
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            if watchdog and (relative in WATCHDOG_STATE or relative == "bin/lean-watchdog"):
                continue
            if watchdog and relative == "bin/lean":
                path = root / "bin/lean-watchdog"
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                digest.update(f"l {relative} {os.readlink(path)}\n".encode())
            elif stat.S_ISREG(info.st_mode):
                digest.update(f"f {relative} {info.st_mode & 0o777:o} {sha256_file(path)}\n".encode())
            elif stat.S_ISDIR(info.st_mode):
                digest.update(f"d {relative}\n".encode())
            else:
                raise Invalid(f"unexpected file type in {relative}")
    return digest.hexdigest()


# What Fleet's Lean pressure wrapper (`tauceti-lean --install-watchdog`, lean_pressure.py) adds to
# a watchdog toolchain the first time a guest step runs with WATCHDOG_TOOLCHAIN set: it moves
# bin/lean to bin/lean-watchdog, puts itself at bin/lean, and leaves these state files.
WATCHDOG_STATE = (".tauceti-lean.json", ".tauceti-lean.lock", ".tauceti-lean-ready")


def watchdog_digest(root, wrapper):
    """The watchdog's digest as prepared, whether or not the pressure wrapper has installed itself.

    A job computes its key after that install (the guest's BASH_ENV hook runs it at the first step
    that sees WATCHDOG_TOOLCHAIN) and the builder before it. The install is a fixed function of the
    prepared tree and the wrapper, which the key names separately (wrapper_sha256), so the
    installed form digests as the prepared one: bin/lean-watchdog is read as bin/lean, and the
    state files are left out. An installed bin/lean that is not exactly the wrapper fails.
    """
    root = Path(root)
    installed = (root / "bin/lean-watchdog").is_file() and not (root / "bin/lean-watchdog").is_symlink()
    if installed:
        lean = root / "bin/lean"
        if lean.is_symlink() or not lean.is_file() or sha256_file(lean) != sha256_file(wrapper):
            raise Invalid("the watchdog's bin/lean is neither the watchdog nor the pressure wrapper")
        # The marker chooses the executable the wrapper runs: it must be this tree's watchdog.
        marker = root / ".tauceti-lean.json"
        try:
            selected = json.loads(marker.read_text()) if regular(marker) else None
        except (OSError, ValueError):
            selected = None
        if selected != {"lean": str(root.absolute() / "bin/lean-watchdog")}:
            raise Invalid("the pressure wrapper's marker does not select this watchdog")
    elif any((root / name).exists() or (root / name).is_symlink() for name in WATCHDOG_STATE):
        raise Invalid("watchdog wrapper state without an installed wrapper")
    return tree_digest(root, watchdog=installed)


def run(args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def real_lean(project):
    return run(["elan", "which", "lean"], cwd=project)


def key(project, watchdog, wrapper="/usr/local/bin/tauceti-lean"):
    """What decides whether a job's traces match the entry, as content, not paths alone.

    Lake names Lean in its traces mainly by its Git hash, so a different wrapper or executable
    need not trigger a rebuild; the key therefore names both by content.
    """
    project = Path(project).resolve()
    manifest = json.loads((project / "lake-manifest.json").read_text())
    lean = real_lean(project)
    value = dict(
        schema=SCHEMA,
        project=str(project),
        toolchain=(project / "lean-toolchain").read_text().strip(),
        lean_githash=run([lean, "--githash"]),
        lean_sha256=sha256_file(lean),
        lake_version=run(["lake", "--version"], cwd=project),
        manifest_sha256=sha256_file(project / "lake-manifest.json"),
        lakefile_sha256=sha256_file(project / "lakefile.toml"),
        packages=sorted([package["name"], package.get("rev")] for package in manifest.get("packages", [])),
        architecture=platform.machine(),
        lake_override_lean="true",
        wrapper_sha256=sha256_file(wrapper),
        watchdog_digest=watchdog_digest(watchdog, wrapper),
    )
    return value, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def map_url(scope, commit):
    return f"{REVISIONS}/{REPOSITORY}/tc/{scope}/{commit}.jsonl"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def newest_published(repo, ref, limit, map_out, fetcher=fetch):
    """The newest first-parent commit whose map is published; the map is saved to map_out."""
    commits = run(["git", "-C", str(repo), "rev-list", "--first-parent", "-n", str(limit), ref]).split()
    scopes = {}
    for commit in commits:
        blob = run(["git", "-C", str(repo), "rev-parse", f"{commit}:lean-toolchain"])
        if blob not in scopes:
            toolchain = run(["git", "-C", str(repo), "cat-file", "blob", blob]).strip()
            scopes[blob] = toolchain.replace("/", "--").replace(":", "---")
        try:
            data = fetcher(map_url(scopes[blob], commit))
        except urllib.error.HTTPError as error:
            if error.code in (403, 404):
                continue
            raise
        Path(map_out).write_bytes(data)
        return commit
    raise Invalid(f"no published map in the newest {limit} first-parent commits")


def read_map(path):
    lines = Path(path).read_text().splitlines()
    if not lines or not isinstance(json.loads(lines[0]), str):
        raise Invalid("published map without its header")
    rows = {}
    for line in lines[1:]:
        value = json.loads(line)
        if (not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str)
                or not NAME.fullmatch(value[0]) or not isinstance(value[1], str)
                or not re.fullmatch(NAME.pattern + r"\.ltar", value[1])):
            raise Invalid("malformed published map row")
        if rows.setdefault(value[0], value[1]) != value[1]:
            raise Invalid("published map names two archives for one input")
    if not rows:
        raise Invalid("empty published map")
    return rows


def output_names(outputs):
    """[(extension, name)] for every output a trace or mapping names; malformed names fail."""
    if not isinstance(outputs, dict):
        raise Invalid("trace or mapping without outputs")
    # Every module has its ilean, and three oleans in the module system (`m`), one outside it.
    if "m" in outputs and not isinstance(outputs["m"], bool):
        raise Invalid("malformed module flag")
    oleans = 3 if outputs.get("m", True) else 1
    if not isinstance(outputs.get("o"), list) or len(outputs["o"]) != oleans or "i" not in outputs:
        raise Invalid("outputs without their oleans and ilean")
    for key, value in outputs.items():
        if key not in ("o", "m") and not isinstance(value, str):
            raise Invalid(f"malformed {key} output")
    names = []
    for key, extensions in DESCRIPTORS.items():
        if key not in outputs:
            continue
        values = outputs[key] if isinstance(outputs[key], list) else [outputs[key]]
        if len(values) > len(extensions):
            raise Invalid(f"too many {key} outputs")
        for extension, name in zip(extensions, values):
            if not isinstance(name, str) or not re.fullmatch(NAME.pattern + r"\." + re.escape(extension), name):
                raise Invalid(f"malformed {key} output name")
            names.append((extension, name))
    unknown = set(outputs) - set(DESCRIPTORS) - {"m"}
    if unknown:
        raise Invalid("unknown output kinds " + ",".join(sorted(unknown)))
    return names


def lake_hashes(paths, hasher, lean, jobs=16):
    """Lake content hash of each path (16 hex digits), one read per inode."""
    if not paths:
        return {}
    inodes = {}
    for path in paths:
        info = os.lstat(path)
        inodes.setdefault((info.st_dev, info.st_ino), str(path))
    unique = list(inodes.values())
    count = max(1, min(jobs, len(unique), len(os.sched_getaffinity(0))))
    by_path = {}
    with tempfile.TemporaryDirectory(prefix="ci-tree-hash-") as scratch:
        helpers = []
        try:
            for index in range(count):
                request = Path(scratch) / f"request-{index}.json"
                request.write_text(json.dumps(unique[index::count]))
                helpers.append(subprocess.Popen([lean, "--run", str(hasher), str(request)], stdout=subprocess.PIPE, text=True))
            with concurrent.futures.ThreadPoolExecutor(len(helpers)) as pool:
                outputs = list(pool.map(lambda helper: helper.communicate(timeout=3600)[0], helpers))
            for helper, output in zip(helpers, outputs):
                if helper.returncode != 0:
                    raise Invalid("the Lake hash helper failed")
                for line in output.splitlines():
                    path, value = json.loads(line)
                    by_path[path] = f"{value:016x}"
        finally:
            for helper in helpers:
                if helper.poll() is None:
                    helper.kill()
                    helper.wait()
    if set(by_path) != set(unique):
        raise Invalid("incomplete output hashes")
    result = {}
    for path in paths:
        info = os.lstat(path)
        result[str(path)] = by_path[inodes[(info.st_dev, info.st_ino)]]
    return result


def regular(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def remove(path):
    if os.path.islink(path) or not os.path.isdir(path):
        os.unlink(path)
    else:
        shutil.rmtree(path)


def raise_error(error):
    raise Invalid(f"cannot read the tree: {error}")


def traces(build, required=False):
    """Yield (stem, trace path, parsed trace) under build/lib/lean."""
    lib = build / "lib/lean"
    if not lib.is_dir():
        if required:
            raise Invalid(f"{build} has no build")
        return
    for directory, _, files in os.walk(lib, onerror=raise_error):
        for file in files:
            if file.endswith(".trace"):
                path = Path(directory) / file
                try:
                    value = json.loads(path.read_text())
                except ValueError:
                    raise Invalid(f"unreadable trace {path.relative_to(build)}") from None
                if not isinstance(value, dict) or value.get("schemaVersion") not in TRACE_SCHEMAS:
                    raise Invalid(f"malformed or unknown trace {path.relative_to(build)}")
                yield path.relative_to(lib).with_suffix(""), path, value


def mapping_data(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or value.get("schemaVersion") not in MAPPING_SCHEMAS:
        raise Invalid(f"malformed or unknown mapping {Path(path).name}")
    return value.get("data")


def output_files(build):
    """Every file under a build that has an output's name: each must be claimed by a trace."""
    found = set()
    for sub in ("lib/lean", "ir"):
        if (build / sub).is_dir():
            for directory, _, files in os.walk(build / sub, onerror=raise_error):
                found.update(str(Path(directory) / f) for f in files if f.endswith(OUTPUT_SUFFIXES))
    return found


def build_path(build, stem, extension):
    return build / ("ir" if extension in IR_EXTENSIONS else "lib/lean") / (str(stem) + "." + extension)


def verify(project, commit, map_path, hasher, watchdog, *, hash_function=None, lean=None, git=None):
    """Prune and check the builder's tree; return the report. Raises Invalid on any doubt."""
    started = time.monotonic()
    project = Path(project).resolve()
    lake = project / ".lake"
    git = git or (lambda *args: run(["git", *args]))
    removed = dict(tmp=0, revisions=0, root_mappings=0, package_mappings=0, artifacts=0)
    if git("-C", str(project), "rev-parse", "HEAD") != commit:
        raise Invalid("the project checkout is not the entry's commit")
    if git("-C", str(project), "status", "--porcelain", "--untracked-files=no"):
        raise Invalid("the project checkout has modified tracked files")
    # Job scratch, never part of an entry.
    for name in ("tmp", "telemetry", "outputs.jsonl"):
        if (lake / name).exists():
            remove(lake / name)
            removed["tmp"] += 1
    (lake / "tmp").mkdir(exist_ok=True)
    cache = lake / "cache"
    for leftover in cache.parent.glob("cache.discard.*"):
        raise Invalid(f"a discarded cache was left behind: {leftover.name}")
    rows = read_map(map_path)
    archives = set(rows.values())

    # Revision maps: only the entry's own.
    revisions = cache / "revisions"
    keep_map = revisions / ROOT_PACKAGE / f"{commit}.jsonl"
    if not regular(keep_map):
        raise Invalid("the restore did not record the entry's revision map")
    if read_map(keep_map) != rows:
        raise Invalid("the restored revision map differs from the published one")
    for path in sorted(revisions.rglob("*"), reverse=True):
        if path.is_dir() and not path.is_symlink():
            continue
        if path != keep_map:
            os.unlink(path)
            removed["revisions"] += 1

    # Root mappings: exactly the published inputs, plus the few root traces outside the map.
    manifest = json.loads((project / "lake-manifest.json").read_text())
    packages = {package["name"]: package for package in manifest.get("packages", [])}
    root_traces = {}
    for stem, path, value in traces(lake / "build", required=True):
        dep = value.get("depHash")
        if not isinstance(dep, str) or not NAME.fullmatch(dep):
            raise Invalid(f"root trace without an input hash: {stem}")
        if dep in root_traces:
            raise Invalid(f"two root modules share input hash {dep}")
        root_traces[dep] = (stem, value)
    missing = set(rows) - set(root_traces)
    if missing:
        raise Invalid(f"{len(missing)} published modules are not in the tree")
    unmapped = set(root_traces) - set(rows)
    if len(unmapped) > MAX_UNMAPPED_ROOT:
        raise Invalid(f"{len(unmapped)} root modules are not in the published map")

    mappings = cache / "outputs"
    referenced = {}  # artifact name -> where it is referenced
    for path in sorted((mappings / ROOT_PACKAGE).glob("*")):
        stem = path.name[:-len(".json")] if path.name.endswith(".json") else None
        if not stem or not NAME.fullmatch(stem) or not regular(path):
            raise Invalid(f"unexpected root mapping file {path.name}")
        if stem not in root_traces:
            os.unlink(path)  # Cache history the entry does not need.
            removed["root_mappings"] += 1
    checks = []  # (path, expected hash)
    claimed = set()  # every output file some trace or mapping accounts for
    for dep, (stem, value) in root_traces.items():
        mapping = mappings / ROOT_PACKAGE / f"{dep}.json"
        if not regular(mapping):
            raise Invalid(f"root module {stem} has no mapping")
        mapped = output_names(mapping_data(mapping))
        if dep in rows and dict(mapped).get("ltar") != rows[dep]:
            raise Invalid(f"root module {stem} maps to an archive the published map does not name")
        built = output_names(value.get("outputs"))
        if sorted(built) != sorted((e, n) for e, n in mapped if e != "ltar"):
            raise Invalid(f"root module {stem}'s trace and mapping disagree")
        for extension, name in mapped:
            referenced[name] = stem
            if extension == "ltar" and regular(build_path(lake / "build", stem, "ltar")):
                # The archive Lake keeps beside the module is the mapped one.
                claimed.add(str(build_path(lake / "build", stem, "ltar")))
                checks.append((build_path(lake / "build", stem, "ltar"), name.split(".", 1)[0]))
        for extension, name in built:
            path = build_path(lake / "build", stem, extension)
            if not regular(path):
                raise Invalid(f"root module {stem} lacks its {extension}")
            claimed.add(str(path))
            checks.append((path, name.split(".", 1)[0]))

    # Packages: the manifest's exact clean checkouts, with every traced output present and right.
    present = {path.name for path in (lake / "packages").iterdir()} if (lake / "packages").is_dir() else set()
    if present != set(packages):
        raise Invalid("the package checkouts are not the manifest's packages")
    package_outputs = 0
    for name, package in packages.items():
        directory = lake / "packages" / name
        if git("-C", str(directory), "rev-parse", "HEAD") != package.get("rev"):
            raise Invalid(f"package {name} is not at its manifest revision")
        if git("-C", str(directory), "status", "--porcelain", "--untracked-files=no"):
            raise Invalid(f"package {name} has modified tracked files")
        for stem, path, value in traces(directory / ".lake/build"):
            for extension, output in output_names(value.get("outputs")):
                target = build_path(directory / ".lake/build", stem, extension)
                if not regular(target):
                    raise Invalid(f"package {name} module {stem} lacks its {extension}")
                claimed.add(str(target))
                checks.append((target, output.split(".", 1)[0]))
                package_outputs += 1
    for directory in sorted(mappings.iterdir()) if mappings.is_dir() else []:
        if directory.name == ROOT_PACKAGE:
            continue
        if directory.name not in packages or not directory.is_dir() or directory.is_symlink():
            raise Invalid(f"unexpected mapping directory {directory.name}")
        for path in sorted(directory.iterdir()):
            stem = path.name[:-len(".json")] if path.name.endswith(".json") else None
            if not stem or not NAME.fullmatch(stem) or not regular(path):
                raise Invalid(f"unexpected mapping file {directory.name}/{path.name}")
            names = output_names(mapping_data(path))
            if all(regular(cache / "artifacts" / output) for _, output in names):
                for _, output in names:
                    referenced.setdefault(output, directory.name)
            else:
                os.unlink(path)  # A mapping whose artifacts are gone is history, not state.
                removed["package_mappings"] += 1

    # Artifacts: every retained file is referenced, well named and holds the bytes its name says.
    artifacts = cache / "artifacts"
    for path in sorted(artifacts.iterdir()) if artifacts.is_dir() else []:
        match = ARTIFACT.fullmatch(path.name)
        if not match or not regular(path):
            raise Invalid(f"unexpected artifact {path.name}")
        if path.name not in referenced:
            os.unlink(path)
            removed["artifacts"] += 1
            continue
        checks.append((path, match.group(1)))
    for name in referenced:
        if not regular(artifacts / name):
            raise Invalid(f"referenced artifact {name} is missing")
    if not archives <= set(referenced):
        raise Invalid("a published archive is not referenced by the tree")

    # No output file that no trace accounts for: a stray olean is read by audits and imports.
    builds = [lake / "build"] + [lake / "packages" / name / ".lake/build" for name in packages]
    stray = set().union(*(output_files(build) for build in builds)) - claimed
    if stray:
        raise Invalid(f"{len(stray)} output files no trace names, first {Path(min(stray)).relative_to(lake)}")

    # `.hash` records Lake trusts in place of rehashing must name their file's real hash, wherever
    # Lake keeps state: the project's .lake and every package's.
    # A package Lake never built (Cli, say) has no .lake and so no records.
    states = [lake] + [lake / "packages" / name / ".lake" for name in packages
                       if (lake / "packages" / name / ".lake").is_dir()]
    for state in states:
        for directory, directories, files in os.walk(state, onerror=raise_error):
            if Path(directory) == lake:
                directories[:] = [d for d in directories if d != "packages"]
            for file in files:
                if file.endswith(".hash"):
                    record = Path(directory) / file
                    target = record.with_suffix("")
                    value = record.read_text().strip()
                    if not NAME.fullmatch(value) or not regular(target):
                        raise Invalid(f"malformed hash record {record.relative_to(lake)}")
                    checks.append((target, value))

    paths = sorted({str(path) for path, _ in checks})
    hashes = (hash_function or (lambda items: lake_hashes(items, hasher, lean or real_lean(project))))(paths)
    wrong = [path for path, expected in checks if hashes.get(str(path)) != expected]
    if wrong:
        raise Invalid(f"{len(wrong)} outputs differ from their recorded hashes, first {Path(wrong[0]).relative_to(lake)}")
    entry_key, digest = key(project, watchdog) if watchdog is not None else (None, None)
    return dict(ok=True, commit=commit, key=entry_key, key_digest=digest, modules=len(rows),
                root_traces=len(root_traces), unmapped_root_traces=len(unmapped), package_outputs=package_outputs,
                artifacts=len(referenced), hashed_files=len(paths), removed=removed,
                verify_seconds=round(time.monotonic() - started, 1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    k = sub.add_parser("key")
    k.add_argument("--project", required=True)
    k.add_argument("--watchdog", required=True)
    k.add_argument("--wrapper", default="/usr/local/bin/tauceti-lean")
    n = sub.add_parser("newest-published")
    n.add_argument("--repo", required=True)
    n.add_argument("--ref", required=True)
    n.add_argument("--limit", type=int, default=400)
    n.add_argument("--map-out", required=True)
    v = sub.add_parser("verify")
    for name in ("project", "commit", "map", "hasher", "watchdog", "report"):
        v.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    if args.action == "key":
        value, digest = key(args.project, args.watchdog, args.wrapper)
        print(json.dumps(dict(key=value, key_digest=digest), sort_keys=True))
        return 0
    if args.action == "newest-published":
        print(newest_published(args.repo, args.ref, args.limit, args.map_out))
        return 0
    try:
        report = verify(args.project, args.commit, args.map, args.hasher, args.watchdog)
    except (Invalid, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        report = dict(ok=False, commit=args.commit, error=f"{type(error).__name__}: {error}"[:500])
    temporary = Path(args.report).with_suffix(".next")
    temporary.write_text(json.dumps(report, sort_keys=True) + "\n")
    temporary.replace(args.report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
