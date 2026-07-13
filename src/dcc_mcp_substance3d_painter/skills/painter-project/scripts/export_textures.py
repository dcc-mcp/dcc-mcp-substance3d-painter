"""Export current Painter texture sets with an explicit preset."""

from __future__ import annotations

from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(export_path: str, preset_url: str, padding_algorithm: str = "infinite", **_kwargs):
    import substance_painter.export as export  # Lazy import: requires Painter.
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    output = Path(export_path).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    stacks = [stack for item in textureset.all_texture_sets() for stack in item.all_stacks()]
    if not stacks:
        return skill_error("No exportable texture sets found", "textureset.all_texture_sets() returned no stacks")
    result = export.export_project_textures(
        {
            "exportShaderParams": False,
            "exportPath": str(output),
            "exportList": [{"rootPath": str(stack)} for stack in stacks],
            "exportPresets": [{"name": "dcc-mcp", "maps": []}],
            "defaultExportPreset": preset_url,
            "exportParameters": [{"parameters": {"paddingAlgorithm": padding_algorithm}}],
        }
    )
    return skill_success(
        "Submitted Painter texture export", export_path=str(output), stack_count=len(stacks), result=result
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
