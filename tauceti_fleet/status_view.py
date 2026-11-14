"""Draw the status view from per-host status objects alone, as a status export provides them."""

import contextlib
import io

from .cli import main


def render(objects):
    """Return the `status` text for {label: per-host `status --json` object}.

    Reads nothing but the objects: no host configuration, cached observations or network.
    """
    output = io.StringIO()
    if objects:
        with contextlib.redirect_stdout(output):
            main(["status"], _status_results=objects, _standalone=True)
    return output.getvalue()
