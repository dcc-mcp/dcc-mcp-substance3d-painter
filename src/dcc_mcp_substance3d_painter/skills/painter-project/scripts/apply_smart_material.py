"""Apply an existing Painter smart material to the active texture set."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(resource_url: str, layer_name: str | None = None, **_kwargs):
    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.resource as resource
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")

    stack = textureset.get_active_stack()
    if stack is None:
        return skill_error("No active Painter texture-set stack", "textureset.get_active_stack() returned None")

    try:
        identifier = resource.ResourceID.from_url(str(resource_url))
        position = layerstack.InsertPosition.from_textureset_stack(stack)
        group = layerstack.insert_smart_material(position, identifier)
        if layer_name:
            group.set_name(str(layer_name))
    except (RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to apply Painter smart material", str(exc))

    return skill_success(
        "Applied Painter smart material",
        resource_url=str(resource_url),
        layer_name=group.get_name(),
        layer_uid=int(group.uid()),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
