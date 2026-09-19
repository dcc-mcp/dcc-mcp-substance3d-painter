"""Set Painter's environment map from a resource URL, with host readback."""

from __future__ import annotations

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_environment import (
    environment_module,
    require_getter,
    require_setter,
)


@skill_entry
def main(resource_url: str, **_kwargs):
    resolved_url = str(resource_url).strip()
    if not resolved_url:
        return skill_error("Invalid Painter environment resource URL", "resource_url must not be empty")

    import substance_painter.resource as resource  # Lazy: Painter host only.

    # from_url() rejects malformed URLs and non-resource schemes with ValueError.
    # That is a caller input problem, not a host capability gap, so it is
    # diagnosed separately from the environment probing below.
    try:
        identifier = resource.ResourceID.from_url(resolved_url)
    except (TypeError, ValueError) as exc:
        return skill_error(
            "Invalid Painter environment resource URL",
            "invalid_resource_url",
            resource_url=resolved_url,
            detail=str(exc),
            prompt=(
                "Pass a Painter resource URL, for example one returned by "
                "painter_project__import_resource with the environment usage."
            ),
        )

    try:
        module = environment_module()
        setter = require_setter(module, "environment_map")
        getter = require_getter(module, "environment_map")
        setter(identifier)
        actual = getattr(getter(), "url", None)
        actual_url = str(actual()) if callable(actual) else None
        if actual_url != str(identifier.url()):
            return skill_error(
                "Painter environment map readback failed",
                "HOST_READBACK_MISMATCH",
                expected=str(identifier.url()),
                actual=actual_url,
            )
    except ImportError as exc:
        return skill_error(
            "This Painter build exposes no environment lighting module",
            "environment_module_unavailable",
            detail=str(exc),
        )
    except ValueError as exc:
        return skill_error(
            "This Painter build does not support environment map control",
            "environment_capability_unsupported",
            detail=str(exc),
        )
    except (AttributeError, RuntimeError, TypeError) as exc:
        return skill_error("Unable to set Painter environment map", str(exc))

    return skill_success(
        "Set Painter environment map",
        environment_map=actual_url,
        verified=True,
        postcondition={"method": "environment_map_readback", "expected": str(identifier.url()), "actual": actual_url},
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
