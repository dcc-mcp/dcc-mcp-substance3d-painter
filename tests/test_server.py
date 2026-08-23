from __future__ import annotations

import socket
from urllib.parse import urlparse

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


def test_default_server_starts_when_legacy_port_is_occupied(monkeypatch, tmp_path):
    monkeypatch.delenv("DCC_MCP_SUBSTANCE3D_PAINTER_PORT", raising=False)
    monkeypatch.setenv("DCC_MCP_GATEWAY_PORT", "0")
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path))
    server._server = None

    with socket.create_server(("127.0.0.1", 8765)):
        instance = server.start_server(object())
        try:
            assert instance.is_running
            assert urlparse(instance.mcp_url).port != 8765
        finally:
            server.stop_server()
