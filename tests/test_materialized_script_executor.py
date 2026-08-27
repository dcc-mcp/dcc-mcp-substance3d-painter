from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
import yaml
from dcc_mcp_core import materialize_script
from dcc_mcp_core.script_materialization import SCRIPT_MATERIALIZATION_ROOT_ENV
from jsonschema import Draft202012Validator, ValidationError
from test_mesh_map_baking import McpClient, _job, _structured

import dcc_mcp_substance3d_painter.materialized_script_executor as executor
from dcc_mcp_substance3d_painter.dispatcher import PainterQtDispatcher
from dcc_mcp_substance3d_painter.materialized_script_executor import (
    MAX_MATERIALIZED_SCRIPT_BYTES,
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
