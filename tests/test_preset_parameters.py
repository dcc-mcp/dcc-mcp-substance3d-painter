"""Unit tests for the host-agnostic preset parameter helpers.

The mocks reflect the real Substance 3D Painter two-layer API: a layer-stack
node/effect exposes ``get_source()`` returning a ``SourceSubstance``, and that
source owns ``get_parameters()`` / ``set_parameters()``. Parameters are NEVER
probed on the node itself. ``SourceSubstance.set_parameters`` rejects Python
``bool``, so the mocks raise on bool to prove the writer coerces to int.
"""

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


class _SourceSubstance:
    """The parameter host returned by ``node.get_source()``.

    Real Painter keeps parameters on the SourceSubstance, exposes
    ``get_parameters()`` (a value map) and ``set_parameters(dict)``, and rejects
    Python ``bool`` — boolean params must be sent as ``0`` / ``1`` ints.
    """

    def __init__(self, params: dict | None = None, *, flavour: str = "standard", reject_bool: bool = True) -> None:
        self.params = dict(params or {})
        self.flavour = flavour
        self.reject_bool = reject_bool

    def get_parameters(self):  # value-bearing lister
        if self.flavour == "no_list":
            raise RuntimeError("not available")
        if self.flavour == "names_only":
            return list(self.params)
        if self.flavour == "descriptors":
            return [{"name": k, "value": v} for k, v in self.params.items()]
        return dict(self.params)

    def get_parameter_names(self):  # name-only lister
        return list(self.params)

    def get_parameter(self, name):  # single getter
        return self.params[name]

    def set_parameter(self, name, value):  # single setter
        self._check(value)
        self.params[name] = value

    def set_parameters(self, mapping):  # bulk setter (real API)
        for value in mapping.values():
            self._check(value)
        self.params.update(mapping)

    def _check(self, value):
        if self.reject_bool and isinstance(value, bool):
            raise TypeError("SourceSubstance.set_parameters does not accept bool")


class _ReadOnlySource:
    """A source that can list/read values but exposes NO setter at all.

    Capability probing checks for method existence, so a read-only source must
    simply lack the setter methods (rather than defining ones that raise).
    """

    def __init__(self, params: dict | None = None) -> None:
        self.params = dict(params or {})

    def get_parameters(self):
        return dict(self.params)

    def get_parameter(self, name):
        return self.params[name]


class _PresetNode:
    """A layer-stack node/effect: identity + traversal + ``get_source()``."""

    def __init__(
        self,
        uid: int,
        stack: _Stack,
        source: _SourceSubstance | None = None,
        *,
        subs=None,
        effects=None,
        fill: bool = False,
    ) -> None:
        self._uid = uid
        self._stack = stack
        self._source = source
        self._subs = list(subs or [])
        self._effects = list(effects or [])
        self._fill = fill

    def uid(self) -> int:
        return self._uid

    def get_name(self) -> str:
        return f"preset-{self._uid}"

    def get_type(self) -> SimpleNamespace:
        return SimpleNamespace(name="Generator")

    def get_stack(self) -> _Stack:
        return self._stack

    def get_source(self, channel=None):
        # A fill layer requires get_source(channeltype) and raises TypeError
        # when called with no argument. A generator effect takes no argument.
        if self._fill and channel is None:
            raise TypeError("get_source() requires a channeltype")
        return self._source

    def sub_layers(self):
        return list(self._subs)

    def content_effects(self):
        return list(self._effects)

    def mask_effects(self):
        return []


class _LayerStack:
    def __init__(self, nodes, roots) -> None:
        self._nodes = nodes
        self._roots = roots

    def get_node_by_uid(self, uid):
        return self._nodes.get(uid)

    def get_root_layer_nodes(self, stack):
        return list(self._roots)


def _node_with_source(uid, stack, params=None, **kwargs) -> tuple[_PresetNode, _SourceSubstance]:
    source = _SourceSubstance(params, **{k: v for k, v in kwargs.items() if k in ("flavour", "reject_bool")})
    node_kwargs = {k: v for k, v in kwargs.items() if k in ("subs", "effects", "fill")}
    node = _PresetNode(uid, stack, source, **node_kwargs)
    return node, source


def _install(
    monkeypatch,
    *,
    open_project=True,
    layerstack=None,
    textureset=None,
    resource=None,
    export=None,
    stack=None,
    channel_type=None,
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
    if channel_type is not None:
        textureset.ChannelType = channel_type
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


def test_validate_parameter_value_rejects_huge_int():
    # P3-CR2: 10**400 used to pass validation and overflow float() on readback.
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_value(10**400)
    with pytest.raises(pp.ParameterValidationError):
        pp.validate_parameter_value(-(10**400))
    assert pp.validate_parameter_value(pp.MAX_INTEGER) == pp.MAX_INTEGER
    assert pp.validate_parameter_value(-pp.MAX_INTEGER) == -pp.MAX_INTEGER


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


class _Color:
    def __init__(self, r, g, b, a=1.0):
        self.r, self.g, self.b, self.a = r, g, b, a


def test_serialize_parameter_value_is_bounded():
    assert pp.serialize_parameter_value(float("inf")) == "inf"
    assert pp.serialize_parameter_value("x" * 500) == "x" * pp.MAX_STRING_LENGTH
    assert pp.serialize_parameter_value([1, 2, 3, 4, 5]) == [1, 2, 3, 4]
    assert pp.serialize_parameter_value(None) is None
    assert pp.serialize_parameter_value(True) is True


def test_serialize_parameter_value_handles_color_and_tuple():
    # P2-BOOL: readback can be a Color object or a tuple; normalize to vectors.
    assert pp.serialize_parameter_value(_Color(0.1, 0.2, 0.3)) == [0.1, 0.2, 0.3, 1.0]
    assert pp.serialize_parameter_value((1.0, 0.5, 0.25)) == [1.0, 0.5, 0.25]
    assert pp.serialize_parameter_value(b"\x01") == "b'\\x01'"


def test_to_host_value_coerces_bool():
    # P2-BOOL: bool must never reach SourceSubstance.set_parameters.
    assert pp._to_host_value(True) == 1
    assert pp._to_host_value(False) == 0
    assert pp._to_host_value([True, 0.5, False]) == [1, 0.5, 0]
    assert pp._to_host_value(3) == 3


@pytest.mark.parametrize(
    ("expected", "actual", "equal"),
    [
        (1, 1, True),
        (0.5, 0.5 + 1e-9, True),
        (1, 2, False),
        ("a", "a", True),
        ("a", "b", False),
        (True, True, True),
        (True, 1, True),  # bool written as int, read back as int 1 (P2-BOOL)
        ([True, 1], [1, 1], True),
        ([1, 2], [1, 2], True),
        ([1, 2], [1, 3], False),
        ([0.1, 0.2, 0.3], (0.1, 0.2, 0.3), True),  # Color/vector readback
        (1, pp.MISSING, False),
        (1, None, False),
    ],
)
def test_values_equal(expected, actual, equal):
    assert pp.values_equal(expected, actual) is equal


def test_values_equal_huge_int_does_not_raise():
    # P3-CR2: float() on a huge int must not escape as OverflowError.
    assert pp.values_equal(10**400, 10**400) is True
    assert pp.values_equal(10**400, 1) is False


# ---------------------------------------------------------------------------
# Capability probing (on a SourceSubstance, never on the node)
# ---------------------------------------------------------------------------


def test_resolve_parameter_access_standard_source():
    source = _SourceSubstance({"scale": 0.5})
    access = pp.resolve_parameter_access(source)
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
    source = SimpleNamespace(parameters={"offset": 2}, uid=lambda: 3)
    access = pp.resolve_parameter_access(source)
    assert access.capabilities()["list"] is True
    assert access.capabilities()["readback"] is True
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


def test_name_only_lister_reports_no_readback():
    # P2-CR1: a lister that yields names only must not claim readback.
    class NamesOnly:
        def get_parameter_names(self):
            return ["a", "b"]

    access = pp.resolve_parameter_access(NamesOnly())
    assert access.capabilities() == {"list": True, "read": False, "write": False, "readback": False}


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


def test_read_all_parameters_mapping_truncated():
    # P3-CR3: the Mapping branch must be capped at MAX_PARAMETERS.
    big = {f"p{i}": i for i in range(pp.MAX_PARAMETERS + 20)}
    source = _SourceSubstance(big)
    access = pp.resolve_parameter_access(source)
    assert len(pp.read_all_parameters(access)) == pp.MAX_PARAMETERS


def test_read_all_parameters_lister_failure_is_empty():
    source = _SourceSubstance({"a": 1}, flavour="no_list")
    access = pp.resolve_parameter_access(source)
    # getter still exists, so readback works even if lister fails.
    assert pp.read_parameter(access, "a") == 1


def test_write_parameter_coerces_bool_to_int():
    # P2-BOOL: the source rejects bool; write_parameter must send an int.
    source = _SourceSubstance({"enabled": False})
    access = pp.resolve_parameter_access(source)
    pp.write_parameter(access, "enabled", True)
    assert source.params["enabled"] == 1
    assert isinstance(source.params["enabled"], int) and not isinstance(source.params["enabled"], bool)


def test_write_parameter_coerces_bool_in_vector():
    source = _SourceSubstance({"switches": [False, False]})
    access = pp.resolve_parameter_access(source)
    pp.write_parameter(access, "switches", [True, False])
    assert source.params["switches"] == [1, 0]


# ---------------------------------------------------------------------------
# Node -> SourceSubstance resolution (P1-API)
# ---------------------------------------------------------------------------


def test_resolve_preset_access_reads_from_get_source():
    stack = _Stack()
    node, source = _node_with_source(1, stack, {"droplet_density": 0.4})
    owner, access = pp.resolve_preset_access(node)
    assert owner is node
    assert access.source is source
    assert access.capabilities()["write"] is True
    assert pp.read_all_parameters(access) == {"droplet_density": 0.4}
    pp.write_parameter(access, "droplet_density", 0.9)
    assert source.params["droplet_density"] == 0.9


def test_resolve_preset_access_ignores_params_on_node():
    # P1-API regression: params living on the node (not get_source) are not an
    # interface real Painter exposes; the helper must not drive them.
    class NodeWithOwnParams:
        def __init__(self):
            self.params = {"rogue": 1}

        def uid(self):
            return 5

        def get_parameters(self):
            return dict(self.params)

        def set_parameter(self, name, value):
            self.params[name] = value

    node = NodeWithOwnParams()
    owner, access = pp.resolve_preset_access(node)
    assert owner is node
    assert access.capabilities() == {"list": False, "read": False, "write": False, "readback": False}


def test_resolve_preset_access_descends_into_group_effect():
    stack = _Stack()
    effect, source = _node_with_source(11, stack, {"weave": 4})
    group = _PresetNode(10, stack, None, effects=[effect])
    owner, access = pp.resolve_preset_access(group)
    assert owner is effect
    assert access.source is source
    assert pp.read_all_parameters(access) == {"weave": 4}


def test_resolve_preset_access_fill_layer_uses_channel():
    stack = _Stack()
    node, source = _node_with_source(3, stack, {"roughness": 0.5}, fill=True)

    class _ChannelType:
        __members__ = {"BASECOLOR": "BASECOLOR", "ROUGHNESS": "ROUGHNESS"}

    channel_types = tuple(pp.resolve_channel_types(SimpleNamespace(ChannelType=_ChannelType)))
    owner, access = pp.resolve_preset_access(node, channel_types)
    assert owner is node
    assert access.source is source
    assert pp.read_all_parameters(access) == {"roughness": 0.5}


def test_resolve_preset_access_required_capability_prefers_writable():
    # P3-CR4: a readable source must not shadow a writable effect deeper down.
    stack = _Stack()
    readonly = _ReadOnlySource({"weave": 1})
    writable_source = _SourceSubstance({"weave": 1})
    effect = _PresetNode(12, stack, writable_source)
    group = _PresetNode(10, stack, readonly, effects=[effect])
    owner, access = pp.resolve_preset_access(group, required_capability="write")
    assert owner is effect
    assert access.source is writable_source
    assert access.capabilities()["write"] is True


def test_resolve_preset_access_unsupported_host():
    stack = _Stack()
    bare = _PresetNode(9, stack, None)
    owner, access = pp.resolve_preset_access(bare)
    assert owner is bare
    assert access.capabilities() == {"list": False, "read": False, "write": False, "readback": False}


# ---------------------------------------------------------------------------
# Node resolution
# ---------------------------------------------------------------------------


def test_find_preset_node_via_get_node_by_uid():
    stack = _Stack()
    node, _ = _node_with_source(5, stack, {"a": 1})
    layerstack = _LayerStack({5: node}, [node])
    assert pp.find_preset_node(layerstack, stack, 5) is node


def test_find_preset_node_via_effect_walk():
    stack = _Stack()
    effect, _ = _node_with_source(11, stack, {"a": 1})
    group = _PresetNode(10, stack, None, effects=[effect])
    # get_node_by_uid knows only the group, not the effect.
    layerstack = _LayerStack({10: group}, [group])
    assert pp.find_preset_node(layerstack, stack, 11) is effect


def test_find_preset_node_missing_returns_none():
    stack = _Stack()
    layerstack = _LayerStack({}, [])
    assert pp.find_preset_node(layerstack, stack, 999) is None


# ---------------------------------------------------------------------------
# Script-level tests (mocked substance_painter, two-layer node/source)
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
    node, _ = _node_with_source(7, stack, {"droplet_density": 0.4, "weave_scale": 2.0})
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
    node, source = _node_with_source(7, stack, {"droplet_density": 0.4})
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"droplet_density": 0.85, "seed": 3})
    assert result["success"] is True
    assert result["postcondition"]["verified"] is True
    assert result["postcondition"]["method"] == "preset_parameter_readback"
    assert result["context"]["observed"]["droplet_density"] == 0.85
    assert source.params["droplet_density"] == 0.85


def test_set_preset_parameters_bool_coerced_and_verified(monkeypatch):
    # P2-BOOL: a bool value is written as int and read back as int, verified.
    stack = _Stack()
    node, source = _node_with_source(7, stack, {"enabled": False})
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"enabled": True})
    assert result["success"] is True, result
    assert result["postcondition"]["verified"] is True
    assert source.params["enabled"] == 1


def test_set_preset_parameters_rejects_invalid_before_host_mutation(monkeypatch):
    stack = _Stack()
    node, source = _node_with_source(7, stack, {"a": 1})
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"a": float("inf")})
    assert result["success"] is False
    assert source.params["a"] == 1


def test_set_preset_parameters_mixed_valid_invalid_no_partial_write(monkeypatch):
    # P3-T: a mixed valid+invalid batch must not write anything to the host.
    stack = _Stack()
    node, source = _node_with_source(7, stack, {"good": 1, "rogue": 99})
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"good": 5, "bad": float("inf")})
    assert result["success"] is False
    assert source.params == {"good": 1, "rogue": 99}


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

    class StaleSource(_SourceSubstance):
        def set_parameter(self, name, value):
            # Accept the write but never change the stored value.
            return None

        def set_parameters(self, mapping):
            return None

    source = StaleSource({"droplet_density": 0.4})
    node = _PresetNode(7, stack, source)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"droplet_density": 0.9})
    assert result["success"] is False
    assert result["error"] == "HOST_READBACK_MISMATCH"
    assert "droplet_density" in result["context"]["mismatches"]


def test_set_preset_parameters_write_only_no_readback(monkeypatch):
    stack = _Stack()

    class WriteOnlySource:
        # Only a bulk setter; no getter, no lister, no parameters attribute.
        def __init__(self):
            self.written = []

        def set_parameters(self, mapping):
            self.written.append(dict(mapping))

    source = WriteOnlySource()
    node = _PresetNode(7, stack, source)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"a": 5})
    assert result["success"] is True
    assert result["postcondition"]["verified"] is False
    assert result["postcondition"]["verifies"] == "write_accepted_only"
    assert source.written == [{"a": 5}]


def test_set_preset_parameters_name_only_lister_no_false_mismatch(monkeypatch):
    # P2-CR1: a source whose value lister yields only names must report
    # write_accepted_only, not HOST_READBACK_MISMATCH.
    stack = _Stack()

    class NamesReturningSource(_SourceSubstance):
        def get_parameters(self):
            return list(self.params)

        def get_parameter(self, name):
            return self.params[name]

    source = NamesReturningSource({"scale": 0.5})
    node = _PresetNode(7, stack, source)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"scale": 0.8})
    # The single getter succeeds, so this is a genuine verified write; the key
    # point is a name-only lister without a getter never yields a false mismatch.
    assert result["success"] is True


def test_set_preset_parameters_name_only_no_getter_write_accepted_only(monkeypatch):
    stack = _Stack()

    class NamesOnlySource:
        def get_parameter_names(self):
            return ["scale"]

        def set_parameter(self, name, value):
            self.written = (name, value)

    source = NamesOnlySource()
    node = _PresetNode(7, stack, source)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"scale": 0.8})
    assert result["success"] is True
    assert result["postcondition"]["verified"] is False
    assert result["postcondition"]["verifies"] == "write_accepted_only"
    assert result["error"] is None or result.get("error") is None


def test_set_preset_parameters_none_readback_is_unreadable_not_mismatch(monkeypatch):
    # P2-CR1 (alternate fix): a value lister claims readback capability, but a
    # parameter whose readback is None must be reported as unreadable
    # (write_accepted_only), never as HOST_READBACK_MISMATCH.
    stack = _Stack()

    class NoneValueSource:
        def get_parameters(self):
            return {"scale": None}  # value lister, but value is unreadable

        def set_parameter(self, name, value):
            self.written = (name, value)

    source = NoneValueSource()
    node = _PresetNode(7, stack, source)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"scale": 0.8})
    assert result["success"] is True
    assert result["error"] is None or result.get("error") is None
    assert result["postcondition"]["verified"] is False
    assert result["postcondition"]["verifies"] == "write_accepted_only"


def test_set_preset_parameters_group_resolves_inner_writable_effect(monkeypatch):
    # P3-CR4 at script level: a readable group source must not shadow the
    # writable generator effect that actually carries the parameters.
    stack = _Stack()
    readonly = _ReadOnlySource({"weave": 1})
    writable_source = _SourceSubstance({"weave": 1})
    effect = _PresetNode(11, stack, writable_source)
    group = _PresetNode(10, stack, readonly, effects=[effect])
    layerstack = _LayerStack({10: group}, [group])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=10, parameters={"weave": 6})
    assert result["success"] is True
    assert result["postcondition"]["verified"] is True
    assert writable_source.params["weave"] == 6
    assert readonly.params["weave"] == 1


# ---------------------------------------------------------------------------
# Round-2 review fixes (R2-1 / R2-2)
# ---------------------------------------------------------------------------


def test_get_properties_not_treated_as_value_lister():
    # Non-blocker cleanup: a real get_properties() returns Property descriptors,
    # not parameter values. It must not fabricate a value-lister/readback
    # capability the host cannot actually honour.
    class PropertySource:
        def get_properties(self):
            return [object(), object()]

    access = pp.resolve_parameter_access(PropertySource())
    assert access.lister_has_values is False
    assert access.capabilities()["list"] is False
    assert access.capabilities()["readback"] is False


def test_inspect_and_set_resolve_same_writable_owner(monkeypatch):
    # R2-1: inspect must pass required_capability="write" so both tools resolve
    # to the same writable inner effect. Otherwise an agent inspects the outer
    # read-only source's parameter table and then calls set against a different
    # source. The two sources carry different parameters to make the mismatch
    # observable.
    stack = _Stack()
    readonly = _ReadOnlySource({"group_only_param": 1})
    writable_source = _SourceSubstance({"weave": 1})
    effect = _PresetNode(11, stack, writable_source)
    group = _PresetNode(10, stack, readonly, effects=[effect])
    layerstack = _LayerStack({10: group}, [group])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    inspected = _load("inspect_preset_parameters").main(node_uid=10)
    tuned = _load("set_preset_parameters").main(node_uid=10, parameters={"weave": 6})

    assert inspected["success"] is True, inspected
    assert tuned["success"] is True, tuned
    # Both tools report the same owner: the writable inner effect (uid 11).
    assert inspected["context"]["node"]["uid"] == tuned["context"]["node"]["uid"] == effect.uid()
    # inspect now shows the writable source's parameter table + write capability.
    assert inspected["context"]["parameters"] == {"weave": 1}
    assert inspected["context"]["capabilities"]["write"] is True
    assert writable_source.params["weave"] == 6
    assert readonly.params == {"group_only_param": 1}


def test_set_preset_parameters_unknown_name_fails(monkeypatch):
    # R2-2: a value-bearing lister (get_parameters() -> dict) is the host's
    # authoritative parameter-name set. Writing a name absent from the readback
    # map means the parameter does not exist (e.g. a typo); the host silently
    # ignored the write, so this must FAIL, not a success with verified=False.
    stack = _Stack()

    class FixedKeySource(_SourceSubstance):
        def set_parameter(self, name, value):
            self._check(value)
            if name in self.params:  # a real host ignores unknown names
                self.params[name] = value

        def set_parameters(self, mapping):
            for value in mapping.values():
                self._check(value)
            for name, value in mapping.items():
                if name in self.params:
                    self.params[name] = value

    source = FixedKeySource({"weave": 1})
    node = _PresetNode(7, stack, source)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"weave_typo": 6})
    assert result["success"] is False
    assert result["error"] == "HOST_PARAMETER_UNKNOWN"
    assert "weave_typo" in result["context"]["unknown"]
    assert source.params == {"weave": 1}


def test_set_preset_parameters_unknown_name_in_mixed_batch_fails(monkeypatch):
    # R2-2: a batch mixing a valid write with a typo must still fail overall
    # (no partial success reported), and only the typo is flagged unknown.
    stack = _Stack()

    class FixedKeySource(_SourceSubstance):
        def set_parameter(self, name, value):
            self._check(value)
            if name in self.params:
                self.params[name] = value

        def set_parameters(self, mapping):
            for value in mapping.values():
                self._check(value)
            for name, value in mapping.items():
                if name in self.params:
                    self.params[name] = value

    source = FixedKeySource({"weave": 1})
    node = _PresetNode(7, stack, source)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    result = _load("set_preset_parameters").main(node_uid=7, parameters={"weave": 6, "weave_typo": 9})
    assert result["success"] is False
    assert result["error"] == "HOST_PARAMETER_UNKNOWN"
    assert result["context"]["unknown"] == ["weave_typo"]


def test_set_preset_parameters_truncated_snapshot_no_false_unknown(monkeypatch):
    # R2-2 guard: read_all_parameters caps a value lister at MAX_PARAMETERS, so
    # a truncated snapshot cannot prove a written name is unknown. With no
    # single getter, such a write stays write_accepted_only, never a false
    # HOST_PARAMETER_UNKNOWN for a large preset.
    stack = _Stack()

    class BigSource:
        def __init__(self):
            self.params = {f"p{i}": i for i in range(pp.MAX_PARAMETERS + 10)}

        def get_parameters(self):  # value lister, but NO getter
            return dict(self.params)

        def set_parameter(self, name, value):
            self.params[name] = value

    source = BigSource()
    node = _PresetNode(7, stack, source)
    layerstack = _LayerStack({7: node}, [node])
    _install(monkeypatch, layerstack=layerstack, stack=stack)

    # p73 is a real parameter but falls outside the first MAX_PARAMETERS keys.
    result = _load("set_preset_parameters").main(node_uid=7, parameters={"p73": 999})
    assert result["success"] is True
    assert result["error"] in (None, "")
    assert result["postcondition"]["verified"] is False
    assert result["postcondition"]["verifies"] == "write_accepted_only"
