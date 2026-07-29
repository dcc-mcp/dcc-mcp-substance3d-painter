"""Start a timed orbit of the default Substance 3D Painter camera."""

from __future__ import annotations

import math

from dcc_mcp_core.skill import skill_entry, skill_error, skill_success


def _camera_pose(
    center: tuple[float, float, float],
    radius: float,
    height: float,
    angle_degrees: float,
) -> tuple[list[float], list[float]]:
    angle = math.radians(angle_degrees)
    x = center[0] + radius * math.sin(angle)
    y = center[1] + height
    z = center[2] + radius * math.cos(angle)
    dx, dy, dz = center[0] - x, center[1] - y, center[2] - z
    horizontal = math.hypot(dx, dz)
    pitch = math.degrees(math.atan2(dy, horizontal))
    yaw = math.degrees(math.atan2(-dx, -dz))
    return [x, y, z], [pitch, yaw, 0.0]


@skill_entry
def main(
    center: list[float],
    radius: float,
    height: float = 0.0,
    duration_seconds: float = 10.0,
    revolutions: float = 1.0,
    start_angle_degrees: float = 0.0,
    frames_per_second: int = 30,
    **_kwargs,
):
    try:
        if len(center) != 3:
            raise ValueError("center must contain exactly three values")
        resolved_center = tuple(float(value) for value in center)
        resolved_radius = float(radius)
        resolved_height = float(height)
        resolved_duration = float(duration_seconds)
        resolved_revolutions = float(revolutions)
        resolved_start = float(start_angle_degrees)
        resolved_fps = int(frames_per_second)
        if resolved_radius <= 0:
            raise ValueError("radius must be greater than zero")
        if not 1.0 <= resolved_duration <= 180.0:
            raise ValueError("duration_seconds must be between 1 and 180")
        if not 0.0 < resolved_revolutions <= 4.0:
            raise ValueError("revolutions must be greater than zero and at most 4")
        if not 1 <= resolved_fps <= 60:
            raise ValueError("frames_per_second must be between 1 and 60")
    except (TypeError, ValueError) as exc:
        return skill_error("Invalid Painter camera orbit parameters", str(exc))

    import substance_painter.display as display  # Lazy: Painter host only.
    import substance_painter.project as project
    from PySide6.QtCore import QCoreApplication, QElapsedTimer, QTimer

    if not project.is_open():
        return skill_error("No Painter project is open", "project.is_open() returned False")
    application = QCoreApplication.instance()
    previous = application.findChild(QTimer, "dcc_mcp_camera_orbit")
    if previous is not None:
        previous.stop()
        previous.deleteLater()

    camera = display.Camera.get_default_camera()
    duration_ms = round(resolved_duration * 1000)
    elapsed = QElapsedTimer()
    timer = QTimer(application)
    timer.setObjectName("dcc_mcp_camera_orbit")
    timer.setInterval(max(1, round(1000 / resolved_fps)))

    def update_camera() -> None:
        fraction = min(1.0, elapsed.elapsed() / duration_ms)
        angle = resolved_start + 360.0 * resolved_revolutions * fraction
        camera.position, camera.rotation = _camera_pose(
            resolved_center,
            resolved_radius,
            resolved_height,
            angle,
        )
        if fraction >= 1.0:
            timer.stop()
            timer.deleteLater()

    update_camera()
    elapsed.start()
    timer.timeout.connect(update_camera)
    timer.start()

    return skill_success(
        "Started Painter camera orbit",
        center=list(resolved_center),
        radius=resolved_radius,
        height=resolved_height,
        duration_seconds=resolved_duration,
        revolutions=resolved_revolutions,
        frames_per_second=resolved_fps,
    )


if __name__ == "__main__":
    from dcc_mcp_core.skill import run_main

    run_main(main)
