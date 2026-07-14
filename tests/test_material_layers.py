from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, call


def _load_script() -> ModuleType:
    path = (
        Path(__file__).parent.parent
        / "src"
        / "dcc_mcp_substance3d_painter"
        / "skills"
        / "painter-project"
        / "scripts"
        / "create_pbr_fill_layer.py"
    )
    spec = importlib.util.spec_from_file_location("create_pbr_fill_layer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_pbr_fill_layer_uses_public_painter_api(monkeypatch):
    layer = MagicMock()
    layer.uid.return_value = 17
    stack = object()
    channel_type = SimpleNamespace(BaseColor="base", Metallic="metal", Roughness="rough")
    color = MagicMock(side_effect=lambda r, g, b: (r, g, b))

    painter = ModuleType("substance_painter")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.get_active_stack = MagicMock(return_value=stack)
    textureset.ChannelType = channel_type
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.InsertPosition = SimpleNamespace(from_textureset_stack=MagicMock(return_value="top"))
    layerstack.insert_fill = MagicMock(return_value=layer)
    colormanagement = ModuleType("substance_painter.colormanagement")
    colormanagement.Color = color

    monkeypatch.setitem(sys.modules, "substance_painter", painter)
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)
    monkeypatch.setitem(sys.modules, "substance_painter.textureset", textureset)
    monkeypatch.setitem(sys.modules, "substance_painter.layerstack", layerstack)
    monkeypatch.setitem(sys.modules, "substance_painter.colormanagement", colormanagement)

    result = _load_script().main(
        name="SIGNAL FORGE | Oxidized Alloy",
        base_color=[0.08, 0.19, 0.22],
        metallic=0.82,
        roughness=0.38,
    )

    assert result["success"] is True
    layerstack.InsertPosition.from_textureset_stack.assert_called_once_with(stack)
    layerstack.insert_fill.assert_called_once_with("top")
    layer.set_name.assert_called_once_with("SIGNAL FORGE | Oxidized Alloy")
    assert layer.active_channels == {"base", "metal", "rough"}
    assert layer.set_source.call_args_list == [
        call("base", (0.08, 0.19, 0.22)),
        call("metal", (0.82, 0.82, 0.82)),
        call("rough", (0.38, 0.38, 0.38)),
    ]


def test_create_pbr_fill_layer_rejects_out_of_range_values(monkeypatch):
    painter = ModuleType("substance_painter")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    monkeypatch.setitem(sys.modules, "substance_painter", painter)
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)
    monkeypatch.setitem(sys.modules, "substance_painter.textureset", ModuleType("substance_painter.textureset"))
    monkeypatch.setitem(sys.modules, "substance_painter.layerstack", ModuleType("substance_painter.layerstack"))
    monkeypatch.setitem(
        sys.modules,
        "substance_painter.colormanagement",
        ModuleType("substance_painter.colormanagement"),
    )

    result = _load_script().main(name="bad", base_color=[1.5, 0.0, 0.0])

    assert result["success"] is False
