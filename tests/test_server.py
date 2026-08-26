from __future__ import annotations

import socket
import time
from urllib.parse import urlparse

import pytest
from dcc_mcp_core.install_lifecycle import query_runtime_state

import dcc_mcp_substance3d_painter.server as server


def _isolate_runtime(monkeypatch, tmp_path):
    registry_dir = tmp_path / "registry"
    monkeypatch.setenv("DCC_MCP_GATEWAY_PORT", "0")
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(registry_dir))
    monkeypatch.setenv("DCC_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DCC_MCP_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_JOB_PERSISTENCE", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_TELEMETRY", "1")
    server._server = None
    return registry_dir


def _assert_reachable(port):
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


def _assert_listener_is_stopped(port):
    deadline = time.monotonic() + 2
    while True:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.1)
        except OSError:
            return
        connection.close()
        if time.monotonic() >= deadline:
            pytest.fail(f"listener on port {port} still accepts connections after stop")
        time.sleep(0.01)


def _assert_registry_is_empty(registry_dir):
    state = query_runtime_state(registry_dir, dcc_type="substance3d_painter", include_dead=True)
    assert state["total"] == 0
    assert state["alive_count"] == 0


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
    registry_dir = _isolate_runtime(monkeypatch, tmp_path)

    with socket.create_server(("127.0.0.1", 8765)):
        instance = server.start_server(object())
        try:
            port = urlparse(instance.mcp_url).port
            assert instance.is_running
            assert port not in (None, 0, 8765)
            _assert_reachable(port)
        finally:
            server.stop_server()

    assert server.get_server() is None
    _assert_listener_is_stopped(port)
    _assert_registry_is_empty(registry_dir)


def test_explicit_free_port_is_exact_and_reusable_after_repeated_stop(monkeypatch, tmp_path):
    registry_dir = _isolate_runtime(monkeypatch, tmp_path)
    with socket.create_server(("127.0.0.1", 0)) as reservation:
        explicit_port = reservation.getsockname()[1]
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PORT", str(explicit_port))

    for _ in range(2):
        instance = server.start_server(object())
        try:
            assert urlparse(instance.mcp_url).port == explicit_port
            _assert_reachable(explicit_port)
        finally:
            server.stop_server()
        assert server.get_server() is None
        _assert_listener_is_stopped(explicit_port)
        _assert_registry_is_empty(registry_dir)


def test_explicit_occupied_port_fails_without_fallback_or_registry_leak(monkeypatch, tmp_path):
    registry_dir = _isolate_runtime(monkeypatch, tmp_path)
    with socket.create_server(("127.0.0.1", 0)) as occupied:
        explicit_port = occupied.getsockname()[1]
        monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PORT", str(explicit_port))
        try:
            with pytest.raises(RuntimeError):
                server.start_server(object())

            assert server.get_server() is None
            _assert_registry_is_empty(registry_dir)
        finally:
            server.stop_server()
