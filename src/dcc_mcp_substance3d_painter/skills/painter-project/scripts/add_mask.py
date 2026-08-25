"""Add a bounded black, white, or smart mask with host readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import (
    enum_name,
    find_node,
    resolve_stack,
    serialize_effect,
    validate_resource,
)

_HOST_ERROR_CODES = {
    "HOST_READBACK_MASK_BACKGROUND_MISMATCH",
    "HOST_READBACK_MASK_MISSING",
    "HOST_READBACK_SMART_MASK_MISMATCH",
    "HOST_SMART_MASK_INSERT_EMPTY",
}


def _cleanup_added_mask(layerstack, target, layer_uid: int, target_stack) -> dict[str, object]:
    cleanup: dict[str, object] = {"attempted": True, "status": "unconfirmed"}
    try:
        target.remove_mask()
        readback = find_node(layerstack, layer_uid, target_stack)
        if readback is not None and not bool(readback.has_mask()):
            cleanup["status"] = "confirmed"
    except Exception:
        pass
    return cleanup


def _error_code(exc: Exception) -> str:
    try:
        args = exc.args
        detail = args[0] if isinstance(args, tuple) and len(args) == 1 and isinstance(args[0], str) else None
    except Exception:
        return "PAINTER_MASK_OPERATION_FAILED"
    if detail in _HOST_ERROR_CODES:
        return detail
    return "PAINTER_MASK_OPERATION_FAILED"


@skill_entry
def main(
    layer_uid: int,
    kind: str,
    resource_url: str | None = None,
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
    resolved_kind = str(kind).strip().lower()
    if resolved_kind not in {"black", "white", "smart"}:
        return skill_error("Unsupported Painter mask kind", "kind must be black, white, or smart")

    added_plain_mask = False
    target = None
    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        target = find_node(layerstack, int(layer_uid), target_stack)
        if target is None:
            raise ValueError("The target layer was not found in the selected stack")
        if bool(target.has_mask()):
            raise ValueError("The target layer already has a mask")

        inserted_effects = []
        if resolved_kind in {"black", "white"}:
            background = getattr(layerstack.MaskBackground, resolved_kind.capitalize())
            target.add_mask(background)
            added_plain_mask = True
        else:
            if not resource_url:
                raise ValueError("resource_url is required for a smart mask")
            identifier = resource.ResourceID.from_url(str(resource_url))
            validate_resource(resource, identifier, "SMART_MASK")
            target.add_mask(layerstack.MaskBackground.Black)
            added_plain_mask = True
            position = layerstack.InsertPosition.inside_node(target, layerstack.NodeStack.Mask)
            inserted_effects = list(layerstack.insert_smart_mask(position, identifier))
            if not inserted_effects:
                raise RuntimeError("HOST_SMART_MASK_INSERT_EMPTY")

        readback = find_node(layerstack, int(layer_uid), target_stack)
        if readback is None or not bool(readback.has_mask()):
            raise RuntimeError("HOST_READBACK_MASK_MISSING")
        background_name = enum_name(readback.get_mask_background())
        if resolved_kind in {"black", "white"} and background_name.lower() != resolved_kind:
            raise RuntimeError("HOST_READBACK_MASK_BACKGROUND_MISMATCH")
        readback_effects = list(readback.mask_effects())
        inserted_ids = {int(effect.uid()) for effect in inserted_effects}
        if inserted_ids and not inserted_ids.issubset({int(effect.uid()) for effect in readback_effects}):
            raise RuntimeError("HOST_READBACK_SMART_MASK_MISMATCH")
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        cleanup: dict[str, object] = {"attempted": False, "status": "not_needed"}
        if added_plain_mask and target is not None:
            cleanup = _cleanup_added_mask(layerstack, target, int(layer_uid), target_stack)
        return skill_error("Unable to add Painter mask", _error_code(exc), cleanup=cleanup)

    return skill_success(
        "Added Painter mask",
        layer_uid=int(layer_uid),
        kind=resolved_kind,
        background=background_name,
        effects=[serialize_effect(effect) for effect in readback_effects],
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
