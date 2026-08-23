"""Bake a bounded mesh-map selection for one Painter texture set."""

from __future__ import annotations

from typing import Any

from dcc_mcp_core import DeferredToolResult
from dcc_mcp_core.skill import skill_entry, skill_error, skill_success

_BAKER_MEMBERS = {
    "ambient_occlusion": "AO",
    "curvature": "Curvature",
    "id": "ID",
    "normal": "Normal",
    "position": "Position",
    "thickness": "Thickness",
    "world_space_normal": "WorldSpaceNormal",
}
_SUCCESS_STATUSES = {"completed", "succeeded", "success"}


def _value(event: object, name: str, default: Any = None) -> Any:
    value = getattr(event, name, default)
    return value() if callable(value) else value


def _status_name(value: object) -> str:
    name = getattr(value, "name", value)
    name = name() if callable(name) else name
    return str(name).strip().lower()


def _texture_set_name(item: Any) -> str:
    name = item.name
    return str(name() if callable(name) else name)


@skill_entry
def main(texture_set: str, maps: list[str], **_kwargs):
    import substance_painter.baking as baking  # Lazy imports: provided by Painter.
    import substance_painter.event as event
    import substance_painter.project as project
    import substance_painter.textureset as textureset

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    if callable(getattr(project, "is_busy", None)) and project.is_busy():
        return skill_error("Painter is busy", "Wait for the active Painter operation to finish before baking")

    requested_maps = list(maps)
    unknown_maps = sorted(set(requested_maps) - _BAKER_MEMBERS.keys())
    if unknown_maps:
        return skill_error("Unsupported mesh-map baker", f"Unknown maps: {', '.join(unknown_maps)}")
    available = {_texture_set_name(item): item for item in textureset.all_texture_sets()}
    selected = available.get(texture_set)
    if selected is None:
        names = ", ".join(sorted(available)) or "none"
        return skill_error("Texture set was not found", f"Requested {texture_set!r}; available texture sets: {names}")

    parameters = baking.BakingParameters.from_texture_set(selected)
    usage_type = getattr(textureset, "MeshMapUsage", None)
    enabled_bakers = [getattr(usage_type, _BAKER_MEMBERS[name], None) for name in requested_maps]
    missing_members = [name for name, member in zip(requested_maps, enabled_bakers) if member is None]
    if missing_members:
        return skill_error(
            "Mesh-map baker is unavailable in this Painter version",
            f"Missing MeshMapUsage members for: {', '.join(missing_members)}",
        )
    parameters.set_enabled_bakers(enabled_bakers)
    state: dict[str, Any] = {
        "done": False,
        "message": "",
        "progress": 0.0,
        "status": "pending",
    }

    def on_started(_event: object) -> None:
        state["status"] = "running"

    def on_progress(progress_event: object) -> None:
        progress = _value(progress_event, "progress", _value(progress_event, "value", 0.0))
        state["progress"] = max(0.0, min(1.0, float(progress)))

    def on_ended(ended_event: object) -> None:
        state["status"] = _status_name(_value(ended_event, "status", "unknown"))
        state["message"] = str(_value(ended_event, "message", ""))
        if state["status"] in _SUCCESS_STATUSES:
            state["progress"] = 1.0
        state["done"] = True

    handlers = (
        (event.BakingProcessAboutToStart, on_started),
        (event.BakingProcessProgress, on_progress),
        (event.BakingProcessEnded, on_ended),
    )
    for event_type, callback in handlers:
        event.DISPATCHER.connect(event_type, callback)
    try:
        baking.bake_async(selected)
    except Exception:
        for event_type, callback in handlers:
            event.DISPATCHER.disconnect(event_type, callback)
        raise

    def check_is_finished():
        if not state["done"]:
            return None
        for event_type, callback in handlers:
            event.DISPATCHER.disconnect(event_type, callback)
        if state["status"] not in _SUCCESS_STATUSES:
            detail = state["message"] or f"Painter reported status {state['status']!r}"
            return skill_error(
                "Painter mesh-map baking failed",
                detail,
                prompt="Inspect the project and baking inputs before starting a new bake job.",
                texture_set=texture_set,
                maps=requested_maps,
                native_status=state["status"],
                progress=state["progress"],
                cancellation_supported=False,
            )
        return skill_success(
            "Painter mesh-map baking completed",
            texture_set=texture_set,
            maps=requested_maps,
            native_status=state["status"],
            progress=state["progress"],
            cancellation_supported=False,
        )

    return DeferredToolResult(check_is_finished=check_is_finished, timeout_secs=1800.0, poll_interval_secs=0.1)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
