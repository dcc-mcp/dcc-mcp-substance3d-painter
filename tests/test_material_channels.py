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
    def __init__(self, channels):
        self._channels = channels

    def all_channels(self):
        return self._channels


def _channel(name, label, fmt="RGB", bit_depth=8, is_color=True):
    return SimpleNamespace(
        label=lambda: label,
        format=lambda: SimpleNamespace(name=fmt),
        bit_depth=lambda: bit_depth,
        is_color=lambda: is_color,
    )


_CHANNEL_TYPE = SimpleNamespace(
    __members__={"BaseColor": "bc", "Metallic": "me", "Roughness": "ro"},
    BaseColor="bc",
    Metallic="me",
    Roughness="ro",
)

_NAME_OF = {"bc": "BaseColor", "me": "Metallic", "ro": "Roughness"}


class _Node:
    def __init__(self, stack, active=("bc",)):
        self._stack = stack
        self.active_channels = set(active)
        self.sources = []

    def uid(self):
        return 7

    def get_name(self):
        return "Fill"

    def get_type(self):
        return SimpleNamespace(name="FillLayer")

    def get_stack(self):
        return self._stack

    def set_source(self, channel, color):
        self.sources.append((channel, color))


def _install_host(monkeypatch, *, open_project=True, stack=None, node=None, colormanagement=True):
    project = ModuleType("substance_painter.project")
    project.is_open = lambda: open_project
    textureset = ModuleType("substance_painter.textureset")
    textureset.ChannelType = _CHANNEL_TYPE
    if stack is None:
        stack = _FakeStack({})
    textureset.get_active_stack = lambda: stack
    textureset.all_texture_sets = lambda: []
    layerstack = ModuleType("substance_painter.layerstack")
    layerstack.get_node_by_uid = lambda uid: [node] if node is not None else None
    cm = ModuleType("substance_painter.colormanagement")
    cm.Color = lambda r, g, b: (r, g, b)
    monkeypatch.setitem(sys.modules, "substance_painter", ModuleType("substance_painter"))
    monkeypatch.setitem(sys.modules, "substance_painter.project", project)
    monkeypatch.setitem(sys.modules, "substance_painter.textureset", textureset)
    monkeypatch.setitem(sys.modules, "substance_painter.layerstack", layerstack)
    monkeypatch.setitem(sys.modules, "substance_painter.colormanagement", cm)
    return textureset, layerstack


def test_inspect_material_channels_lists_stack_inventory(monkeypatch):
    stack = _FakeStack(
        {
            "bc": _channel("BaseColor", "Base Color", is_color=True),
            "ro": _channel("Roughness", "Roughness", fmt="L", bit_depth=16, is_color=False),
        }
    )
    _install_host(monkeypatch, stack=stack)

    result = _load("painter-material", "inspect_material_channels").main()

    assert result["success"] is True
    channels = result["context"]["channels"]
    # Sorted by host enum name via enum_name().
    assert [item["label"] for item in channels] == ["Base Color", "Roughness"]
    assert channels[1] == {
        "name": "ro",
        "label": "Roughness",
        "format": "L",
        "bit_depth": 16,
        "is_color": False,
    }
    assert result["context"]["available_channel_types"] == ["BaseColor", "Metallic", "Roughness"]


def test_inspect_material_channels_requires_open_project(monkeypatch):
    _install_host(monkeypatch, open_project=False)

    result = _load("painter-material", "inspect_material_channels").main()

    assert result["success"] is False


def test_set_layer_channels_is_confirmed_by_readback(monkeypatch):
    stack = _FakeStack({})
    node = _Node(stack, active=("bc",))
    _install_host(monkeypatch, stack=stack, node=node)

    result = _load("painter-material", "set_layer_channels").main(layer_uid=7, channels=["BaseColor", "Roughness"])

    assert result["success"] is True
    assert node.active_channels == {"bc", "ro"}
    assert result["context"]["channels"] == ["bc", "ro"]
    assert result["postcondition"]["verified"] is True


def test_set_layer_channels_reports_unknown_channel_with_supported_list(monkeypatch):
    stack = _FakeStack({})
    node = _Node(stack)
    _install_host(monkeypatch, stack=stack, node=node)

    result = _load("painter-material", "set_layer_channels").main(layer_uid=7, channels=["Nope"])

    assert result["success"] is False
    assert "unknown channel" in result["error"]
    assert "BaseColor" in result["error"]


def test_set_layer_channels_rejects_empty_set(monkeypatch):
    stack = _FakeStack({})
    node = _Node(stack)
    _install_host(monkeypatch, stack=stack, node=node)

    result = _load("painter-material", "set_layer_channels").main(layer_uid=7, channels=[])

    assert result["success"] is False


def test_set_layer_channels_detects_readback_mismatch(monkeypatch):
    class _Stubborn(_Node):
        @property
        def active_channels(self):
            return {"bc"}

        @active_channels.setter
        def active_channels(self, value):
            pass

    stack = _FakeStack({})
    node = _Stubborn(stack)
    _install_host(monkeypatch, stack=stack, node=node)

    result = _load("painter-material", "set_layer_channels").main(layer_uid=7, channels=["Roughness"])

    assert result["success"] is False
    assert result["error"] == "HOST_READBACK_MISMATCH"
    assert result["context"]["expected"] == ["ro"]
    assert result["context"]["actual"] == ["bc"]


def test_set_layer_channel_value_writes_uniform_source(monkeypatch):
    stack = _FakeStack({})
    node = _Node(stack, active=("ro",))
    _install_host(monkeypatch, stack=stack, node=node)

    result = _load("painter-material", "set_layer_channel_value").main(
        layer_uid=7, channel="Roughness", value=[0.3, 0.3, 0.3]
    )

    assert result["success"] is True
    assert node.sources == [("ro", (0.3, 0.3, 0.3))]
    # Value readback is unavailable, so the tool must not claim verification.
    assert result["postcondition"]["verified"] is False
    assert result["postcondition"]["verifies"] == "channel_activation_only"


def test_set_layer_channel_value_rejects_out_of_range(monkeypatch):
    stack = _FakeStack({})
    node = _Node(stack, active=("ro",))
    _install_host(monkeypatch, stack=stack, node=node)
    module = _load("painter-material", "set_layer_channel_value")

    assert module.main(layer_uid=7, channel="Roughness", value=[1.5, 0.0, 0.0])["success"] is False
    assert module.main(layer_uid=7, channel="Roughness", value=[0.1, 0.2])["success"] is False


def test_set_layer_channel_value_reports_unknown_channel(monkeypatch):
    stack = _FakeStack({})
    node = _Node(stack, active=("ro",))
    _install_host(monkeypatch, stack=stack, node=node)

    result = _load("painter-material", "set_layer_channel_value").main(
        layer_uid=7, channel="Emissive", value=[0.5, 0.5, 0.5]
    )

    assert result["success"] is False
    assert result["error"] == "channel_not_found"
    assert result["context"]["supported_channels"] == ["BaseColor", "Metallic", "Roughness"]


def test_set_layer_channel_value_detects_inactive_channel(monkeypatch):
    stack = _FakeStack({})
    # Channel is not active, so activation cannot be confirmed after the write.
    node = _Node(stack, active=("bc",))
    _install_host(monkeypatch, stack=stack, node=node)

    result = _load("painter-material", "set_layer_channel_value").main(
        layer_uid=7, channel="Roughness", value=[0.5, 0.5, 0.5]
    )

    assert result["success"] is False
    assert result["error"] == "HOST_READBACK_MISMATCH"
