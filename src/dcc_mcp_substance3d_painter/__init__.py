"""Substance 3D Painter adapter for DCC-MCP."""

from dcc_mcp_substance3d_painter.__version__ import __version__
from dcc_mcp_substance3d_painter.server import (
    DEFAULT_PORT,
    SERVER_NAME,
    SubstancePainterMcpServer,
    get_server,
    start_server,
    stop_server,
)

__all__ = [
    "__version__",
    "DEFAULT_PORT",
    "SERVER_NAME",
    "SubstancePainterMcpServer",
    "get_server",
    "start_server",
    "stop_server",
]
