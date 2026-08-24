from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


def _load_script():
    path = (
        Path(__file__).parent.parent
        / "src"
        / "dcc_mcp_substance3d_painter"
        / "skills"
        / "painter-project"
        / "scripts"
        / "inspect_project.py"
    )
    spec = importlib.util.spec_from_file_location("inspect_project", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inspect_project_reports_ocio_environment(monkeypatch, tmp_path):
    config = tmp_path / "config.ocio"
    config.write_text("ocio_profile_version: 2.4\n", encoding="utf-8")
    project = ModuleType("substance_painter.project")
    project.is_open = lambda: True
    project.file_path = lambda: "P:/lookdev/drawcall557.spp"
    project.name = lambda: "drawcall557"
    project.needs_saving = lambda: False
    project.is_busy = lambda: False
    project.is_in_edition_state = lambda: True
    display = ModuleType("substance_painter.display")
    display.get_tone_mapping = lambda: (_ for _ in ()).throw(RuntimeError("color managed"))
    colormanagement = ModuleType("substance_painter.colormanagement")
    colormanagement.GenericColorSpace = SimpleNamespace(sRGB="sRGB")
    colormanagement.Color = lambda *_args: SimpleNamespace(working=(0.1, 0.2, 0.3))
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)
    monkeypatch.setitem(sys.modules, "substance_painter.display", display)
    monkeypatch.setitem(sys.modules, "substance_painter.colormanagement", colormanagement)
    textureset = ModuleType("substance_painter.textureset")
    textureset.all_texture_sets = lambda: []
    layerstack = ModuleType("substance_painter.layerstack")
    monkeypatch.setitem(sys.modules, "substance_painter.textureset", textureset)
    monkeypatch.setitem(sys.modules, "substance_painter.layerstack", layerstack)
    monkeypatch.setenv("OCIO", str(config))

    result = _load_script().main()

    assert result["success"] is True
    color_management = result["context"]["color_management"]
    assert color_management["enabled"] is True
    assert color_management["ocio_environment"]["path"] == str(config)
    assert len(color_management["ocio_environment"]["sha256"]) == 64
