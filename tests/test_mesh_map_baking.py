from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock
from urllib.parse import urlsplit
from uuid import UUID

import yaml
from dcc_mcp_core.cancellation import CancelToken, reset_cancel_token, set_cancel_token

from dcc_mcp_substance3d_painter.dispatcher import PainterQtDispatcher
from dcc_mcp_substance3d_painter.server import SubstancePainterMcpServer

SKILL = Path(__file__).parent.parent / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-project"


class McpClient:
    def __init__(self, url: str) -> None:
        self._url = urlsplit(url)
        self._next_id = 1
        self._session_id: Optional[str] = None

    def initialize(self) -> None:
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "painter-baking-contract-test", "version": "1"},
            },
        )
        self.notify("notifications/initialized", {})

    def request(self, method: str, params: dict) -> dict:
        request_id = self._next_id
        self._next_id += 1
        message = self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if "error" in message:
            raise RuntimeError(f"MCP request failed: {message['error']}")
        return message["result"]

    def notify(self, method: str, params: dict) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": params})

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        connection = http.client.HTTPConnection(self._url.hostname, self._url.port, timeout=5)
        try:
            connection.request("POST", self._url.path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read().decode("utf-8")
            if not 200 <= response.status < 300:
                raise RuntimeError(f"MCP HTTP {response.status}: {response_body}")
            self._session_id = response.getheader("Mcp-Session-Id") or self._session_id
        finally:
            connection.close()
        if not response_body:
            return {}
        if response_body.lstrip().startswith("{"):
            return json.loads(response_body)
        messages = [
            json.loads(line.removeprefix("data:").strip())
            for line in response_body.splitlines()
            if line.startswith("data:")
        ]
        if not messages:
            raise RuntimeError(f"MCP returned no JSON message: {response_body}")
        return messages[-1]


def _structured(result: dict) -> dict:
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def _job(payload: dict) -> dict:
    context = payload.get("context")
    if isinstance(context, dict) and context:
        return context.get("job", context)
    return payload


def test_bake_mesh_maps_is_a_pollable_core_job_that_preserves_the_host(monkeypatch, tmp_path):
    manifest = yaml.safe_load(SKILL.joinpath("tools.yaml").read_text(encoding="utf-8"))
    tool_manifest = next(tool for tool in manifest["tools"] if tool["name"] == "bake_mesh_maps")
    assert tool_manifest["execution"] == "async"
    assert tool_manifest["job_strategy"] == "monolithic"
    assert tool_manifest["affinity"] == "main"
    assert tool_manifest["annotations"]["deferred_hint"] is True
    assert tool_manifest["input_schema"]["required"] == ["texture_set", "maps"]
    assert tool_manifest["input_schema"]["additionalProperties"] is False
    assert tool_manifest["input_schema"]["properties"]["maps"]["uniqueItems"] is True

    texture_set = MagicMock()
    texture_set.name.return_value = "Body"
    parameters = MagicMock()
    entered = threading.Event()

    class BakingProcessAboutToStart:
        pass

    class BakingProcessProgress:
        pass

    class BakingProcessEnded:
        pass

    class EventDispatcher:
        def __init__(self):
            self._callbacks = {}

        def connect(self, event_type, callback):
            self._callbacks.setdefault(event_type, []).append(callback)

        def disconnect(self, event_type, callback):
            self._callbacks[event_type].remove(callback)

        def emit(self, event_type, payload):
            for callback in tuple(self._callbacks.get(event_type, ())):
                callback(payload)

    event_dispatcher = EventDispatcher()

    def start_bake(_texture_set):
        event_dispatcher.emit(BakingProcessAboutToStart, BakingProcessAboutToStart())
        entered.set()

    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    project.close = MagicMock()
    textureset = ModuleType("substance_painter.textureset")
    textureset.all_texture_sets = MagicMock(return_value=[texture_set])
    ao_usage = object()
    normal_usage = object()
    textureset.MeshMapUsage = SimpleNamespace(AO=ao_usage, Normal=normal_usage)
    baking = ModuleType("substance_painter.baking")
    baking.BakingParameters = MagicMock()
    baking.BakingParameters.from_texture_set.return_value = parameters
    baking.bake_async = MagicMock(side_effect=start_bake)
    event = ModuleType("substance_painter.event")
    event.BakingProcessAboutToStart = BakingProcessAboutToStart
    event.BakingProcessProgress = BakingProcessProgress
    event.BakingProcessEnded = BakingProcessEnded
    event.DISPATCHER = event_dispatcher
    painter = ModuleType("substance_painter")
    painter.project = project
    painter.textureset = textureset
    painter.baking = baking
    painter.event = event
    for name, module in (
        ("substance_painter", painter),
        ("substance_painter.project", project),
        ("substance_painter.textureset", textureset),
        ("substance_painter.baking", baking),
        ("substance_painter.event", event),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DCC_MCP_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_JOB_PERSISTENCE", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_TELEMETRY", "1")
    dispatcher = PainterQtDispatcher()
    server = SubstancePainterMcpServer(dispatcher, port=0)
    server.register_builtin_actions()
    assert server.load_skill("painter-project")
    server.start(install_atexit_hook=False)
    assert server.mcp_url is not None
    original_instance_id = server.instance_id
    assert original_instance_id is not None

    def exercise() -> None:
        client = McpClient(server.mcp_url)
        client.initialize()
        listed = client.request("tools/list", {})
        tools = {tool["name"]: tool for tool in listed["tools"]}
        while listed.get("nextCursor") is not None:
            listed = client.request("tools/list", {"cursor": listed["nextCursor"]})
            tools.update({tool["name"]: tool for tool in listed["tools"]})
        discovered = tools["bake_mesh_maps"]
        assert discovered["inputSchema"] == tool_manifest["input_schema"]

        started = time.monotonic()
        submitted = client.call_tool(
            discovered["name"],
            {"texture_set": "Body", "maps": ["ambient_occlusion", "normal"]},
        )
        assert time.monotonic() - started < 1.0
        pending = _structured(submitted)
        assert pending["status"] == "pending"
        job_id = pending["job_id"]
        assert str(UUID(job_id)) == job_id

        status = _job(_structured(client.call_tool("jobs_get_status", {"job_id": job_id})))
        assert status["status"] in {"pending", "running"}
        assert status.get("progress", 0) in {0, None}
        assert server.instance_id == original_instance_id
        assert project.is_open()
        project.close.assert_not_called()

        stop_pump = threading.Event()

        def pump_host() -> None:
            while not stop_pump.is_set():
                dispatcher.drain_queue(20)
                time.sleep(0.005)

        host_thread = threading.Thread(target=pump_host, daemon=True)
        host_thread.start()
        assert entered.wait(2)
        event_dispatcher.emit(BakingProcessProgress, SimpleNamespace(progress=0.5))
        running = _job(_structured(client.call_tool("jobs_get_status", {"job_id": job_id})))
        assert running["status"] == "running"
        event_dispatcher.emit(
            BakingProcessEnded,
            SimpleNamespace(status=SimpleNamespace(name="Failed"), message="high-poly mesh is missing"),
        )

        deadline = time.monotonic() + 3
        while True:
            terminal_payload = _structured(client.call_tool("jobs_get_status", {"job_id": job_id}))
            terminal = _job(terminal_payload)
            if terminal.get("status") in {"completed", "failed", "error"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)
        result = terminal["result"]
        assert result["success"] is False
        assert result["context"]["native_status"] == "failed"
        assert result["context"]["progress"] == 0.5
        assert "high-poly mesh is missing" in result["error"]
        parameters.set_enabled_bakers.assert_called_once_with([ao_usage, normal_usage])
        baking.bake_async.assert_called_once_with(texture_set)
        assert server.instance_id == original_instance_id
        assert project.is_open()
        project.close.assert_not_called()
        stop_pump.set()
        host_thread.join(timeout=2)

    try:
        exercise()
    finally:
        server.stop()


def test_bake_mesh_maps_forwards_core_cancellation_to_native_stop_source(monkeypatch):
    texture_set = MagicMock()
    texture_set.name.return_value = "Body"
    parameters = MagicMock()
    stop_source = MagicMock()
    stop_source.request_stop.return_value = True

    class BakingProcessAboutToStart:
        pass

    class BakingProcessProgress:
        pass

    class BakingProcessEnded:
        pass

    class EventDispatcher:
        def __init__(self):
            self._callbacks = {}

        def connect(self, event_type, callback):
            self._callbacks.setdefault(event_type, []).append(callback)

        def disconnect(self, event_type, callback):
            self._callbacks[event_type].remove(callback)

        def emit(self, event_type, payload):
            for callback in tuple(self._callbacks.get(event_type, ())):
                callback(payload)

    event_dispatcher = EventDispatcher()
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.all_texture_sets = MagicMock(return_value=[texture_set])
    textureset.MeshMapUsage = SimpleNamespace(Normal="normal")
    baking = ModuleType("substance_painter.baking")
    baking.BakingParameters = MagicMock()
    baking.BakingParameters.from_texture_set.return_value = parameters
    baking.bake_async = MagicMock(return_value=stop_source)
    event = ModuleType("substance_painter.event")
    event.BakingProcessAboutToStart = BakingProcessAboutToStart
    event.BakingProcessProgress = BakingProcessProgress
    event.BakingProcessEnded = BakingProcessEnded
    event.DISPATCHER = event_dispatcher
    painter = ModuleType("substance_painter")
    for name, module in (
        ("substance_painter", painter),
        ("substance_painter.project", project),
        ("substance_painter.textureset", textureset),
        ("substance_painter.baking", baking),
        ("substance_painter.event", event),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    script_path = SKILL / "scripts" / "bake_mesh_maps.py"
    spec = __import__("importlib.util").util.spec_from_file_location("bake_mesh_maps_cancel", script_path)
    assert spec and spec.loader
    script = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(script)
    deferred = script.main(texture_set="Body", maps=["normal"])
    token = CancelToken(job_id="core-job")
    token.cancel()
    reset = set_cancel_token(token)
    try:
        assert deferred.check_is_finished() is None
        stop_source.request_stop.assert_called_once_with()
        event_dispatcher.emit(
            BakingProcessEnded,
            SimpleNamespace(status=SimpleNamespace(name="Cancelled"), message="cancelled by client"),
        )
        result = deferred.check_is_finished()
    finally:
        reset_cancel_token(reset)

    assert result["success"] is False
    assert result["context"]["native_status"] == "cancelled"
    assert result["context"]["cancellation_supported"] is True


def test_successful_bake_reads_back_resources_and_texture_set_resolution(monkeypatch):
    normal_usage = object()
    resource = MagicMock()
    resource.url.return_value = "resource://project0/Body_Normal"
    texture_set = MagicMock()
    texture_set.name.return_value = "Body"
    texture_set.get_mesh_map_resource.return_value = resource
    texture_set.get_resolution.return_value = SimpleNamespace(width=2048, height=2048)

    class BakingProcessAboutToStart:
        pass

    class BakingProcessProgress:
        pass

    class BakingProcessEnded:
        pass

    class EventDispatcher:
        def __init__(self):
            self._callbacks = {}

        def connect(self, event_type, callback):
            self._callbacks.setdefault(event_type, []).append(callback)

        def disconnect(self, event_type, callback):
            self._callbacks[event_type].remove(callback)

        def emit(self, event_type, payload):
            for callback in tuple(self._callbacks.get(event_type, ())):
                callback(payload)

    event_dispatcher = EventDispatcher()
    project = ModuleType("substance_painter.project")
    project.is_open = MagicMock(return_value=True)
    textureset = ModuleType("substance_painter.textureset")
    textureset.all_texture_sets = MagicMock(return_value=[texture_set])
    textureset.MeshMapUsage = SimpleNamespace(Normal=normal_usage)
    baking = ModuleType("substance_painter.baking")
    baking.BakingParameters = MagicMock()
    baking.BakingParameters.from_texture_set.return_value = MagicMock()
    baking.bake_async = MagicMock(return_value=MagicMock())
    event = ModuleType("substance_painter.event")
    event.BakingProcessAboutToStart = BakingProcessAboutToStart
    event.BakingProcessProgress = BakingProcessProgress
    event.BakingProcessEnded = BakingProcessEnded
    event.DISPATCHER = event_dispatcher
    painter = ModuleType("substance_painter")
    for name, module in (
        ("substance_painter", painter),
        ("substance_painter.project", project),
        ("substance_painter.textureset", textureset),
        ("substance_painter.baking", baking),
        ("substance_painter.event", event),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    script_path = SKILL / "scripts" / "bake_mesh_maps.py"
    spec = __import__("importlib.util").util.spec_from_file_location("bake_mesh_maps_verify", script_path)
    assert spec and spec.loader
    script = __import__("importlib.util").util.module_from_spec(spec)
    spec.loader.exec_module(script)
    deferred = script.main(texture_set="Body", maps=["normal"])
    event_dispatcher.emit(BakingProcessEnded, SimpleNamespace(status=SimpleNamespace(name="Success"), message=""))

    result = deferred.check_is_finished()

    assert result["success"] is True
    assert result["context"]["mesh_map_resources"] == {"normal": "resource://project0/Body_Normal"}
    assert result["context"]["resolution"] == {"width": 2048, "height": 2048}
    texture_set.get_mesh_map_resource.assert_called_once_with(normal_usage)
