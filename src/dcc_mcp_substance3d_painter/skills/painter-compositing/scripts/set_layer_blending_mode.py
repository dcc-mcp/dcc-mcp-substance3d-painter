"""Set one Painter layer's blending mode and confirm the result by host readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import (
    enum_members,
    enum_name,
    find_node,
    node_summary,
    resolve_enum_member,
    resolve_stack,
)

_BLENDING_ENUM_ATTRIBUTES = ("BlendingMode", "BlendMode")


def _resolve_member(layerstack, name: str):
    """Resolve a blending-mode member against whichever enum the host exposes."""

    for attribute in _BLENDING_ENUM_ATTRIBUTES:
        enum_type = getattr(layerstack, attribute, None)
        member = resolve_enum_member(enum_type, name)
        if member is not None:
            return member
    return None


def _supported_names(layerstack) -> list[str]:
    for attribute in _BLENDING_ENUM_ATTRIBUTES:
        members = enum_members(getattr(layerstack, attribute, None))
        if members:
            return sorted(members)
    return []


@skill_entry
def main(
    layer_uid: int,
    blending_mode: str,
    texture_set: str | None = None,
    stack: str | None = None,
    **_kwargs,
):
    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")

    requested = str(blending_mode).strip()
    if not requested:
        return skill_error("Invalid Painter blending mode", "blending_mode must not be empty")

    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        node = find_node(layerstack, int(layer_uid), target_stack)
        if node is None:
            return skill_error(
                "Painter layer was not found in the selected stack",
                "HOST_LAYER_NOT_FOUND",
                layer_uid=int(layer_uid),
            )
        setter = getattr(node, "set_blending_mode", None)
        getter = getattr(node, "get_blending_mode", None)
        if not callable(setter) or not callable(getter):
            return skill_error(
                "This Painter layer does not expose blending-mode control",
                "layer_blending_mode_unsupported",
                layer_uid=int(layer_uid),
            )

        member = _resolve_member(layerstack, requested)
        if member is None:
            return skill_error(
                "Unsupported Painter blending mode",
                "blending_mode_not_found",
                blending_mode=requested,
                supported_blending_modes=_supported_names(layerstack),
                prompt="Call painter_compositing__inspect_layer_compositing to read valid blending-mode names.",
            )

        setter(member)
        readback = find_node(layerstack, int(layer_uid), target_stack)
        if readback is None:
            return skill_error(
                "Painter blending-mode readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
            )
        # Read back through the freshly resolved handle, not the pre-write
        # binding, otherwise the verification cannot detect a stale write.
        actual = enum_name(readback.get_blending_mode())
        if actual.casefold() != enum_name(member).casefold():
            return skill_error(
                "Painter blending-mode readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
                expected=enum_name(member),
                actual=actual,
            )
        return skill_success(
            "Set Painter layer blending mode",
            node=node_summary(readback),
            blending_mode=actual,
            verified=True,
            postcondition={
                "method": "layer_blending_mode_readback",
                "expected": enum_name(member),
                "actual": actual,
            },
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to set Painter layer blending mode", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
