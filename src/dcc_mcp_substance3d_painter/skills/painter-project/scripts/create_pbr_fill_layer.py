"""Create a named PBR fill layer in the active Painter texture-set stack."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


def _unit_interval(value: float, label: str) -> float:
    resolved = float(value)
    if not 0.0 <= resolved <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return resolved


@skill_entry
def main(
    name: str,
    base_color: list[float],
    metallic: float = 0.0,
    roughness: float = 0.5,
    **_kwargs,
):
    import substance_painter.colormanagement as colormanagement  # Lazy: Painter host only.
    import substance_painter.layerstack as layerstack
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    if len(base_color) != 3:
        return skill_error("base_color must contain exactly three values", "INVALID_BASE_COLOR")

    try:
        red, green, blue = (
            _unit_interval(component, f"base_color[{index}]")
            for index, component in enumerate(base_color)
        )
        metallic_value = _unit_interval(metallic, "metallic")
        roughness_value = _unit_interval(roughness, "roughness")
    except (TypeError, ValueError) as exc:
        return skill_error("Invalid PBR fill values", str(exc))

    stack = textureset.get_active_stack()
    if stack is None:
        return skill_error("No active Painter texture-set stack", "textureset.get_active_stack() returned None")

    position = layerstack.InsertPosition.from_textureset_stack(stack)
    layer = layerstack.insert_fill(position)
    layer.set_name(str(name))

    channel_type = textureset.ChannelType
    layer.active_channels = {channel_type.BaseColor, channel_type.Metallic, channel_type.Roughness}
    layer.set_source(channel_type.BaseColor, colormanagement.Color(red, green, blue))
    layer.set_source(
        channel_type.Metallic,
        colormanagement.Color(metallic_value, metallic_value, metallic_value),
    )
    layer.set_source(
        channel_type.Roughness,
        colormanagement.Color(roughness_value, roughness_value, roughness_value),
    )

    return skill_success(
        "Created Painter PBR fill layer",
        layer_name=str(name),
        layer_uid=int(layer.uid()),
        base_color=[red, green, blue],
        metallic=metallic_value,
        roughness=roughness_value,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
