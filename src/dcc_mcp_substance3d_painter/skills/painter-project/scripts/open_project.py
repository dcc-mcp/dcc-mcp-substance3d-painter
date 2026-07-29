"""Open an existing Substance 3D Painter project."""

from __future__ import annotations

from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(project_path: str, **_kwargs):
    path = Path(project_path).expanduser().resolve()
    if path.suffix.casefold() != ".spp":
        return skill_error("Painter project_path must end with .spp", "INVALID_PROJECT_EXTENSION")
    if not path.is_file():
        return skill_error("Painter project does not exist", str(path))

    import substance_painter.project as project  # Lazy: Painter host only.

    if project.is_open():
        return skill_error("A Painter project is already open", "Close it before opening another project")
    try:
        project.open(str(path))
    except (RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to open Painter project", str(exc))

    return skill_success("Opened Painter project", project_path=str(path), opened=project.is_open())


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
