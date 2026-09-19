"""Replace the material channels a Painter layer contributes, with host readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import (
    enum_name,
    find_node,
    node_summary,
    resolve_enum_member,
    resolve_stack,
)


def _resolve_channels(textureset, names) -> list[object]:
    """Resolve channel names against the host ``textureset.ChannelType`` enum."""

    channel_type = getattr(textureset, "ChannelType", None)
    supported = sorted(str(name) for name in _supported_names(textureset))
    resolved = []
    for name in names:
        member = resolve_enum_member(channel_type, name)
        if member is None:
            raise ValueError(f"unknown channel {str(name)!r}; supported channels: {', '.join(supported)}")
        resolved.append(member)
    return resolved


def _supported_names(textureset) -> list[str]:
    members = getattr(getattr(textureset, "ChannelType", None), "__members__", None)
    return sorted(members) if members else []


@skill_entry
def main(
    layer_uid: int,
    channels: list[str],
    texture_set: str | None = None,
    stack: str | None = None,
    **_kwargs,
):
    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    if not channels:
        return skill_error("Invalid Painter channel set", "channels must not be empty")

    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        node = find_node(layerstack, int(layer_uid), target_stack)
        if node is None:
            return skill_error(
                "Painter layer was not found in the selected stack",
                "HOST_LAYER_NOT_FOUND",
                layer_uid=int(layer_uid),
            )

        members = _resolve_channels(textureset, channels)
        node.active_channels = set(members)

        readback = find_node(layerstack, int(layer_uid), target_stack)
        if readback is None:
            return skill_error(
                "Painter layer channel readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
            )
        expected = {enum_name(member) for member in members}
        actual = {enum_name(member) for member in readback.active_channels}
        if actual != expected:
            return skill_error(
                "Painter layer channel readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
                expected=sorted(expected),
                actual=sorted(actual),
            )
        return skill_success(
            "Set Painter layer material channels",
            node=node_summary(readback),
            channels=sorted(actual),
            verified=True,
            postcondition={
                "method": "layer_active_channels_readback",
                "expected": sorted(expected),
                "actual": sorted(actual),
            },
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to set Painter layer material channels", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
