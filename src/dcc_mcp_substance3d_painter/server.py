"""Embedded Substance 3D Painter MCP server."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dcc_mcp_core import DccServerOptions, HostExecutionBridge
from dcc_mcp_core.server_base import DccServerBase

from dcc_mcp_substance3d_painter.__version__ import __version__

DEFAULT_PORT = 8765
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
    _server = SubstancePainterMcpServer(
        host_dispatcher,
        port or int(os.environ.get("DCC_MCP_SUBSTANCE3D_PAINTER_PORT", DEFAULT_PORT)),
    )
    _server.register_builtin_actions()
    _server.start()
    return _server


def stop_server() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def get_server() -> Optional[SubstancePainterMcpServer]:
    return _server
