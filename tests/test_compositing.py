from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

SKILLS = Path(__file__).parent.parent / "src" / "dcc_mcp_substance3d_painter" / "skills"


def _load(skill: str, script: str) -> ModuleType:
    path = SKILLS / skill / "scripts" / f"{script}.py"
    spec = importlib.util.spec_from_file_location(f"{skill}_{script}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeStack:
    """Sentinel stack object used to prove same-stack readback."""


def _install_host(monkeypatch, *, open_project: bool = True, node=None, blending_enum=True):
    stack = _FakeStack()
    project = ModuleType("substance_painter.project")
    project.is_open = lambda: open_project
    textureset = ModuleType("substance_painter.textureset")
    textureset.get_active_stack = lambda: stack
    textureset.all_texture_sets = lambda: []
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.get_node_by_uid = lambda uid: [node] if node is not None else None
    if node is not None:
        # find_node() requires the node to report membership of the resolved stack.
        node.get_stack = lambda: stack
    if blending_enum:
        layerstack.BlendingMode = SimpleNamespace(
            __members__={"Passthrough": "passthrough", "Normal": "normal", "Multiply": "multiply"}
        )
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)
    monkeypatch.setitem(sys.modules, "substance_painter.textureset", textureset)
    monkeypatch.setitem(sys.modules, "substance_painter.layerstack", layerstack)
    return stack, layerstack


class _Node:
    def __init__(self, *, visible=True, opacity=1.0, blending="Normal", supports=True):
        self._visible = visible
        self._opacity = opacity
        self._blending = blending
        self._supports = supports
        self.uid_value = 7
        self.has_mask = lambda: False
        self.is_mask_enabled = lambda: False
        self.get_mask_background = lambda: SimpleNamespace(name="Black")
        self.get_name = lambda: "Layer"
        self.get_type = lambda: SimpleNamespace(name="PaintLayer")
        if supports:
            self.set_visible = self._set_visible
            self.set_opacity = self._set_opacity
            self.set_blending_mode = self._set_blending_mode

    def uid(self):
        return self.uid_value

    def is_visible(self):
        return self._visible

    def get_opacity(self):
        return self._opacity

    def get_blending_mode(self):
        return SimpleNamespace(name=self._blending)

    def _set_visible(self, value):
        self._visible = bool(value)

    def _set_opacity(self, value):
        self._opacity = float(value)

    def _set_blending_mode(self, member):
        self._blending = {"passthrough": "Passthrough", "normal": "Normal", "multiply": "Multiply"}[member]


_install_stack = _FakeStack()


def test_inspect_layer_compositing_reports_state_and_capabilities(monkeypatch):
    node = _Node()
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "inspect_layer_compositing").main(layer_uid=7)

    assert result["success"] is True
    context = result["context"]
    assert context["visible"] is True
    assert context["opacity"] == 1.0
    assert context["blending_mode"] == "Normal"
    assert context["capabilities"] == {"visibility": True, "opacity": True, "blending_mode": True}
    assert context["supported_blending_modes"] == ["Multiply", "Normal", "Passthrough"]
    assert context["mask"] == {"has_mask": False, "enabled": None, "background": None}


def test_inspect_layer_compositing_requires_open_project(monkeypatch):
    _install_host(monkeypatch, open_project=False)

    result = _load("painter-compositing", "inspect_layer_compositing").main(layer_uid=7)

    assert result["success"] is False


def test_inspect_layer_compositing_reports_missing_layer(monkeypatch):
    _install_host(monkeypatch, node=None)

    result = _load("painter-compositing", "inspect_layer_compositing").main(layer_uid=7)

    assert result["success"] is False
    assert result["error"] == "HOST_LAYER_NOT_FOUND"


def test_set_layer_visibility_is_confirmed_by_readback(monkeypatch):
    node = _Node(visible=True)
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "set_layer_visibility").main(layer_uid=7, visible=False)

    assert result["success"] is True
    assert node.is_visible() is False
    assert result["postcondition"]["verified"] is True
    assert result["postcondition"]["method"] == "layer_visibility_readback"
    assert result["context"]["visible"] is False


def test_set_layer_visibility_reports_unsupported_host(monkeypatch):
    node = _Node(supports=False)
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "set_layer_visibility").main(layer_uid=7, visible=True)

    assert result["success"] is False
    assert result["error"] == "layer_visibility_unsupported"


def test_set_layer_opacity_rejects_out_of_range(monkeypatch):
    node = _Node()
    _install_host(monkeypatch, node=node)
    module = _load("painter-compositing", "set_layer_opacity")
    monkeypatch.setitem(sys.modules, "substance_painter.project", sys.modules["substance_painter.project"])

    assert module.main(layer_uid=7, opacity=1.5)["success"] is False
    assert module.main(layer_uid=7, opacity=-0.1)["success"] is False


def test_set_layer_opacity_is_confirmed_by_readback(monkeypatch):
    node = _Node()
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "set_layer_opacity").main(layer_uid=7, opacity=0.42)

    assert result["success"] is True
    assert node.get_opacity() == 0.42
    assert result["context"]["opacity"] == 0.42


def test_set_layer_opacity_detects_readback_mismatch(monkeypatch):
    class _Drifting(_Node):
        def _set_opacity(self, value):
            self._opacity = float(value) + 0.5

    node = _Drifting()
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "set_layer_opacity").main(layer_uid=7, opacity=0.25)

    assert result["success"] is False
    assert result["error"] == "HOST_READBACK_MISMATCH"
    assert result["context"]["actual"] == 0.75


def test_set_layer_blending_mode_is_confirmed_by_readback(monkeypatch):
    node = _Node(blending="Normal")
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "set_layer_blending_mode").main(layer_uid=7, blending_mode="Multiply")

    assert result["success"] is True
    assert node.get_blending_mode().name == "Multiply"
    assert result["context"]["blending_mode"] == "Multiply"


def test_set_layer_blending_mode_accepts_case_insensitive_names(monkeypatch):
    node = _Node()
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "set_layer_blending_mode").main(layer_uid=7, blending_mode="passthrough")

    assert result["success"] is True
    assert result["context"]["blending_mode"] == "Passthrough"


def test_set_layer_blending_mode_reports_unknown_name_with_supported_list(monkeypatch):
    node = _Node()
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "set_layer_blending_mode").main(layer_uid=7, blending_mode="Nonsense")

    assert result["success"] is False
    assert result["error"] == "blending_mode_not_found"
    assert result["context"]["supported_blending_modes"] == ["Multiply", "Normal", "Passthrough"]


def test_set_layer_blending_mode_rejects_empty_name(monkeypatch):
    node = _Node()
    _install_host(monkeypatch, node=node)

    result = _load("painter-compositing", "set_layer_blending_mode").main(layer_uid=7, blending_mode="   ")

    assert result["success"] is False
