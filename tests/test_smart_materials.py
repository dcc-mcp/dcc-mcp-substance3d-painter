from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

SCRIPTS = (
    Path(__file__).parent.parent
    / "src"
    / "dcc_mcp_substance3d_painter"
    / "skills"
    / "painter-project"
    / "scripts"
)


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_search_resources_filters_by_usage(monkeypatch):
    smart_usage = SimpleNamespace(name="SMART_MATERIAL")
    export_usage = SimpleNamespace(name="EXPORT")
    first = MagicMock()
    first.usages.return_value = [smart_usage]
    first.identifier.return_value.url.return_value = "resource://smart/steel"
    first.gui_name.return_value = "Steel Painted"
    first.category.return_value = "metal"
    first.type.return_value.name = "SMART_MATERIAL"
    second = MagicMock()
    second.usages.return_value = [export_usage]
    unsupported = MagicMock()
    unsupported.usages.side_effect = ValueError("postfx is not a valid Usage")

    resource = ModuleType("substance_painter.resource")
    resource.Usage = {"SMART_MATERIAL": smart_usage, "EXPORT": export_usage}
    resource.search = MagicMock(return_value=[unsupported, first, second])
    painter = ModuleType("substance_painter")
    painter.resource = resource
    monkeypatch.setitem(sys.modules, "substance_painter", painter)
    monkeypatch.setitem(sys.modules, "substance_painter.resource", resource)

    result = _load_script("search_resources").main(query="steel", usage="smart_material", limit=5)

    assert result["success"] is True
    assert result["context"]["resources"][0]["url"] == "resource://smart/steel"
    assert result["context"]["match_count"] == 1
    assert result["context"]["skipped_count"] == 1


def test_apply_smart_material_uses_public_painter_api(monkeypatch):
    stack = object()
    identifier = object()
    group = MagicMock()
    group.uid.return_value = 42
    group.get_name.return_value = "SIGNAL FORGE | Layered Steel"

    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.get_active_stack = MagicMock(return_value=stack)
    resource = ModuleType("substance_painter.resource")
    resource.ResourceID = SimpleNamespace(from_url=MagicMock(return_value=identifier))
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.InsertPosition = SimpleNamespace(from_textureset_stack=MagicMock(return_value="top"))
    layerstack.insert_smart_material = MagicMock(return_value=group)

    painter = ModuleType("substance_painter")
    painter.project = project
    painter.textureset = textureset
    painter.resource = resource
    painter.layerstack = layerstack
    monkeypatch.setitem(sys.modules, "substance_painter", painter)
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)
    monkeypatch.setitem(sys.modules, "substance_painter.textureset", textureset)
    monkeypatch.setitem(sys.modules, "substance_painter.resource", resource)
    monkeypatch.setitem(sys.modules, "substance_painter.layerstack", layerstack)

    result = _load_script("apply_smart_material").main(
        resource_url="resource://smart/steel",
        layer_name="SIGNAL FORGE | Layered Steel",
    )

    assert result["success"] is True
    layerstack.insert_smart_material.assert_called_once_with("top", identifier)
    group.set_name.assert_called_once_with("SIGNAL FORGE | Layered Steel")


def test_list_export_presets_uses_dedicated_export_api(monkeypatch):
    predefined = SimpleNamespace(name="PBR Metallic Roughness", url="predefined://pbr")
    identifier = SimpleNamespace(name="Arnold (AiStandard)", url=MagicMock(return_value="resource://arnold"))
    resource_preset = SimpleNamespace(resource_id=identifier)
    export = ModuleType("substance_painter.export")
    export.list_predefined_export_presets = MagicMock(return_value=[predefined])
    export.list_resource_export_presets = MagicMock(return_value=[resource_preset])
    painter = ModuleType("substance_painter")
    painter.export = export
    monkeypatch.setitem(sys.modules, "substance_painter", painter)
    monkeypatch.setitem(sys.modules, "substance_painter.export", export)

    result = _load_script("list_export_presets").main(query="Arnold")

    assert result["success"] is True
    assert result["context"]["presets"] == [
        {"name": "Arnold (AiStandard)", "url": "resource://arnold", "kind": "resource"}
    ]


def test_save_project_as_uses_explicit_spp_path(monkeypatch, tmp_path):
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    project.save_as = MagicMock(side_effect=lambda path: Path(path).write_bytes(b"spp"))
    painter = ModuleType("substance_painter")
    painter.project = project
    monkeypatch.setitem(sys.modules, "substance_painter", painter)
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)
    output = tmp_path / "signal_forge.spp"

    result = _load_script("save_project_as").main(project_path=str(output))

    assert result["success"] is True
    project.save_as.assert_called_once_with(str(output.resolve()))
    assert result["context"]["size_bytes"] == 3
