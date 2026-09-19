"""Read Painter's current environment lighting state."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_environment import environment_module, read_state


@skill_entry
def main(**_kwargs):
    try:
        module = environment_module()
        state = read_state(module)
    except ImportError as exc:
        return skill_error(
            "This Painter build exposes no environment lighting module",
            "environment_module_unavailable",
            detail=str(exc),
            prompt="Painter's lighting is image-based; confirm this build exposes an environment module.",
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to inspect Painter environment lighting", str(exc))

    return skill_success(
        "Inspected Painter environment lighting",
        environment_map=state["environment_map"],
        background_texture=state["background_texture"],
        exposure=state["exposure"],
        rotation=state["rotation"],
        capabilities=state["capabilities"],
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
