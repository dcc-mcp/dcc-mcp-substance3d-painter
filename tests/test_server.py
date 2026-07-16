from __future__ import annotations

import dcc_mcp_substance3d_painter.server as server


def test_start_server_resolves_ephemeral_env_and_explicit_ports(monkeypatch):
    ports = []

    class FakeServer:
        is_running = False

        def __init__(self, _dispatcher, port):
            ports.append(port)

        def register_builtin_actions(self):
            pass

        def start(self):
            self.is_running = True

        def stop(self):
            pass

    monkeypatch.setattr(server, "SubstancePainterMcpServer", FakeServer)
    for env_port, explicit_port in ((None, None), ("9123", None), ("9123", 0)):
        server._server = None
        if env_port is None:
            monkeypatch.delenv("DCC_MCP_SUBSTANCE3D_PAINTER_PORT", raising=False)
        else:
            monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PORT", env_port)
        server.start_server(object(), explicit_port)

    assert ports == [0, 9123, 0]
