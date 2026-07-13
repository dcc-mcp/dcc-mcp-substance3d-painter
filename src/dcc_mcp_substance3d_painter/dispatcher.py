"""Qt event-loop pump for Painter main-thread calls."""

from __future__ import annotations

from typing import Any, Callable
from uuid import uuid4

from dcc_mcp_core import HostUiDispatcherBase


class PainterQtDispatcher(HostUiDispatcherBase):
    """Drain DCC-MCP work from Painter's Qt main thread."""

    def __init__(self, interval_ms: int = 16) -> None:
        super().__init__(label="Substance 3D Painter Qt dispatcher")
        self._interval_ms = interval_ms
        self._timer: Any = None

    def install(self) -> None:
        """Attach a small repeating Qt timer from Painter's main thread."""
        if self._timer is not None:
            return
        try:
            from PySide6.QtCore import QTimer  # Lazy import: provided by current Painter.
        except ImportError:
            from PySide2.QtCore import QTimer  # Older Painter releases.

        self._timer = QTimer()
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(lambda: self.drain_queue(self._interval_ms // 2))
        self._timer.start()

    def uninstall(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.shutdown()

    def poke_host_pump(self) -> None:
        """The installed repeating Qt timer observes queued work shortly."""

    def dispatch_callable(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a core-routed callable on the Painter main thread."""
        affinity = kwargs.pop("affinity", kwargs.pop("thread_affinity", "main"))
        context = kwargs.pop("context", None)
        timeout = kwargs.pop("timeout_hint_secs", None) or getattr(context, "timeout_hint_secs", None)
        timeout_ms = int(timeout * 1000) if timeout else None
        result = self.submit_callable(
            uuid4().hex, lambda: func(*args, **kwargs), affinity=affinity, timeout_ms=timeout_ms
        )
        if not result.get("success"):
            raise RuntimeError(result.get("error") or "Painter main-thread dispatch failed")
        return result.get("output")
