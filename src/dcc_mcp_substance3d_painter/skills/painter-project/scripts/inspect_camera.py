"""Inspect the default Substance 3D Painter camera."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(**_kwargs):
    import substance_painter.display as display  # Lazy: Painter host only.
    import substance_painter.project as project

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    camera = display.Camera.get_default_camera()
    return skill_success(
        "Inspected Painter camera",
        position=list(camera.position),
        rotation=list(camera.rotation),
        field_of_view=float(camera.field_of_view),
        projection_type=str(camera.projection_type),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
