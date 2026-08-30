from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
import yaml
from dcc_mcp_core import materialize_script
from dcc_mcp_core.cancellation import (
    CancelToken,
    DccMcpCancelledError,
    current_cancel_token,
    reset_cancel_token,
    set_cancel_token,
)
from dcc_mcp_core.script_materialization import SCRIPT_MATERIALIZATION_ROOT_ENV
from jsonschema import Draft202012Validator, ValidationError
from test_mesh_map_baking import McpClient, _job, _structured

import dcc_mcp_substance3d_painter.materialized_script_executor as executor
from dcc_mcp_substance3d_painter.dispatcher import PainterQtDispatcher
from dcc_mcp_substance3d_painter.materialized_script_executor import (
    MAX_MATERIALIZED_SCRIPT_BYTES,
    MAX_SCRIPT_RESULT_DEPTH,
    MAX_SCRIPT_RESULT_JSON_BYTES,
    MAX_SCRIPT_RESULT_NODES,
    MaterializedScriptRejected,
    execute_materialized_file_ref,
)
from dcc_mcp_substance3d_painter.server import SubstancePainterMcpServer

PROJECT_SKILL = (
    Path(__file__).resolve().parents[1] / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-project"
)


def _materialized(monkeypatch, tmp_path, content=None, *, ttl_secs=None):
    root = tmp_path / "materialized"
    monkeypatch.setenv(SCRIPT_MATERIALIZATION_ROOT_ENV, str(root))
    descriptor = materialize_script(
        content
        or ("from dcc_mcp_core.skill import skill_success\ndef main():\n    return skill_success('ok', value=42)\n"),
        dcc_type="substance3d_painter",
        instance_id="painter-fixture",
        session_id="contract-session",
        tool_call_id="materialize-call",
        ttl_secs=ttl_secs,
        root=root,
    )
    return descriptor, root


def _rejection(file_ref):
    with pytest.raises(MaterializedScriptRejected) as caught:
        execute_materialized_file_ref(file_ref)
    assert str(caught.value) == caught.value.code
    return caught.value.code


def _executor_schema():
    manifest = yaml.safe_load(PROJECT_SKILL.joinpath("tools.yaml").read_text(encoding="utf-8"))
    return next(tool for tool in manifest["tools"] if tool["name"] == "execute_materialized_script")


def _execute_through_mcp(monkeypatch, tmp_path, content):
    monkeypatch.setenv(SCRIPT_MATERIALIZATION_ROOT_ENV, str(tmp_path / "materialized"))
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("DCC_MCP_DISABLE_FILE_LOGGING", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_JOB_PERSISTENCE", "1")
    monkeypatch.setenv("DCC_MCP_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("DCC_MCP_GATEWAY_PORT", "0")

    dispatcher = PainterQtDispatcher()
    server = SubstancePainterMcpServer(dispatcher, port=0)
    server.register_builtin_actions()
    assert server.load_skill("painter-project")
    server.start(install_atexit_hook=False)
    client = McpClient(server.mcp_url)
    client.initialize()
    try:
        materialized = _structured(
            client.call_tool(
                "materialize_script",
                {
                    "content": content,
                    "language": "python",
                    "suffix": ".py",
                    "session_id": "contract-session",
                    "tool_call_id": "materialize-call",
                },
            )
        )
        submitted = _structured(client.call_tool("execute_materialized_script", {"file_ref": materialized["file_ref"]}))
        deadline = time.monotonic() + 3
        while True:
            dispatcher.drain_queue(20)
            terminal = _job(_structured(client.call_tool("jobs_get_status", {"job_id": submitted["job_id"]})))
            if terminal.get("status") in {"completed", "failed", "error"}:
                return terminal["result"]
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        server.stop()


def test_materialize_script_executes_through_the_typed_main_thread_tool(monkeypatch, tmp_path):
    monkeypatch.setenv(SCRIPT_MATERIALIZATION_ROOT_ENV, str(tmp_path / "materialized"))
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

    client = McpClient(server.mcp_url)
    client.initialize()
    try:
        materialized = _structured(
            client.call_tool(
                "materialize_script",
                {
                    "content": (
                        "import threading\n"
                        "from dcc_mcp_core.skill import skill_success\n"
                        "def main():\n"
                        "    return skill_success('Materialized script executed', "
                        "value=42, thread_id=threading.get_ident())\n"
                    ),
                    "language": "python",
                    "suffix": ".py",
                    "session_id": "contract-session",
                    "tool_call_id": "materialize-call",
                },
            )
        )

        submitted = _structured(
            client.call_tool(
                "execute_materialized_script",
                {"file_ref": materialized["file_ref"]},
            )
        )
        assert submitted["status"] == "pending"
        job_id = submitted["job_id"]
        expected_thread_id = threading.get_ident()

        deadline = time.monotonic() + 3
        while True:
            dispatcher.drain_queue(20)
            terminal = _job(_structured(client.call_tool("jobs_get_status", {"job_id": job_id})))
            if terminal.get("status") in {"completed", "failed", "error"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

        result = terminal["result"]
        assert result["success"] is True, json.dumps(result, sort_keys=True)
        assert result["context"]["value"] == 42
        assert result["context"]["thread_id"] == expected_thread_id
        assert result["context"]["sha256"] == materialized["sha256"]
        assert result["context"]["bytes"] == materialized["bytes"]
    finally:
        server.stop()


def test_executor_manifest_is_fixed_to_file_ref_async_main_thread_contract():
    tool = _executor_schema()
    assert tool["execution"] == "async"
    assert tool["affinity"] == "main"
    assert tool["timeout_hint_secs"] == 120
    assert tool["input_schema"]["required"] == ["file_ref"]
    assert tool["input_schema"]["additionalProperties"] is False
    assert "code" not in tool["input_schema"]["properties"]
    assert "file_path" not in tool["input_schema"]["properties"]
    assert "mode" not in tool["input_schema"]["properties"]
    assert "next-tools" not in tool


@pytest.mark.parametrize(
    "unsupported",
    [
        {"code": "print('inline')"},
        {"file_path": "script.py"},
        {"mode": "eval"},
        {"arguments": {"value": 42}},
    ],
)
def test_executor_schema_rejects_caller_controlled_source_paths_and_modes(monkeypatch, tmp_path, unsupported):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    schema = _executor_schema()["input_schema"]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({"file_ref": descriptor.file_ref})
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate({"file_ref": descriptor.file_ref, **unsupported})


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda ref, outside: ref.update(uri=outside.as_uri()), "file_ref_scope_denied"),
        (lambda ref, _outside: ref.update(mime="text/plain"), "file_ref_scope_denied"),
        (
            lambda ref, _outside: ref["metadata"].update(dcc_type="maya"),
            "file_ref_scope_denied",
        ),
        (
            lambda ref, _outside: ref["metadata"].update(language="javascript"),
            "file_ref_scope_denied",
        ),
        (lambda ref, _outside: ref.update(digest="sha256:" + "0" * 64), "file_ref_integrity_mismatch"),
        (lambda ref, _outside: ref.update(size_bytes=ref["size_bytes"] + 1), "file_ref_integrity_mismatch"),
    ],
)
def test_file_ref_decision_table_rejects_scope_and_integrity_drift(monkeypatch, tmp_path, mutate, expected):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    file_ref = json.loads(json.dumps(descriptor.file_ref))
    outside = tmp_path / "outside.py"
    outside.write_text("def main(): pass", encoding="utf-8")
    mutate(file_ref, outside)
    assert _rejection(file_ref) == expected


def test_executor_rejects_hardlinked_materialization(monkeypatch, tmp_path):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    os.link(descriptor.file_path, tmp_path / "second-owner.py")
    assert _rejection(descriptor.file_ref) == "file_ref_hardlink"


def test_executor_rejects_hardlinked_materialization_sidecar(monkeypatch, tmp_path):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    path = Path(descriptor.file_path)
    os.link(path.with_name(path.name + ".meta.json"), tmp_path / "second-owner.meta.json")
    assert _rejection(descriptor.file_ref) == "file_ref_hardlink"


def test_executor_rejects_reparse_or_symlink_components_without_following(monkeypatch, tmp_path):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    monkeypatch.setattr(executor, "_is_reparse", lambda _value: True)
    assert _rejection(descriptor.file_ref) == "file_ref_unsafe_link"


def test_executor_rejects_independent_same_bytes_replacement(monkeypatch, tmp_path):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    path = Path(descriptor.file_path)
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(path.read_bytes())
    os.replace(replacement, path)
    assert _rejection(descriptor.file_ref) == "file_ref_independent_replacement"


def test_executor_rejects_identity_drift_during_validation(monkeypatch, tmp_path):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    path = Path(descriptor.file_path)
    original_recapture = executor._recapture
    calls = 0

    def mutate_then_recapture(candidate, captured):
        nonlocal calls
        calls += 1
        if calls == 1:
            candidate.write_bytes(candidate.read_bytes() + b"\n")
        original_recapture(candidate, captured)

    monkeypatch.setattr(executor, "_recapture", mutate_then_recapture)
    assert _rejection(descriptor.file_ref) == "file_ref_identity_drift"
    assert path.exists()


@pytest.mark.parametrize("mutation", ["replacement", "hardlink"])
def test_executor_revalidates_file_identity_immediately_before_source(monkeypatch, tmp_path, mutation):
    marker = f"_dcc_mcp_pre_dispatch_{mutation}_marker"
    content = (
        f"import builtins\nbuiltins.{marker} = True\n"
        "from dcc_mcp_core.skill import skill_success\n"
        "def main():\n    return skill_success('must not run')\n"
    )
    descriptor, _ = _materialized(monkeypatch, tmp_path, content)
    path = Path(descriptor.file_path)
    original_parse = executor.ast.parse
    mutated = False

    def mutate_after_parse(*args, **kwargs):
        nonlocal mutated
        syntax = original_parse(*args, **kwargs)
        if mutated:
            return syntax
        mutated = True
        if mutation == "replacement":
            replacement = tmp_path / "late-replacement.py"
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)
        else:
            os.link(path, tmp_path / "late-owner.py")
        return syntax

    monkeypatch.setattr(executor.ast, "parse", mutate_after_parse)
    expected = "file_ref_independent_replacement" if mutation == "replacement" else "file_ref_hardlink"
    try:
        assert _rejection(descriptor.file_ref) == expected
        assert not hasattr(__import__("builtins"), marker)
    finally:
        if hasattr(__import__("builtins"), marker):
            delattr(__import__("builtins"), marker)


def test_executor_rechecks_expiry_immediately_before_source(monkeypatch, tmp_path):
    marker = "_dcc_mcp_pre_dispatch_expiry_marker"
    content = (
        f"import builtins\nbuiltins.{marker} = True\n"
        "from dcc_mcp_core.skill import skill_success\n"
        "def main():\n    return skill_success('must not run')\n"
    )
    descriptor, _ = _materialized(monkeypatch, tmp_path, content, ttl_secs=1)
    original_parse = executor.ast.parse
    expired = False

    def expire_after_parse(*args, **kwargs):
        nonlocal expired
        syntax = original_parse(*args, **kwargs)
        if expired:
            return syntax
        expired = True
        time.sleep(1.1)
        return syntax

    monkeypatch.setattr(executor.ast, "parse", expire_after_parse)
    try:
        assert _rejection(descriptor.file_ref) == "file_ref_expired"
        assert not hasattr(__import__("builtins"), marker)
    finally:
        if hasattr(__import__("builtins"), marker):
            delattr(__import__("builtins"), marker)


def test_executor_rejects_oversized_and_non_regular_files(monkeypatch, tmp_path):
    oversized, _ = _materialized(monkeypatch, tmp_path, "#" * (MAX_MATERIALIZED_SCRIPT_BYTES + 1))
    assert _rejection(oversized.file_ref) == "file_ref_too_large"

    non_regular, _ = _materialized(monkeypatch, tmp_path / "other")
    path = Path(non_regular.file_path)
    path.unlink()
    path.mkdir()
    assert _rejection(non_regular.file_ref) in {"file_ref_not_regular", "file_ref_unavailable"}


def test_executor_rejects_invalid_utf8_even_with_a_self_consistent_descriptor(monkeypatch, tmp_path):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    path = Path(descriptor.file_path)
    metadata_path = path.with_name(path.name + ".meta.json")
    invalid = b"\xff\xfe\x00"
    digest = __import__("hashlib").sha256(invalid).hexdigest()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"] = digest
    metadata["bytes"] = len(invalid)
    metadata["file_ref"]["digest"] = f"sha256:{digest}"
    metadata["file_ref"]["size_bytes"] = len(invalid)
    path.write_bytes(invalid)
    metadata_path.write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    assert _rejection(metadata["file_ref"]) == "file_ref_invalid_encoding"


def test_executor_errors_are_stable_and_do_not_disclose_paths(monkeypatch, tmp_path):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    file_ref = json.loads(json.dumps(descriptor.file_ref))
    file_ref["digest"] = "sha256:" + "0" * 64
    code = _rejection(file_ref)
    assert code == "file_ref_integrity_mismatch"
    assert str(tmp_path) not in code


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("value = 42\n", "script_entrypoint_invalid"),
        ("def main():\n    return 42\n", "script_result_invalid"),
        ("def main():\n    raise RuntimeError('private host path')\n", "script_execution_failed"),
    ],
)
def test_fixed_entrypoint_decision_table_returns_stable_redacted_errors(monkeypatch, tmp_path, content, expected):
    descriptor, _ = _materialized(monkeypatch, tmp_path, content)
    code = _rejection(descriptor.file_ref)
    assert code == expected
    assert "private host path" not in code


def test_materialize_script_rejects_main_rebinding_through_the_real_mcp_route(monkeypatch, tmp_path):
    marker = "_dcc_mcp_rebound_main_marker"
    monkeypatch.setenv(SCRIPT_MATERIALIZATION_ROOT_ENV, str(tmp_path / "materialized"))
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

    client = McpClient(server.mcp_url)
    client.initialize()
    try:
        materialized = _structured(
            client.call_tool(
                "materialize_script",
                {
                    "content": (
                        "import builtins\n"
                        "from dcc_mcp_core.skill import skill_success\n"
                        "def alternate():\n"
                        f"    builtins.{marker} = True\n"
                        "    return skill_success('alternate ran')\n"
                        "def main():\n"
                        "    return skill_success('validated main')\n"
                        "main = alternate\n"
                    ),
                    "language": "python",
                    "suffix": ".py",
                    "session_id": "contract-session",
                    "tool_call_id": "materialize-call",
                },
            )
        )
        submitted = _structured(client.call_tool("execute_materialized_script", {"file_ref": materialized["file_ref"]}))
        deadline = time.monotonic() + 3
        while True:
            dispatcher.drain_queue(20)
            terminal = _job(_structured(client.call_tool("jobs_get_status", {"job_id": submitted["job_id"]})))
            if terminal.get("status") in {"completed", "failed", "error"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert terminal["result"]["success"] is True
        assert terminal["result"]["message"] == "validated main"
        assert not hasattr(__import__("builtins"), marker)
    finally:
        server.stop()
        if hasattr(__import__("builtins"), marker):
            delattr(__import__("builtins"), marker)


def test_executor_uses_host_entrypoint_boundary_captured_before_prefix(monkeypatch, tmp_path):
    """Prefix code cannot replace the host callable that invokes fixed main()."""
    marker = "_dcc_mcp_rebound_host_invoker_marker"
    content = (
        "import builtins\n"
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def forged(entrypoint):\n"
        f"    builtins.{marker} = True\n"
        "    return {'success': True, 'message': 'forged', 'context': {}}\n"
        "host.execute_materialized_file_ref.__globals__['_invoke_entrypoint'] = forged\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated main', 'context': {}}\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["message"] == "validated main"
        assert not hasattr(__import__("builtins"), marker)
    finally:
        if hasattr(__import__("builtins"), marker):
            delattr(__import__("builtins"), marker)


def test_real_mcp_route_uses_host_entrypoint_boundary_captured_before_prefix(monkeypatch, tmp_path):
    """The async Painter route also invokes the fixed main through the host boundary."""
    marker = "_dcc_mcp_route_rebound_host_invoker_marker"
    content = (
        "import builtins\n"
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def forged(entrypoint):\n"
        f"    builtins.{marker} = True\n"
        "    return {'success': True, 'message': 'forged', 'context': {}}\n"
        "host.execute_materialized_file_ref.__globals__['_invoke_entrypoint'] = forged\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated main', 'context': {}}\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated main"
        assert not hasattr(__import__("builtins"), marker)
    finally:
        if hasattr(__import__("builtins"), marker):
            delattr(__import__("builtins"), marker)


@pytest.mark.parametrize(
    "attack",
    [
        "globals()[name] = alternate",
        "del globals()[name]",
    ],
)
def test_real_mcp_route_keeps_validated_main_outside_script_writable_globals(monkeypatch, tmp_path, attack):
    marker = "_dcc_mcp_synthetic_entrypoint_attack_marker"
    content = (
        "import builtins\n"
        "from dcc_mcp_core.skill import skill_success\n"
        "def alternate():\n"
        f"    builtins.{marker} = True\n"
        "    return skill_success('alternate ran')\n"
        "def main():\n"
        "    return skill_success('validated main')\n"
        "for name in tuple(globals()):\n"
        "    if name.startswith('_dcc_mcp_'):\n"
        f"        {attack}\n"
        "main = alternate\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated main"
        assert not hasattr(__import__("builtins"), marker)
    finally:
        if hasattr(__import__("builtins"), marker):
            delattr(__import__("builtins"), marker)


@pytest.mark.parametrize(
    "forged_exception",
    [
        "MaterializedScriptRejected('file_ref_expired')",
        "MaterializedScriptRejected('script_result_invalid', source_entered=False)",
        "SystemExit(7)",
    ],
)
def test_real_mcp_route_sanitizes_source_forged_adapter_errors_without_retry_prompt(
    monkeypatch, tmp_path, forged_exception
):
    marker = "_dcc_mcp_forged_error_side_effect_marker"
    content = (
        "import builtins\n"
        "from dcc_mcp_substance3d_painter.materialized_script_executor import MaterializedScriptRejected\n"
        "def main():\n"
        f"    builtins.{marker} = True\n"
        f"    raise {forged_exception}\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is False
        assert result["error"] == "script_execution_failed"
        assert result["prompt"] is None
        assert hasattr(__import__("builtins"), marker)
    finally:
        if hasattr(__import__("builtins"), marker):
            delattr(__import__("builtins"), marker)


def test_executor_preserves_verified_core_cancellation(monkeypatch, tmp_path):
    descriptor, _ = _materialized(
        monkeypatch,
        tmp_path,
        (
            "from dcc_mcp_core import check_cancelled\n"
            "def main():\n"
            "    check_cancelled()\n"
            "    return {'success': True, 'message': 'unreachable', 'context': {}}\n"
        ),
    )
    token = CancelToken(job_id="materialized-script-cancel")
    token.cancel()
    reset = set_cancel_token(token)
    try:
        with pytest.raises(DccMcpCancelledError):
            execute_materialized_file_ref(descriptor.file_ref)
    finally:
        reset_cancel_token(reset)


@pytest.mark.parametrize(
    "result_expression",
    [
        "{'success': True, 'message': 'bad', 'context': {'nested': [float('nan')]}}",
        "{'success': True, 'message': 'bad', 'context': {}, 'postcondition': {'value': float('inf')}}",
        "{'success': False, 'message': 'bad', 'context': {'nested': {'value': -float('inf')}}}",
    ],
)
def test_executor_rejects_non_finite_values_nested_in_result_shapes(monkeypatch, tmp_path, result_expression):
    descriptor, _ = _materialized(
        monkeypatch,
        tmp_path,
        f"def main():\n    return {result_expression}\n",
    )
    assert _rejection(descriptor.file_ref) == "script_result_invalid"


@pytest.mark.parametrize(
    "content",
    [
        (
            "def main():\n"
            "    return {'success': True, 'message': 'bad', 'context': {'nested': {1: 'int', '1': 'string'}}}\n"
        ),
        ("def main():\n    return {'success': True, 'message': 'bad', 'context': {'nested': {True: 'bool'}}}\n"),
        ("def main():\n    return {'success': True, 'message': 'bad', 'context': {'nested': ('tuple',)}}\n"),
        (
            "class CustomMapping(dict):\n"
            "    pass\n"
            "def main():\n"
            "    return {'success': True, 'message': 'bad', 'context': {'nested': CustomMapping(safe=1)}}\n"
        ),
    ],
)
def test_executor_rejects_non_json_object_keys_and_container_types(monkeypatch, tmp_path, content):
    descriptor, _ = _materialized(monkeypatch, tmp_path, content)
    assert _rejection(descriptor.file_ref) == "script_result_invalid"


def test_invalid_entrypoint_is_rejected_before_top_level_source_runs(monkeypatch, tmp_path):
    marker = "_dcc_mcp_invalid_entrypoint_marker"
    content = f"import builtins\nbuiltins.{marker} = True\nvalue = 42\n"
    descriptor, _ = _materialized(monkeypatch, tmp_path, content)
    assert _rejection(descriptor.file_ref) == "script_entrypoint_invalid"
    assert not hasattr(__import__("builtins"), marker)


@pytest.mark.parametrize(
    "declaration",
    [
        "def main(value):\n    return value\n",
        "def main(*args):\n    return args\n",
        "async def main():\n    return None\n",
        "@staticmethod\ndef main():\n    return None\n",
    ],
)
def test_executor_rejects_unsupported_entrypoint_modes_before_execution(monkeypatch, tmp_path, declaration):
    descriptor, _ = _materialized(monkeypatch, tmp_path, declaration)
    assert _rejection(descriptor.file_ref) == "script_entrypoint_invalid"


def test_executor_rejects_expired_file_ref_before_source_execution(monkeypatch, tmp_path):
    descriptor, _ = _materialized(monkeypatch, tmp_path)
    file_ref = json.loads(json.dumps(descriptor.file_ref))
    file_ref["expires_at"] = "2000-01-01T00:00:00Z"
    assert _rejection(file_ref) == "file_ref_expired"


def test_real_mcp_route_keeps_captured_main_immune_to_function_object_mutation(monkeypatch, tmp_path):
    marker = "_dcc_mcp_entrypoint_code_mutation_marker"
    content = (
        "import builtins\n"
        "from dcc_mcp_core.skill import skill_success\n"
        "def alternate():\n"
        f"    builtins.{marker} = True\n"
        "    return skill_success('DIVERTED')\n"
        "def main():\n"
        "    return skill_success('validated main')\n"
        "main.__code__ = alternate.__code__\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated main"
        assert not hasattr(__import__("builtins"), marker)
    finally:
        if hasattr(__import__("builtins"), marker):
            delattr(__import__("builtins"), marker)


def test_real_mcp_route_keeps_captured_main_globals_immune_to_suffix_rebinding(monkeypatch, tmp_path):
    content = (
        "message = 'validated value'\n"
        "def main():\n"
        "    return {'success': True, 'message': message, 'context': {}}\n"
        "message = 'DIVERTED_BY_SUFFIX'\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is True
    assert result["message"] == "validated value"


def test_real_mcp_route_keeps_frozen_main_result_immune_to_frame_locals_rebinding(monkeypatch, tmp_path):
    content = (
        "import sys\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated value', 'context': {'state': {'message': 'validated value'}}}\n"
        "sys._getframe(1).f_locals['normalized']['message'] = 'DIVERTED_HOST_SNAPSHOT'\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is True
    assert result["message"] == "validated value"
    assert result["context"]["state"]["message"] == "validated value"


@pytest.mark.parametrize(
    ("result_expression", "mutation"),
    [
        (
            "{'success': True, 'message': 'nodes', 'context': {'items': [0] * 10000}}",
            "if name == 'result_node_limit': cell.cell_contents = 10**9\n            if name == 'node_count': cell.cell_contents = 0",
        ),
        (
            "{'success': True, 'message': 'finite', 'context': {'value': float('nan')}}",
            "if name == 'finite_number': cell.cell_contents = lambda value: True",
        ),
    ],
    ids=["node-limit", "finite-number"],
)
def test_executor_rejects_main_frame_validator_mutation(monkeypatch, tmp_path, result_expression, mutation):
    content = (
        "import sys\n"
        "def main():\n"
        "    caller = sys._getframe(1)\n"
        "    validator = caller.f_locals.get('normalize_result')\n"
        "    if validator is not None:\n"
        "        for name, cell in zip(validator.__code__.co_freevars, validator.__closure__):\n"
        f"            {mutation}\n"
        f"    return {result_expression}\n"
    )
    descriptor, _ = _materialized(monkeypatch, tmp_path, content)
    assert _rejection(descriptor.file_ref) == "script_result_invalid"


@pytest.mark.parametrize(
    "result_expression",
    [
        "{'success': True, 'message': 'nodes', 'context': {'items': [0] * 10000}}",
        "{'success': True, 'message': 'finite', 'context': {'value': float('nan')}}",
    ],
    ids=["node-limit", "finite-number"],
)
def test_real_mcp_route_rejects_main_frame_validator_mutation(monkeypatch, tmp_path, result_expression):
    content = (
        "import sys\n"
        "def main():\n"
        "    caller = sys._getframe(1)\n"
        "    validator = caller.f_locals.get('normalize_result')\n"
        "    if validator is not None:\n"
        "        for name, cell in zip(validator.__code__.co_freevars, validator.__closure__):\n"
        "            if name == 'result_node_limit': cell.cell_contents = 10**9\n"
        "            if name == 'node_count': cell.cell_contents = 0\n"
        "            if name == 'finite_number': cell.cell_contents = lambda value: True\n"
        f"    return {result_expression}\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is False
    assert result["error"] == "script_result_invalid"
    assert result["prompt"] is None


@pytest.mark.parametrize(
    "main_body",
    [
        "raise RuntimeError('main failed')",
        "return {'success': 'invalid', 'message': 'bad', 'context': {}}",
    ],
    ids=["exception", "invalid-result"],
)
def test_executor_runs_suffix_once_after_main_failure(monkeypatch, tmp_path, main_body):
    marker = "_dcc_mcp_suffix_after_main_failure_count"
    content = (
        f"import builtins\ndef main():\n    {main_body}\nbuiltins.{marker} = getattr(builtins, '{marker}', 0) + 1\n"
    )
    builtins_module = __import__("builtins")
    if hasattr(builtins_module, marker):
        delattr(builtins_module, marker)
    try:
        assert _rejection(_materialized(monkeypatch, tmp_path, content)[0].file_ref) in {
            "script_execution_failed",
            "script_result_invalid",
        }
        assert getattr(builtins_module, marker) == 1
    finally:
        if hasattr(builtins_module, marker):
            delattr(builtins_module, marker)


@pytest.mark.parametrize(
    "main_body",
    [
        "raise RuntimeError('main failed')",
        "return {'success': 'invalid', 'message': 'bad', 'context': {}}",
    ],
    ids=["exception", "invalid-result"],
)
def test_real_mcp_route_runs_suffix_once_after_main_failure(monkeypatch, tmp_path, main_body):
    marker = "_dcc_mcp_route_suffix_after_main_failure_count"
    content = (
        f"import builtins\ndef main():\n    {main_body}\nbuiltins.{marker} = getattr(builtins, '{marker}', 0) + 1\n"
    )
    builtins_module = __import__("builtins")
    if hasattr(builtins_module, marker):
        delattr(builtins_module, marker)
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is False
        assert result["error"] in {"script_execution_failed", "script_result_invalid"}
        assert getattr(builtins_module, marker) == 1
    finally:
        if hasattr(builtins_module, marker):
            delattr(builtins_module, marker)


def test_real_mcp_route_isolates_captured_main_from_suffix_mutable_dict_alias(monkeypatch, tmp_path):
    content = (
        "state = {'message': 'validated value'}\n"
        "def main():\n"
        "    return {'success': True, 'message': state['message'], 'context': {}}\n"
        "state['message'] = 'DIVERTED_BY_SUFFIX_ALIAS'\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is True
    assert result["message"] == "validated value"


@pytest.mark.parametrize(
    ("prefix", "message_expression", "suffix"),
    [
        ("state = ['validated value']\n", "state[0]", "state[0] = 'DIVERTED_BY_LIST_ALIAS'\n"),
        (
            "state = {'validated value'}\n",
            "'validated value' if 'validated value' in state else 'DIVERTED_BY_SET_ALIAS'",
            "state.clear()\nstate.add('DIVERTED_BY_SET_ALIAS')\n",
        ),
        (
            "class MutableBox:\n"
            "    def __init__(self):\n"
            "        self.values = {'message': 'validated value'}\n"
            "    def __getitem__(self, key):\n"
            "        return self.values[key]\n"
            "    def __setitem__(self, key, value):\n"
            "        self.values[key] = value\n"
            "state = MutableBox()\n",
            "state['message']",
            "state['message'] = 'DIVERTED_BY_CUSTOM_ALIAS'\n",
        ),
        (
            "class State:\n    pass\nstate = State()\nstate.message = 'validated value'\n",
            "state.message",
            "state.message = 'DIVERTED_BY_OBJECT_ALIAS'\n",
        ),
        (
            "import types\nstate = types.ModuleType('materialized_state')\nstate.message = 'validated value'\n",
            "state.message",
            "state.message = 'DIVERTED_BY_MODULE_ALIAS'\n",
        ),
    ],
    ids=["list", "set", "custom-mutable", "object-attribute", "module-attribute"],
)
def test_real_mcp_route_isolates_captured_main_from_all_suffix_mutable_alias_shapes(
    monkeypatch, tmp_path, prefix, message_expression, suffix
):
    content = (
        prefix
        + "def main():\n"
        + f"    return {{'success': True, 'message': {message_expression}, 'context': {{}}}}\n"
        + suffix
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is True
    assert result["message"] == "validated value"


def test_real_mcp_route_preserves_prefix_helpers_and_executes_each_source_phase_once(monkeypatch, tmp_path):
    prefix_marker = "_dcc_mcp_materialized_prefix_execution_count"
    suffix_marker = "_dcc_mcp_materialized_suffix_execution_count"
    content = (
        "import builtins\n"
        "import json\n"
        f"builtins.{prefix_marker} = getattr(builtins, '{prefix_marker}', 0) + 1\n"
        "def helper():\n"
        "    return json.loads('\"validated value\"')\n"
        "def main():\n"
        "    return {'success': True, 'message': helper(), 'context': {}}\n"
        f"builtins.{suffix_marker} = getattr(builtins, '{suffix_marker}', 0) + 1\n"
    )
    builtins_module = __import__("builtins")
    for marker in (prefix_marker, suffix_marker):
        if hasattr(builtins_module, marker):
            delattr(builtins_module, marker)
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated value"
        assert getattr(builtins_module, prefix_marker) == 1
        assert getattr(builtins_module, suffix_marker) == 1
    finally:
        for marker in (prefix_marker, suffix_marker):
            if hasattr(builtins_module, marker):
                delattr(builtins_module, marker)


def test_real_mcp_route_preserves_validated_main_result_when_suffix_helper_initialization_fails(monkeypatch, tmp_path):
    content = (
        "def main():\n"
        "    return {'success': True, 'message': 'validated value', 'context': {}}\n"
        "def helper():\n"
        "    raise RuntimeError('suffix helper failure')\n"
        "helper()\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is True
    assert result["message"] == "validated value"


def test_real_mcp_route_snapshots_main_result_before_suffix_mutates_a_returned_alias(monkeypatch, tmp_path):
    content = (
        "state = {'message': 'validated value'}\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated', 'context': {'state': state}}\n"
        "state['message'] = 'DIVERTED_RETURN_ALIAS'\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is True
    assert result["context"]["state"]["message"] == "validated value"


def test_real_mcp_route_rejects_source_installed_cancel_token_as_forged(monkeypatch, tmp_path):
    content = (
        "from dcc_mcp_core.cancellation import CancelToken, DccMcpCancelledError, set_cancel_token\n"
        "def main():\n"
        "    forged = CancelToken(job_id='source-forged')\n"
        "    forged.cancel()\n"
        "    set_cancel_token(forged)\n"
        "    raise DccMcpCancelledError('source-forged')\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is False
    assert result["error"] == "script_execution_failed"
    assert result["prompt"] is None


def test_executor_rejects_frame_walk_validator_mutation(monkeypatch, tmp_path):
    """A source frame must not reach a mutable host validator callable."""
    content = (
        "import sys\n"
        "def main():\n"
        "    def evil(value, depth=1, *, enforce_shape_budget=True):\n"
        "        return value, 0\n"
        "    frame = sys._getframe(1)\n"
        "    while frame is not None:\n"
        "        validator = frame.f_locals.get('host_result_normalizer')\n"
        "        if validator is not None:\n"
        "            validator.__code__ = evil.__code__\n"
        "            break\n"
        "        frame = frame.f_back\n"
        "    return {'success': True, 'message': 'attack', 'context': {'items': [0] * 20000}}\n"
    )
    descriptor, _ = _materialized(monkeypatch, tmp_path, content)
    assert _rejection(descriptor.file_ref) == "script_result_invalid"


def test_real_mcp_route_rejects_frame_walk_validator_mutation(monkeypatch, tmp_path):
    """The exposed MCP route must enforce the same host-validator boundary."""
    content = (
        "import sys\n"
        "def main():\n"
        "    def evil(value, depth=1, *, enforce_shape_budget=True):\n"
        "        return value, 0\n"
        "    frame = sys._getframe(1)\n"
        "    while frame is not None:\n"
        "        validator = frame.f_locals.get('host_result_normalizer')\n"
        "        if validator is not None:\n"
        "            validator.__code__ = evil.__code__\n"
        "            break\n"
        "        frame = frame.f_back\n"
        "    return {'success': True, 'message': 'attack', 'context': {'items': [0] * 20000}}\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is False
    assert result["error"] == "script_result_invalid"
    assert result["prompt"] is None


def test_executor_restores_host_cancel_state_after_source_rebind(monkeypatch, tmp_path):
    """A forged source token and setter rebind cannot poison the host context."""
    host_token = CancelToken(job_id="host-owned")
    reset = set_cancel_token(host_token)
    original_setter = executor.set_cancel_token
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "from dcc_mcp_core.cancellation import CancelToken, set_cancel_token\n"
        "def main():\n"
        "    forged = CancelToken(job_id='source-forged')\n"
        "    set_cancel_token(forged)\n"
        "    host.set_cancel_token = lambda token: None\n"
        "    return {'success': True, 'message': 'ok', 'context': {}}\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["success"] is True
        assert current_cancel_token() is host_token
        assert executor.set_cancel_token is original_setter
    finally:
        reset_cancel_token(reset)
        executor.set_cancel_token = original_setter


def test_real_mcp_route_restores_host_cancel_api_after_source_rebind(monkeypatch, tmp_path):
    """The MCP route must leave cancellation state/API intact after a source attack."""
    original_setter = executor.set_cancel_token
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "from dcc_mcp_core.cancellation import CancelToken, set_cancel_token\n"
        "def main():\n"
        "    forged = CancelToken(job_id='source-forged')\n"
        "    set_cancel_token(forged)\n"
        "    host.set_cancel_token = lambda token: None\n"
        "    return {'success': True, 'message': 'ok', 'context': {}}\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    try:
        assert result["success"] is True
        assert executor.set_cancel_token is original_setter
    finally:
        executor.set_cancel_token = original_setter


def test_executor_restores_host_cancel_state_after_suffix_rebind(monkeypatch, tmp_path):
    """The side-effect-only suffix cannot poison the host context either."""
    host_token = CancelToken(job_id="host-owned")
    reset = set_cancel_token(host_token)
    original_setter = executor.set_cancel_token
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "from dcc_mcp_core.cancellation import CancelToken, set_cancel_token\n"
        "def main():\n"
        "    return {'success': True, 'message': 'ok', 'context': {}}\n"
        "forged = CancelToken(job_id='suffix-forged')\n"
        "set_cancel_token(forged)\n"
        "host.set_cancel_token = lambda token: None\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["success"] is True
        assert current_cancel_token() is host_token
        assert executor.set_cancel_token is original_setter
    finally:
        reset_cancel_token(reset)
        executor.set_cancel_token = original_setter


def test_real_mcp_route_keeps_result_validator_outside_source_writable_state(monkeypatch, tmp_path):
    attacked_names = (
        "_require_strict_json",
        "_reject",
        "FunctionType",
        "MaterializedScriptRejected",
        "DccMcpCancelledError",
        "json",
        "MAX_SCRIPT_RESULT_DEPTH",
        "MAX_SCRIPT_RESULT_NODES",
        "MAX_SCRIPT_RESULT_JSON_BYTES",
    )
    sentinel = object()
    originals = {name: getattr(executor, name, sentinel) for name in attacked_names}
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    host._require_strict_json = lambda value: None\n"
        "    host._reject = lambda *args, **kwargs: None\n"
        "    host.FunctionType = lambda *args, **kwargs: None\n"
        "    host.MaterializedScriptRejected = RuntimeError\n"
        "    host.DccMcpCancelledError = RuntimeError\n"
        "    host.MAX_SCRIPT_RESULT_DEPTH = 10**9\n"
        "    host.MAX_SCRIPT_RESULT_NODES = 10**9\n"
        "    host.MAX_SCRIPT_RESULT_JSON_BYTES = 10**9\n"
        "    host.json = None\n"
        "    return {'success': True, 'message': 'bypassed', 'context': {'tuple': (1, 2)}}\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is False
        assert result["error"] == "script_result_invalid"
        assert result["prompt"] is None
    finally:
        for name, original in originals.items():
            if original is sentinel:
                if hasattr(executor, name):
                    delattr(executor, name)
            else:
                setattr(executor, name, original)


def test_executor_rejects_over_budget_result_when_source_patches_runpy_loader(monkeypatch, tmp_path):
    import runpy

    original_run_path = runpy.run_path
    content = (
        "import runpy\n"
        "runpy.run_path = lambda *args, **kwargs: {'normalize_result': lambda value, **kwargs: (value, 0)}\n"
        "def main():\n"
        "    return {'success': True, 'message': 'over-budget', 'context': {'items': [0] * 20000}}\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        assert _rejection(descriptor.file_ref) == "script_result_invalid"
    finally:
        runpy.run_path = original_run_path


def test_real_mcp_route_rejects_over_budget_result_when_source_patches_runpy_loader(monkeypatch, tmp_path):
    import runpy

    original_run_path = runpy.run_path
    content = (
        "import runpy\n"
        "runpy.run_path = lambda *args, **kwargs: {'normalize_result': lambda value, **kwargs: (value, 0)}\n"
        "def main():\n"
        "    return {'success': True, 'message': 'over-budget', 'context': {'items': [0] * 20000}}\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is False
        assert result["error"] == "script_result_invalid"
        assert result["prompt"] is None
    finally:
        runpy.run_path = original_run_path


def test_executor_rejects_cross_request_host_validator_alias_poisoning(monkeypatch, tmp_path):
    """A prior materialized request cannot replace the host validator for the next request."""
    missing = object()
    original_alias = getattr(executor, "_HOST_RESULT_NORMALIZER", missing)
    poison = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    host._HOST_RESULT_NORMALIZER = lambda value, **kwargs: (value, 0)\n"
        "    return {'success': True, 'message': 'poisoned', 'context': {}}\n"
    )
    over_budget = (
        "def main():\n    return {'success': True, 'message': 'over-budget', 'context': {'items': [0] * 20000}}\n"
    )
    try:
        first, _ = _materialized(monkeypatch, tmp_path / "first", poison)
        assert execute_materialized_file_ref(first.file_ref)["message"] == "poisoned"
        second, _ = _materialized(monkeypatch, tmp_path / "second", over_budget)
        assert _rejection(second.file_ref) == "script_result_invalid"
    finally:
        if original_alias is missing:
            executor.__dict__.pop("_HOST_RESULT_NORMALIZER", None)
        else:
            executor._HOST_RESULT_NORMALIZER = original_alias


def test_executor_ignores_source_json_serializer_mutation(monkeypatch, tmp_path):
    """A source-owned json.dumps cannot forge a post-validation snapshot."""
    original_json = executor.json
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def forged_dump(*args, **kwargs):\n"
        '    return \'{"success":true,"message":"forged","context":{"items":[\' + \',\'.join([\'0\'] * 20000) + \']}}\'\n'
        "def main():\n"
        "    host.json.dumps = forged_dump\n"
        "    return {'success': True, 'message': 'ok', 'context': {}}\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["success"] is True
        assert result["message"] == "ok"
        assert result["context"]["execution_file"]["method"] == "validated_file_ref_snapshot"
        assert "items" not in result["context"]
    finally:
        executor.json = original_json


def test_executor_restores_core_cancellation_module_after_source_rebind(monkeypatch, tmp_path):
    """A materialized request cannot replace Core's process-global ContextVar."""
    import dcc_mcp_core.cancellation as cancellation

    original_context = cancellation.__dict__["_current_token"]
    content = (
        "import contextvars\n"
        "import dcc_mcp_core.cancellation as cancellation\n"
        "def main():\n"
        "    cancellation._current_token = contextvars.ContextVar('forged-token', default='FORGED')\n"
        "    return {'success': True, 'message': 'ok', 'context': {}}\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        assert execute_materialized_file_ref(descriptor.file_ref)["success"] is True
        assert cancellation.__dict__["_current_token"] is original_context
        assert current_cancel_token() is None
    finally:
        cancellation.__dict__["_current_token"] = original_context


def test_real_mcp_route_rejects_cross_request_host_validator_alias_poisoning(monkeypatch, tmp_path):
    """The public MCP route restores the validator alias between requests."""
    poison = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    host._HOST_RESULT_NORMALIZER = lambda value, **kwargs: (value, 0)\n"
        "    return {'success': True, 'message': 'poisoned', 'context': {}}\n"
    )
    over_budget = (
        "def main():\n    return {'success': True, 'message': 'over-budget', 'context': {'items': [0] * 20000}}\n"
    )
    first = _execute_through_mcp(monkeypatch, tmp_path / "first", poison)
    second = _execute_through_mcp(monkeypatch, tmp_path / "second", over_budget)
    assert first["success"] is True
    assert second["success"] is False
    assert second["error"] == "script_result_invalid"
    assert second["prompt"] is None


def test_real_mcp_route_restores_core_cancellation_module_after_source_rebind(monkeypatch, tmp_path):
    """The public MCP route cannot leave a forged cancellation ContextVar behind."""
    import dcc_mcp_core.cancellation as cancellation

    original_context = cancellation.__dict__["_current_token"]
    content = (
        "import contextvars\n"
        "import dcc_mcp_core.cancellation as cancellation\n"
        "def main():\n"
        "    cancellation._current_token = contextvars.ContextVar('forged-token', default='FORGED')\n"
        "    return {'success': True, 'message': 'ok', 'context': {}}\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert cancellation.__dict__["_current_token"] is original_context
        assert current_cancel_token() is None
    finally:
        cancellation.__dict__["_current_token"] = original_context


def test_real_mcp_route_ignores_source_json_serializer_mutation(monkeypatch, tmp_path):
    """The MCP route serializes from the host-owned json callable."""
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def forged_dump(*args, **kwargs):\n"
        '    return \'{"success":true,"message":"forged","context":{"items":[\' + \',\'.join([\'0\'] * 20000) + \']}}\'\n'
        "def main():\n"
        "    host.json.dumps = forged_dump\n"
        "    return {'success': True, 'message': 'ok', 'context': {}}\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is True
    assert result["message"] == "ok"
    assert result["context"]["execution_file"]["method"] == "validated_file_ref_snapshot"
    assert "items" not in result["context"]


@pytest.mark.parametrize(
    "content",
    [
        (
            "def main():\n"
            "    return {'success': True, 'message': helper(), 'context': {}}\n"
            "def helper():\n"
            "    return 'helper declared after main'\n"
        ),
        (
            "state = {}\n"
            "def main():\n"
            "    return {'success': True, 'message': state['message'], 'context': {}}\n"
            "state['message'] = 'initialized after main'\n"
        ),
    ],
    ids=["helper-declaration", "state-initialization"],
)
def test_executor_rejects_suffix_only_main_dependencies_with_stable_error(monkeypatch, tmp_path, content):
    descriptor, _ = _materialized(monkeypatch, tmp_path, content)
    assert _rejection(descriptor.file_ref) == "script_suffix_dependency"


@pytest.mark.parametrize(
    "content",
    [
        (
            "def main():\n"
            "    return {'success': True, 'message': helper(), 'context': {}}\n"
            "def helper():\n"
            "    return 'helper declared after main'\n"
        ),
        (
            "state = {}\n"
            "def main():\n"
            "    return {'success': True, 'message': state['message'], 'context': {}}\n"
            "state['message'] = 'initialized after main'\n"
        ),
    ],
    ids=["helper-declaration", "state-initialization"],
)
def test_real_mcp_route_rejects_suffix_only_main_dependencies_with_stable_error(monkeypatch, tmp_path, content):
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is False
    assert result["error"] == "script_suffix_dependency"
    assert result["prompt"] is None


def test_executor_rejects_over_budget_result_when_source_rebinds_module_validator(monkeypatch, tmp_path):
    original_validator = executor._normalize_result
    missing = object()
    original_alias = getattr(executor, "_HOST_NORMALIZE_RESULT", missing)
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    host._HOST_NORMALIZE_RESULT = lambda *args, **kwargs: ({'success': True, 'message': 'bypassed', 'context': {}}, 0)\n"
        "    return {'success': True, 'message': 'over-budget', 'context': {'items': [0] * 20000}}\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        assert _rejection(descriptor.file_ref) == "script_result_invalid"
    finally:
        executor._normalize_result = original_validator
        if original_alias is missing:
            executor.__dict__.pop("_HOST_NORMALIZE_RESULT", None)
        else:
            executor._HOST_NORMALIZE_RESULT = original_alias


def test_real_mcp_route_rejects_over_budget_result_when_source_rebinds_module_validator(monkeypatch, tmp_path):
    original_validator = executor._normalize_result
    missing = object()
    original_alias = getattr(executor, "_HOST_NORMALIZE_RESULT", missing)
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    host._HOST_NORMALIZE_RESULT = lambda *args, **kwargs: ({'success': True, 'message': 'bypassed', 'context': {}}, 0)\n"
        "    return {'success': True, 'message': 'over-budget', 'context': {'items': [0] * 20000}}\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is False
        assert result["error"] == "script_result_invalid"
        assert result["prompt"] is None
    finally:
        executor._normalize_result = original_validator
        if original_alias is missing:
            executor.__dict__.pop("_HOST_NORMALIZE_RESULT", None)
        else:
            executor._HOST_NORMALIZE_RESULT = original_alias


def test_executor_keeps_postprocessed_result_when_suffix_rebinds_module_validator(monkeypatch, tmp_path):
    original_validator = executor._normalize_result
    missing = object()
    original_alias = getattr(executor, "_HOST_NORMALIZE_RESULT", missing)
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
        "host._HOST_NORMALIZE_RESULT = lambda *args, **kwargs: ({'success': True, 'message': 'bypassed', 'context': {}}, 0)\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["message"] == "validated"
        assert result["context"]["sha256"] == descriptor.sha256
        assert result["postcondition"]["verified"] is True
    finally:
        executor._normalize_result = original_validator
        if original_alias is missing:
            executor.__dict__.pop("_HOST_NORMALIZE_RESULT", None)
        else:
            executor._HOST_NORMALIZE_RESULT = original_alias


def test_real_mcp_route_keeps_postprocessed_result_when_suffix_rebinds_module_validator(monkeypatch, tmp_path):
    original_validator = executor._normalize_result
    missing = object()
    original_alias = getattr(executor, "_HOST_NORMALIZE_RESULT", missing)
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
        "host._HOST_NORMALIZE_RESULT = lambda *args, **kwargs: ({'success': True, 'message': 'bypassed', 'context': {}}, 0)\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated"
        assert result["context"]["execution_file"]["method"] == "validated_file_ref_snapshot"
        assert result["postcondition"]["verified"] is True
    finally:
        executor._normalize_result = original_validator
        if original_alias is missing:
            executor.__dict__.pop("_HOST_NORMALIZE_RESULT", None)
        else:
            executor._HOST_NORMALIZE_RESULT = original_alias


def test_executor_rejects_over_budget_result_when_source_mutates_validator_defaults(monkeypatch, tmp_path):
    original_defaults = dict(executor._normalize_result.__kwdefaults__ or {})
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    host._normalize_result.__kwdefaults__['_result_node_limit'] = 10**9\n"
        "    return {'success': True, 'message': 'over-budget', 'context': {'items': [0] * 20000}}\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        assert _rejection(descriptor.file_ref) == "script_result_invalid"
    finally:
        executor._normalize_result.__kwdefaults__.clear()
        executor._normalize_result.__kwdefaults__.update(original_defaults)


def test_real_mcp_route_rejects_over_budget_result_when_source_mutates_validator_defaults(monkeypatch, tmp_path):
    original_defaults = dict(executor._normalize_result.__kwdefaults__ or {})
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    host._normalize_result.__kwdefaults__['_result_node_limit'] = 10**9\n"
        "    return {'success': True, 'message': 'over-budget', 'context': {'items': [0] * 20000}}\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is False
        assert result["error"] == "script_result_invalid"
        assert result["prompt"] is None
    finally:
        executor._normalize_result.__kwdefaults__.clear()
        executor._normalize_result.__kwdefaults__.update(original_defaults)


def test_executor_keeps_postprocessed_result_when_suffix_mutates_validator_defaults(monkeypatch, tmp_path):
    original_defaults = dict(executor._normalize_result.__kwdefaults__ or {})
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
        "host._normalize_result.__kwdefaults__['_result_node_limit'] = 10**9\n"
    )
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["message"] == "validated"
        assert result["context"]["sha256"] == descriptor.sha256
        assert result["postcondition"]["verified"] is True
    finally:
        executor._normalize_result.__kwdefaults__.clear()
        executor._normalize_result.__kwdefaults__.update(original_defaults)


def test_real_mcp_route_keeps_postprocessed_result_when_suffix_mutates_validator_defaults(monkeypatch, tmp_path):
    original_defaults = dict(executor._normalize_result.__kwdefaults__ or {})
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
        "host._normalize_result.__kwdefaults__['_result_node_limit'] = 10**9\n"
    )
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated"
        assert result["context"]["execution_file"]["method"] == "validated_file_ref_snapshot"
        assert result["postcondition"]["verified"] is True
    finally:
        executor._normalize_result.__kwdefaults__.clear()
        executor._normalize_result.__kwdefaults__.update(original_defaults)


@pytest.mark.parametrize(
    ("depth", "expected_success"),
    [
        (MAX_SCRIPT_RESULT_DEPTH, True),
        (MAX_SCRIPT_RESULT_DEPTH + 1, False),
    ],
)
def test_real_mcp_route_enforces_exact_result_depth_boundary(monkeypatch, tmp_path, depth, expected_success):
    content = (
        "def main():\n"
        "    value = 'leaf'\n"
        f"    for _ in range({depth - 2}):\n"
        "        value = [value]\n"
        "    return {'success': True, 'message': 'depth', 'context': {'value': value}}\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is expected_success
    if not expected_success:
        assert result["error"] == "script_result_invalid"
        assert result["prompt"] is None


@pytest.mark.parametrize(
    ("node_count", "expected_success"),
    [
        (MAX_SCRIPT_RESULT_NODES, True),
        (MAX_SCRIPT_RESULT_NODES + 1, False),
    ],
)
def test_real_mcp_route_enforces_exact_result_node_boundary(monkeypatch, tmp_path, node_count, expected_success):
    fixed_nodes = 5  # root dict + success/message values + context dict + items list
    content = (
        "def main():\n"
        f"    return {{'success': True, 'message': 'nodes', 'context': {{'items': [0] * {node_count - fixed_nodes}}}}}\n"
    )
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is expected_success
    if not expected_success:
        assert result["error"] == "script_result_invalid"
        assert result["prompt"] is None


def _public_response_boundary_fixture(target_bytes):
    payload_size = target_bytes
    while True:
        payload = "x" * payload_size
        content = (
            f"def main():\n    return {{'success': True, 'message': 'bytes', 'context': {{'payload': {payload!r}}}}}\n"
        )
        source_bytes = len(content.encode("utf-8"))
        execution_file = {
            "method": "validated_file_ref_snapshot",
            "sha256": "0" * 64,
            "bytes": source_bytes,
        }
        public_response = {
            "success": True,
            "message": "bytes",
            "context": {
                "payload": payload,
                "sha256": "0" * 64,
                "bytes": source_bytes,
                "execution_file": execution_file,
            },
            "postcondition": {"verified": True, **execution_file},
        }
        actual_bytes = len(json.dumps(public_response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        difference = target_bytes - actual_bytes
        if difference == 0:
            return content, public_response
        payload_size += difference


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_real_mcp_route_enforces_exact_complete_public_response_byte_boundary(monkeypatch, tmp_path, extra_bytes):
    target_bytes = MAX_SCRIPT_RESULT_JSON_BYTES + extra_bytes
    content, expected_response = _public_response_boundary_fixture(target_bytes)
    assert len(json.dumps(expected_response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) == target_bytes
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is (extra_bytes == 0)
    if not extra_bytes:
        assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) == target_bytes
    else:
        assert result["error"] == "script_result_invalid"
        assert result["prompt"] is None


@pytest.mark.parametrize("serializer", ["encoder", "decoder"])
def test_executor_isolates_json_serializer_internals_from_materialized_source(monkeypatch, tmp_path, serializer):
    """Source cannot replace the host JSON encoder/decoder used after main()."""
    if serializer == "encoder":
        content = (
            "import json\n"
            "_OriginalEncoder = json.JSONEncoder\n"
            "class EvilEncoder(_OriginalEncoder):\n"
            "    def encode(self, value):\n"
            "        json.JSONEncoder = _OriginalEncoder\n"
            '        return \'{"success":true,"message":"forged","context":{"items":[\' + \',\'.join([\'0\'] * 20000) + \']}}\'\n'
            "json.JSONEncoder = EvilEncoder\n"
            "def main():\n"
            "    return {'success': True, 'message': 'validated', 'context': {}}\n"
        )
    else:
        content = (
            "import json\n"
            "_OriginalDecoder = json._default_decoder\n"
            "class EvilDecoder:\n"
            "    def decode(self, value, **kwargs):\n"
            "        json._default_decoder = _OriginalDecoder\n"
            "        return {'success': True, 'message': 'forged', 'context': {'items': [0] * 20000}}\n"
            "json._default_decoder = EvilDecoder()\n"
            "def main():\n"
            "    return {'success': True, 'message': 'validated', 'context': {}}\n"
        )
    original_encoder = json.JSONEncoder
    original_decoder = json._default_decoder
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["success"] is True
        assert result["message"] == "validated"
        assert "items" not in result["context"]
    finally:
        json.JSONEncoder = original_encoder
        json._default_decoder = original_decoder


@pytest.mark.parametrize("serializer", ["encoder", "decoder"])
def test_real_mcp_route_isolates_json_serializer_internals_from_materialized_source(monkeypatch, tmp_path, serializer):
    """The public Painter route must reject serializer-internal result forgeries."""
    if serializer == "encoder":
        content = (
            "import json\n"
            "_OriginalEncoder = json.JSONEncoder\n"
            "class EvilEncoder(_OriginalEncoder):\n"
            "    def encode(self, value):\n"
            "        json.JSONEncoder = _OriginalEncoder\n"
            '        return \'{"success":true,"message":"forged","context":{"items":[\' + \',\'.join([\'0\'] * 20000) + \']}}\'\n'
            "json.JSONEncoder = EvilEncoder\n"
            "def main():\n"
            "    return {'success': True, 'message': 'validated', 'context': {}}\n"
        )
    else:
        content = (
            "import json\n"
            "_OriginalDecoder = json._default_decoder\n"
            "class EvilDecoder:\n"
            "    def decode(self, value, **kwargs):\n"
            "        json._default_decoder = _OriginalDecoder\n"
            "        return {'success': True, 'message': 'forged', 'context': {'items': [0] * 20000}}\n"
            "json._default_decoder = EvilDecoder()\n"
            "def main():\n"
            "    return {'success': True, 'message': 'validated', 'context': {}}\n"
        )
    original_encoder = json.JSONEncoder
    original_decoder = json._default_decoder
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated"
        assert "items" not in result["context"]
    finally:
        json.JSONEncoder = original_encoder
        json._default_decoder = original_decoder


def test_executor_prevents_delayed_source_thread_from_poisoning_json_serializer_state(monkeypatch, tmp_path):
    """A source-created daemon cannot rebind host JSON state after return."""
    content = (
        "import json\n"
        "import threading\n"
        "import time\n"
        "_OriginalEncoder = json.JSONEncoder\n"
        "class EvilEncoder(_OriginalEncoder):\n"
        "    pass\n"
        "def poison():\n"
        "    time.sleep(0.05)\n"
        "    json.JSONEncoder = EvilEncoder\n"
        "def main():\n"
        "    threading.Thread(target=poison, daemon=True).start()\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
    )
    original_encoder = json.JSONEncoder
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["message"] == "validated"
        time.sleep(0.1)
        assert json.JSONEncoder is original_encoder
    finally:
        json.JSONEncoder = original_encoder


def test_executor_keeps_delayed_host_alias_on_request_private_json(monkeypatch, tmp_path):
    """A retained executor alias must never regain the canonical JSON package."""
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "import threading\n"
        "import time\n"
        "class EvilEncoder:\n"
        "    pass\n"
        "def poison():\n"
        "    time.sleep(0.05)\n"
        "    host.json.JSONEncoder = EvilEncoder\n"
        "def main():\n"
        "    threading.Thread(target=poison, daemon=True).start()\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
    )
    original_encoder = json.JSONEncoder
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["message"] == "validated"
        time.sleep(0.1)
        assert json.JSONEncoder is original_encoder
    finally:
        json.JSONEncoder = original_encoder


def test_real_mcp_route_prevents_delayed_source_thread_from_poisoning_json_serializer_state(monkeypatch, tmp_path):
    """The MCP route must remain isolated after a source daemon outlives main()."""
    content = (
        "import json\n"
        "import threading\n"
        "import time\n"
        "_OriginalEncoder = json.JSONEncoder\n"
        "class EvilEncoder(_OriginalEncoder):\n"
        "    pass\n"
        "def poison():\n"
        "    time.sleep(0.05)\n"
        "    json.JSONEncoder = EvilEncoder\n"
        "def main():\n"
        "    threading.Thread(target=poison, daemon=True).start()\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
    )
    original_encoder = json.JSONEncoder
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated"
        time.sleep(0.1)
        assert json.JSONEncoder is original_encoder
    finally:
        json.JSONEncoder = original_encoder


def test_real_mcp_route_keeps_delayed_host_alias_on_request_private_json(monkeypatch, tmp_path):
    """The async Painter route must not return a retained alias to host JSON."""
    content = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "import threading\n"
        "import time\n"
        "class EvilEncoder:\n"
        "    pass\n"
        "def poison():\n"
        "    time.sleep(0.05)\n"
        "    host.json.JSONEncoder = EvilEncoder\n"
        "def main():\n"
        "    threading.Thread(target=poison, daemon=True).start()\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
    )
    original_encoder = json.JSONEncoder
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated"
        time.sleep(0.1)
        assert json.JSONEncoder is original_encoder
    finally:
        json.JSONEncoder = original_encoder


def test_executor_isolates_json_callable_globals_from_materialized_source(monkeypatch, tmp_path):
    """Serializer callable globals exposed to source are per-request copies."""
    content = (
        "import json\n"
        "def forged_dumps(*args, **kwargs):\n"
        '    return \'{"success":true,"message":"forged","context":{"items":[\' + \',\'.join([\'0\'] * 20000) + \']}}\'\n'
        "json.dumps.__globals__['JSONEncoder'] = forged_dumps\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
    )
    host_globals = json.dumps.__globals__
    original = host_globals.get("JSONEncoder")
    try:
        descriptor, _ = _materialized(monkeypatch, tmp_path, content)
        result = execute_materialized_file_ref(descriptor.file_ref)
        assert result["message"] == "validated"
        assert host_globals.get("JSONEncoder") is original
    finally:
        if original is None:
            host_globals.pop("JSONEncoder", None)
        else:
            host_globals["JSONEncoder"] = original


def test_real_mcp_route_isolates_json_callable_globals_from_materialized_source(monkeypatch, tmp_path):
    """The public route does not expose canonical ``json.loads`` globals."""
    content = (
        "import json\n"
        "class EvilDecoder:\n"
        "    def decode(self, value, **kwargs):\n"
        "        return {'success': True, 'message': 'forged', 'context': {'items': [0] * 20000}}\n"
        "json.loads.__globals__['_default_decoder'] = EvilDecoder()\n"
        "def main():\n"
        "    return {'success': True, 'message': 'validated', 'context': {}}\n"
    )
    host_globals = json.loads.__globals__
    original = host_globals.get("_default_decoder")
    try:
        result = _execute_through_mcp(monkeypatch, tmp_path, content)
        assert result["success"] is True
        assert result["message"] == "validated"
        assert json.loads('{"verified": true}') == {"verified": True}
        assert host_globals.get("_default_decoder") is not original
    finally:
        if original is None:
            host_globals.pop("_default_decoder", None)
        else:
            host_globals["_default_decoder"] = original


def _freeze_snapshot_frame_walk_attack():
    return (
        "import sys\n"
        "result = {'success': True, 'message': 'validated', 'context': {'items': []}}\n"
        "def main():\n"
        "    frame = sys._getframe(1)\n"
        "    while frame is not None:\n"
        "        freezer = frame.f_locals.get('freeze_snapshot')\n"
        "        if freezer is not None:\n"
        "            def preserve_alias(value):\n"
        "                return ('atom', value)\n"
        "            freezer.__closure__[0].cell_contents = preserve_alias\n"
        "            break\n"
        "        frame = frame.f_back\n"
        "    return result\n"
        "result['context']['items'] = [0] * 20000\n"
    )


def test_executor_preserves_snapshot_after_frame_walk_mutates_snapshot_freezer(monkeypatch, tmp_path):
    """The host snapshot boundary must not expose a source-mutable Python callable."""
    descriptor, _ = _materialized(monkeypatch, tmp_path, _freeze_snapshot_frame_walk_attack())
    result = execute_materialized_file_ref(descriptor.file_ref)
    assert result["success"] is True
    assert result["message"] == "validated"
    assert result["context"]["items"] == []


def test_real_mcp_route_preserves_snapshot_after_frame_walk_mutates_snapshot_freezer(monkeypatch, tmp_path):
    """The public route must preserve the result node budget after the same attack."""
    result = _execute_through_mcp(monkeypatch, tmp_path, _freeze_snapshot_frame_walk_attack())
    assert result["success"] is True
    assert result["message"] == "validated"
    assert result["context"]["items"] == []


def test_executor_restores_fresh_json_decoder_state_between_requests(monkeypatch, tmp_path):
    """Mutable decoder internals reached through the executor alias must not survive."""
    original_decoder = executor.json._default_decoder
    first = (
        "import dcc_mcp_substance3d_painter.materialized_script_executor as host\n"
        "def main():\n"
        "    host.json._default_decoder.scan_once = lambda value, index: ({}, len(value))\n"
        "    return {'success': True, 'message': 'first', 'context': {}}\n"
    )
    second = "def main():\n    return {'success': True, 'message': 'second', 'context': {}}\n"
    try:
        first_descriptor, _ = _materialized(monkeypatch, tmp_path / "first", first)
        assert execute_materialized_file_ref(first_descriptor.file_ref)["message"] == "first"

        second_descriptor, _ = _materialized(monkeypatch, tmp_path / "second", second)
        assert execute_materialized_file_ref(second_descriptor.file_ref)["message"] == "second"
        assert executor.json.loads('{"verified": true}') == {"verified": True}
        assert executor.json._default_decoder is not original_decoder
    finally:
        executor.json._default_decoder = original_decoder


def test_executor_preserves_execution_failed_for_unrelated_main_exception(monkeypatch, tmp_path):
    """A later suffix assignment must not relabel an unrelated main failure."""
    content = (
        "message = 'ready'\n"
        "def main():\n"
        "    assert message == 'ready'\n"
        "    raise RuntimeError('unrelated')\n"
        "message = 'updated after main'\n"
    )
    descriptor, _ = _materialized(monkeypatch, tmp_path, content)
    assert _rejection(descriptor.file_ref) == "script_execution_failed"


@pytest.mark.parametrize("value", [1.7976931348623157e308, -1.7976931348623157e308])
def test_real_mcp_route_accepts_finite_float_extremes(monkeypatch, tmp_path, value):
    content = f"def main():\n    return {{'success': True, 'message': 'finite', 'context': {{'value': {value!r}}}}}\n"
    result = _execute_through_mcp(monkeypatch, tmp_path, content)
    assert result["success"] is True
    assert result["context"]["value"] == value
