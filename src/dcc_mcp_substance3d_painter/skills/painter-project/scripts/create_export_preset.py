"""Build a bounded inline PNG export preset for Painter."""

from __future__ import annotations

from typing import Any

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

from dcc_mcp_substance3d_painter.export_contract import build_export_preset


@skill_entry
def main(
    name: str,
    maps: list[dict[str, Any]],
    bit_depth: int = 8,
    dithering: bool = False,
    **_kwargs,
):
    try:
        preset = build_export_preset(name, maps, bit_depth, dithering)
    except (TypeError, ValueError) as exc:
        return skill_error("Invalid Painter export preset", "INVALID_EXPORT_PRESET", validation_error=str(exc))
    return skill_success("Created bounded Painter export preset", preset=preset)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
