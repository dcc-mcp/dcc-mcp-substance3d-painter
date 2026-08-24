"""Return a diffable tree for one Painter texture-set stack."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import resolve_stack, serialize_stack, stack_root_path


@skill_entry
def main(texture_set: str | None = None, stack: str | None = None, **_kwargs):
    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        layers = serialize_stack(layerstack, target_stack)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to read Painter layer stack", str(exc))
    return skill_success(
        "Listed Painter layer stack",
        stack=stack_root_path(target_stack),
        layer_count=len(layers),
        layers=layers,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
