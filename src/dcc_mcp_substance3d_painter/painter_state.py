"""Typed Painter state selection and readback helpers.

This module does not import Painter at module import time. Script entry points pass
the host modules in after the embedded Painter runtime has loaded them.
"""

from __future__ import annotations

from typing import Any


def enum_name(value: Any) -> str:
    return str(getattr(value, "name", value))


def node_summary(node: Any) -> dict[str, Any]:
    return {
        "uid": int(node.uid()),
        "name": str(node.get_name()),
        "type": enum_name(node.get_type()),
    }


def stack_root_path(stack: Any) -> str:
    """Return Painter's canonical export root path for a stack."""

    return str(stack)


def resolve_stack(textureset: Any, texture_set_name: str | None = None, stack_name: str | None = None) -> Any:
    if texture_set_name is None and stack_name is None:
        stack = textureset.get_active_stack()
        if stack is None:
            raise ValueError("No active Painter texture-set stack")
        return stack
    if not texture_set_name:
        raise ValueError("texture_set is required when stack is specified")
    matches = [item for item in textureset.all_texture_sets() if str(item.name()) == str(texture_set_name)]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one texture set named {texture_set_name!r}; found {len(matches)}")
    stacks = list(matches[0].all_stacks())
    if stack_name is None:
        if len(stacks) != 1:
            raise ValueError("stack is required for a layered texture set")
        return stacks[0]
    stack_matches = [item for item in stacks if str(item.name()) == str(stack_name)]
    if len(stack_matches) != 1:
        raise ValueError(
            f"Expected exactly one stack named {stack_name!r} in texture set {texture_set_name!r}; "
            f"found {len(stack_matches)}"
        )
    return stack_matches[0]


def find_node(layerstack: Any, node_uid: int, stack: Any) -> Any | None:
    matches = layerstack.get_node_by_uid(int(node_uid))
    if matches is None:
        return None
    if not isinstance(matches, (list, tuple)):
        matches = [matches]
    in_stack = []
    for node in matches:
        try:
            if node.get_stack() == stack:
                in_stack.append(node)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
    return in_stack[0] if len(in_stack) == 1 else None


def resolve_insert_position(
    layerstack: Any,
    stack: Any,
    placement: str,
    relative_to_uid: int | None,
) -> Any:
    resolved = str(placement).strip().lower()
    if resolved == "top":
        if relative_to_uid is not None:
            raise ValueError("relative_to_uid is not allowed when placement is top")
        return layerstack.InsertPosition.from_textureset_stack(stack)
    if resolved not in {"above", "below", "inside"}:
        raise ValueError("placement must be top, above, below, or inside")
    if relative_to_uid is None:
        raise ValueError(f"relative_to_uid is required when placement is {resolved}")
    reference = find_node(layerstack, int(relative_to_uid), stack)
    if reference is None:
        raise ValueError("The reference node was not found in the selected stack")
    if resolved == "above":
        return layerstack.InsertPosition.above_node(reference)
    if resolved == "below":
        return layerstack.InsertPosition.below_node(reference)
    if enum_name(reference.get_type()) not in {"Group", "GroupLayer", "GroupLayerNode"}:
        raise ValueError("inside placement requires a group layer reference")
    return layerstack.InsertPosition.inside_node(reference, layerstack.NodeStack.Substack)


def resource_url_from_effect(effect: Any) -> str | None:
    if enum_name(effect.get_type()) not in {"Generator", "GeneratorEffect", "GeneratorEffectNode"}:
        return None
    try:
        return str(effect.get_source().resource_id.url())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def serialize_effect(effect: Any) -> dict[str, Any]:
    payload = node_summary(effect)
    resource_url = resource_url_from_effect(effect)
    if resource_url is not None:
        payload["resource_url"] = resource_url
    return payload


def serialize_layer(node: Any) -> dict[str, Any]:
    node_type = enum_name(node.get_type())
    mask = None
    if bool(node.has_mask()):
        mask = {
            "enabled": bool(node.is_mask_enabled()),
            "background": enum_name(node.get_mask_background()),
            "effects": [serialize_effect(effect) for effect in node.mask_effects()],
        }
    children = []
    if node_type in {"Group", "GroupLayer", "GroupLayerNode"}:
        children = [serialize_layer(child) for child in node.sub_layers()]
    return {
        **node_summary(node),
        "visible": bool(node.is_visible()),
        "mask": mask,
        "content_effects": [serialize_effect(effect) for effect in node.content_effects()],
        "children": children,
    }


def serialize_stack(layerstack: Any, stack: Any) -> list[dict[str, Any]]:
    return [serialize_layer(node) for node in layerstack.get_root_layer_nodes(stack)]


def validate_resource(resource: Any, identifier: Any, expected_usage: str) -> list[Any]:
    matches = list(resource.Resource.retrieve(identifier))
    wanted = str(expected_usage).upper()
    usable = []
    for item in matches:
        try:
            usages = {enum_name(value).upper() for value in item.usages()}
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
        if wanted in usages and str(item.identifier().url()) == str(identifier.url()):
            usable.append(item)
    if not usable:
        raise ValueError(f"Resource readback did not confirm usage {wanted}")
    return usable
