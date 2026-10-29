"""Host-agnostic helpers for generator / smart-material preset parameters.

This module never imports the Painter host. Skill scripts resolve a layer-stack
node from the running project, then pass it here to probe for a parameter
interface and to read or write bounded, JSON-safe parameter values.

Painter releases expose parameter controls under different names, so every
accessor is resolved by probing a bounded set of known aliases (the same
``resolve_callable`` discipline used by :mod:`painter_state`). A host that
exposes none of them is reported as unsupported rather than being driven
through an invented API. Parameter values are restricted to strict-JSON
scalars and short flat vectors so a preset write can always be serialized back
to the agent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from dcc_mcp_substance3d_painter.painter_state import find_node, resolve_callable

# Bounded alias sets. The first callable found wins; keep these short and
# auditable so a probe stays cheap across Painter releases.
_GETTER_NAMES = ("get_parameter", "get_parameter_value", "get_property", "get_input")
_SETTER_NAMES = ("set_parameter", "set_parameter_value", "set_property", "set_input")
_BULK_SETTER_NAMES = ("set_parameters", "set_properties", "set_parameter_values")
_LISTER_NAMES = ("get_parameters", "list_parameters", "get_parameter_names")
_LIST_ATTRIBUTE = "parameters"

MAX_PARAMETERS = 64
MAX_NAME_LENGTH = 128
MAX_STRING_LENGTH = 256
MAX_VECTOR_LENGTH = 4

# Sentinel for "parameter could not be read". Kept public so scripts can build
# a JSON-safe observed map without leaking a non-serializable object.
MISSING = object()


class ParameterValidationError(ValueError):
    """Raised when a parameter name or value is outside the supported contract."""


@dataclass(frozen=True)
class ParameterAccess:
    """Resolved parameter controls for one host node."""

    node: Any
    getter: Optional[Callable[..., Any]] = None
    setter: Optional[Callable[..., Any]] = None
    bulk_setter: Optional[Callable[..., Any]] = None
    lister: Optional[Callable[..., Any]] = None

    def capabilities(self) -> dict[str, bool]:
        readable = self.getter is not None or self.lister is not None
        return {
            "list": self.lister is not None,
            "read": readable,
            "write": self.setter is not None or self.bulk_setter is not None,
            "readback": readable,
        }


def _resolve_lister(node: Any) -> Optional[Callable[..., Any]]:
    candidate = resolve_callable(node, _LISTER_NAMES)
    if candidate is not None:
        return candidate
    attribute = getattr(node, _LIST_ATTRIBUTE, None)
    if isinstance(attribute, Mapping):
        # Bind the current mapping so reads see the live host state.
        return lambda: attribute
    return None


def resolve_parameter_access(node: Any) -> ParameterAccess:
    """Probe *node* for a parameter interface and return a resolved access.

    The node itself is the only probe target. A missing interface yields an
    access whose capabilities are all false, which scripts surface as an
    explicit unsupported result.
    """

    return ParameterAccess(
        node=node,
        getter=resolve_callable(node, _GETTER_NAMES),
        setter=resolve_callable(node, _SETTER_NAMES),
        bulk_setter=resolve_callable(node, _BULK_SETTER_NAMES),
        lister=_resolve_lister(node),
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
            for key, item in list(value.items())[:MAX_PARAMETERS]
        }
    return str(value)[:MAX_STRING_LENGTH]


_HOST_READ_ERRORS = (AttributeError, KeyError, RuntimeError, TypeError, ValueError)


def read_all_parameters(access: ParameterAccess) -> Optional[dict[str, Any]]:
    """Return a name -> value map, or ``None`` when no lister is exposed."""

    if access.lister is None:
        return None
    try:
        raw = access.lister()
    except _HOST_READ_ERRORS:
        return None
    if isinstance(raw, Mapping):
        return {str(key)[:MAX_NAME_LENGTH]: serialize_parameter_value(value) for key, value in raw.items()}
    if isinstance(raw, (list, tuple)):
        result: dict[str, Any] = {}
        for item in raw[:MAX_PARAMETERS]:
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


def write_parameter(access: ParameterAccess, name: str, value: Any) -> None:
    """Write one parameter through the first available setter."""

    if access.setter is not None:
        access.setter(name, value)
        return
    if access.bulk_setter is not None:
        access.bulk_setter({name: value})
        return
    raise ParameterValidationError("no parameter setter is exposed by this node")


def resolve_preset_access(node: Any) -> tuple[Any, ParameterAccess]:
    """Return ``(node, access)`` for the parameter-bearing node.

    A generator effect carries parameters directly. A smart-material *group*
    usually does not: its parameters live on the generator effects inside it.
    When the resolved node exposes no parameter interface, descend into its
    sub-layers and content/mask effects and return the first child that does.
    The search is bounded by identity so cyclic host graphs cannot loop.
    """

    access = resolve_parameter_access(node)
    if any(access.capabilities().values()):
        return node, access
    for candidate in _walk_layers(node):
        if candidate is not node:
            candidate_access = resolve_parameter_access(candidate)
            if any(candidate_access.capabilities().values()):
                return candidate, candidate_access
        for effect in _node_effects(candidate):
            effect_access = resolve_parameter_access(effect)
            if any(effect_access.capabilities().values()):
                return effect, effect_access
    return node, access


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


def values_equal(expected: Any, actual: Any) -> bool:
    """Compare a normalized value against a host readback value, tolerantly."""

    if actual is MISSING or actual is None:
        return False
    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)) or len(expected) != len(actual):
            return False
        return all(values_equal(left, right) for left, right in zip(expected, actual))
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected is actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(float(expected), float(actual), rel_tol=1e-6, abs_tol=1e-9)
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
