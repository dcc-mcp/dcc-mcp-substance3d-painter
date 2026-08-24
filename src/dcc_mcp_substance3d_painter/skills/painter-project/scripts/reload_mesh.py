"""Reload the current Painter project mesh through the native async callback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dcc_mcp_core import DeferredToolResult
from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


def _status_name(value: object) -> str:
    name = getattr(value, "name", value)
    name = name() if callable(name) else name
    return str(name).strip().lower()


@skill_entry
def main(
    mesh_path: str,
    import_cameras: bool = True,
    preserve_strokes: bool = True,
    **_kwargs,
):
    mesh = Path(mesh_path).expanduser().resolve()
    if not mesh.is_file():
        return skill_error("Painter mesh does not exist", str(mesh))

    import substance_painter.project as project  # Lazy: Painter host only.

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    state: dict[str, Any] = {"done": False, "status": "pending"}

    def on_finished(status: object) -> None:
        state["status"] = _status_name(status)
        state["done"] = True

    settings = project.MeshReloadingSettings(
        import_cameras=bool(import_cameras),
        preserve_strokes=bool(preserve_strokes),
        auto_unwrap_settings=None,
    )
    try:
        project.reload_mesh(str(mesh), settings, on_finished)
    except (RuntimeError, TypeError, ValueError) as exc:
        return skill_error("Unable to start Painter mesh reload", str(exc))

    def check_is_finished():
        if not state["done"]:
            return None
        if state["status"] != "success":
            return skill_error(
                "Painter mesh reload failed",
                f"Painter reported reload status {state['status']!r}",
                mesh_path=str(mesh),
                native_status=state["status"],
            )
        observed_path = project.last_imported_mesh_path()
        readback_path = Path(observed_path).expanduser().resolve() if observed_path else None
        if not project.is_open() or readback_path != mesh:
            return skill_error(
                "Painter mesh reload was not confirmed by host readback",
                f"expected {mesh}; observed {readback_path}",
                mesh_path=str(mesh),
                native_status=state["status"],
            )
        return skill_success(
            "Reloaded Painter project mesh",
            mesh_path=str(readback_path),
            native_status=state["status"],
            import_cameras=bool(import_cameras),
            preserve_strokes=bool(preserve_strokes),
        )

    return DeferredToolResult(check_is_finished=check_is_finished, timeout_secs=600.0, poll_interval_secs=0.1)


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
