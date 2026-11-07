"""Trusted, credential-free dependency warmer for an unregistered image guest.

This program runs as the guest's runner, at the production workspace prefix. It
uses immutable main configuration and the same stock Lake cache target as the
dependency warmer. No candidate code is executed; the existing trusted-main consumer validates the payload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

PROJECT = Path("/opt/actions-runner/_work/TauCeti/TauCeti/pr")
CONFIG = ("lakefile.toml", "lake-manifest.json", "lean-toolchain")
REPOSITORY = "https://github.com/TauCetiProject/TauCeti.git"
RELEASE = re.compile(r"leanprover/lean4:v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?")
SHA = re.compile(r"[0-9a-f]{40}")
PACKAGE_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def environment():
    # No registration credential, inherited Git settings, compiler wrapper or
    # Actions token reaches this producer or its package repositories.
    home = Path.home()
    return {
        "HOME": str(home),
        "PATH": str(home / ".elan/bin") + ":/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "RAYON_NUM_THREADS": "16",
        "LEAN_NUM_THREADS": "4",
    }


def run(arguments, *, cwd=None, env=None, timeout=1200):
    program = Path(arguments[0]).name
    observed = program in ("elan", "lake", "tar")
    started = time.monotonic()
    if observed:
        print("FLEET_PREFILL_STEP " + json.dumps(dict(program=program, state="started")), flush=True)
    try:
        return subprocess.run(
            arguments, cwd=cwd, env=env or environment(), check=True, text=True, capture_output=True, timeout=timeout
        )
    except subprocess.CalledProcessError as error:
        # The environment contains no secrets and all executed inputs are trusted
        # public configuration/dependencies. Escape diagnostics, never replay them
        # as shell or workflow commands.
        print(
            "FLEET_PREFILL_FAILED "
            + json.dumps(
                dict(program=program, stdout=(error.stdout or "")[-2000:], stderr=(error.stderr or "")[-2000:])
            ),
            flush=True,
        )
        raise
    finally:
        if observed:
            print(
                "FLEET_PREFILL_STEP "
                + json.dumps(dict(program=program, state="finished", seconds=time.monotonic() - started)),
                flush=True,
            )


def git(directory, *arguments):
    return run(
        [
            "git",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            "-C",
            str(directory),
            *arguments,
        ],
        timeout=120,
    ).stdout.strip()


def package_inventory(project):
    packages = json.loads((project / "lake-manifest.json").read_text())["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValueError("expected pinned Git dependencies")
    names = set()
    for package in packages:
        name = package["name"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_'-]+", name)
            or name in names
            or not SHA.fullmatch(package["rev"])
            or not PACKAGE_URL.fullmatch(package["url"])
        ):
            raise ValueError("invalid immutable package identity")
        names.add(name)
    if "mathlib" not in names:
        raise ValueError("Mathlib cache executable is required")
    return packages


def seal_packages(project, destination, revision):
    """Publish the existing portable payload format only after complete checks."""
    project, destination = Path(project).resolve(), Path(destination)
    if not SHA.fullmatch(revision) or not RELEASE.fullmatch((project / "lean-toolchain").read_text().strip()):
        raise ValueError("expected immutable main and an official Lean release")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("unsupported image ABI")
    packages = package_inventory(project)
    root = project / ".lake/packages"
    if root.is_symlink() or {p.name for p in root.iterdir()} != {p["name"] for p in packages}:
        raise ValueError("package inventory mismatch")
    for package in packages:
        directory = root / package["name"]
        metadata = directory / ".git"
        if (
            directory.is_symlink()
            or not metadata.is_dir()
            or metadata.is_symlink()
            or (metadata / "objects/info/alternates").exists()
            or (metadata / "commondir").exists()
        ):
            raise ValueError("package must have self-contained private Git metadata")
        if (
            git(directory, "rev-parse", "HEAD") != package["rev"]
            or git(directory, "remote", "get-url", "origin").removesuffix(".git") != package["url"].removesuffix(".git")
            or git(directory, "status", "--porcelain", "--untracked-files=all")
        ):
            raise ValueError("package differs from the trusted immutable pins")
        # Clones were made without credentials. Refuse any unexpected persisted
        # authentication/executable settings instead of archiving them.
        configuration = git(directory, "config", "--local", "--list").lower()
        if any(
            value in configuration for value in ("extraheader", "credential.", "insteadof", "hookspath", "fsmonitor")
        ):
            raise ValueError("unexpected package Git configuration")
    executable = root / "mathlib/.lake/build/bin/cache"
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("native cache executable has not been built")
    if destination.exists() or destination.is_symlink():
        raise ValueError("payload destination must be new")
    with tempfile.TemporaryDirectory(prefix="prefill-publish-", dir=destination.parent) as temporary:
        stage = Path(temporary)
        archive = stage / "packages.tar.zst"
        run(["tar", "--use-compress-program=zstd -T2 -3", "-cf", str(archive), ".lake/packages"], cwd=project)
        if archive.stat().st_size > 1024**3:
            raise ValueError("payload exceeds the consumer's supported format size")
        manifest = dict(
            schema=1,
            platform="Linux-x86_64",
            project_path=str(project),
            uid=os.getuid(),
            config_sha256={name: digest(project / name) for name in CONFIG},
            payload_sha256=digest(archive),
            payload_bytes=archive.stat().st_size,
            producer="TauCetiFleet/ci-prefill-v1",
            producer_revision=revision,
            toolchain=(project / "lean-toolchain").read_text().strip(),
            packages={p["name"]: {key: p[key] for key in ("rev", "url")} for p in packages},
        )
        (stage / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        for path in stage.iterdir():
            path.chmod(0o444)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        stage.rename(destination)
    return manifest


def produce(revision, output):
    started = time.monotonic()
    if not SHA.fullmatch(revision):
        raise ValueError("expected immutable trusted-main revision")
    if PROJECT.exists() or PROJECT.is_symlink():
        raise ValueError("producer workspace must be new")
    PROJECT.mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="prefill-main-") as temporary:
        source = Path(temporary) / "source.git"
        run(["git", "init", "--bare", "--template=", str(source)])
        run(
            [
                "git",
                "--git-dir=" + str(source),
                "-c",
                "fetch.fsckObjects=true",
                "-c",
                "core.hooksPath=/dev/null",
                "fetch",
                "--no-tags",
                REPOSITORY,
                revision,
            ]
        )

        def read(path):
            entry = run(["git", "--git-dir=" + str(source), "ls-tree", revision, "--", path]).stdout
            if not entry.startswith("100644 blob ") or entry.split("\t", 1)[1].strip() != path:
                raise ValueError("trusted input is not an ordinary file")
            return run(["git", "--git-dir=" + str(source), "show", revision + ":" + path]).stdout

        for name in CONFIG:
            (PROJECT / name).write_text(read(name))
        consumer = Path(temporary) / "dependency_prefill.py"
        consumer.write_text(read("scripts/dependency_prefill.py"))
        pin = (PROJECT / "lean-toolchain").read_text().strip()
        if not RELEASE.fullmatch(pin):
            raise ValueError("expected stock official Lean release")
        package_inventory(PROJECT)
        expected = {name: digest(PROJECT / name) for name in CONFIG}
        run(["elan", "toolchain", "install", pin])
        run(["lake", "build", "cache"], cwd=PROJECT)
        if expected != {name: digest(PROJECT / name) for name in CONFIG}:
            raise ValueError("Lake modified the trusted configuration")
        manifest = seal_packages(PROJECT, output, revision)
        # Exercise the actual existing consumer, then simulate CI's ordinary
        # official-toolchain restoration. Never forge warmer snapshot provenance.
        shutil.rmtree(PROJECT / ".lake")
        (PROJECT / ".lake").mkdir()
        hydrated = run(["python3", str(consumer), str(PROJECT), "--source", str(output)])
        result = json.loads(hydrated.stdout.removeprefix("dependency-prefill "))
        if not result["hit"]:
            raise ValueError("producer payload failed its native consumer")
        sysroot = Path(run(["lean", "--print-prefix"], cwd=PROJECT).stdout.strip())
        if sysroot.parent != Path.home() / ".elan/toolchains" or sysroot.is_symlink():
            raise ValueError("unexpected official toolchain installation")
        shutil.rmtree(sysroot)
        run(["elan", "toolchain", "install", pin])
        executable = PROJECT / ".lake/packages/mathlib/.lake/build/bin/cache"
        identity = (digest(executable), executable.stat().st_mtime_ns)
        env = environment()
        store = PROJECT.parent / ".mathlib-ltar-cache"
        env["MATHLIB_CACHE_DIR"] = str(store)
        native = run(["lake", "exe", "cache", "get"], cwd=PROJECT, env=env)
        log = native.stdout + native.stderr
        if (
            identity != (digest(executable), executable.stat().st_mtime_ns)
            or "Built cache:exe" in log
            or "some files were not found in the cache" in log
        ):
            raise ValueError("native cache preparation failed executable reuse or complete restoration")
        # Only sources/executable belong to this image mode. Mathlib expansion,
        # compressed-cache restoration and official toolchain restoration remain
        # the consumer workflow's normal operations.
        shutil.rmtree(PROJECT)
        shutil.rmtree(store, ignore_errors=True)
        shutil.rmtree(sysroot)
    return dict(
        manifest=manifest,
        seconds=time.monotonic() - started,
        native_executable_reused=True,
        consumer=result,
        native_executable_sha256=identity[0],
        native_cache_get_exit_status=0,
        native_cache_get_tail=log[-4000:],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("FLEET_PREFILL_READY " + json.dumps(produce(args.revision, args.output), sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
