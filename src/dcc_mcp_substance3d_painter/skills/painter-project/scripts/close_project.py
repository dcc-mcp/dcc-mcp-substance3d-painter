"""Close the active saved Substance 3D Painter project."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(**_kwargs):
    import substance_painter.project as project  # Lazy: Painter host only.

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    if project.needs_saving():
        return skill_error("Painter project has unsaved changes", "Save the project before closing it")
    try:
        project.close()
    except (RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to close Painter project", str(exc))

    if project.is_open():
        return skill_error(
            "Painter project close was not confirmed by host readback",
            "project.is_open() remained True after project.close()",
        )

    return skill_success("Closed Painter project", closed=True)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
