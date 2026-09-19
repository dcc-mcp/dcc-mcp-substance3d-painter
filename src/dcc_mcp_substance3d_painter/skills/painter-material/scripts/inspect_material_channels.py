"""List the material channels carried by a Painter texture-set stack."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import enum_members, enum_name, resolve_stack


def _channel_inventory(stack) -> list[dict[str, object]]:
    channels = []
    for channel_type, channel in stack.all_channels().items():
        channels.append(
            {
                "name": enum_name(channel_type),
                "label": str(channel.label()),
                "format": enum_name(channel.format()),
                "bit_depth": int(channel.bit_depth()),
                "is_color": bool(channel.is_color()),
            }
        )
    return sorted(channels, key=lambda item: str(item["name"]))


@skill_entry
def main(
    texture_set: str | None = None,
    stack: str | None = None,
    **_kwargs,
):
    import substance_painter.project as project  # Lazy: Painter host only.
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")

    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        return skill_success(
            "Inspected Painter material channels",
            stack=str(target_stack),
            channel_count=len(_channel_inventory(target_stack)),
            channels=_channel_inventory(target_stack),
            available_channel_types=sorted(enum_members(getattr(textureset, "ChannelType", None))),
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to inspect Painter material channels", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
