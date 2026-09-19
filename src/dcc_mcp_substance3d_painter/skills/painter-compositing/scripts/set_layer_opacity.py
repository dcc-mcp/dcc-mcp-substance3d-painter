"""Set one Painter layer's opacity and confirm the result by host readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import find_node, node_summary, resolve_stack

_TOLERANCE = 1e-6


@skill_entry
def main(
    layer_uid: int,
    opacity: float,
    texture_set: str | None = None,
    stack: str | None = None,
    **_kwargs,
):
    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")

    try:
        resolved_opacity = float(opacity)
    except (TypeError, ValueError) as exc:
        return skill_error("Invalid Painter layer opacity", str(exc))
    if not 0.0 <= resolved_opacity <= 1.0:
        return skill_error(
            "Invalid Painter layer opacity",
            "opacity must be between 0 and 1",
            opacity=resolved_opacity,
        )

    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        node = find_node(layerstack, int(layer_uid), target_stack)
        if node is None:
            return skill_error(
                "Painter layer was not found in the selected stack",
                "HOST_LAYER_NOT_FOUND",
                layer_uid=int(layer_uid),
            )
        setter = getattr(node, "set_opacity", None)
        getter = getattr(node, "get_opacity", None)
        if not callable(setter) or not callable(getter):
            return skill_error(
                "This Painter layer does not expose opacity control",
                "layer_opacity_unsupported",
                layer_uid=int(layer_uid),
            )

        setter(resolved_opacity)
        readback = find_node(layerstack, int(layer_uid), target_stack)
        if readback is None:
            return skill_error(
                "Painter layer opacity readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
            )
        # Read back through the freshly resolved handle, not the pre-write
        # binding, otherwise the verification cannot detect a stale write.
        actual = float(readback.get_opacity())
        if abs(actual - resolved_opacity) > _TOLERANCE:
            return skill_error(
                "Painter layer opacity readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
                expected=resolved_opacity,
                actual=actual,
            )
        return skill_success(
            "Set Painter layer opacity",
            node=node_summary(readback),
            opacity=actual,
            verified=True,
            postcondition={
                "method": "layer_opacity_readback",
                "expected": resolved_opacity,
                "actual": actual,
            },
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to set Painter layer opacity", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
