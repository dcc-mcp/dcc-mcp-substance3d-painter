"""Adversarial regressions for the Painter Install SOP lifecycle."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from dcc_mcp_substance3d_painter import _installer
from dcc_mcp_substance3d_painter.install_cli import main


def _context(tmp_path: Path, *, state: str = "current") -> _installer.InstallContext:
    host_root = tmp_path / "Adobe" / "Adobe Substance 3D Painter 12.0.1"
    host = host_root / ("Adobe Substance 3D Painter.exe" if os.name == "nt" else "Adobe Substance 3D Painter")
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_bytes(b"synthetic-painter-host")
    profile = tmp_path / "profile"
    loader = profile / "python" / "startup" / "dcc_mcp_substance3d_painter_plugin.py"
    bootstrap_root = profile / "python" / "modules" / "dcc_mcp_substance3d_painter_bootstrap"
    return _installer.InstallContext(
        host_path=host.resolve(),
        host_version="12.0.1",
        host_version_source="path",
        embedded_python_version=None,
        profile=profile.resolve(),
        python_path=Path(sys.executable).resolve(),
        python_version="{}.{}.{}".format(*sys.version_info[:3]),
        python_root=(tmp_path / "site-packages").resolve(),
        core_version=_installer.MIN_CORE_VERSION,
        state=state,
        receipt_path=profile / ".dcc-mcp" / "receipts" / "substance3d_painter.json",
        loader_path=loader,
        bootstrap_root=bootstrap_root,
        bootstrap_path=bootstrap_root / "__init__.py",
        bootstrap_log_dir=profile / ".dcc-mcp" / "logs",
    )


def _write_current_install(ctx: _installer.InstallContext) -> dict:
    ctx.loader_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.loader_path.write_text("old loader\n", encoding="utf-8")
    ctx.bootstrap_path.write_text("old bootstrap\n", encoding="utf-8")
    receipt = _installer._receipt(ctx, time.time())
    ctx.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    return receipt


def _runtime_probe_context(ctx: _installer.InstallContext, *, start_identity: str = "start-123") -> dict:
    return {
        "success": True,
        "context": {
            "host_dispatch_ready": True,
            "host": "substance3d_painter",
            "host_pid": 123,
            "process_start_identity": start_identity,
            "host_executable": str(ctx.host_path),
            "adapter_version": _installer.__version__,
            "core_version": _installer.MIN_CORE_VERSION,
        },
    }


def _patch_verify_prerequisites(
    monkeypatch: pytest.MonkeyPatch, ctx: _installer.InstallContext, probe_context: dict
) -> None:
    monkeypatch.setattr(_installer, "_query_python", lambda _path: {})
    monkeypatch.setattr(
        _installer,
        "query_runtime_state",
        lambda *_args, **_kwargs: {
            "entries": [
                {
                    "dcc_type": "substance3d_painter",
                    "mcp_url": "http://127.0.0.1:18812/mcp",
                    "instance_id": "painter-123",
                    "adapter_version": _installer.__version__,
                    "metadata": {"dcc_pid": 123, "dcc_version": "12.0.1"},
                }
            ]
        },
    )
    monkeypatch.setattr(
        _installer,
        "_probe_runtime_tool",
        lambda *_args, **_kwargs: {
            "success": True,
            "status": "probe_ok",
            "result": {"structuredContent": probe_context},
        },
    )


def test_uses_released_core_02015_contract_and_official_schema() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8").replace(" ", "")
    runbook = (root / "install.md").read_text(encoding="utf-8")

    assert _installer.MIN_CORE_VERSION == "0.20.15"
    assert "dcc-mcp-core>=0.20.15,<1.0.0" in pyproject
    assert "dcc-mcp-core>=0.20.15" in runbook
    assert not (root / "src" / "dcc_mcp_substance3d_painter" / "_install_contract.py").exists()

    from dcc_mcp_core.deployment import load_install_sop_schema

    Draft202012Validator.check_schema(load_install_sop_schema())


@pytest.mark.parametrize(
    "value",
    ["garbage12.0suffix", " 12.0.1 ", "12.0", "12.0.1.2", "012.0.1", "9" * 5000 + ".0.1"],
)
def test_versions_reject_noncanonical_or_unbounded_values(value: str) -> None:
    assert _installer._version_tuple(value, components=3) is None


def test_explicit_host_rejects_an_arbitrary_executable(tmp_path: Path) -> None:
    impostor = tmp_path / "not-painter.exe"
    impostor.write_bytes(b"not Painter")

    with pytest.raises(_installer.LifecycleFailure, match="Painter executable"):
        _installer._resolve_host_path(str(impostor), {})


def test_environment_version_cannot_attest_a_synthetic_exact_named_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = tmp_path / "Adobe Substance 3D Painter.exe"
    host.write_bytes(b"synthetic")
    monkeypatch.setattr(_installer, "_windows_file_version", lambda _path: None)

    version, source = _installer._detect_host_version(host, {"DCC_MCP_SUBSTANCE3D_PAINTER_VERSION": "12.0.1"})

    assert version == ""
    assert source == "unavailable"


def test_python_probe_rejects_shadow_modules_not_owned_by_distributions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shadow_adapter = tmp_path / "shadow" / "dcc_mcp_substance3d_painter" / "__init__.py"
    real_core = tmp_path / "site-packages" / "dcc_mcp_core" / "__init__.py"
    for path in (shadow_adapter, real_core):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic\n", encoding="utf-8")
    payload = {
        "python_version": "{}.{}.{}".format(*sys.version_info[:3]),
        "executable": str(Path(sys.executable).resolve()),
        "adapter_version": _installer.__version__,
        "adapter_dist_version": _installer.__version__,
        "core_version": _installer.MIN_CORE_VERSION,
        "core_dist_version": _installer.MIN_CORE_VERSION,
        "adapter_file": str(shadow_adapter),
        "core_file": str(real_core),
        "adapter_dist_root": str((tmp_path / "site-packages").resolve()),
        "core_dist_root": str((tmp_path / "site-packages").resolve()),
        "adapter_record": None,
        "core_record": "dcc_mcp_core/__init__.py",
        "adapter_direct_url": None,
        "core_direct_url": None,
    }
    completed = SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: completed)
    monkeypatch.setattr(
        _installer,
        "_run_bounded_command",
        lambda *_args, **_kwargs: {"success": True, "stdout": json.dumps(payload), "stderr": ""},
        raising=False,
    )

    with pytest.raises(_installer.LifecycleFailure, match="ownership|RECORD|shadow"):
        _installer._query_python(Path(sys.executable).resolve())


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def test_interpreter_probe_timeout_terminates_root_and_descendant(tmp_path: Path) -> None:
    identities = tmp_path / "probe-pids.txt"
    script = (
        "import os,pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(identities)!r}).write_text(str(os.getpid())+' '+str(child.pid)); "
        "time.sleep(60)"
    )

    # The target interpreter itself may live on a slow mounted filesystem.
    # Give its deterministic ready marker a bounded startup window, then let
    # the deliberately hung root prove full-tree timeout cleanup.
    outcome = _installer._run_bounded_command([sys.executable, "-c", script], timeout=10.0)

    assert outcome["success"] is False
    assert outcome["reason"] == "probe timed out"
    assert identities.is_file(), outcome
    root_pid, descendant_pid = (int(value) for value in identities.read_text(encoding="utf-8").split())
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and (_pid_alive(root_pid) or _pid_alive(descendant_pid)):
        time.sleep(0.05)
    assert not _pid_alive(root_pid)
    assert not _pid_alive(descendant_pid)


def test_receipt_is_bound_to_exact_context_and_owned_paths(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    receipt = _write_current_install(ctx)
    receipt["host"]["path"] = str(tmp_path / "different-painter.exe")
    receipt["files"][0]["path"] = str(tmp_path / "operator-owned.txt")

    with pytest.raises(_installer.LifecycleFailure, match="receipt|ownership|host"):
        _installer._validate_receipt(ctx, receipt)


def test_uninstall_refuses_unreceipted_content_inside_managed_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    _write_current_install(ctx)
    operator_file = ctx.bootstrap_root / "operator-owned.txt"
    operator_file.write_text("keep\n", encoding="utf-8")
    monkeypatch.setattr(_installer, "_resolve_context", lambda *_args, **_kwargs: ctx)

    outcome = _installer.run_lifecycle(
        "uninstall", dcc_path=str(ctx.host_path), python_path=str(ctx.python_path), yes=True, dry_run=False, environ={}
    )

    assert outcome.exit_code == 30
    assert outcome.result["verify"]["failure_stage"] == "receipt"
    assert operator_file.read_text(encoding="utf-8") == "keep\n"
    assert ctx.receipt_path.is_file()


def test_upgrade_readiness_failure_restores_previous_payload_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    _write_current_install(ctx)
    before = {
        ctx.loader_path: ctx.loader_path.read_bytes(),
        ctx.bootstrap_path: ctx.bootstrap_path.read_bytes(),
        ctx.receipt_path: ctx.receipt_path.read_bytes(),
    }
    monkeypatch.setattr(_installer, "_resolve_context", lambda *_args, **_kwargs: ctx)
    monkeypatch.setattr(
        _installer,
        "_verify",
        lambda *_args, **_kwargs: (
            {"directly_usable": False, "failure_stage": "readiness", "failure_reason": "not ready"},
            [],
        ),
    )

    outcome = _installer.run_lifecycle(
        "upgrade", dcc_path=str(ctx.host_path), python_path=str(ctx.python_path), yes=True, dry_run=False, environ={}
    )

    assert outcome.exit_code == 40
    assert outcome.result["previous_restored"] is True
    assert {path: path.read_bytes() for path in before} == before


def test_staging_failure_cleans_transaction_without_moving_previous_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    _write_current_install(ctx)
    before = {ctx.loader_path: ctx.loader_path.read_bytes(), ctx.bootstrap_path: ctx.bootstrap_path.read_bytes()}
    monkeypatch.setattr(_installer, "_resolve_context", lambda *_args, **_kwargs: ctx)
    monkeypatch.setattr(
        _installer,
        "_bootstrap_source",
        lambda _ctx: (_ for _ in ()).throw(OSError("injected staging failure")),
    )

    outcome = _installer.run_lifecycle(
        "upgrade", dcc_path=str(ctx.host_path), python_path=str(ctx.python_path), yes=True, dry_run=False, environ={}
    )

    assert outcome.exit_code == 30
    assert {path: path.read_bytes() for path in before} == before
    staging = ctx.profile / ".dcc-mcp" / "staging"
    assert not staging.exists() or not any(staging.iterdir())


def test_fresh_install_commit_failure_removes_every_unreceipted_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = replace(_context(tmp_path), state="fresh")
    monkeypatch.setattr(_installer, "_resolve_context", lambda *_args, **_kwargs: ctx)
    original_copy = shutil.copy2

    def fail_loader_copy(source, destination, *args, **kwargs):
        if Path(destination).name.startswith(f".{ctx.loader_path.name}."):
            raise PermissionError("injected loader commit failure")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", fail_loader_copy)
    outcome = _installer.run_lifecycle(
        "install", dcc_path=str(ctx.host_path), python_path=str(ctx.python_path), yes=True, dry_run=False, environ={}
    )

    assert outcome.exit_code == 30
    assert not ctx.bootstrap_root.exists()
    assert not ctx.loader_path.exists()
    assert not ctx.receipt_path.exists()
    staging = ctx.profile / ".dcc-mcp" / "staging"
    assert not staging.exists() or not any(staging.iterdir())


def test_failed_backup_move_never_deletes_an_unmoved_previous_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    _write_current_install(ctx)
    before = {
        ctx.bootstrap_path: ctx.bootstrap_path.read_bytes(),
        ctx.loader_path: ctx.loader_path.read_bytes(),
        ctx.receipt_path: ctx.receipt_path.read_bytes(),
    }
    monkeypatch.setattr(_installer, "_resolve_context", lambda *_args, **_kwargs: ctx)
    original_replace = os.replace

    def fail_loader_backup(source, destination, *args, **kwargs):
        if Path(source) == ctx.loader_path and Path(destination).parent.name == "backup":
            raise PermissionError("injected loader backup failure")
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", fail_loader_backup)
    outcome = _installer.run_lifecycle(
        "upgrade", dcc_path=str(ctx.host_path), python_path=str(ctx.python_path), yes=True, dry_run=False, environ={}
    )

    assert outcome.exit_code == 30
    assert {path: path.read_bytes() for path in before} == before


def test_partial_uninstall_failure_restores_all_owned_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    _write_current_install(ctx)
    before = {
        ctx.loader_path: ctx.loader_path.read_bytes(),
        ctx.bootstrap_path: ctx.bootstrap_path.read_bytes(),
        ctx.receipt_path: ctx.receipt_path.read_bytes(),
    }
    monkeypatch.setattr(_installer, "_resolve_context", lambda *_args, **_kwargs: ctx)
    original_unlink = Path.unlink

    def deny_loader(path: Path, *args, **kwargs):
        if path == ctx.loader_path:
            raise PermissionError("injected loader lock")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_loader)
    outcome = _installer.run_lifecycle(
        "uninstall", dcc_path=str(ctx.host_path), python_path=str(ctx.python_path), yes=True, dry_run=False, environ={}
    )

    assert outcome.exit_code == 30
    assert {path: path.read_bytes() for path in before} == before


def test_lifecycle_lock_rejects_a_concurrent_profile_mutation(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    with _installer._profile_lock(profile):
        with pytest.raises(_installer.LifecycleFailure, match="already in progress"):
            with _installer._profile_lock(profile):
                pass


def test_verify_rejects_foreign_runtime_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    _write_current_install(ctx)
    probe = _runtime_probe_context(ctx)
    _patch_verify_prerequisites(monkeypatch, ctx, probe)
    monkeypatch.setattr(
        _installer,
        "_observe_process_identity",
        lambda _pid: {"pid": 123, "executable": str(tmp_path / "foreign.exe"), "start_identity": "start-123"},
        raising=False,
    )

    verify, _next_steps = _installer._verify(ctx, {})

    assert verify["directly_usable"] is False
    assert verify["failure_stage"] == "readiness_identity"


def test_verify_rejects_pid_reuse_between_probe_and_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _context(tmp_path)
    _write_current_install(ctx)
    probe = _runtime_probe_context(ctx)
    _patch_verify_prerequisites(monkeypatch, ctx, probe)
    identities = iter(
        [
            {"pid": 123, "executable": str(ctx.host_path), "start_identity": "start-123"},
            {"pid": 123, "executable": str(ctx.host_path), "start_identity": "reused-123"},
        ]
    )
    monkeypatch.setattr(_installer, "_observe_process_identity", lambda _pid: next(identities), raising=False)

    verify, _next_steps = _installer._verify(ctx, {})

    assert verify["directly_usable"] is False
    assert verify["failure_stage"] == "readiness_identity"


def test_verify_rejects_probe_plugin_origin_not_owned_by_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _context(tmp_path)
    ctx = replace(
        ctx,
        adapter_module_path=(tmp_path / "site-packages" / "dcc_mcp_substance3d_painter" / "__init__.py"),
        core_module_path=(tmp_path / "site-packages" / "dcc_mcp_core" / "__init__.py"),
    )
    for path in (ctx.adapter_module_path, ctx.core_module_path):
        assert path is not None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# synthetic\n", encoding="utf-8")
    _write_current_install(ctx)
    probe = _runtime_probe_context(ctx)
    probe["context"].update(
        {
            "adapter_module_path": str(tmp_path / "shadow" / "dcc_mcp_substance3d_painter" / "__init__.py"),
            "core_module_path": str(ctx.core_module_path),
            "bootstrap_module_path": str(ctx.bootstrap_path),
        }
    )
    _patch_verify_prerequisites(monkeypatch, ctx, probe)
    monkeypatch.setattr(
        _installer,
        "_observe_process_identity",
        lambda _pid: {"pid": 123, "executable": str(ctx.host_path), "start_identity": "start-123"},
    )

    verify, _next_steps = _installer._verify(ctx, {})

    assert verify["directly_usable"] is False
    assert verify["failure_stage"] == "readiness_identity"


def test_invalid_json_cli_arguments_return_schema_valid_json_without_raw_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["install", "--json", "--unknown-option", "secret-token"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert exit_code == 10
    assert captured.err == ""
    assert result["verify"]["failure_stage"] == "arguments"
    assert "secret-token" not in json.dumps(result)

    from dcc_mcp_core.deployment import load_install_sop_schema

    Draft202012Validator(load_install_sop_schema()).validate(result)


def test_unexpected_failures_are_classified_without_secret_or_path_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "token-123"
    monkeypatch.setattr(
        _installer,
        "_resolve_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(f"{secret} at C:/private/operator.txt")),
    )

    outcome = _installer.run_lifecycle("install", dcc_path=None, python_path=None, yes=False, dry_run=True, environ={})
    serialized = json.dumps(outcome.result)

    assert outcome.exit_code == 30
    assert secret not in serialized
    assert "private/operator" not in serialized
    assert outcome.result["verify"]["failure_stage"] == "install"
