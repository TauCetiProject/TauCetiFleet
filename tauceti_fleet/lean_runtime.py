"""Install the worker's standalone pressure gate on hosts and in fresh CI guests."""

import shlex
from pathlib import Path

from . import lean_pressure

# Disposable guests install toolchains during a job, before later sandbox steps
# clear the environment. Host workers need only the scoped PATH alias and overlay.
STARTUP = """if [ "${TAUCETI_LEAN_STARTUP_ACTIVE:-}" != 1 ]; then
    export TAUCETI_LEAN_STARTUP_ACTIVE=1
    if [ -n "${TAUCETI_PREVIOUS_BASH_ENV:-}" ] && [ -f "$TAUCETI_PREVIOUS_BASH_ENV" ] && [ "$TAUCETI_PREVIOUS_BASH_ENV" != "${BASH_ENV:-}" ]; then
        . "$TAUCETI_PREVIOUS_BASH_ENV"
    fi
    if [ -n "${TAUCETI_LEAN_WRAPPER:-}" ] && [ -n "${WATCHDOG_TOOLCHAIN:-}" ] && [ -x "$WATCHDOG_TOOLCHAIN/bin/lean-real" ]; then
        BASH_ENV= "$TAUCETI_LEAN_WRAPPER" --install-toolchain "$WATCHDOG_TOOLCHAIN/bin/lean-real"
    fi
    unset TAUCETI_LEAN_STARTUP_ACTIVE
fi
"""


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
