"""Create a Substance 3D Painter project from a mesh."""

from __future__ import annotations

from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

SUPPORTED_RESOLUTIONS = {128, 256, 512, 1024, 2048, 4096}


@skill_entry
def main(
    mesh_path: str,
    project_path: str,
    resolution: int = 1024,
    normal_map_format: str = "DirectX",
    **_kwargs,
):
    mesh = Path(mesh_path).expanduser().resolve()
    if not mesh.is_file():
        return skill_error("Painter mesh does not exist", str(mesh))
    output = Path(project_path).expanduser().resolve()
    if output.suffix.casefold() != ".spp":
        return skill_error("project_path must end with .spp", "INVALID_PROJECT_EXTENSION")
    resolved_resolution = int(resolution)
    if resolved_resolution not in SUPPORTED_RESOLUTIONS:
        return skill_error(
            "Unsupported Painter texture resolution",
            f"resolution must be one of {sorted(SUPPORTED_RESOLUTIONS)}",
        )
    if normal_map_format not in {"DirectX", "OpenGL"}:
        return skill_error("normal_map_format must be DirectX or OpenGL", "INVALID_NORMAL_FORMAT")

    import substance_painter.project as project  # Lazy: Painter host only.

    if project.is_open():
        return skill_error("A Painter project is already open", "Close it before creating another project")
    output.parent.mkdir(parents=True, exist_ok=True)
    settings = project.Settings(
        default_save_path=str(output),
        normal_map_format=getattr(project.NormalMapFormat, normal_map_format),
        default_texture_resolution=resolved_resolution,
    )
    project.create(str(mesh), settings=settings)

    return skill_success(
        "Created Painter project from mesh",
        mesh_path=str(mesh),
        project_path=str(output),
        resolution=resolved_resolution,
        normal_map_format=normal_map_format,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
