"""Import one bounded resource into the open Painter project."""

from __future__ import annotations

from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.painter_state import validate_resource

_USAGES = {
    "alpha": "ALPHA",
    "environment": "ENVIRONMENT",
    "export": "EXPORT",
    "generator": "GENERATOR",
    "smart_mask": "SMART_MASK",
    "smart_material": "SMART_MATERIAL",
    "texture": "TEXTURE",
}


@skill_entry
def main(file_path: str, usage: str, name: str | None = None, group: str | None = None, **_kwargs):
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        return skill_error("Painter resource file does not exist", "INVALID_RESOURCE_PATH")
    resolved_usage = str(usage).strip().lower()
    if resolved_usage not in _USAGES:
        return skill_error("Unsupported Painter resource usage", f"supported={sorted(_USAGES)}")
    resolved_name = str(name).strip() if name is not None else path.stem
    resolved_group = str(group).strip() if group is not None else None
    if not 1 <= len(resolved_name) <= 128:
        return skill_error("Invalid Painter resource name", "name must contain between 1 and 128 characters")
    if resolved_group is not None and not 1 <= len(resolved_group) <= 128:
        return skill_error("Invalid Painter resource group", "group must contain between 1 and 128 characters")

    import substance_painter.project as project  # Lazy: Painter host only.
    import substance_painter.resource as resource

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    usage_name = _USAGES[resolved_usage]
    try:
        usage_value = getattr(resource.Usage, usage_name)
        imported = resource.import_project_resource(
            str(path),
            usage_value,
            name=resolved_name,
            group=resolved_group,
        )
        identifier = imported.identifier()
        validate_resource(resource, identifier, usage_name)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Painter resource import readback failed", str(exc))
    return skill_success(
        "Imported Painter project resource",
        resource={
            "name": resolved_name,
            "url": str(identifier.url()),
            "usage": usage_name,
            "group": resolved_group,
        },
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
