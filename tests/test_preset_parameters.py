"""Unit tests for the host-agnostic preset parameter helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dcc_mcp_substance3d_painter import preset_parameters as pp  # noqa: E402

SCRIPTS = ROOT / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-presets" / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"preset_{name}", SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Stack:
    def __init__(self, name: str = "") -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _LayerNode:
    """A bare layer-stack node: identity and traversal, no parameter methods."""

    def __init__(self, uid: int, stack: _Stack) -> None:
        self._uid = uid
        self._stack = stack

    def uid(self) -> int:
        return self._uid

    def get_name(self) -> str:
        return f"preset-{self._uid}"

    def get_type(self) -> SimpleNamespace:
        return SimpleNamespace(name="Generator")

    def get_stack(self) -> _Stack:
        return self._stack

    def sub_layers(self):
        return []

    def content_effects(self):
        return []

    def mask_effects(self):
        return []


class _ParamNode(_LayerNode):
    """A host node exposing one flavour of the parameter accessor aliases."""

    def __init__(self, uid: int, stack: _Stack, params: dict | None = None, *, flavour: str = "standard") -> None:
        super().__init__(uid, stack)
        self._params = dict(params or {})
        self._flavour = flavour

    def get_parameters(self):  # lister
        if self._flavour == "no_list":
            raise RuntimeError("not available")
        return dict(self._params)

    def get_parameter(self, name):  # getter
        return self._params[name]

    def set_parameter(self, name, value):  # setter
        self._params[name] = value


class _GroupNode(_LayerNode):
    """A smart-material group whose parameters live on an inner effect."""

    def __init__(self, uid: int, stack: _Stack, effect: _ParamNode) -> None:
        super().__init__(uid, stack)
        self._effect = effect

    def content_effects(self):
        return [self._effect]


class _LayerStack:
    def __init__(self, nodes, roots) -> None:
        self._nodes = nodes
        self._roots = roots

    def get_node_by_uid(self, uid):
        return self._nodes.get(uid)

    def get_root_layer_nodes(self, stack):
        return list(self._roots)


def _install(
    monkeypatch, *, open_project=True, layerstack=None, textureset=None, resource=None, export=None, stack=None
):
    painter = ModuleType("substance_painter")
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=open_project)
    painter.project = project
    modules = {"project": project}
    if textureset is None:
        textureset = ModuleType("substance_painter.textureset")
        textureset.get_active_stack = MagicMock(return_value=stack)
    painter.textureset = textureset
    modules["textureset"] = textureset
    if layerstack is not None:
        painter.layerstack = layerstack
        modules["layerstack"] = layerstack
    if resource is not None:
        painter.resource = resource
        modules["resource"] = resource
    if export is not None:
        painter.export = export
        modules["export"] = export
    monkeypatch.setitem(sys.modules, "substance_painter", painter)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, f"substance_painter.{name}", module)
    return painter


# ---------------------------------------------------------------------------
# Pure validation / serialization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [True, False, 0, 42, -7, 0.5, -1.25, "hello", "", [1, 2, 3], [0.5, 0.25, 0.75, 1.0], (True, 2)],
)
def test_validate_parameter_value_accepts_bounded_json(value):
    result = pp.validate_parameter_value(value)
    assert result is not None


@pytest.mark.parametrize(
    "value",
    [None, {}, {"a": 1}, float("nan"), float("inf"), float("-inf"), [1, [2]], [], [1, 2, 3, 4, 5], object()],
)
def test_validate_parameter_value_rejects_unbounded(value):
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_value(value)


def test_validate_parameter_value_rejects_long_strings_and_control_chars():
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_value("x" * (pp.MAX_STRING_LENGTH + 1))
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_value("bad\nvalue")


@pytest.mark.parametrize("name", [123, "", "x" * (pp.MAX_NAME_LENGTH + 1), "bad\nname", None])
def test_validate_parameter_name_rejects_bad_names(name):
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_bindings({name: 1})


def test_validate_parameter_bindings_bounds():
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_bindings({})
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_bindings({f"p{i}": i for i in range(pp.MAX_PARAMETERS + 1)})
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_bindings("not a dict")
    assert pp.validate_parameter_bindings({"a": 1, "b": [0.1, 0.2]}) == {"a": 1, "b": [0.1, 0.2]}


def test_serialize_parameter_value_is_bounded():
    assert pp.serialize_parameter_value(float("inf")) == "inf"
    assert pp.serialize_parameter_value("x" * 500) == "x" * pp.MAX_STRING_LENGTH
    assert pp.serialize_parameter_value([1, 2, 3, 4, 5]) == [1, 2, 3, 4]
    assert pp.serialize_parameter_value(None) is None
    assert pp.serialize_parameter_value(True) is True


@pytest.mark.parametrize(
    ("expected", "actual", "equal"),
    [
        (1, 1, True),
        (0.5, 0.5 + 1e-9, True),
        (1, 2, False),
        ("a", "a", True),
        ("a", "b", False),
        (True, True, True),
        (True, 1, False),
        ([1, 2], [1, 2], True),
        ([1, 2], [1, 3], False),
        (1, pp.MISSING, False),
        (1, None, False),
    ],
)
def test_values_equal(expected, actual, equal):
    assert pp.values_equal(expected, actual) is equal


# ---------------------------------------------------------------------------
# Capability probing and access
# ---------------------------------------------------------------------------


def test_resolve_parameter_access_standard_node():
    node = _ParamNode(1, _Stack(), {"scale": 0.5})
    access = pp.resolve_parameter_access(node)
    assert access.capabilities() == {"list": True, "read": True, "write": True, "readback": True}
    assert pp.read_all_parameters(access) == {"scale": 0.5}
    assert pp.read_parameter(access, "scale") == 0.5


def test_resolve_parameter_access_no_interface():
    bare = SimpleNamespace(uid=lambda: 9)
    access = pp.resolve_parameter_access(bare)
    assert access.capabilities() == {"list": False, "read": False, "write": False, "readback": False}
    assert pp.read_all_parameters(access) is None
    assert pp.read_parameter(access, "anything") is pp.MISSING
    with pytest.raises(pp.ParameterValidationError):
        pp.write_parameter(access, "x", 1)


def test_resolve_parameter_access_mapping_attribute_as_lister():
    node = SimpleNamespace(parameters={"offset": 2}, uid=lambda: 3)
    access = pp.resolve_parameter_access(node)
    assert access.capabilities()["list"] is True
    assert pp.read_all_parameters(access) == {"offset": 2}


def test_resolve_parameter_access_bulk_setter_only():
    written = {}

    class Bulk:
        def set_parameters(self, mapping):
            written.update(mapping)

    access = pp.resolve_parameter_access(Bulk())
    assert access.capabilities()["write"] is True
    pp.write_parameter(access, "density", 0.7)
    assert written == {"density": 0.7}


def test_read_all_parameters_list_of_descriptors():
    class Descriptors:
        def get_parameters(self):
            return [{"name": "a", "value": 1}, ["b", 2], "c"]

        def get_parameter(self, name):
            return {"c": 3}[name]

    access = pp.resolve_parameter_access(Descriptors())
    assert pp.read_all_parameters(access) == {"a": 1, "b": 2, "c": 3}


def test_read_all_parameters_name_list_without_getter_does_not_recurse():
    class NamesOnly:
        def get_parameters(self):
            return ["a", "b"]

    access = pp.resolve_parameter_access(NamesOnly())
    assert pp.read_all_parameters(access) == {"a": None, "b": None}


def test_read_all_parameters_lister_failure_is_empty():
    node = _ParamNode(1, _Stack(), {"a": 1}, flavour="no_list")
    access = pp.resolve_parameter_access(node)
    # getter still exists, so readback works even if lister fails.
    assert pp.read_parameter(access, "a") == 1


def test_resolve_preset_access_descends_into_group_effect():
    stack = _Stack()
    effect = _ParamNode(11, stack, {"weave": 4})
    group = _GroupNode(10, stack, effect)
    node, access = pp.resolve_preset_access(group)
    assert node is effect
    assert pp.read_all_parameters(access) == {"weave": 4}


# ---------------------------------------------------------------------------
# Node resolution
# ---------------------------------------------------------------------------


def test_find_preset_node_via_get_node_by_uid():
    stack = _Stack()
    node = _ParamNode(5, stack, {"a": 1})
    layerstack = _LayerStack({5: node}, [node])
    assert pp.find_preset_node(layerstack, stack, 5) is node


def test_find_preset_node_via_effect_walk():
    stack = _Stack()
    effect = _ParamNode(11, stack, {"a": 1})
    group = _GroupNode(10, stack, effect)
    # get_node_by_uid knows only the group, not the effect.
    layerstack = _LayerStack({10: group}, [group])
    assert pp.find_preset_node(layerstack, stack, 11) is effect


def test_find_preset_node_missing_returns_none():
    stack = _Stack()
    layerstack = _LayerStack({}, [])
    assert pp.find_preset_node(layerstack, stack, 999) is None


# ---------------------------------------------------------------------------
# Script-level tests (mocked substance_painter)
# ---------------------------------------------------------------------------


def test_list_preset_resources_filters_to_preset_usages(monkeypatch):
    generator = SimpleNamespace(
        usages=lambda: [SimpleNamespace(name="GENERATOR")],
        identifier=lambda: SimpleNamespace(url=lambda: "resource://gen/droplets"),
        gui_name=lambda: "Water Droplets",
        category=lambda: "procedural",
        type=lambda: SimpleNamespace(name="GENERATOR"),
    )
    texture = SimpleNamespace(
        usages=lambda: [SimpleNamespace(name="TEXTURE")],
        identifier=lambda: SimpleNamespace(url=lambda: "resource://tex/foo"),
        gui_name=lambda: "Foo",
        category=lambda: "texture",
        type=lambda: SimpleNamespace(name="TEXTURE"),
    )
    resource = ModuleType("substance_painter.resource")
    resource.search = MagicMock(return_value=[generator, texture])
    _install(monkeypatch, resource=resource)

    result = _load("list_preset_resources").main(query="droplet")
    assert result["success"] is True
    assert result["context"]["match_count"] == 1
    assert result["context"]["resources"][0]["url"] == "resource://gen/droplets"


def test_list_preset_resources_rejects_bad_limit(monkeypatch):
    resource = ModuleType("substance_painter.resource")
    resource.search = MagicMock(return_value=[])
    _install(monkeypatch, resource=resource)
    result = _load("list_preset_resources").main(limit=0)
    assert result["success"] is False


def test_inspect_preset_parameters_reports_values(monkeypatch):
    stack = _Stack()
    node = _ParamNode(7, stack, {"droplet_density": 0.4, "weave_scale": 2.0})
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("inspect_preset_parameters").main(node_uid=7)
    assert result["success"] is True
    assert result["context"]["parameter_count"] == 2
    assert result["context"]["parameters"]["droplet_density"] == 0.4
    assert result["context"]["capabilities"]["write"] is True


def test_inspect_preset_parameters_unsupported_host(monkeypatch):
    stack = _Stack()
    bare = SimpleNamespace(
        uid=lambda: 7,
        get_name=lambda: "preset",
        get_type=lambda: SimpleNamespace(name="Generator"),
        get_stack=lambda: stack,
    )
    layerstack = _LayerStack({7: bare}, [bare])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("inspect_preset_parameters").main(node_uid=7)
    assert result["success"] is True
    assert result["context"]["parameter_count"] == 0
    assert result["context"]["capabilities"] == {"list": False, "read": False, "write": False, "readback": False}


def test_inspect_preset_parameters_node_not_found(monkeypatch):
    stack = _Stack()
    layerstack = _LayerStack({}, [])
    _install(monkeypatch, layerstack=layerstack, stack=stack)
    result = _load("inspect_preset_parameters").main(node_uid=404)
    assert result["success"] is False
    assert result["error"] == "HOST_PRESET_NODE_NOT_FOUND"


def test_inspect_preset_parameters_no_project_open(monkeypatch):
    _install(monkeypatch, open_project=False)
    result = _load("inspect_preset_parameters").main(node_uid=7)
    assert result["success"] is False


def test_set_preset_parameters_verified_readback(monkeypatch):
    stack = _Stack()
    node = _ParamNode(7, stack, {"droplet_density": 0.4})
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"droplet_density": 0.85, "seed": 3})
    assert result["success"] is True
    assert result["postcondition"]["verified"] is True
    assert result["postcondition"]["method"] == "preset_parameter_readback"
    assert result["context"]["observed"]["droplet_density"] == 0.85
    assert node._params["droplet_density"] == 0.85


def test_set_preset_parameters_rejects_invalid_before_host_mutation(monkeypatch):
    stack = _Stack()
    node = _ParamNode(7, stack, {"a": 1})
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"a": float("inf")})
    assert result["success"] is False
    assert node._params["a"] == 1


def test_set_preset_parameters_unsupported_host(monkeypatch):
    stack = _Stack()
    bare = SimpleNamespace(
        uid=lambda: 7,
        get_name=lambda: "preset",
        get_type=lambda: SimpleNamespace(name="Generator"),
        get_stack=lambda: stack,
    )
    layerstack = _LayerStack({7: bare}, [bare])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"a": 1})
    assert result["success"] is False
    assert result["error"] == "preset_parameter_write_unsupported"


def test_set_preset_parameters_readback_mismatch(monkeypatch):
    stack = _Stack()

    class StaleNode(_ParamNode):
        def set_parameter(self, name, value):
            # Accept the write but never change the stored value.
            return None

    node = StaleNode(7, stack, {"droplet_density": 0.4})
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"droplet_density": 0.9})
    assert result["success"] is False
    assert result["error"] == "HOST_READBACK_MISMATCH"
    assert "droplet_density" in result["context"]["mismatches"]


def test_set_preset_parameters_write_only_no_readback(monkeypatch):
    stack = _Stack()

    class WriteOnlyNode(_LayerNode):
        # Only a setter is exposed; no getter, no lister, no parameters attribute.
        def set_parameter(self, name, value):
            self.written = (name, value)

    node = WriteOnlyNode(7, stack)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"a": 5})
    assert result["success"] is True
    assert result["postcondition"]["verified"] is False
    assert result["postcondition"]["verifies"] == "write_accepted_only"
    assert node.written == ("a", 5)


def test_set_preset_parameters_group_resolves_inner_effect(monkeypatch):
    stack = _Stack()
    effect = _ParamNode(11, stack, {"weave": 1})
    group = _GroupNode(10, stack, effect)
    layerstack = _LayerStack({10: group}, [group])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=10, parameters={"weave": 6})
    assert result["success"] is True
    assert result["postcondition"]["verified"] is True
    assert effect._params["weave"] == 6
