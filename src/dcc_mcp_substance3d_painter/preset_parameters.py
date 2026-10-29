"""Host-agnostic helpers for generator / smart-material preset parameters.

This module never imports the Painter host. Skill scripts resolve a layer-stack
node from the running project, then pass it here to reach its parameter source
and to read or write bounded, JSON-safe parameter values.

Substance 3D Painter does NOT expose preset parameters on the layer-stack node.
A generator effect (``GeneratorEffectNode``) exposes ``get_source()`` which
returns a ``SourceSubstance``; that ``SourceSubstance`` is the object that owns
``get_parameters()`` / ``set_parameters(mapping)`` (see the Adobe Painter Python
API). A fill layer exposes its substance through ``get_source(channeltype)``.
So :func:`resolve_preset_access` walks the node, its sub-layers and its effects,
resolves each one's source, and probes *the source* for a parameter interface —
mirroring how ``painter_state.resource_url_from_effect`` and
``add_generator_effect`` already reach ``effect.get_source()``.

Painter releases expose the parameter controls under different names, so every
accessor is resolved by probing a bounded set of known aliases (the same
``resolve_callable`` discipline used by :mod:`painter_state`). A host that
exposes none of them is reported as unsupported rather than being driven
through an invented API. Parameter values are restricted to strict-JSON
scalars and short flat vectors so a preset write can always be serialized back
to the agent.

``SourceSubstance.set_parameters`` rejects Python ``bool`` values; boolean
parameters are coerced to ``0`` / ``1`` integers on the way to the host.
Readback values may be ``Color`` objects or tuples, which are normalized to
flat numeric vectors before serialization and comparison.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import islice
from typing import Any, Callable, Mapping, Optional

from dcc_mcp_substance3d_painter.painter_state import find_node, resolve_callable

# The parameter source of a generator effect / fill layer: node.get_source().
_SOURCE_GETTER_NAMES = ("get_source",)

# Parameter accessors resolved on the SourceSubstance (never on the node itself).
_GETTER_NAMES = ("get_parameter", "get_parameter_value", "get_property", "get_input")
_SETTER_NAMES = ("set_parameter", "set_parameter_value", "set_property", "set_input")
_BULK_SETTER_NAMES = ("set_parameters", "set_properties", "set_parameter_values")
# Value-bearing listers return a name -> value mapping (or value descriptors).
_VALUE_LISTER_NAMES = ("get_parameters", "get_parameter_values", "list_parameters", "get_properties")
# Name-only listers return parameter names but no values. They can enumerate a
# preset's parameters, but a write cannot be verified through them (P2-CR1).
_NAME_LISTER_NAMES = ("get_parameter_names", "list_parameter_names")
_LIST_ATTRIBUTE = "parameters"

MAX_PARAMETERS = 64
MAX_NAME_LENGTH = 128
MAX_STRING_LENGTH = 256
MAX_VECTOR_LENGTH = 4
# Largest range of integers exactly representable as a double. Keeping writes
# within this bound also keeps readback float() comparisons safe (P3-CR2).
MAX_INTEGER = 2**53

# Sentinel for "parameter could not be read". Kept public so scripts can build
# a JSON-safe observed map without leaking a non-serializable object.
MISSING = object()

_HOST_READ_ERRORS = (AttributeError, KeyError, OverflowError, RuntimeError, TypeError, ValueError)


class ParameterValidationError(ValueError):
    """Raised when a parameter name or value is outside the supported contract."""


@dataclass(frozen=True)
class ParameterAccess:
    """Resolved parameter controls for one parameter source (a SourceSubstance).

    ``source`` is the parameter-bearing object returned by ``get_source()``.
    ``node`` is the owning layer-stack node/effect, kept only for reporting.
    ``lister_has_values`` records whether the resolved lister returns actual
    values (as opposed to names alone); this decides whether a write can be
    honestly confirmed by readback.
    """

    source: Any
    node: Any = None
    getter: Optional[Callable[..., Any]] = None
    setter: Optional[Callable[..., Any]] = None
    bulk_setter: Optional[Callable[..., Any]] = None
    lister: Optional[Callable[..., Any]] = None
    lister_has_values: bool = False

    def capabilities(self) -> dict[str, bool]:
        can_enumerate = self.lister is not None
        can_read_values = self.getter is not None or (self.lister is not None and self.lister_has_values)
        return {
            "list": can_enumerate,
            "read": can_read_values,
            "write": self.setter is not None or self.bulk_setter is not None,
            "readback": can_read_values,
        }


def _resolve_lister(source: Any) -> tuple[Optional[Callable[..., Any]], bool]:
    """Return ``(lister, has_values)`` for the first matching lister alias."""

    candidate = resolve_callable(source, _VALUE_LISTER_NAMES)
    if candidate is not None:
        return candidate, True
    candidate = resolve_callable(source, _NAME_LISTER_NAMES)
    if candidate is not None:
        return candidate, False
    attribute = getattr(source, _LIST_ATTRIBUTE, None)
    if isinstance(attribute, Mapping):
        # Bind the live mapping so reads see current host state.
        return lambda: attribute, True
    return None, False


def resolve_parameter_access(source: Any, node: Any = None) -> ParameterAccess:
    """Probe one parameter *source* (a SourceSubstance) for its interface.

    A missing interface yields an access whose capabilities are all false,
    which scripts surface as an explicit unsupported result.
    """

    lister, has_values = _resolve_lister(source)
    return ParameterAccess(
        source=source,
        node=node,
        getter=resolve_callable(source, _GETTER_NAMES),
        setter=resolve_callable(source, _SETTER_NAMES),
        bulk_setter=resolve_callable(source, _BULK_SETTER_NAMES),
        lister=lister,
        lister_has_values=has_values,
    )


def validate_parameter_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ParameterValidationError("parameter names must be strings")
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        raise ParameterValidationError(f"parameter name must be 1..{MAX_NAME_LENGTH} characters")
    if any(ord(char) < 32 for char in name):
        raise ParameterValidationError("parameter name must not contain control characters")
    return name


def _validate_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_INTEGER <= value <= MAX_INTEGER:
            raise ParameterValidationError(f"parameter integer must be within ±{MAX_INTEGER}")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ParameterValidationError("parameter value must be a finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH or any(ord(char) < 32 for char in value):
            raise ParameterValidationError("parameter string must be short and free of control characters")
        return value
    raise ParameterValidationError("unsupported parameter value type")


def validate_parameter_value(value: Any) -> Any:
    """Validate and normalize one JSON-safe parameter value."""

    if isinstance(value, (list, tuple)):
        if not 1 <= len(value) <= MAX_VECTOR_LENGTH:
            raise ParameterValidationError(f"parameter vector must hold 1..{MAX_VECTOR_LENGTH} scalars")
        return [_validate_scalar(item) for item in value]
    return _validate_scalar(value)


def validate_parameter_bindings(bindings: Any) -> dict[str, Any]:
    """Validate a name -> value mapping before any host mutation."""

    if not isinstance(bindings, Mapping):
        raise ParameterValidationError("parameters must be an object")
    if not 1 <= len(bindings) <= MAX_PARAMETERS:
        raise ParameterValidationError(f"parameters must hold 1..{MAX_PARAMETERS} entries")
    resolved: dict[str, Any] = {}
    for name, value in bindings.items():
        resolved[validate_parameter_name(name)] = validate_parameter_value(value)
    return resolved


def _coerce_color(value: Any) -> Optional[list[float]]:
    """Return a flat numeric vector for a Color/tuple-like host value, else None."""

    if isinstance(value, (str, bytes, bytearray, Mapping, list, tuple)):
        return None
    # A Color can be a sequence of channel floats.
    if hasattr(value, "__iter__"):
        try:
            items = list(islice(value, MAX_VECTOR_LENGTH + 1))
            if items and all(isinstance(i, (int, float)) and not isinstance(i, bool) for i in items):
                return [float(i) for i in items[:MAX_VECTOR_LENGTH]]
        except _HOST_READ_ERRORS:
            pass
    # A Color can also expose named channel attributes (r/g/b/a, x/y/z/w...).
    for attrs in (("r", "g", "b", "a"), ("r", "g", "b"), ("x", "y", "z", "w"), ("x", "y", "z")):
        try:
            channels = [getattr(value, attr) for attr in attrs]
        except (AttributeError, TypeError, ValueError):
            continue
        if all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in channels):
            return [float(c) for c in channels]
    return None


def serialize_parameter_value(value: Any) -> Any:
    """Coerce a host-returned parameter value into a bounded JSON-safe form."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:MAX_STRING_LENGTH]
    if isinstance(value, (list, tuple)):
        return [serialize_parameter_value(item) for item in value[:MAX_VECTOR_LENGTH]]
    if isinstance(value, Mapping):
        return {
            str(key)[:MAX_NAME_LENGTH]: serialize_parameter_value(item)
            for key, item in islice(value.items(), MAX_PARAMETERS)
        }
    color = _coerce_color(value)
    if color is not None:
        return color
    return str(value)[:MAX_STRING_LENGTH]


def read_all_parameters(access: ParameterAccess) -> Optional[dict[str, Any]]:
    """Return a name -> value map, or ``None`` when no lister is exposed."""

    if access.lister is None:
        return None
    try:
        raw = access.lister()
    except _HOST_READ_ERRORS:
        return None
    if isinstance(raw, Mapping):
        return {
            str(key)[:MAX_NAME_LENGTH]: serialize_parameter_value(value)
            for key, value in islice(raw.items(), MAX_PARAMETERS)
        }
    if isinstance(raw, (list, tuple)):
        result: dict[str, Any] = {}
        for item in islice(raw, MAX_PARAMETERS):
            if isinstance(item, str):
                key = item[:MAX_NAME_LENGTH]
                # Read names through the getter only, never back into
                # read_all_parameters, which would recurse on name-only lists.
                if access.getter is not None:
                    try:
                        result[key] = serialize_parameter_value(access.getter(item))
                    except _HOST_READ_ERRORS:
                        result[key] = None
                else:
                    result[key] = None
            elif isinstance(item, Mapping) and "name" in item:
                key = str(item["name"])[:MAX_NAME_LENGTH]
                result[key] = serialize_parameter_value(item.get("value", item.get("default")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                result[str(item[0])[:MAX_NAME_LENGTH]] = serialize_parameter_value(item[1])
        return result
    return None


def read_parameter(access: ParameterAccess, name: str) -> Any:
    """Read one parameter, returning ``MISSING`` when it cannot be read."""

    if access.getter is not None:
        try:
            return serialize_parameter_value(access.getter(name))
        except _HOST_READ_ERRORS:
            return MISSING
    all_values = read_all_parameters(access)
    if all_values is None:
        return MISSING
    return all_values.get(name, MISSING)


def _to_host_value(value: Any) -> Any:
    """Coerce a validated value into what ``SourceSubstance.set_parameters`` accepts.

    The Painter host rejects Python ``bool`` for boolean parameters; they must
    be ``0`` / ``1`` integers. Vectors are coerced element-wise.
    """

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple)):
        return [_to_host_value(item) for item in value]
    return value


def write_parameter(access: ParameterAccess, name: str, value: Any) -> None:
    """Write one parameter through the first available setter."""

    host_value = _to_host_value(value)
    if access.setter is not None:
        access.setter(name, host_value)
        return
    if access.bulk_setter is not None:
        access.bulk_setter({name: host_value})
        return
    raise ParameterValidationError("no parameter setter is exposed by this source")


def resolve_channel_types(textureset: Any) -> list[Any]:
    """Return the host's ChannelType members for fill-layer ``get_source(channel)``."""

    enum_type = getattr(textureset, "ChannelType", None)
    members = getattr(enum_type, "__members__", None)
    if not members:
        return []
    try:
        return [member for _, member in members.items()]
    except (AttributeError, TypeError, ValueError):
        return []


def _resolve_sources(node: Any, channel_types: tuple):
    """Yield parameter-source objects reachable from one layer-stack node/effect.

    A generator effect exposes ``get_source()`` with no argument. A fill layer
    needs ``get_source(channeltype)``; when the no-arg call fails or returns
    nothing, every supplied channel type is tried.
    """

    getter = resolve_callable(node, _SOURCE_GETTER_NAMES)
    if getter is None:
        return
    try:
        source = getter()
    except _HOST_READ_ERRORS:
        source = None
    if source is not None and source is not node:
        yield source
        return
    for channel in channel_types:
        try:
            source = getter(channel)
        except _HOST_READ_ERRORS:
            continue
        if source is not None and source is not node:
            yield source


def resolve_preset_access(
    node: Any,
    channel_types: tuple = (),
    required_capability: str | None = None,
) -> tuple[Any, ParameterAccess]:
    """Return ``(owner_node, access)`` for the parameter-bearing source.

    Walk the node, its sub-layers and its content/mask effects; for each,
    resolve its ``SourceSubstance`` through ``get_source()`` and probe *that
    source*. Prefer a source that satisfies ``required_capability`` (e.g.
    ``"write"``); otherwise return the first source exposing any capability.
    The returned owner is a layer-stack node/effect (usable with
    ``node_summary``), never the source object. The search is bounded by
    identity so cyclic host graphs cannot loop.
    """

    def matches(access: ParameterAccess) -> bool:
        capabilities = access.capabilities()
        if required_capability is not None:
            return bool(capabilities.get(required_capability, False))
        return any(capabilities.values())

    fallback: Optional[tuple[Any, ParameterAccess]] = None
    for candidate in _candidate_nodes(node):
        for source in _resolve_sources(candidate, channel_types):
            access = resolve_parameter_access(source, node=candidate)
            if matches(access):
                return candidate, access
            if fallback is None and any(access.capabilities().values()):
                fallback = (candidate, access)
    if fallback is not None:
        return fallback
    # No parameter source anywhere: report honest all-false capabilities.
    return node, ParameterAccess(source=None, node=node)


def _candidate_nodes(node: Any):
    """Yield the node, its sub-layers, and all their content/mask effects."""

    seen: set[int] = set()
    for layer in _walk_layers(node):
        for candidate in (layer, *_node_effects(layer)):
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            yield candidate


def _node_effects(node: Any):
    """Yield the content/mask effects attached to *node*, if any."""

    for accessor in ("content_effects", "mask_effects"):
        getter = getattr(node, accessor, None)
        if not callable(getter):
            continue
        try:
            effects = list(getter())
        except _HOST_READ_ERRORS:
            continue
        yield from effects


def _compare_scalar(value: Any) -> Any:
    """Normalize a bool to int so a host 0/1 readback compares equal."""

    return int(value) if isinstance(value, bool) else value


def values_equal(expected: Any, actual: Any) -> bool:
    """Compare a normalized value against a host readback value, tolerantly.

    Booleans written to the host come back as ``0`` / ``1`` integers, and color
    parameters may come back as ``Color`` objects or tuples already normalized
    to flat vectors by :func:`serialize_parameter_value`. Both sides are
    normalized so a legitimate match is never reported as a mismatch.
    """

    if actual is MISSING or actual is None:
        return False
    expected = _compare_scalar(expected)
    actual = _compare_scalar(actual)
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)) or len(expected) != len(actual):
            return False
        return all(values_equal(left, right) for left, right in zip(expected, actual))
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        try:
            return math.isclose(float(expected), float(actual), rel_tol=1e-6, abs_tol=1e-9)
        except (OverflowError, ValueError):
            return expected == actual
    return expected == actual


def find_preset_node(layerstack: Any, stack: Any, node_uid: int) -> Any | None:
    """Resolve a preset instance node by uid within *stack*.

    A smart-material instance is a node in the layer stack and is found by the
    shared ``find_node`` (which binds it to the selected stack). A generator is
    an *effect* attached to a layer and is not always returned by
    ``get_node_by_uid``, so fall back to walking content/mask effects of the
    stack's root layers. Never fabricate a node: return ``None`` when the uid
    is not part of this stack.
    """

    node = find_node(layerstack, int(node_uid), stack)
    if node is not None:
        return node
    roots_getter = getattr(layerstack, "get_root_layer_nodes", None)
    if not callable(roots_getter):
        return None
    try:
        roots = list(roots_getter(stack))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    for top in roots:
        for layer in _walk_layers(top):
            for accessor in ("content_effects", "mask_effects"):
                getter = getattr(layer, accessor, None)
                if not callable(getter):
                    continue
                try:
                    effects = list(getter())
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    continue
                for effect in effects:
                    try:
                        if int(effect.uid()) == int(node_uid):
                            return effect
                    except (AttributeError, RuntimeError, TypeError, ValueError):
                        continue
    return None


def _walk_layers(node: Any):
    """Yield *node* then descend into group sub-layers, bounded by identity."""

    seen: set[int] = set()
    pending = [node]
    while pending:
        current = pending.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current
        getter = getattr(current, "sub_layers", None)
        if not callable(getter):
            continue
        try:
            children = list(getter())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
        pending.extend(children)
