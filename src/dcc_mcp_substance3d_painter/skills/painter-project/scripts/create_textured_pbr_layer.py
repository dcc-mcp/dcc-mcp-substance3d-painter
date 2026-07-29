"""Create a Painter fill layer driven by five imported PBR texture maps."""

from __future__ import annotations

from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


def _resolve_texture(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    return path


@skill_entry
def main(
    name: str,
    base_color_path: str,
    normal_path: str,
    roughness_path: str,
    metallic_path: str,
    ambient_occlusion_path: str,
    **_kwargs,
):
    try:
        texture_paths = {
            "BaseColor": _resolve_texture(base_color_path, "base_color_path"),
            "Normal": _resolve_texture(normal_path, "normal_path"),
            "Roughness": _resolve_texture(roughness_path, "roughness_path"),
            "Metallic": _resolve_texture(metallic_path, "metallic_path"),
            "AO": _resolve_texture(ambient_occlusion_path, "ambient_occlusion_path"),
        }
    except (TypeError, ValueError) as exc:
        return skill_error("Invalid Painter PBR texture paths", str(exc))

    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.resource as resource
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    stack = textureset.get_active_stack()
    if stack is None:
        return skill_error("No active Painter texture-set stack", "textureset.get_active_stack() returned None")

    channel_type = textureset.ChannelType
    channel_by_name = {
        "BaseColor": channel_type.BaseColor,
        "Normal": channel_type.Normal,
        "Roughness": channel_type.Roughness,
        "Metallic": channel_type.Metallic,
        "AO": channel_type.AO,
    }
    channel_formats = {
        "BaseColor": textureset.ChannelFormat.sRGB8,
        "Normal": textureset.ChannelFormat.RGB8,
        "Roughness": textureset.ChannelFormat.L8,
        "Metallic": textureset.ChannelFormat.L8,
        "AO": textureset.ChannelFormat.L8,
    }
    for channel_name, channel in channel_by_name.items():
        if not stack.has_channel(channel):
            stack.add_channel(channel, channel_formats[channel_name])

    imported = {
        channel_name: resource.import_project_resource(
            str(path),
            resource.Usage.TEXTURE,
            name=f"{name}_{channel_name}",
            group=str(name),
        ).identifier()
        for channel_name, path in texture_paths.items()
    }

    position = layerstack.InsertPosition.from_textureset_stack(stack)
    layer = layerstack.insert_fill(position)
    layer.set_name(str(name))
    layer.active_channels = set(channel_by_name.values())
    for channel_name, channel in channel_by_name.items():
        layer.set_source(channel, imported[channel_name])

    return skill_success(
        "Created Painter textured PBR fill layer",
        layer_name=str(name),
        layer_uid=int(layer.uid()),
        texture_files={key: str(value) for key, value in texture_paths.items()},
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
