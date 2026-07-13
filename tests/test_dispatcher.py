from __future__ import annotations

import sys
import threading
import time
from types import ModuleType
from unittest.mock import MagicMock

from dcc_mcp_substance3d_painter.dispatcher import PainterQtDispatcher


def test_install_supports_pyside6(monkeypatch):
    timer = MagicMock()
    qt_core = ModuleType("PySide6.QtCore")
    qt_core.QTimer = MagicMock(return_value=timer)
    monkeypatch.setitem(sys.modules, "PySide6", ModuleType("PySide6"))
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qt_core)
    monkeypatch.setitem(sys.modules, "PySide2", None)

    PainterQtDispatcher().install()

    qt_core.QTimer.assert_called_once_with()
    timer.setInterval.assert_called_once_with(16)
    timer.start.assert_called_once_with()


def test_dispatch_callable_runs_on_drained_main_queue():
    dispatcher = PainterQtDispatcher()
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("value", dispatcher.dispatch_callable(lambda: 42)))
    thread.start()
    while dispatcher.queue_size() == 0:
        time.sleep(0.001)
    dispatcher.drain_queue(10)
    thread.join(timeout=1)
    assert outcome["value"] == 42
