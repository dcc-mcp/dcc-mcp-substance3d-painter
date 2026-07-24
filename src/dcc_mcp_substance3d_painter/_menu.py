"""Unified DCC MCP menu for Substance 3D Painter.

Adds a "DCC MCP" menu to the Substance Painter menu bar with:
- Copy Instance ID — copies the DCC instance UUID to the system clipboard
- Server Info — shows MCP URL, gateway URL, and server status
- About DCC MCP — adapter version and project info
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)

_MENU_NAME = "DCC MCP"
_widgets: List[object] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_server():
    """Return the running SubstancePainterMcpServer singleton, or None."""
    try:
        from dcc_mcp_substance3d_painter.server import get_server

        return get_server()
    except Exception:
        return None


def _get_instance_id() -> str:
    """Resolve the DCC instance UUID from the running server.

    Mirrors ``_extract_instance_id`` from dcc-mcp-core capabilities.py,
    probing ``server.instance_id``, ``server._config.instance_id``, and
    ``server._server.instance_id`` in that order.
    """
    server = _get_server()
    if server is None:
        return "unknown"
    for attr_path in ("instance_id", "_config.instance_id", "_server.instance_id"):
        target = server
        try:
            for part in attr_path.split("."):
                target = getattr(target, part)
            if target:
                return str(target)
        except AttributeError:
            continue
    return "unknown"


def _get_pyside_module():
    """Return the available PySide QtWidgets module (PySide2 or PySide6)."""
    try:
        from PySide2 import QtWidgets

        return QtWidgets
    except ImportError:
        pass
    try:
        from PySide6 import QtWidgets

        return QtWidgets
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Menu action callbacks
# ---------------------------------------------------------------------------


def _copy_instance_id() -> None:
    """Copy the DCC instance UUID to the system clipboard."""
    instance_id = _get_instance_id()
    QtWidgets = _get_pyside_module()
    if QtWidgets is None:
        logger.warning("Cannot copy to clipboard: PySide2/6 not available")
        return

    app = QtWidgets.QApplication.instance()
    if app is None:
        logger.warning("Cannot copy to clipboard: no QApplication instance")
        return

    clipboard = app.clipboard()
    if clipboard is not None:
        clipboard.setText(instance_id)
        logger.info("Copied instance ID to clipboard: %s", instance_id)


def _show_server_info() -> None:
    """Show a dialog with MCP server connection details."""
    QtWidgets = _get_pyside_module()
    server = _get_server()
    instance_id = _get_instance_id()

    if server is not None and getattr(server, "is_running", False):
        mcp_url = "<not available>"
        gateway_url = "<not available>"
        try:
            mcp_url_callable = getattr(server, "mcp_url", None)
            if callable(mcp_url_callable):
                mcp_url = mcp_url_callable() or mcp_url
            elif mcp_url_callable:
                mcp_url = str(mcp_url_callable)
        except Exception:
            pass
        try:
            gw = getattr(server, "gateway_url", None)
            if gw:
                gateway_url = str(gw)
        except Exception:
            pass

        info = (
            f"Instance ID: {instance_id}\n"
            f"MCP URL: {mcp_url}\n"
            f"Gateway URL: {gateway_url}\n"
            f"PID: {getattr(server, 'dcc_pid', 'N/A')}"
        )
    else:
        info = "MCP server is not running."

    if QtWidgets is not None:
        QtWidgets.QMessageBox.information(None, "DCC MCP — Server Info", info)
    else:
        logger.info("Server info: %s", info)


def _show_about() -> None:
    """Show the About DCC MCP dialog."""
    QtWidgets = _get_pyside_module()
    from dcc_mcp_substance3d_painter.__version__ import __version__

    about = (
        f"DCC MCP — Substance 3D Painter Adapter\n"
        f"Version: {__version__}\n\n"
        f"Model Context Protocol adapter for Substance 3D Painter.\n"
        f"Part of the DCC-MCP ecosystem.\n\n"
        f"https://github.com/dcc-mcp/dcc-mcp-substance3d-painter"
    )

    if QtWidgets is not None:
        QtWidgets.QMessageBox.about(None, "About DCC MCP", about)
    else:
        logger.info("About: %s", about)


# ---------------------------------------------------------------------------
# Menu lifecycle
# ---------------------------------------------------------------------------


def add_menu() -> None:
    """Add the unified DCC MCP menu to Substance Painter's menu bar.

    Safe to call multiple times — subsequent calls are no-ops when the
    menu is already present.
    """
    global _widgets

    # Check if Substance Painter UI API is available
    try:
        import substance_painter.ui as sp_ui
    except ImportError:
        logger.debug("substance_painter.ui not available — menu skipped")
        return

    QtWidgets = _get_pyside_module()
    if QtWidgets is None:
        logger.debug("PySide2/6 not available — menu skipped")
        return

    main_window = sp_ui.get_main_window()
    if main_window is None:
        logger.debug("Main window not ready — menu skipped")
        return

    menu_bar = main_window.menuBar()
    if menu_bar is None:
        return

    # Idempotency: skip if menu already exists
    for action in menu_bar.actions():
        if action.text() == _MENU_NAME:
            return

    menu = QtWidgets.QMenu(_MENU_NAME, main_window)

    # Copy Instance ID
    copy_action = QtWidgets.QAction("Copy Instance ID", menu)
    copy_action.triggered.connect(_copy_instance_id)
    menu.addAction(copy_action)

    # Server Info
    server_info_action = QtWidgets.QAction("Server Info", menu)
    server_info_action.triggered.connect(_show_server_info)
    menu.addAction(server_info_action)

    menu.addSeparator()

    # About DCC MCP
    about_action = QtWidgets.QAction("About DCC MCP", menu)
    about_action.triggered.connect(_show_about)
    menu.addAction(about_action)

    menu_bar.addMenu(menu)
    _widgets.append(menu)
    logger.info("DCC MCP menu added to Substance Painter menu bar")


def remove_menu() -> None:
    """Remove the DCC MCP menu from Substance Painter's menu bar.

    Idempotent — safe to call even when the menu was never created.
    """
    global _widgets

    try:
        import substance_painter.ui as sp_ui
    except ImportError:
        _widgets.clear()
        return

    for widget in _widgets:
        try:
            sp_ui.delete_ui_element(widget)
        except Exception as exc:
            logger.debug("Failed to delete UI element: %s", exc)
    _widgets.clear()
