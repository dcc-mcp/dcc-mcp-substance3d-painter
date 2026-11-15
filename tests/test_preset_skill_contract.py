"""Contract tests for the bundled painter-presets skill package."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dcc_mcp_core import validate_skill  # noqa: E402

SKILL = ROOT / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-presets"


def test_skill_validates_clean():
    report = validate_skill(str(SKILL))
    assert report.is_clean, [issue.message for issue in report.issues]


def test_every_tool_has_explicit_metadata_and_existing_source():
    manifest = yaml.safe_load(SKILL.joinpath("tools.yaml").read_text(encoding="utf-8"))
    names = [tool["name"] for tool in manifest["tools"]]
    assert names == ["list_preset_resources", "inspect_preset_parameters", "set_preset_parameters"]
    for tool in manifest["tools"]:
        assert tool["execution"] == "async"
        assert tool["affinity"] == "main"
        assert tool["timeout_hint_secs"] > 0
        assert tool["input_schema"]["type"] == "object"
        assert {"read_only", "destructive", "idempotent"}.issubset(tool)
        source = SKILL / tool["source_file"]
        assert source.is_file(), f"missing source_file: {tool['source_file']}"


def test_main_thread_tools_advertise_the_job_envelope_contract():
    manifest = yaml.safe_load(SKILL.joinpath("tools.yaml").read_text(encoding="utf-8"))
    incorrectly_synchronous = [
        tool["name"] for tool in manifest["tools"] if tool["affinity"] == "main" and tool["execution"] != "async"
    ]
    assert incorrectly_synchronous == []


def test_read_only_tools_are_marked_non_destructive():
    manifest = yaml.safe_load(SKILL.joinpath("tools.yaml").read_text(encoding="utf-8"))
    for tool in manifest["tools"]:
        if tool["name"] in {"list_preset_resources", "inspect_preset_parameters"}:
            assert tool["read_only"] is True
            assert tool["destructive"] is False
            assert tool["annotations"]["read_only_hint"] is True


def test_no_bundled_binary_preset_assets():
    # v1 ships capability, not Adobe/third-party preset files. Only source code
    # and text metadata may live under the skill directory.
    binary_suffixes = {".sbsar", ".spsm", ".spmsk", ".sbs", ".exr", ".hdr", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
    offenders = [str(p.relative_to(SKILL)) for p in SKILL.rglob("*") if p.suffix.lower() in binary_suffixes]
    assert offenders == []


def test_next_tool_references_resolve_to_declared_or_project_tools():
    manifest = yaml.safe_load(SKILL.joinpath("tools.yaml").read_text(encoding="utf-8"))
    declared = {f"painter_presets__{tool['name']}" for tool in manifest["tools"]}
    project_tools = {
        "painter_project__import_resource",
        "painter_project__apply_smart_material",
        "painter_project__search_resources",
        "painter_project__list_layer_stack",
        "painter_project__export_textures",
    }
    for tool in manifest["tools"]:
        for direction, refs in (tool.get("next-tools") or {}).items():
            for ref in refs or []:
                assert ref in declared or ref in project_tools, f"{tool['name']} {direction} -> {ref}"
