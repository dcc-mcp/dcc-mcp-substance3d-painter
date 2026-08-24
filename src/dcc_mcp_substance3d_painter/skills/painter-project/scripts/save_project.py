"""Save the active Substance 3D Painter project with verified readback."""

from __future__ import annotations

from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

_SAVE_MODES = {"incremental": "Incremental", "full": "Full"}


@skill_entry
def main(mode: str = "incremental", **_kwargs):
    import substance_painter.project as project  # Lazy: Painter host only.

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    mode_name = _SAVE_MODES.get(str(mode).strip().lower())
    if mode_name is None:
        return skill_error("Unsupported Painter save mode", "mode must be incremental or full")

    try:
        project.save(getattr(project.ProjectSaveMode, mode_name))
    except (RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to save Painter project", str(exc))

    observed_path = project.file_path()
    project_path = Path(observed_path).expanduser().resolve() if observed_path else None
    needs_saving = bool(project.needs_saving())
    path_exists = project_path is not None and project_path.is_file()
    if needs_saving or not path_exists:
        return skill_error(
            "Painter project save was not confirmed by host readback",
            f"path_exists={path_exists}, needs_saving={needs_saving}",
        )
    return skill_success(
        "Saved Painter project",
        project_path=str(project_path),
        needs_saving=False,
        mode=str(mode).strip().lower(),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
