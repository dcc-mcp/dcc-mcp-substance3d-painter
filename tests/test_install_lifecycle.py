from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _synthetic_painter_file_metadata(monkeypatch):
    from dcc_mcp_substance3d_painter import _installer

    monkeypatch.setattr(_installer, "_windows_file_version", lambda _path: "12.0.1")
    monkeypatch.setattr(_installer, "_host_product_identity", lambda _path: True)
    monkeypatch.setattr(_installer, "_endpoint_is_owned_by_process", lambda _url, _pid: True)


def test_install_defaults_to_a_non_mutating_json_plan(tmp_path, monkeypatch, capsys):
    host = tmp_path / "Adobe Substance 3D Painter.exe"
    host.write_bytes(b"synthetic host")
    profile = tmp_path / "profile"
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE", str(profile))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_VERSION", "12.0.1")

    from dcc_mcp_substance3d_painter.install_cli import main

    exit_code = main(
        [
            "install",
            "--dcc-path",
            str(host),
            "--python",
            sys.executable,
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["schema_version"] == 1
    assert result["status"] == "planned"
    assert result["dcc_type"] == "substance3d_painter"
    assert result["adapter_version"]
    assert result["core_version"]
    assert result["receipt_path"]
    assert result["verify"] == {
        "directly_usable": False,
        "failure_stage": None,
        "failure_reason": None,
    }
    assert [step["id"] for step in result["steps"]] == [
        "preflight",
        "install",
        "receipt",
        "verify",
    ]
    assert result["next_steps"] == [
        {
            "id": "execute_install",
            "description": "Execute the validated Painter install plan.",
            "command": [
                "dcc-mcp-substance3d-painter",
                "install",
                "--dcc-path",
                str(host),
                "--python",
                str(Path(sys.executable).absolute()),
                "--json",
                "--yes",
            ],
            "why": "Planning does not modify the Painter profile.",
        }
    ]
    assert not profile.exists()


def test_install_writes_a_receipt_and_uninstall_consumes_only_that_receipt(tmp_path, monkeypatch, capsys):
    host = tmp_path / "Adobe Substance 3D Painter.exe"
    host.write_bytes(b"synthetic host")
    profile = tmp_path / "profile"
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE", str(profile))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_VERSION", "12.0.1")
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "0.01")

    from dcc_mcp_substance3d_painter.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    install_exit = main(["install", *common, "--yes"])
    installed = json.loads(capsys.readouterr().out)

    assert install_exit == 40
    assert installed["status"] == "partial"
    assert installed["verify"]["directly_usable"] is False
    assert installed["verify"]["failure_stage"] == "readiness"
    receipt_path = Path(installed["receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    owned_paths = {Path(item["path"]) for item in receipt["files"]}
    assert owned_paths == {
        profile / "python" / "startup" / "dcc_mcp_substance3d_painter_plugin.py",
        profile / "python" / "modules" / "dcc_mcp_substance3d_painter_bootstrap" / "__init__.py",
    }
    assert all(path.exists() for path in owned_paths)
    assert all(len(item["sha256"]) == 64 for item in receipt["files"])

    planned_exit = main(["uninstall", *common])
    planned = json.loads(capsys.readouterr().out)
    assert planned_exit == 0
    assert planned["status"] == "planned"
    assert receipt_path.exists()
    assert all(path.exists() for path in owned_paths)

    uninstall_exit = main(["uninstall", *common, "--yes"])
    removed = json.loads(capsys.readouterr().out)
    assert uninstall_exit == 0
    assert removed["status"] == "ok"
    assert not receipt_path.exists()
    assert all(not path.exists() for path in owned_paths)

    repeated_exit = main(["uninstall", *common, "--yes"])
    repeated = json.loads(capsys.readouterr().out)
    assert repeated_exit == 0
    assert repeated["status"] == "ok"


def test_verify_proves_direct_usability_with_host_ping(tmp_path, monkeypatch, capsys):
    host = tmp_path / "Adobe Substance 3D Painter.exe"
    host.write_bytes(b"synthetic host")
    profile = tmp_path / "profile"
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE", str(profile))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_VERSION", "12.0.1")
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))
    import dcc_mcp_core

    import dcc_mcp_substance3d_painter as adapter
    from dcc_mcp_substance3d_painter import _installer
    from dcc_mcp_substance3d_painter.install_cli import main

    pid = 4242
    start_identity = "synthetic-start-4242"
    entry = {
        "dcc_type": "substance3d_painter",
        "mcp_url": "http://127.0.0.1:18812/mcp",
        "instance_id": "painter-4242",
        "adapter_version": adapter.__version__,
        "metadata": {"dcc_pid": pid, "dcc_version": "12.0.1"},
    }
    monkeypatch.setattr(_installer, "query_runtime_state", lambda *_args, **_kwargs: {"entries": [entry]})
    monkeypatch.setattr(
        _installer,
        "_observe_process_identity",
        lambda _pid: {"pid": pid, "executable": str(host.resolve()), "start_identity": start_identity},
    )
    monkeypatch.setattr(
        _installer,
        "_probe_runtime_tool",
        lambda *_args, **_kwargs: {
            "success": True,
            "result": {
                "structuredContent": {
                    "success": True,
                    "context": {
                        "host_dispatch_ready": True,
                        "host": "substance3d_painter",
                        "host_pid": pid,
                        "host_executable": str(host.resolve()),
                        "process_start_identity": start_identity,
                        "adapter_version": adapter.__version__,
                        "core_version": dcc_mcp_core.__version__,
                        "adapter_module_path": str(Path(adapter.__file__).resolve()),
                        "core_module_path": str(Path(dcc_mcp_core.__file__).resolve()),
                        "bootstrap_module_path": str(
                            profile / "python" / "modules" / "dcc_mcp_substance3d_painter_bootstrap" / "__init__.py"
                        ),
                    },
                }
            },
        },
    )
    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    install_exit = main(["install", *common, "--yes"])
    installed = json.loads(capsys.readouterr().out)
    assert install_exit == 0, json.dumps(installed, indent=2)
    assert installed["verify"]["directly_usable"] is True

    verify_exit = main(["verify", *common])
    verified = json.loads(capsys.readouterr().out)
    assert verify_exit == 0
    assert verified["verify"]["directly_usable"] is True
    assert verified["verify"]["probe_tool"] == "painter_diagnostics__ping"


def test_packaged_startup_hook_captures_bootstrap_errors():
    startup = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "dcc_mcp_substance3d_painter"
        / "painter"
        / "startup"
        / "dcc_mcp_substance3d_painter_plugin.py"
    ).read_text(encoding="utf-8")

    assert "capture_bootstrap_errors" in startup
    assert 'phase="import"' in startup
    assert 'phase="startup"' in startup
    assert 'phase="shutdown"' in startup


def test_status_detects_a_receipted_install_that_needs_repair(tmp_path, monkeypatch, capsys):
    host = tmp_path / "Adobe Substance 3D Painter.exe"
    host.write_bytes(b"synthetic host")
    profile = tmp_path / "profile"
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE", str(profile))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_VERSION", "12.0.1")
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))

    from dcc_mcp_substance3d_painter.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    assert main(["install", *common, "--yes"]) == 40
    installed = json.loads(capsys.readouterr().out)
    receipt = json.loads(Path(installed["receipt_path"]).read_text(encoding="utf-8"))
    Path(receipt["files"][0]["path"]).unlink()

    status_exit = main(["status", *common])
    status = json.loads(capsys.readouterr().out)

    assert status_exit == 10
    assert status["status"] == "partial"
    assert status["install_state"] == "repair"

    assert main(["install", *common, "--yes"]) == 40
    capsys.readouterr()
    assert main(["status", *common]) == 0
    repaired = json.loads(capsys.readouterr().out)
    assert repaired["install_state"] == "current"


def test_distribution_exposes_the_standard_lifecycle_entry_point():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")

    assert "[project.scripts]" in pyproject
    assert 'dcc-mcp-substance3d-painter = "dcc_mcp_substance3d_painter.install_cli:main"' in pyproject
    assert "dcc-mcp-core>=0.20.15,<1.0.0" in pyproject


def test_install_runbook_covers_the_standard_lifecycle_and_all_platforms():
    runbook_path = Path(__file__).resolve().parents[1] / "install.md"
    runbook = runbook_path.read_text(encoding="utf-8")

    for heading in (
        "## Requirements",
        "## Supported versions",
        "## Agent quick path",
        "## Manual path",
        "## Verify",
        "## Upgrade",
        "## Uninstall",
        "## Troubleshooting",
    ):
        assert heading in runbook
    for platform_name in ("Windows", "macOS", "Linux"):
        assert platform_name in runbook
    for verb in ("install", "status", "verify", "upgrade", "uninstall"):
        assert f"dcc-mcp-substance3d-painter {verb}" in runbook


def test_preflight_detects_a_versioned_painter_executable_without_an_environment_override(
    tmp_path, monkeypatch, capsys
):
    host = tmp_path / "Adobe Substance 3D Painter 12.0.1" / "Adobe Substance 3D Painter.exe"
    host.parent.mkdir()
    host.write_bytes(b"synthetic host")
    monkeypatch.setattr(
        "dcc_mcp_substance3d_painter._installer._windows_file_version",
        lambda _path: None,
    )
    monkeypatch.delenv("DCC_MCP_SUBSTANCE3D_PAINTER_VERSION", raising=False)
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE", str(tmp_path / "profile"))

    from dcc_mcp_substance3d_painter.install_cli import main

    exit_code = main(["install", "--dcc-path", str(host), "--python", sys.executable, "--json", "--dry-run"])
    planned = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert planned["host"]["version"] == "12.0.1"


@pytest.mark.skipif(os.name != "nt", reason="Windows standard install discovery")
def test_preflight_discovers_a_single_standard_painter_install(tmp_path, monkeypatch, capsys):
    host = tmp_path / "Adobe" / "Adobe Substance 3D Painter" / "Adobe Substance 3D Painter.exe"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"synthetic host")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_VERSION", "12.0.1")
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE", str(tmp_path / "profile"))

    from dcc_mcp_substance3d_painter.install_cli import main

    exit_code = main(["install", "--python", sys.executable, "--json", "--dry-run"])
    planned = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert planned["host"]["path"] == str(host.resolve())


def test_ci_runs_the_install_lifecycle_smoke_explicitly():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "Install lifecycle smoke" in workflow
    assert "python -m pytest tests/test_install_lifecycle.py" in workflow


def test_failed_upgrade_restores_the_previous_payload_and_receipt(tmp_path, monkeypatch, capsys):
    host = tmp_path / "Adobe Substance 3D Painter.exe"
    host.write_bytes(b"synthetic host")
    profile = tmp_path / "profile"
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE", str(profile))
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_VERSION", "12.0.1")
    monkeypatch.setenv("DCC_MCP_REGISTRY_DIR", str(tmp_path / "registry"))

    from dcc_mcp_substance3d_painter import _installer
    from dcc_mcp_substance3d_painter.install_cli import main

    common = ["--dcc-path", str(host), "--python", sys.executable, "--json"]
    assert main(["install", *common, "--yes"]) == 40
    installed = json.loads(capsys.readouterr().out)
    receipt_path = Path(installed["receipt_path"])
    receipt_before = receipt_path.read_bytes()
    owned_before = {Path(item["path"]): Path(item["path"]).read_bytes() for item in json.loads(receipt_before)["files"]}

    def fail_receipt(_path, _payload):
        raise OSError("injected receipt commit failure")

    monkeypatch.setattr(_installer, "_write_json_atomic", fail_receipt)
    upgrade_exit = main(["upgrade", *common, "--yes"])
    failed = json.loads(capsys.readouterr().out)

    assert upgrade_exit == 30
    assert failed["verify"]["failure_stage"] == "install"
    assert receipt_path.read_bytes() == receipt_before
    assert {path: path.read_bytes() for path in owned_before} == owned_before
    assert not any((profile / ".dcc-mcp" / "staging").iterdir())


def test_preflight_rejects_a_wheel_interpreter_that_does_not_match_painters_embedded_python(
    tmp_path, monkeypatch, capsys
):
    install_root = tmp_path / "Painter"
    host = install_root / "Adobe Substance 3D Painter.exe"
    sdk = install_root / "resources" / "pythonsdk" / "lib"
    sdk.mkdir(parents=True)
    host.write_bytes(b"synthetic host")
    (sdk / "python310.zip").write_bytes(b"synthetic sdk marker")
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_VERSION", "12.0.1")
    monkeypatch.setenv("DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE", str(tmp_path / "profile"))

    from dcc_mcp_substance3d_painter.install_cli import main

    exit_code = main(["install", "--dcc-path", str(host), "--python", sys.executable, "--json"])
    failed = json.loads(capsys.readouterr().out)

    assert exit_code == 10
    assert failed["verify"]["failure_stage"] == "python_compatibility"
    assert not (tmp_path / "profile").exists()


def test_readiness_probe_rejects_a_non_loopback_registry_url(monkeypatch):
    from dcc_mcp_substance3d_painter import _installer

    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("external URL must be rejected before transport")

    monkeypatch.setattr(_installer, "probe_sidecar_tool", must_not_probe)
    for url in (
        "https://example.com/mcp",
        "http://user:secret@127.0.0.1:18812/mcp",
        "http://127.0.0.1:18812/mcp?token=secret",
        "http://localhost:18812/mcp",
    ):
        result = _installer._probe_runtime_tool(url, 0.1)
        assert result["success"] is False
        assert result["status"] == "probe_unsafe_url"
