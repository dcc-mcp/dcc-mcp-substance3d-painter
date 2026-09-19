"""Shared access to Painter's environment lighting state.

Substance 3D Painter has no light objects: all scene illumination comes from a
single image-based environment. Painter's environment module carries the French
spelling ``environnement``, and individual accessor names have changed between
releases. This helper probes a bounded set of known spellings so callers either
talk to the real host API or fail with a precise, actionable error. It never
synthesises a value or pretends an unsupported capability succeeded.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Optional

from dcc_mcp_substance3d_painter.painter_state import resolve_callable

MODULE_CANDIDATES = ("substance_painter.environnement", "substance_painter.environment")

GETTER_CANDIDATES = {
    "environment_map": ("get_environment_map", "get_environnement_map"),
    "background_texture": ("get_background_texture",),
    "exposure": ("get_exposure",),
    "rotation": ("get_rotation",),
}

SETTER_CANDIDATES = {
    "environment_map": ("set_environment_map", "set_environnement_map"),
    "background_texture": ("set_background_texture",),
    "exposure": ("set_exposure",),
    "rotation": ("set_rotation",),
}


def environment_module():
    """Import Painter's environment module, or raise ``ImportError``."""

    for name in MODULE_CANDIDATES:
        try:
            return import_module(name)
        except ImportError:
            continue
    raise ImportError(
        "Substance 3D Painter exposes no environment module (probed: " + ", ".join(MODULE_CANDIDATES) + ")"
    )


def _url_of(resource_id: Any) -> Optional[str]:
    """Return a resource URL string, or ``None`` when unset or unavailable."""

    if resource_id is None:
        return None
    getter = getattr(resource_id, "url", None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    return None if value is None else str(value)


def _read(module, capability: str):
    """Read one environment capability, or return ``None`` when unavailable."""

    getter = resolve_callable(module, GETTER_CANDIDATES[capability])
    if getter is None:
        return None
    try:
        value = getter()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    if capability in {"environment_map", "background_texture"}:
        return _url_of(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def has_getter(module, capability: str) -> bool:
    return resolve_callable(module, GETTER_CANDIDATES[capability]) is not None


def has_setter(module, capability: str) -> bool:
    return resolve_callable(module, SETTER_CANDIDATES[capability]) is not None


def read_state(module) -> dict[str, Any]:
    """Return the current environment state plus the host's capabilities.

    ``readable`` reflects whether the host exposes a getter, not whether the
    current value happens to be empty: an unset background is still readable.
    """

    values = {capability: _read(module, capability) for capability in GETTER_CANDIDATES}
    return {
        **values,
        "capabilities": {
            capability: {"readable": has_getter(module, capability), "writable": has_setter(module, capability)}
            for capability in SETTER_CANDIDATES
        },
    }


def require_setter(module, capability: str):
    """Return the setter for *capability*, or raise ``ValueError``."""

    setter = resolve_callable(module, SETTER_CANDIDATES[capability])
    if setter is None:
        raise ValueError(
            f"this Painter build exposes no setter for {capability} "
            f"(probed: {', '.join(SETTER_CANDIDATES[capability])})"
        )
    return setter


def require_getter(module, capability: str):
    """Return the getter for *capability*, or raise ``ValueError``."""

    getter = resolve_callable(module, GETTER_CANDIDATES[capability])
    if getter is None:
        raise ValueError(
            f"this Painter build exposes no getter for {capability} "
            f"(probed: {', '.join(GETTER_CANDIDATES[capability])})"
        )
    return getter
