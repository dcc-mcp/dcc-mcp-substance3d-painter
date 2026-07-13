"""Inspect a running Substance 3D Painter project."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(**_kwargs):
    import substance_painter.project as project  # Lazy import: requires Painter.

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    return skill_success("Inspected Substance 3D Painter project", file_path=str(project.file_path()))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
