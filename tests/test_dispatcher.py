from __future__ import annotations

import threading
import time

from dcc_mcp_substance3d_painter.dispatcher import PainterQtDispatcher


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
