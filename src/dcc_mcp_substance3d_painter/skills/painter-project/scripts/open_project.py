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

    opened = bool(project.is_open())
    observed_path = project.file_path() if opened else None
    readback_path = Path(observed_path).expanduser().resolve() if observed_path else None
    if not opened or readback_path != path:
        return skill_error(
            "Painter project open was not confirmed by host readback",
            f"expected {path}; observed {readback_path if readback_path is not None else 'no open project'}",
        )

    return skill_success("Opened Painter project", project_path=str(readback_path), opened=True)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
