"""Stable Painter plugin entry point for the DCC-MCP adapter.

The module name intentionally differs from the Python package name. Painter
imports startup plugins by their top-level module name, so reusing
``dcc_mcp_substance3d_painter`` would resolve the package before this entry
point whenever the adapter is installed on ``PYTHONPATH``.
"""

from dcc_mcp_substance3d_painter.plugin import close_plugin, start_plugin

__all__ = ["close_plugin", "start_plugin"]
