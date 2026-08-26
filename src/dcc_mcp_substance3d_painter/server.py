"""Embedded Substance 3D Painter MCP server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from dcc_mcp_core import DccServerOptions, HostExecutionBridge
from dcc_mcp_core.server_base import DccServerBase

from dcc_mcp_substance3d_painter.__version__ import __version__

DEFAULT_PORT = 0
SERVER_NAME = "dcc-mcp-substance3d-painter"
_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
_server: Optional["SubstancePainterMcpServer"] = None


class SubstancePainterMcpServer(DccServerBase):
    """DCC-MCP server hosted by a running Substance 3D Painter process."""

    def __init__(self, host_dispatcher: object, port: int = DEFAULT_PORT) -> None:
        options = DccServerOptions.from_env(
            "substance3d_painter",
            _SKILLS_DIR,
            port=port,
            server_name=SERVER_NAME,
            server_version=__version__,
            execution_bridge=HostExecutionBridge(dispatcher=host_dispatcher),
            enable_file_logging=True,
            enable_telemetry=True,
        )
        super().__init__(options=options)

    def register_builtin_actions(
        self,
        extra_skill_paths: Optional[list[str]] = None,
        include_bundled: bool = True,
        minimal_mode: Optional[Any] = None,
    ) -> None:
        """Register Core actions and keep the install readiness probe loaded."""
        super().register_builtin_actions(
            extra_skill_paths=extra_skill_paths,
            include_bundled=include_bundled,
            minimal_mode=minimal_mode,
        )
        if not self.load_skill("painter-diagnostics"):
            raise RuntimeError("Painter diagnostics skill could not be loaded")

    def _version_string(self) -> str:
        try:
            import substance_painter  # Lazy import: provided by Painter.

            return str(getattr(substance_painter, "version", "Substance 3D Painter"))
        except Exception:
            return "Substance 3D Painter"


def start_server(host_dispatcher: object, port: Optional[int] = None) -> SubstancePainterMcpServer:
    """Start the singleton server after the host Qt dispatcher is installed."""
    global _server
    if _server is not None and _server.is_running:
        return _server
    _server = None
    resolved_port = int(os.environ.get("DCC_MCP_SUBSTANCE3D_PAINTER_PORT", DEFAULT_PORT)) if port is None else port
    candidate = SubstancePainterMcpServer(host_dispatcher, resolved_port)
    try:
        candidate.register_builtin_actions()
        candidate.start()
    except BaseException:
        try:
            candidate.stop()
        except BaseException:
            pass
        raise
    _server = candidate
    return candidate


def stop_server() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def get_server() -> Optional[SubstancePainterMcpServer]:
    return _server
