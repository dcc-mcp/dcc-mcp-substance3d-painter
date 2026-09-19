"""Set Painter's environment rotation in turns, with host readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_environment import (
    environment_module,
    require_getter,
    require_setter,
)

_TOLERANCE = 1e-6


@skill_entry
def main(rotation: float, **_kwargs):
    try:
        resolved = float(rotation)
    except (TypeError, ValueError) as exc:
        return skill_error("Invalid Painter environment rotation", str(exc))
    if not 0.0 <= resolved <= 1.0:
        return skill_error(
            "Invalid Painter environment rotation",
            "rotation must be between 0 and 1 turns",
            rotation=resolved,
        )

    try:
        module = environment_module()
        setter = require_setter(module, "rotation")
        getter = require_getter(module, "rotation")
        setter(resolved)
        actual = float(getter())
        if abs(actual - resolved) > _TOLERANCE:
            return skill_error(
                "Painter environment rotation readback failed",
                "HOST_READBACK_MISMATCH",
                expected=resolved,
                actual=actual,
            )
    except ImportError as exc:
        return skill_error(
            "This Painter build exposes no environment lighting module",
            "environment_module_unavailable",
            detail=str(exc),
        )
    except ValueError as exc:
        return skill_error(
            "This Painter build does not support environment rotation control",
            "environment_capability_unsupported",
            detail=str(exc),
        )
    except (AttributeError, RuntimeError, TypeError) as exc:
        return skill_error("Unable to set Painter environment rotation", str(exc))

    return skill_success(
        "Set Painter environment rotation",
        rotation=actual,
        verified=True,
        postcondition={"method": "environment_rotation_readback", "expected": resolved, "actual": actual},
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
