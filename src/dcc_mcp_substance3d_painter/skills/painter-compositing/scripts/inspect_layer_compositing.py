"""Inspect the compositing state of one Substance 3D Painter layer."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import (
    enum_members,
    enum_name,
    find_node,
    node_summary,
    resolve_stack,
)


def _blending_mode_names(layerstack) -> list[str]:
    """Return the blending-mode member names the running host exposes."""

    for attribute in ("BlendingMode", "BlendMode"):
        members = enum_members(getattr(layerstack, attribute, None))
        if members:
            return sorted(members)
    return []


def _read(node, getter_name: str):
    getter = getattr(node, getter_name, None)
    if not callable(getter):
        return None
    try:
        return getter()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


@skill_entry
def main(
    layer_uid: int,
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

        opacity = _read(node, "get_opacity")
        blending_mode = _read(node, "get_blending_mode")
        has_mask = bool(node.has_mask())
        mask = {
            "has_mask": has_mask,
            "enabled": bool(node.is_mask_enabled()) if has_mask else None,
            "background": enum_name(node.get_mask_background()) if has_mask else None,
        }
        return skill_success(
            "Inspected Painter layer compositing state",
            node=node_summary(node),
            visible=bool(node.is_visible()),
            opacity=float(opacity) if opacity is not None else None,
            blending_mode=enum_name(blending_mode) if blending_mode is not None else None,
            mask=mask,
            capabilities={
                "visibility": callable(getattr(node, "set_visible", None)),
                "opacity": callable(getattr(node, "set_opacity", None)),
                "blending_mode": callable(getattr(node, "set_blending_mode", None)),
            },
            supported_blending_modes=_blending_mode_names(layerstack),
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to inspect Painter layer compositing state", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
