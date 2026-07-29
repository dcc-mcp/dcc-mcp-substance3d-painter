from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from dcc_mcp_substance3d_painter import _menu


def test_qaction_uses_qtgui_with_pyside6(monkeypatch):
    action_class = object()
    pyside6 = ModuleType("PySide6")
    pyside6.QtGui = SimpleNamespace(QAction=action_class)
    monkeypatch.setitem(sys.modules, "PySide6", pyside6)

    assert _menu._get_qaction_class(SimpleNamespace()) is action_class
