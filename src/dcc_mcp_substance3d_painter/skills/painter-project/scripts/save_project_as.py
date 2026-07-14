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

    return skill_success(
        "Saved Painter project",
        project_path=str(output),
        exists=output.exists(),
        size_bytes=output.stat().st_size if output.exists() else 0,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
