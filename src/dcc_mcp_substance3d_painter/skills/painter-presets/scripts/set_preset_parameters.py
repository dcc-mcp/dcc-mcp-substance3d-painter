"""Set bounded parameters on an applied Painter preset instance, verifying by readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import node_summary, resolve_stack
from dcc_mcp_substance3d_painter.preset_parameters import (
    MISSING,
    ParameterValidationError,
    find_preset_node,
    read_parameter,
    resolve_preset_access,
    validate_parameter_bindings,
    values_equal,
    write_parameter,
)


@skill_entry
def main(
    node_uid: int,
    parameters: dict,
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
        bindings = validate_parameter_bindings(parameters)
    except ParameterValidationError as exc:
        return skill_error("Invalid Painter preset parameters", str(exc))

    try:
        target_stack = resolve_stack(textureset, texture_set, stack)
        node = find_preset_node(layerstack, target_stack, int(node_uid))
        if node is None:
            return skill_error(
                "Preset node was not found in the selected stack",
                "HOST_PRESET_NODE_NOT_FOUND",
                node_uid=int(node_uid),
            )
        parameter_node, access = resolve_preset_access(node)
        capabilities = access.capabilities()
        if not capabilities["write"]:
            return skill_error(
                "This Painter node exposes no parameter control",
                "preset_parameter_write_unsupported",
                node=node_summary(parameter_node),
                capabilities=capabilities,
                prompt="Call painter_presets__inspect_preset_parameters to read the host parameter capabilities.",
            )

        written = []
        for name, value in bindings.items():
            try:
                write_parameter(access, name, value)
            except (AttributeError, KeyError, RuntimeError, TypeError, ValueError) as exc:
                return skill_error(
                    "Unable to set Painter preset parameter",
                    str(exc),
                    parameter=name,
                    written=written,
                )
            written.append(name)

        # Re-resolve the node so readback uses a fresh host handle, not the
        # pre-write binding; a stale handle cannot prove the write landed.
        readback_node = find_preset_node(layerstack, target_stack, int(node_uid))
        _, readback_access = resolve_preset_access(readback_node) if readback_node is not None else (None, None)
        if readback_access is None or not readback_access.capabilities()["readback"]:
            return skill_success(
                "Set Painter preset parameters (parameter values have no host readback)",
                node=node_summary(parameter_node),
                parameters=bindings,
                capabilities=capabilities,
                verified=False,
                postcondition={
                    "method": "preset_parameter_write_only",
                    "verifies": "write_accepted_only",
                },
                prompt="Painter exposes no parameter readback on this node; export the texture set to confirm the result.",
            )

        observed = {}
        mismatches = []
        for name, expected in bindings.items():
            actual = read_parameter(readback_access, name)
            observed[name] = None if actual is MISSING else actual
            if not values_equal(expected, actual):
                mismatches.append(name)
        if mismatches:
            return skill_error(
                "Painter preset parameter readback failed",
                "HOST_READBACK_MISMATCH",
                node=node_summary(parameter_node),
                mismatches=mismatches,
                observed=observed,
            )
        return skill_success(
            "Set Painter preset parameters",
            node=node_summary(parameter_node),
            parameters=bindings,
            observed=observed,
            capabilities=capabilities,
            verified=True,
            postcondition={
                "method": "preset_parameter_readback",
                "parameter_count": len(bindings),
            },
        )
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to set Painter preset parameters", str(exc))


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
