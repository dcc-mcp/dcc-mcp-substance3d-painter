"""Fail closed when the Painter wheel carries compatibility or test-only files."""

from __future__ import annotations

import sys
import zipfile
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("usage: verify_wheel.py <wheel>")
    wheel = Path(argv[0])
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        forbidden = [
            name
            for name in names
            if name.endswith("/_install_contract.py") or name.startswith("tests/") or "/tests/" in name
        ]
        if forbidden:
            raise SystemExit(f"wheel contains forbidden files: {sorted(forbidden)}")
        required = {
            "dcc_mcp_substance3d_painter/_installer.py",
            "dcc_mcp_substance3d_painter/_probe_supervisor.py",
            "dcc_mcp_substance3d_painter/materialized_script_executor.py",
            "dcc_mcp_substance3d_painter/skills/painter-project/SKILL.md",
            "dcc_mcp_substance3d_painter/skills/painter-project/tools.yaml",
            "dcc_mcp_substance3d_painter/skills/painter-project/scripts/execute_materialized_script.py",
        }
        missing = required - names
        if missing:
            raise SystemExit(f"wheel is missing runtime files: {sorted(missing)}")
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        core_requirements = [
            Requirement(value)
            for value in metadata.get_all("Requires-Dist", [])
            if Requirement(value).name == "dcc-mcp-core"
        ]
        if len(core_requirements) != 1 or str(core_requirements[0].specifier) not in {
            "<1.0.0,>=0.20.15",
            ">=0.20.15,<1.0.0",
        }:
            raise SystemExit("wheel metadata does not require released Core 0.20.15")
        installer = archive.read("dcc_mcp_substance3d_painter/_installer.py").decode("utf-8")
        if "_install_contract" in installer or "except ImportError" in installer:
            raise SystemExit("wheel contains an adapter-owned Install SOP compatibility fallback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
