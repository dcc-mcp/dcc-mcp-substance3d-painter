"""Selectively export Painter texture sets and verify exported PNGs."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.export_contract import validate_export_preset
from dcc_mcp_substance3d_painter.painter_state import stack_root_path

_PADDING = {"infinite", "dilation", "passthrough"}


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"Exported file is not a readable PNG: {path.name}")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise ValueError(f"Exported PNG has an invalid resolution: {path.name}")
    return width, height


def _resolution_pairs(texture_set: Any) -> set[tuple[int, int]]:
    if bool(texture_set.has_uv_tiles()):
        return {
            (int(tile.get_resolution().width), int(tile.get_resolution().height)) for tile in texture_set.all_uv_tiles()
        }
    resolution = texture_set.get_resolution()
    return {(int(resolution.width), int(resolution.height))}


def _exported_records(result: Any) -> list[tuple[str | None, Path]]:
    records = []
    for key, values in result.textures.items():
        texture_set = str(key[0]) if isinstance(key, tuple) and key else None
        records.extend((texture_set, Path(value).expanduser().resolve()) for value in values)
    return records


@skill_entry
def main(
    export_path: str,
    preset_url: str | None = None,
    preset: dict[str, Any] | None = None,
    texture_sets: list[str] | None = None,
    padding_algorithm: str = "infinite",
    **_kwargs,
):
    import substance_painter.export as export  # Lazy import: requires Painter.
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    if (preset_url is None) == (preset is None):
        return skill_error("Invalid Painter export preset selection", "Provide exactly one of preset_url or preset")
    padding = str(padding_algorithm).strip().lower()
    if padding not in _PADDING:
        return skill_error("Unsupported Painter padding algorithm", f"supported={sorted(_PADDING)}")

    try:
        inline_preset = validate_export_preset(preset) if preset is not None else None
        output = Path(export_path).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        available = list(textureset.all_texture_sets())
        by_name = {str(item.name()): item for item in available}
        selected_names = list(texture_sets) if texture_sets is not None else list(by_name)
        if not selected_names or len(selected_names) > 50 or len(set(selected_names)) != len(selected_names):
            raise ValueError("texture_sets must contain between 1 and 50 unique names")
        missing = sorted(set(selected_names) - set(by_name))
        if missing:
            raise ValueError(f"Unknown Painter texture sets: {missing}")
        selected = [by_name[name] for name in selected_names]
        stacks = [stack for item in selected for stack in item.all_stacks()]
        if not stacks:
            raise ValueError("The selected texture sets have no exportable stacks")
        expected_resolutions = {str(item.name()): _resolution_pairs(item) for item in selected}
        export_presets = [inline_preset] if inline_preset is not None else [{"name": "dcc-mcp", "maps": []}]
        default_preset = inline_preset["name"] if inline_preset is not None else str(preset_url)
        config = {
            "exportShaderParams": False,
            "exportPath": str(output),
            "exportList": [{"rootPath": stack_root_path(stack)} for stack in stacks],
            "exportPresets": export_presets,
            "defaultExportPreset": default_preset,
            "exportParameters": [{"parameters": {"paddingAlgorithm": padding}}],
        }
        result = export.export_project_textures(config)
        status = str(getattr(result.status, "name", result.status))
        if status != "Success":
            raise RuntimeError(f"Painter export ended with status {status}: {result.message}")
        exported = _exported_records(result)
        if not exported:
            raise RuntimeError("HOST_READBACK_EXPORT_EMPTY")
        if len({path for _texture_set, path in exported}) != len(exported):
            raise RuntimeError("HOST_READBACK_EXPORT_DUPLICATE_PATH")
        verified = []
        for texture_set_name, path in exported:
            try:
                path.relative_to(output)
            except ValueError as exc:
                raise RuntimeError("HOST_READBACK_EXPORT_OUTSIDE_TARGET") from exc
            if not path.is_file():
                raise RuntimeError(f"HOST_READBACK_EXPORT_MISSING: {path.name}")
            width = height = None
            if inline_preset is not None:
                if texture_set_name not in expected_resolutions:
                    if len(selected) != 1:
                        raise RuntimeError(f"HOST_READBACK_TEXTURE_SET_UNKNOWN: {texture_set_name!r}")
                    texture_set_name = selected_names[0]
                width, height = _png_size(path)
                allowed_resolutions = expected_resolutions[texture_set_name]
                if (width, height) not in allowed_resolutions:
                    raise RuntimeError(
                        f"HOST_READBACK_RESOLUTION_MISMATCH: {path.name} is {width}x{height}; "
                        f"expected one of {sorted(allowed_resolutions)}"
                    )
            verified.append({"path": str(path), "width": width, "height": height})
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Painter texture export verification failed", str(exc))
    return skill_success(
        "Exported and verified Painter textures",
        export_path=str(output),
        texture_sets=selected_names,
        stack_count=len(stacks),
        verified_files=verified,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
