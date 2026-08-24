"""Insert a resource-backed generator into a content or mask effect stack."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import (
    find_node,
    resolve_stack,
    serialize_effect,
    validate_resource,
)


@skill_entry
def main(
    target_uid: int,
    target_stack: str,
    resource_url: str,
    texture_set: str | None = None,
    stack: str | None = None,
    **_kwargs,
):
    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.resource as resource
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    resolved_target_stack = str(target_stack).strip().lower()
    if resolved_target_stack not in {"content", "mask"}:
        return skill_error("Unsupported generator target stack", "target_stack must be content or mask")
    try:
        selected_stack = resolve_stack(textureset, texture_set, stack)
        target = find_node(layerstack, int(target_uid), selected_stack)
        if target is None:
            raise ValueError("The target layer was not found in the selected stack")
        if resolved_target_stack == "mask" and not bool(target.has_mask()):
            raise ValueError("The target layer has no mask; add a mask before inserting a mask generator")
        identifier = resource.ResourceID.from_url(str(resource_url))
        validate_resource(resource, identifier, "GENERATOR")
        node_stack = layerstack.NodeStack.Mask if resolved_target_stack == "mask" else layerstack.NodeStack.Content
        position = layerstack.InsertPosition.inside_node(target, node_stack)
        effect = layerstack.insert_generator_effect(position, identifier)
        readback_target = find_node(layerstack, int(target_uid), selected_stack)
        effects = (
            list(readback_target.mask_effects())
            if resolved_target_stack == "mask"
            else list(readback_target.content_effects())
        )
        readback = next((item for item in effects if int(item.uid()) == int(effect.uid())), None)
        if readback is None:
            raise RuntimeError("HOST_READBACK_GENERATOR_MISSING")
        try:
            readback_url = str(readback.get_source().resource_id.url())
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError("HOST_READBACK_GENERATOR_SOURCE_MISSING") from exc
        if readback_url != str(identifier.url()):
            raise RuntimeError("HOST_READBACK_GENERATOR_SOURCE_MISMATCH")
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to add Painter generator effect", str(exc))
    return skill_success(
        "Added Painter generator effect",
        target_uid=int(target_uid),
        target_stack=resolved_target_stack,
        effect=serialize_effect(readback),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
