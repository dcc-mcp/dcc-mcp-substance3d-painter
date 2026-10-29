"""Set bounded parameters on an applied Painter preset instance, verifying by readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import node_summary, resolve_stack
from dcc_mcp_substance3d_painter.preset_parameters import (
    MAX_PARAMETERS,
    MISSING,
    ParameterValidationError,
    find_preset_node,
    read_all_parameters,
    read_parameter,
    resolve_channel_types,
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
        channel_types = tuple(resolve_channel_types(textureset))
        target_stack = resolve_stack(textureset, texture_set, stack)
        node = find_preset_node(layerstack, target_stack, int(node_uid))
        if node is None:
            return skill_error(
                "Preset node was not found in the selected stack",
                "HOST_PRESET_NODE_NOT_FOUND",
                node_uid=int(node_uid),
            )
        # Resolve the writable SourceSubstance, skipping read-only sources so a
        # smart-material group does not shadow a writable generator effect.
        parameter_node, access = resolve_preset_access(node, channel_types, required_capability="write")
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

        # Re-resolve from a fresh host handle. Requiring the same "write"
        # capability keeps readback on the exact source that was written (a
        # different required capability could select a different effect).
        readback_node = find_preset_node(layerstack, target_stack, int(node_uid))
        _, readback_access = (
            resolve_preset_access(readback_node, channel_types, required_capability="write")
            if readback_node is not None
            else (None, None)
        )
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

        # A value-bearing lister (the real get_parameters() returns a dict)
        # declares the host's actual parameter names. When that snapshot is
        # complete (< MAX_PARAMETERS), a written name absent from it is an
        # unknown parameter (e.g. a typo): the host silently ignored the write,
        # so this must fail, not report success. A truncated snapshot, a host
        # with only a name lister, or one with only a getter cannot prove a name
        # is unknown, so their MISSING/None readbacks stay unverifiable.
        listed_values = read_all_parameters(readback_access) if readback_access.lister_has_values else None
        names_are_complete = listed_values is not None and len(listed_values) < MAX_PARAMETERS
        observed = {}
        mismatches = []
        unreadable = []
        unknown = []
        for name, expected in bindings.items():
            if names_are_complete and name not in listed_values:
                observed[name] = None
                unknown.append(name)
                continue
            actual = read_parameter(readback_access, name)
            observed[name] = None if actual is MISSING else actual
            if actual is MISSING or actual is None:
                # A name-only lister (or a value the host won't expose) is not
                # proof of a mismatch; report it as unverifiable instead.
                unreadable.append(name)
            elif not values_equal(expected, actual):
                mismatches.append(name)
        if unknown:
            return skill_error(
                "Painter preset has no such parameter name",
                "HOST_PARAMETER_UNKNOWN",
                node=node_summary(parameter_node),
                unknown=unknown,
                observed=observed,
                prompt="One or more parameter names are not exposed by this preset; call painter_presets__inspect_preset_parameters to list the valid names.",
            )
        if mismatches:
            return skill_error(
                "Painter preset parameter readback failed",
                "HOST_READBACK_MISMATCH",
                node=node_summary(parameter_node),
                mismatches=mismatches,
                observed=observed,
            )
        if unreadable:
            return skill_success(
                "Set Painter preset parameters (some values have no host readback)",
                node=node_summary(parameter_node),
                parameters=bindings,
                capabilities=capabilities,
                observed=observed,
                verified=False,
                postcondition={
                    "method": "preset_parameter_write_only",
                    "verifies": "write_accepted_only",
                },
                prompt="Painter exposes no value readback for some parameters; export the texture set to confirm the result.",
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
