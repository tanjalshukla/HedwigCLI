from __future__ import annotations

OPTIONS = ["balanced", "hands-on", "delegating"]

_DESCRIPTIONS = {
    "balanced":   "standard — checks in on meaningful risk",
    "hands-on":   "closer oversight — Hedwig asks more",
    "delegating": "lighter touch — gets out of the way",
}

_TO_INTENSITY = {
    "balanced":   None,
    "hands-on":   "active",
    "delegating": "delegating",
}
_FROM_INTENSITY: dict[str | None, str] = {v: k for k, v in _TO_INTENSITY.items()}
_FROM_INTENSITY[None] = "balanced"


def _label_from_intensity(intensity: str | None) -> str:
    return _FROM_INTENSITY.get(intensity, "balanced")


def run_toggle(current_intensity: str | None) -> str | None:
    """No-op — widget removed. Returns current unchanged."""
    return current_intensity
