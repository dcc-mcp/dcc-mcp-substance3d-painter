"""Prove that the Painter main-thread bridge is servicing requests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import dcc_mcp_core
from dcc_mcp_core.skill import skill_entry, skill_success

import dcc_mcp_substance3d_painter as adapter
from dcc_mcp_substance3d_painter._installer import _observe_process_identity


@skill_entry
def main(**_kwargs):
    import substance_painter  # Lazy import: provided only by Painter.

    identity = _observe_process_identity(os.getpid())
    bootstrap = sys.modules.get("dcc_mcp_substance3d_painter_bootstrap")
    if identity is None or bootstrap is None or not getattr(bootstrap, "__file__", None):
        raise RuntimeError("Painter process or bootstrap identity is unavailable")
    return skill_success(
        "Substance 3D Painter main-thread dispatch is ready.",
        host_dispatch_ready=True,
        host="substance3d_painter",
        version=str(getattr(substance_painter, "version", "unknown")),
        host_pid=os.getpid(),
        host_executable=identity["executable"],
        process_start_identity=identity["start_identity"],
        adapter_version=adapter.__version__,
        core_version=dcc_mcp_core.__version__,
        adapter_module_path=str(Path(adapter.__file__).resolve()),
        core_module_path=str(Path(dcc_mcp_core.__file__).resolve()),
        bootstrap_module_path=str(Path(bootstrap.__file__).resolve()),
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
