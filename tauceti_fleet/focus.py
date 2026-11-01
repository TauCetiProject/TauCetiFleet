"""Live, advisory roadmap allocation; never changes admission or worker definitions."""

import re

from .store import FleetError, host_config, update_host

# Initial direct PDE / symplectic-topology pool. Operators can replace it with
# any roadmap list through --areas; supporting areas remain in ordinary selection.
PRESETS = {
    "pde-symplectic": [
        "DGFloer",
        "HamiltonianFloerOperations",
        "HeegaardFloer",
        "PDE",
        "SymplecticGeometry",
        "TaubesGromov",
    ]
}


def validate(percent, areas):
    if type(percent) is not int or not 0 <= percent <= 100:
        raise FleetError("Focus percent must be an integer from 0 to 100")
    if not isinstance(areas, list) or not all(isinstance(a, str) and re.fullmatch(r"[A-Za-z0-9_-]+", a) for a in areas):
        raise FleetError("Focus areas must be a list of roadmap directory names")
    areas = sorted(set(areas))
    if percent and not areas:
        raise FleetError("Nonzero focus percent requires at least one area")
    return dict(percent=percent, areas=areas)


def policy(config, workers):
    raw = config.get("roadmap_focus", {})
    try:
        if not isinstance(raw, dict):
            raise FleetError("Focus configuration must be an object")
        # Ignore newer optional keys while validating the fields this release uses.
        focus = validate(raw.get("percent", 0), raw.get("areas", []))
    except FleetError as exc:
        return dict(percent=0, areas=[], workers=[], fractional_worker=None, fractional_percent=0, error=str(exc))
    percent = focus["percent"]
    # Spread selected slots through the admission order: 20% selects 5,10,15,...
    # Every prefix has floor(n * percent / 100) slots; ceiling changes keep
    # existing whole-slot assignments stable. A fractional slot supplies the
    # remainder probabilistically each round, including on tiny fleets.
    selected = [w for n, w in enumerate(workers, 1) if n * percent // 100 > (n - 1) * percent // 100]
    remainder = len(workers) * percent % 100
    fractional = next((w for w in reversed(workers) if w not in selected), None) if remainder else None
    return dict(**focus, workers=selected, fractional_worker=fractional, fractional_percent=remainder)


def configure(payload):
    if payload.get("clear"):
        value = validate(0, [])
    elif payload.get("preset") is not None or payload.get("areas") is not None:
        if payload.get("preset") is not None and payload.get("areas") is not None:
            raise FleetError("Select a preset or explicit areas")
        preset = payload.get("preset")
        if preset is not None and preset not in PRESETS:
            raise FleetError(f"Unknown focus preset: {preset}")
        current = policy(host_config(), [])
        value = validate(
            payload.get("percent", current["percent"] or 20), PRESETS[preset] if preset else payload["areas"]
        )
    elif "percent" in payload:
        value = validate(payload["percent"], policy(host_config(), [])["areas"])
    else:
        return host_config().get("roadmap_focus", dict(percent=0, areas=[]))
    update_host(roadmap_focus=value)
    return value
