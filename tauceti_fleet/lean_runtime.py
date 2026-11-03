"""Install the worker's standalone pressure gate on hosts and in fresh CI guests."""

import shlex
from pathlib import Path

from . import lean_guest, lean_pressure

# Disposable guests install toolchains during a job, before later sandbox steps
# clear the environment. Host workers need only the scoped PATH alias and overlay.
STARTUP = """if [ -n "${TAUCETI_LEAN_WRAPPER:-}" ] && [ -n "${WATCHDOG_TOOLCHAIN:-}" ] && [ -L "$WATCHDOG_TOOLCHAIN/bin/lean-real" ] && [ ! -e "$WATCHDOG_TOOLCHAIN/bin/lean-watchdog" ]; then
    BASH_ENV= "$TAUCETI_LEAN_WRAPPER" --install-watchdog "$WATCHDOG_TOOLCHAIN/bin/lean-real" ||
        printf '%s\\n' '::warning::Lean pressure gate could not wrap the watchdog; continuing without the gate' >&2
fi
"""


def ci_commands():
    binary = "/usr/local/bin/tauceti-lean"
    startup = "/opt/tauceti-lean-env"
    receiver = "/usr/local/bin/tauceti-host-pressure"
    unit = ("[Unit]\nDescription=Read host pressure for Lean admission\nStartLimitIntervalSec=0\n\n[Service]\n"
            "ExecStart=/usr/bin/python3 " + receiver + "\nRestart=always\nRestartSec=5\n"
            "\n[Install]\nWantedBy=multi-user.target\n")
    return [
        "install -d -m 755 /usr/local/share/tauceti; touch /usr/local/share/tauceti/host-pressure.required",
        "printf %s " + shlex.quote(Path(lean_guest.__file__).read_text()) + " > " + receiver,
        "printf %s " + shlex.quote(unit) + " > /etc/systemd/system/tauceti-host-pressure.service; "
        "systemctl daemon-reload; systemctl enable --now tauceti-host-pressure.service",
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
