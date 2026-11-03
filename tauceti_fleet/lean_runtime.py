"""Install the worker's standalone pressure gate on hosts and in fresh CI guests."""

import os
import shlex
import tempfile
from pathlib import Path

from . import lean_pressure
from .store import data_dir

# Keep this startup hook in sync with TauCetiWorker/scripts/tauceti-lean-env.
STARTUP = """if [ -n "${TAUCETI_PREVIOUS_BASH_ENV:-}" ] && [ -f "$TAUCETI_PREVIOUS_BASH_ENV" ]; then
    tauceti_pressure_previous=$TAUCETI_PREVIOUS_BASH_ENV
    unset TAUCETI_PREVIOUS_BASH_ENV
    . "$tauceti_pressure_previous"
    unset tauceti_pressure_previous
fi
"${TAUCETI_LEAN_WRAPPER:?}" --install-available
"""


def environment():
    directory = data_dir() / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "tauceti-lean"
    startup = directory / "tauceti-lean-env"
    for path, content, mode in (
        (binary, Path(lean_pressure.__file__).read_bytes(), 0o755),
        (directory / "lean", Path(lean_pressure.__file__).read_bytes(), 0o755),
        (startup, STARTUP.encode(), 0o644),
    ):
        fd, temporary = tempfile.mkstemp(dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    env = dict(LEAN=str(binary), LAKE_OVERRIDE_LEAN="true", BASH_ENV=str(startup), TAUCETI_LEAN_WRAPPER=str(binary))
    if previous := os.environ.get("BASH_ENV"):
        if previous != str(startup):
            env["TAUCETI_PREVIOUS_BASH_ENV"] = previous
    return env


def ci_commands():
    binary = "/usr/local/bin/tauceti-lean"
    startup = "/opt/tauceti-lean-env"
    return [
        "printf %s " + shlex.quote(Path(lean_pressure.__file__).read_text()) + " > " + binary + "; chmod 755 " + binary,
        "printf %s " + shlex.quote(STARTUP) + " > " + startup,
        "printf '%s\\n' "
        + " ".join(
            shlex.quote(line)
            for line in (
                "LAKE_OVERRIDE_LEAN=true",
                "LEAN=" + binary,
                "TAUCETI_LEAN_WRAPPER=" + binary,
                "BASH_ENV=" + startup,
            )
        )
        + " >> /opt/actions-runner/.env; chown runner:runner /opt/actions-runner/.env",
    ]
