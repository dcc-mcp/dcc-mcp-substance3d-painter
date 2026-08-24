"""Save the active Painter project to an explicit .spp path."""

from __future__ import annotations

from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(project_path: str, **_kwargs):
    import substance_painter.project as project  # Lazy: Painter host only.

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    output = Path(project_path).expanduser().resolve()
    if output.suffix.casefold() != ".spp":
        return skill_error("Painter project_path must end with .spp", "INVALID_PROJECT_EXTENSION")
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        project.save_as(str(output))
    except (RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to save Painter project", str(exc))

    observed_path = project.file_path()
    readback_path = Path(observed_path).expanduser().resolve() if observed_path else None
    needs_saving = bool(project.needs_saving())
    if readback_path != output or needs_saving or not output.is_file():
        return skill_error(
            "Painter project save-as was not confirmed by host readback",
            f"expected {output}; observed {readback_path}, path_exists={output.is_file()}, needs_saving={needs_saving}",
        )
    return skill_success(
        "Saved Painter project",
        project_path=str(readback_path),
        exists=True,
        size_bytes=output.stat().st_size,
        needs_saving=False,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
