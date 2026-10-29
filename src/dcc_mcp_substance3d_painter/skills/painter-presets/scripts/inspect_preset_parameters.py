"""Inspect the exposed parameters of an applied Painter preset instance."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import node_summary, resolve_stack
from dcc_mcp_substance3d_painter.preset_parameters import (
    find_preset_node,
    read_all_parameters,
    resolve_channel_types,
    resolve_preset_access,
)


@skill_entry
def main(
    node_uid: int,
    texture_set: str | None = None,
    stack: str | None = None,
    **_kwargs,
):
    import substance_painter.layerstack as layerstack  # Lazy: Painter host only.
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")

    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        node = find_preset_node(layerstack, target_stack, int(node_uid))
        if node is None:
            return skill_error(
                "Preset node was not found in the selected stack",
                "HOST_PRESET_NODE_NOT_FOUND",
                node_uid=int(node_uid),
            )
        channel_types = tuple(resolve_channel_types(textureset))
        # Match set_preset_parameters' writable-source selection so an agent
        # inspects the exact parameter table a write will target. When no
        # writable source exists, resolve_preset_access falls back to the first
        # source with any capability, so read-only presets stay inspectable.
        parameter_node, access = resolve_preset_access(node, channel_types, required_capability="write")
        parameters = read_all_parameters(access)
        return skill_success(
            "Inspected Painter preset parameters",
            node=node_summary(parameter_node),
            capabilities=access.capabilities(),
            parameter_count=len(parameters) if parameters is not None else 0,
            parameters=parameters or {},
            prompt=(
                None
                if access.capabilities()["list"]
                else "This Painter build exposes no parameter listing for this node; parameter control is unavailable."
            ),
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to inspect Painter preset parameters", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
