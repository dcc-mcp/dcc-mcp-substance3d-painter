"""Import one bounded resource into the open Painter project."""

from __future__ import annotations

import stat
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
_RASTER_EXTENSIONS = frozenset({".bmp", ".jpeg", ".jpg", ".png", ".psd", ".tga", ".tif", ".tiff"})
_EXTENSIONS_BY_USAGE = {
    "alpha": _RASTER_EXTENSIONS,
    "environment": frozenset({".exr", ".hdr"}),
    "export": frozenset({".spexp"}),
    "generator": frozenset({".sbsar"}),
    "smart_mask": frozenset({".spmsk"}),
    "smart_material": frozenset({".spsm"}),
    "texture": _RASTER_EXTENSIONS,
}
_MAX_RESOURCE_BYTES = 512 * 1024 * 1024


def _validated_resource_path(file_path: str, usage: str) -> tuple[Path | None, str | None]:
    candidate = Path(file_path).expanduser()
    try:
        metadata = candidate.lstat()
    except OSError:
        return None, "INVALID_RESOURCE_PATH"
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    if candidate.is_symlink() or file_attributes & reparse_flag or not stat.S_ISREG(metadata.st_mode):
        return None, "RESOURCE_NOT_REGULAR_FILE"
    if candidate.suffix.lower() not in _EXTENSIONS_BY_USAGE[usage]:
        return None, "RESOURCE_EXTENSION_MISMATCH"
    if not 0 < metadata.st_size <= _MAX_RESOURCE_BYTES:
        return None, "RESOURCE_SIZE_OUT_OF_RANGE"
    try:
        return candidate.resolve(strict=True), None
    except OSError:
        return None, "INVALID_RESOURCE_PATH"


@skill_entry
def main(file_path: str, usage: str, name: str | None = None, group: str | None = None, **_kwargs):
    resolved_usage = str(usage).strip().lower()
    if resolved_usage not in _USAGES:
        return skill_error(
            "Unsupported Painter resource usage",
            "UNSUPPORTED_RESOURCE_USAGE",
            supported_usages=sorted(_USAGES),
        )
    path, source_error = _validated_resource_path(file_path, resolved_usage)
    if source_error is not None or path is None:
        return skill_error("Painter resource file is not safe to import", source_error or "INVALID_RESOURCE_PATH")
    resolved_name = str(name).strip() if name is not None else path.stem
    resolved_group = str(group).strip() if group is not None else None
    if not 1 <= len(resolved_name) <= 128:
        return skill_error("Invalid Painter resource name", "INVALID_RESOURCE_NAME")
    if resolved_group is not None and not 1 <= len(resolved_group) <= 128:
        return skill_error("Invalid Painter resource group", "INVALID_RESOURCE_GROUP")

    import substance_painter.project as project  # Lazy: Painter host only.
    import substance_painter.resource as resource

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    usage_name = _USAGES[resolved_usage]
    try:
        confirmed_path, confirmed_error = _validated_resource_path(file_path, resolved_usage)
        if confirmed_error is not None or confirmed_path != path:
            return skill_error(
                "Painter resource file changed before import",
                confirmed_error or "RESOURCE_PATH_CHANGED",
            )
        usage_value = getattr(resource.Usage, usage_name)
        imported = resource.import_project_resource(
            str(path),
            usage_value,
            name=resolved_name,
            group=resolved_group,
        )
        identifier = imported.identifier()
        validate_resource(resource, identifier, usage_name)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return skill_error("Painter resource import readback failed", "PAINTER_RESOURCE_IMPORT_READBACK_FAILED")
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
