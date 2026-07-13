"""Painter plugin lifecycle entry points."""

from __future__ import annotations

from typing import Optional

from dcc_mcp_substance3d_painter.dispatcher import PainterQtDispatcher
from dcc_mcp_substance3d_painter.server import start_server, stop_server

_dispatcher: Optional[PainterQtDispatcher] = None


def start_plugin() -> None:
    """Start MCP when Painter enables this plugin."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = PainterQtDispatcher()
        _dispatcher.install()
    start_server(_dispatcher)


def close_plugin() -> None:
    """Stop MCP and detach the Qt timer when Painter disables this plugin."""
    global _dispatcher
    stop_server()
    if _dispatcher is not None:
        _dispatcher.uninstall()
        _dispatcher = None
