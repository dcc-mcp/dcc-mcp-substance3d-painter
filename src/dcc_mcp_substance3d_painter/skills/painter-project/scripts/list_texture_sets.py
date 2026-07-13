"""List texture sets in the current Substance 3D Painter project."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


@skill_entry
def main(**_kwargs):
    import substance_painter.project as project  # Lazy import: requires Painter.
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    texture_sets = textureset.all_texture_sets()
    summary = [
        {"name": str(item.name()), "stacks": [str(stack) for stack in item.all_stacks()]} for item in texture_sets
    ]
    return skill_success("Listed Painter texture sets", texture_set_count=len(summary), texture_sets=summary)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
