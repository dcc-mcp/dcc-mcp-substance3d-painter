from __future__ import annotations

import json
import re
from pathlib import Path

import dcc_mcp_substance3d_painter as adapter

ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_is_synchronized():
    manifest = json.loads(ROOT.joinpath(".release-please-manifest.json").read_text(encoding="utf-8"))
    version = re.search(r'(?m)^version = "([^"]+)"$', ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8"))
    assert version is not None
    assert version.group(1) == adapter.__version__ == manifest["."]


def test_plugin_and_skill_contract_files_exist():
    package = ROOT / "src" / "dcc_mcp_substance3d_painter"
    startup_entry = package.joinpath("painter", "plugins", f"{adapter.STARTUP_PLUGIN_MODULE}.py")
    assert startup_entry.exists()
    assert adapter.STARTUP_PLUGIN_MODULE != adapter.__name__
    assert package.joinpath("skills", "painter-project", "SKILL.md").exists()
    assert package.joinpath("skills", "painter-project", "tools.yaml").exists()
