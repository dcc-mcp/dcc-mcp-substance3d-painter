"""Inspect a running Substance 3D Painter project."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import enum_name, serialize_stack, stack_root_path


def _ocio_environment():
    raw_path = os.environ.get("OCIO", "").strip()
    path = Path(raw_path) if raw_path else None
    return {
        "path": raw_path or None,
        "exists": bool(path and path.is_file()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None,
    }


def _resolution(value):
    return {"width": int(value.width), "height": int(value.height)}


def _channel_state(stack):
    channels = []
    for channel_type, channel in stack.all_channels().items():
        channels.append(
            {
                "type": enum_name(channel_type),
                "label": str(channel.label()),
                "format": enum_name(channel.format()),
                "bit_depth": int(channel.bit_depth()),
                "is_color": bool(channel.is_color()),
            }
        )
    return sorted(channels, key=lambda item: item["type"])


def _texture_set_state(texture_set, textureset, layerstack):
    uv_tiles = []
    if bool(texture_set.has_uv_tiles()):
        for tile in texture_set.all_uv_tiles():
            uv_tiles.append(
                {
                    "u": int(tile.u),
                    "v": int(tile.v),
                    "resolution": _resolution(tile.get_resolution()),
                    "mesh_names": sorted(str(name) for name in tile.all_mesh_names()),
                }
            )
    mesh_maps = {}
    members = getattr(textureset.MeshMapUsage, "__members__", {})
    for name, usage in members.items():
        resource_id = texture_set.get_mesh_map_resource(usage)
        if resource_id is not None:
            mesh_maps[str(name)] = str(resource_id.url())
    stacks = []
    for stack in texture_set.all_stacks():
        stacks.append(
            {
                "name": stack_root_path(stack),
                "channels": _channel_state(stack),
                "layers": serialize_stack(layerstack, stack),
            }
        )
    return {
        "name": str(texture_set.name()),
        "resolution": _resolution(texture_set.get_resolution()),
        "mesh_names": sorted(str(name) for name in texture_set.all_mesh_names()),
        "uv_tiles": uv_tiles,
        "mesh_maps": mesh_maps,
        "stacks": stacks,
    }


@skill_entry
def main(**_kwargs):
    import substance_painter.colormanagement as colormanagement
    import substance_painter.display as display
    import substance_painter.layerstack as layerstack
    import substance_painter.project as project  # Lazy import: requires Painter.
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    try:
        tone_mapping = str(display.get_tone_mapping())
        color_managed = False
        working_probe = None
    except RuntimeError:
        color_managed = True
        tone_mapping = None
        working_probe = list(
            colormanagement.Color(
                0.18,
                0.18,
                0.18,
                colormanagement.GenericColorSpace.sRGB,
            ).working
        )
    try:
        texture_sets = [
            _texture_set_state(texture_set, textureset, layerstack) for texture_set in textureset.all_texture_sets()
        ]
        dirty = bool(project.needs_saving())
        busy = bool(project.is_busy())
        editable = bool(project.is_in_edition_state())
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to inspect Painter project state", str(exc))
    return skill_success(
        "Inspected Substance 3D Painter project",
        file_path=str(project.file_path()),
        project_name=str(project.name()),
        dirty=dirty,
        busy=busy,
        editable=editable,
        texture_sets=texture_sets,
        color_management={
            "enabled": color_managed,
            "ocio_environment": _ocio_environment(),
            "legacy_tone_mapping": tone_mapping,
            "srgb_18_to_working_probe": working_probe,
        },
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
