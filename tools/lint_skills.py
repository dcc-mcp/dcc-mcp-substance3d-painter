"""Validate bundled DCC-MCP skill metadata."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    from dcc_mcp_core import validate_skill

    skills_dir = Path(__file__).resolve().parents[1] / "src" / "dcc_mcp_substance3d_painter" / "skills"
    directories = [path for path in skills_dir.iterdir() if path.is_dir()]
    reports = [(path, validate_skill(str(path))) for path in directories]
    failures = [(path, report) for path, report in reports if not report.is_clean]
    if failures:
        for path, report in failures:
            for issue in report.issues:
                print(f"{path.name}: {issue}", file=sys.stderr)
        return 1
    print(f"validated {len(directories)} bundled skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
