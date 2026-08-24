"""Insert a typed paint layer and verify it through Painter's layer model."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import (
    find_node,
    node_summary,
    resolve_insert_position,
    resolve_stack,
)


@skill_entry
def main(
    name: str,
    placement: str = "top",
    relative_to_uid: int | None = None,
    texture_set: str | None = None,
    stack: str | None = None,
    **_kwargs,
):
    resolved_name = str(name).strip()
    if not 1 <= len(resolved_name) <= 128:
        return skill_error("Invalid Painter layer name", "name must contain between 1 and 128 characters")

    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        position = resolve_insert_position(layerstack, target_stack, placement, relative_to_uid)
        inserted = layerstack.insert_paint(position)
        inserted.set_name(resolved_name)
        readback = find_node(layerstack, int(inserted.uid()), target_stack)
        if readback is None or str(readback.get_name()) != resolved_name:
            return skill_error("Painter paint-layer readback failed", "HOST_READBACK_MISMATCH")
        if str(getattr(readback.get_type(), "name", readback.get_type())) not in {"PaintLayer", "PaintLayerNode"}:
            return skill_error("Painter paint-layer readback failed", "HOST_READBACK_TYPE_MISMATCH")
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to insert Painter paint layer", str(exc))
    return skill_success("Inserted Painter paint layer", node=node_summary(readback))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
