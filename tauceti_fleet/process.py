"""Bounded subprocesses with diagnostics that never echo credential-bearing input."""

import os
import shutil
import subprocess

from .store import FleetError, data_dir


def executable(name):
    local = data_dir() / "bin" / name
    return str(local) if local.is_file() else shutil.which(name)


def run(args, *, input=None, timeout=30, check=True, env=None):
    try:
        result = subprocess.run(
            [str(v) for v in args],
            input=input,
            capture_output=True,
            text=not isinstance(input, bytes),
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        raise FleetError(f"Required executable {args[0]} is missing; run tauceti-fleet doctor") from None
    except subprocess.TimeoutExpired:
        raise FleetError(
            f"{os.path.basename(str(args[0]))} timed out after {timeout}s; no automatic mutation retry"
        ) from None
    if check and result.returncode:
        # Callers can surface safe operation-specific evidence; never dump stderr
        # from SSH, gh, package managers or a provider credential command here.
        raise FleetError(
            f"{os.path.basename(str(args[0]))} exited {result.returncode}; run doctor for actionable diagnostics"
        )
    return result
