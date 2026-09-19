"""Show or hide one Painter layer and confirm the result by host readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import find_node, node_summary, resolve_stack


@skill_entry
def main(
    layer_uid: int,
    visible: bool = True,
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
        target_stack = resolve_stack(textureset, texture_set, stack)
        node = find_node(layerstack, int(layer_uid), target_stack)
        if node is None:
            return skill_error(
                "Painter layer was not found in the selected stack",
                "HOST_LAYER_NOT_FOUND",
                layer_uid=int(layer_uid),
            )
        setter = getattr(node, "set_visible", None)
        if not callable(setter):
            return skill_error(
                "This Painter layer does not expose visibility control",
                "layer_visibility_unsupported",
                layer_uid=int(layer_uid),
            )

        setter(bool(visible))
        readback = find_node(layerstack, int(layer_uid), target_stack)
        if readback is None or bool(readback.is_visible()) is not bool(visible):
            return skill_error(
                "Painter layer visibility readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
            )
        return skill_success(
            "Set Painter layer visibility",
            node=node_summary(readback),
            visible=bool(visible),
            verified=True,
            postcondition={"method": "layer_visibility_readback", "expected": bool(visible)},
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to set Painter layer visibility", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
