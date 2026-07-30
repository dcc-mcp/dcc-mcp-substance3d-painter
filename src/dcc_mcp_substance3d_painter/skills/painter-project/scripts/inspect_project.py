"""Inspect a running Substance 3D Painter project."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


def _ocio_environment():
    raw_path = os.environ.get("OCIO", "").strip()
    path = Path(raw_path) if raw_path else None
    return {
        "path": raw_path or None,
        "exists": bool(path and path.is_file()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
        if path and path.is_file()
        else None,
    }


@skill_entry
def main(**_kwargs):
    import substance_painter.colormanagement as colormanagement
    import substance_painter.display as display
    import substance_painter.project as project  # Lazy import: requires Painter.

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    try:
        tone_mapping = str(display.get_tone_mapping())
        color_managed = False
        working_probe = None
    except RuntimeError:
        color_managed = True
        tone_mapping = None
        working_probe = list(
            colormanagement.Color(
                0.18,
                0.18,
                0.18,
                colormanagement.GenericColorSpace.sRGB,
            ).working
        )
    return skill_success(
        "Inspected Substance 3D Painter project",
        file_path=str(project.file_path()),
        color_management={
            "enabled": color_managed,
            "ocio_environment": _ocio_environment(),
            "legacy_tone_mapping": tone_mapping,
            "srgb_18_to_working_probe": working_probe,
        },
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
