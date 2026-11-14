"""The one switch between host operating systems.

A function marked ``@hostos.linux`` holds the systemd, cgroup or /proc
implementation. On a Mac the call goes to the function of the same name in
macos.py; everything unmarked is shared.
"""

import functools
import sys

MACOS = sys.platform == "darwin"


def roles():
    return ("workers",) if MACOS else ("workers", "ci")


def linux(function):
    @functools.wraps(function)
    def dispatch(*args, **kwargs):
        if not MACOS:
            return function(*args, **kwargs)
        from . import macos

        return getattr(macos, function.__name__)(*args, **kwargs)

    return dispatch
