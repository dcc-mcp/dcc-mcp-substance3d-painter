"""Write one uniform value into one material channel of a Painter layer."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import (
    enum_name,
    find_node,
    node_summary,
    resolve_enum_member,
    resolve_stack,
)


def _supported_names(textureset) -> list[str]:
    members = getattr(getattr(textureset, "ChannelType", None), "__members__", None)
    return sorted(members) if members else []


def _unit_interval(value: float, label: str) -> float:
    resolved = float(value)
    if not 0.0 <= resolved <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return resolved


@skill_entry
def main(
    layer_uid: int,
    channel: str,
    value: list[float],
    texture_set: str | None = None,
    stack: str | None = None,
    **_kwargs,
):
    import substance_painter.colormanagement as colormanagement  # Lazy: Painter host only.
    import substance_painter.layerstack as layerstack
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    if len(value) != 3:
        return skill_error("value must contain exactly three components", "INVALID_CHANNEL_VALUE")

    try:
        components = [_unit_interval(component, f"value[{index}]") for index, component in enumerate(value)]
    except (TypeError, ValueError) as exc:
        return skill_error("Invalid Painter channel value", str(exc))

    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        node = find_node(layerstack, int(layer_uid), target_stack)
        if node is None:
            return skill_error(
                "Painter layer was not found in the selected stack",
                "HOST_LAYER_NOT_FOUND",
                layer_uid=int(layer_uid),
            )

        member = resolve_enum_member(getattr(textureset, "ChannelType", None), channel)
        if member is None:
            return skill_error(
                "Unsupported Painter material channel",
                "channel_not_found",
                channel=str(channel),
                supported_channels=_supported_names(textureset),
                prompt="Call painter_material__inspect_material_channels to read valid channel names.",
            )

        node.set_source(member, colormanagement.Color(components[0], components[1], components[2]))

        readback = find_node(layerstack, int(layer_uid), target_stack)
        if readback is None:
            return skill_error(
                "Painter layer readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
            )
        # Painter's Python API exposes no readback for a channel source value, so
        # only channel activation can be confirmed here.
        active = {enum_name(item) for item in readback.active_channels}
        channel_name = enum_name(member)
        if channel_name not in active:
            return skill_error(
                "Painter layer channel activation readback failed",
                "HOST_READBACK_MISMATCH",
                layer_uid=int(layer_uid),
                channel=channel_name,
                active_channels=sorted(active),
            )
        return skill_success(
            "Set Painter layer channel value",
            node=node_summary(readback),
            channel=channel_name,
            value=components,
            active_channels=sorted(active),
            verified=False,
            postcondition={
                "method": "layer_active_channels_readback",
                "verifies": "channel_activation_only",
            },
            prompt="Painter exposes no readback for channel source values; export the texture set to confirm the result.",
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to set Painter layer channel value", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
