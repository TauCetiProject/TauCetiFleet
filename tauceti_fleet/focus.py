"""Live, advisory roadmap allocation; never changes admission or worker definitions."""

import re

from .store import FleetError, host_config, update_host

# Active roadmaps at TauCetiRoadmap fa41b374 (2026-11-01). Include direct
# analytic/geometric foundations, but not every transitive algebraic dependency.
PRESETS = {
    "pde-symplectic": [
        "AffineMorseLefschetz",
        "BanachSpaceTheory",
        "CombinatorialHeegaardFloer",
        "CompactOperators",
        "ComplexKahlerGeometry",
        "ComplexManifolds",
        "DGFloer",
        "DifferentialGeometry",
        "GeometricCurrents",
        "GeometricMeasureTheory",
        "HamiltonianFloerOperations",
        "HeegaardFloer",
        "Hyperfunctions",
        "Isoperimetry",
        "MorseNovikov",
        "MorseTheory",
        "OperatorTheory",
        "OptimalTransport",
        "PDE",
        "PositiveScalarCurvature",
        "QuaternionicAnalysis",
        "RiemannianGeometry",
        "SeibergWittenTheory",
        "SmoothBundlesConnections",
        "SpinGeometry",
        "SymplecticGeometry",
        "TaubesGromov",
        "Yamabe",
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
    focus = validate(**config.get("roadmap_focus", dict(percent=0, areas=[])))
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
        value = validate(payload.get("percent", 20), PRESETS[preset] if preset else payload["areas"])
    elif "percent" in payload:
        value = validate(payload["percent"], host_config().get("roadmap_focus", {}).get("areas", []))
    else:
        return host_config().get("roadmap_focus", dict(percent=0, areas=[]))
    update_host(roadmap_focus=value)
    return value
