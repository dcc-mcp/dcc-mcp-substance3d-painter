"""Painter-owned Install SOP lifecycle built on public Core primitives."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import dcc_mcp_core
from dcc_mcp_core import (
    inspect_install_root,
    probe_sidecar_tool,
    query_runtime_state,
    safe_remove_tree,
    safe_replace_tree,
)

from dcc_mcp_substance3d_painter.__version__ import __version__
from dcc_mcp_substance3d_painter._install_contract import (
    INSTALL_EXIT_INSTALL,
    INSTALL_EXIT_OK,
    INSTALL_EXIT_PREFLIGHT,
    INSTALL_EXIT_REQUIRES_RESTART,
    INSTALL_EXIT_VERIFY,
    INSTALL_SOP_SCHEMA_VERSION,
)

DCC_TYPE = "substance3d_painter"
COMMAND = "dcc-mcp-substance3d-painter"
MIN_CORE_VERSION = "0.20.8"
MIN_PAINTER_VERSION = (7, 2)
_PROFILE_ENV = "DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE"
_VERSION_ENV = "DCC_MCP_SUBSTANCE3D_PAINTER_VERSION"
_PYTHON_ENV = "DCC_MCP_INSTALL_PYTHON"
_LOADER_NAME = "dcc_mcp_substance3d_painter_plugin.py"
_BOOTSTRAP_PACKAGE = "dcc_mcp_substance3d_painter_bootstrap"
_READINESS_TOOL = "painter_diagnostics__ping"


@dataclass(frozen=True)
class InstallContext:
    host_path: Path
    host_version: str
    host_version_source: str
    embedded_python_version: Optional[str]
    profile: Path
    python_path: Path
    python_version: str
    python_root: Path
    core_version: str
    state: str
    receipt_path: Path
    loader_path: Path
    bootstrap_root: Path
    bootstrap_path: Path
    bootstrap_log_dir: Path


@dataclass(frozen=True)
class LifecycleOutcome:
    result: Dict[str, Any]
    exit_code: int


class LifecycleFailure(RuntimeError):
    def __init__(self, stage: str, message: str, exit_code: int = INSTALL_EXIT_PREFLIGHT) -> None:
        super().__init__(message)
        self.stage = stage
        self.exit_code = exit_code


def _version_tuple(value: str) -> Tuple[int, ...]:
    match = re.search(r"\d+(?:\.\d+)+", value)
    if match is None:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleFailure("receipt", f"Receipt is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise LifecycleFailure("receipt", "Receipt root must be a JSON object.")
    return payload


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def _query_python(python_path: Path) -> Dict[str, str]:
    script = (
        "import json,sys,sysconfig; "
        "import dcc_mcp_core,dcc_mcp_substance3d_painter as adapter; "
        "print(json.dumps({'python_version':'.'.join(map(str,sys.version_info[:3])),"
        "'python_root':sysconfig.get_path('purelib'),'core_version':dcc_mcp_core.__version__,"
        "'adapter_version':adapter.__version__}))"
    )
    try:
        completed = subprocess.run(
            [str(python_path), "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleFailure("python", f"Target interpreter could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit {completed.returncode}"
        raise LifecycleFailure("python", f"Target interpreter import check failed: {message}")
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise LifecycleFailure("python", "Target interpreter returned invalid metadata.") from exc
    if result.get("adapter_version") != __version__:
        raise LifecycleFailure(
            "python",
            f"Target interpreter has adapter {result.get('adapter_version')!r}; expected {__version__!r}.",
        )
    if _version_tuple(str(result.get("core_version", ""))) < _version_tuple(MIN_CORE_VERSION):
        raise LifecycleFailure(
            "core_version",
            f"dcc-mcp-core>={MIN_CORE_VERSION} is required in the target interpreter.",
        )
    return {str(key): str(value) for key, value in result.items()}


def _default_profile() -> Path:
    return Path.home() / "Documents" / "Adobe" / "Adobe Substance 3D Painter"


def _host_candidates(environ: Mapping[str, str]) -> Sequence[Path]:
    candidates: list[Path] = []
    if os.name == "nt":
        for key in ("ProgramFiles", "ProgramW6432"):
            root = environ.get(key, "").strip()
            if not root:
                continue
            adobe = Path(root) / "Adobe"
            candidates.extend(adobe.glob("Adobe Substance 3D Painter*/Adobe Substance 3D Painter.exe"))
    elif sys.platform == "darwin":
        candidates.append(
            Path("/Applications/Adobe Substance 3D Painter.app/Contents/MacOS/Adobe Substance 3D Painter")
        )
    else:
        candidates.extend(
            [
                Path("/opt/Adobe/Adobe Substance 3D Painter/Adobe Substance 3D Painter"),
                Path("/usr/bin/substance3d-painter"),
            ]
        )
        discovered = shutil.which("substance3d-painter")
        if discovered:
            candidates.append(Path(discovered))
    unique = {candidate.expanduser().resolve() for candidate in candidates if candidate.is_file()}
    return tuple(sorted(unique, key=str))


def _resolve_host_path(dcc_path: Optional[str], environ: Mapping[str, str]) -> Path:
    if dcc_path:
        candidate = Path(dcc_path).expanduser().resolve()
        if candidate.is_dir() and candidate.suffix.lower() == ".app":
            candidate = candidate / "Contents" / "MacOS" / "Adobe Substance 3D Painter"
        if candidate.is_file():
            return candidate
        raise LifecycleFailure("host", f"Painter executable does not exist: {candidate}")
    candidates = _host_candidates(environ)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise LifecycleFailure("host", "Painter was not found in a standard install location; pass --dcc-path.")
    raise LifecycleFailure("host", "Multiple Painter installations were found; select one with --dcc-path.")


def _windows_file_version(path: Path) -> Optional[str]:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, data):
            return None
        pointer = ctypes.c_void_p()
        length = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None

        class FixedFileInfo(ctypes.Structure):
            _fields_ = [
                ("signature", wintypes.DWORD),
                ("structure_version", wintypes.DWORD),
                ("file_version_ms", wintypes.DWORD),
                ("file_version_ls", wintypes.DWORD),
                ("product_version_ms", wintypes.DWORD),
                ("product_version_ls", wintypes.DWORD),
                ("file_flags_mask", wintypes.DWORD),
                ("file_flags", wintypes.DWORD),
                ("file_os", wintypes.DWORD),
                ("file_type", wintypes.DWORD),
                ("file_subtype", wintypes.DWORD),
                ("file_date_ms", wintypes.DWORD),
                ("file_date_ls", wintypes.DWORD),
            ]

        info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
        parts = (
            info.product_version_ms >> 16,
            info.product_version_ms & 0xFFFF,
            info.product_version_ls >> 16,
            info.product_version_ls & 0xFFFF,
        )
        return ".".join(str(part) for part in parts)
    except (AttributeError, OSError, ValueError):
        return None


def _detect_host_version(path: Path, environ: Mapping[str, str]) -> Tuple[str, str]:
    override = environ.get(_VERSION_ENV, "").strip()
    if override:
        return override, _VERSION_ENV
    file_version = _windows_file_version(path)
    if file_version:
        return file_version, "file_metadata"
    if path.parent.name == "MacOS" and path.parent.parent.name == "Contents":
        plist_path = path.parent.parent / "Info.plist"
        try:
            plist = plistlib.loads(plist_path.read_bytes())
            version = str(plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion") or "").strip()
        except (OSError, ValueError):
            version = ""
        if version:
            return version, "app_bundle"
    path_version = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)?)(?!\d)", str(path))
    if path_version:
        return path_version.group(1), "path"
    return "", "unavailable"


def _detect_embedded_python_version(host: Path) -> Optional[str]:
    roots = {host.parent, *tuple(host.parents)[:3]}
    markers: set[str] = set()
    for root in roots:
        for resources_name in ("resources", "Resources"):
            sdk_lib = root / resources_name / "pythonsdk" / "lib"
            if not sdk_lib.is_dir():
                continue
            for marker in sdk_lib.glob("python*.zip"):
                match = re.fullmatch(r"python(\d)\.?([0-9]{1,2})", marker.stem, re.IGNORECASE)
                if match:
                    markers.add(f"{int(match.group(1))}.{int(match.group(2))}")
    if len(markers) > 1:
        raise LifecycleFailure("python_compatibility", "Painter SDK contains ambiguous embedded Python versions.")
    return next(iter(markers), None)


def _resolve_context(
    dcc_path: Optional[str],
    python_path: Optional[str],
    environ: Mapping[str, str],
) -> InstallContext:
    host = _resolve_host_path(dcc_path, environ)

    host_version, host_version_source = _detect_host_version(host, environ)
    if not host_version or _version_tuple(host_version) < MIN_PAINTER_VERSION:
        raise LifecycleFailure(
            "host_version",
            "Painter 7.2 or newer is required and its installed version could not be verified.",
        )

    selected_python = python_path or environ.get(_PYTHON_ENV) or sys.executable
    interpreter = Path(selected_python).expanduser().resolve()
    if not interpreter.is_file():
        raise LifecycleFailure("python", f"Target interpreter does not exist: {interpreter}")
    python = _query_python(interpreter)
    embedded_python_version = _detect_embedded_python_version(host)
    if embedded_python_version:
        selected = _version_tuple(python["python_version"])
        embedded = _version_tuple(embedded_python_version)
        if selected[:2] != embedded[:2]:
            raise LifecycleFailure(
                "python_compatibility",
                "The wheel interpreter must match Painter's embedded Python "
                f"{embedded_python_version}; selected {python['python_version']}.",
            )

    profile = Path(environ.get(_PROFILE_ENV) or _default_profile()).expanduser().resolve()
    receipt_path = profile / ".dcc-mcp" / "receipts" / f"{DCC_TYPE}.json"
    loader_path = profile / "python" / "startup" / _LOADER_NAME
    bootstrap_root = profile / "python" / "modules" / _BOOTSTRAP_PACKAGE
    bootstrap_path = bootstrap_root / "__init__.py"
    receipt_exists = receipt_path.is_file()
    artifacts_exist = loader_path.exists() or bootstrap_root.exists()
    if artifacts_exist and not receipt_exists:
        state = "partial"
    elif receipt_exists:
        receipt = _load_json(receipt_path)
        recorded_files = receipt.get("files", [])
        intact = bool(recorded_files)
        for item in recorded_files:
            path = Path(str(item.get("path", "")))
            if not path.is_file() or _hash_file(path) != item.get("sha256"):
                intact = False
                break
        if receipt.get("adapter_version") != __version__:
            state = "upgrade"
        else:
            state = "current" if intact else "repair"
    else:
        state = "fresh"
    return InstallContext(
        host_path=host,
        host_version=host_version,
        host_version_source=host_version_source,
        embedded_python_version=embedded_python_version,
        profile=profile,
        python_path=interpreter,
        python_version=python["python_version"],
        python_root=Path(python["python_root"]).resolve(),
        core_version=python["core_version"],
        state=state,
        receipt_path=receipt_path,
        loader_path=loader_path,
        bootstrap_root=bootstrap_root,
        bootstrap_path=bootstrap_path,
        bootstrap_log_dir=profile / ".dcc-mcp" / "logs",
    )


def _base_result(ctx: InstallContext, *, status: str, verify: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "schema_version": INSTALL_SOP_SCHEMA_VERSION,
        "status": status,
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": ctx.core_version,
        "steps": [],
        "next_steps": [],
        "receipt_path": str(ctx.receipt_path),
        "verify": verify
        or {
            "directly_usable": False,
            "failure_stage": None,
            "failure_reason": None,
        },
        "host": {
            "path": str(ctx.host_path),
            "version": ctx.host_version,
            "version_source": ctx.host_version_source,
            "embedded_python_version": ctx.embedded_python_version,
            "profile": str(ctx.profile),
        },
        "python": {
            "path": str(ctx.python_path),
            "version": ctx.python_version,
            "site_packages": str(ctx.python_root),
            "selection_source": "--python",
        },
        "install_state": ctx.state,
    }


def _command(ctx: InstallContext, verb: str, *, execute: bool = False) -> Sequence[str]:
    command = [
        COMMAND,
        verb,
        "--dcc-path",
        str(ctx.host_path),
        "--python",
        str(ctx.python_path),
        "--json",
    ]
    if execute:
        command.append("--yes")
    return command


def _plan(ctx: InstallContext, verb: str) -> LifecycleOutcome:
    result = _base_result(ctx, status="planned")
    if verb in {"install", "upgrade"}:
        result["steps"] = [
            {"id": "preflight", "status": "ok"},
            {"id": "install", "status": "planned"},
            {"id": "receipt", "status": "planned"},
            {"id": "verify", "status": "planned"},
        ]
        result["next_steps"] = [
            {
                "id": f"execute_{verb}",
                "description": f"Execute the validated Painter {verb} plan.",
                "command": list(_command(ctx, verb, execute=True)),
                "why": "Planning does not modify the Painter profile.",
            }
        ]
    else:
        result["steps"] = [
            {"id": "receipt", "status": "ok" if ctx.receipt_path.exists() else "absent"},
            {"id": "uninstall", "status": "planned"},
        ]
        result["next_steps"] = [
            {
                "id": "execute_uninstall",
                "description": "Remove only the receipted Painter adapter files.",
                "command": list(_command(ctx, "uninstall", execute=True)),
                "why": "Planning does not remove files.",
            }
        ]
    return LifecycleOutcome(result, INSTALL_EXIT_OK)


def _bootstrap_source(ctx: InstallContext) -> str:
    runtime_root = json.dumps(str(ctx.python_root))
    log_dir = json.dumps(str(ctx.bootstrap_log_dir))
    return f'''"""Generated DCC-MCP Painter bootstrap. Owned by its install receipt."""
from __future__ import annotations

import site

site.addsitedir({runtime_root})

from dcc_mcp_core import capture_bootstrap_errors

_CAPTURE = {{
    "dcc_name": "substance3d_painter",
    "adapter_version": {json.dumps(__version__)},
    "min_core_version": {json.dumps(MIN_CORE_VERSION)},
    "log_dir": {log_dir},
}}

with capture_bootstrap_errors(phase="import", **_CAPTURE):
    from dcc_mcp_substance3d_painter.plugin import close_plugin as _close_plugin
    from dcc_mcp_substance3d_painter.plugin import start_plugin as _start_plugin


def start_plugin():
    with capture_bootstrap_errors(phase="startup", **_CAPTURE):
        return _start_plugin()


def close_plugin():
    with capture_bootstrap_errors(phase="shutdown", **_CAPTURE):
        return _close_plugin()
'''


def _loader_source() -> str:
    return (
        '"""DCC-MCP Painter startup loader. Owned by its install receipt."""\n\n'
        f"from {_BOOTSTRAP_PACKAGE} import close_plugin, start_plugin\n\n"
        '__all__ = ["close_plugin", "start_plugin"]\n'
    )


def _receipt(ctx: InstallContext, installed_at: float) -> Dict[str, Any]:
    files = [ctx.loader_path, ctx.bootstrap_path]
    prior_version = None
    if ctx.receipt_path.exists():
        prior_version = _load_json(ctx.receipt_path).get("adapter_version")
    return {
        "schema_version": 1,
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": ctx.core_version,
        "host": {
            "path": str(ctx.host_path),
            "version": ctx.host_version,
            "version_source": ctx.host_version_source,
            "embedded_python_version": ctx.embedded_python_version,
            "profile": str(ctx.profile),
        },
        "python": {
            "path": str(ctx.python_path),
            "version": ctx.python_version,
            "site_packages": str(ctx.python_root),
        },
        "files": [{"path": str(path), "sha256": _hash_file(path)} for path in files],
        "bootstrap_error_dir": str(ctx.bootstrap_log_dir),
        "installed_at": datetime.fromtimestamp(installed_at, timezone.utc).isoformat(),
        "installed_at_epoch": installed_at,
        "previous_adapter_version": prior_version,
    }


def _readiness_next_steps(ctx: InstallContext) -> Sequence[Dict[str, Any]]:
    return [
        {
            "id": "launch_painter",
            "description": "Launch Painter so it can load the installed startup plugin.",
            "command": [str(ctx.host_path)],
            "why": "The Painter main-thread ping requires a running Painter instance.",
        },
        {
            "id": "verify_install",
            "description": "Verify the installed adapter after Painter finishes starting.",
            "command": list(_command(ctx, "verify")),
            "why": "Direct usability is not proven until the Painter main-thread ping succeeds.",
        },
    ]


def _parse_streamable_response(body: str) -> Any:
    """Parse either JSON or the single-event SSE response used by Streamable HTTP."""
    candidate = body.strip()
    if candidate.startswith("event:") or "\ndata:" in candidate:
        data_lines = [line.removeprefix("data:").strip() for line in candidate.splitlines() if line.startswith("data:")]
        candidate = data_lines[-1] if data_lines else ""
    try:
        return json.loads(candidate)
    except ValueError:
        return None


def _probe_streamable_tool(mcp_url: str, tool_name: str, timeout_secs: float) -> Dict[str, Any]:
    """Bridge Core 0.20.8's JSON-only Accept header to Streamable HTTP.

    Remove this compatibility path once Core's public readiness probe accepts
    both JSON and SSE responses. It is deliberately limited to one fixed MCP
    ``tools/call`` request against a registry-provided local adapter URL.
    """
    request_id = "painter-install-probe-" + uuid.uuid4().hex
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        mcp_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, timeout_secs)) as response:
            payload = _parse_streamable_response(response.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        return {"success": False, "status": "probe_unreachable", "message": str(exc)}
    if not isinstance(payload, dict):
        return {"success": False, "status": "probe_bad_response", "message": "Ping returned no JSON-RPC result."}
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        return {"success": False, "status": "probe_failed", "message": str(message)}
    result = payload.get("result")
    if not isinstance(result, dict) or result.get("isError") is True:
        return {"success": False, "status": "probe_failed", "message": "Painter ping reported an error."}
    return {"success": True, "status": "probe_ok", "result": result}


def _probe_runtime_tool(mcp_url: str, timeout_secs: float) -> Dict[str, Any]:
    parsed = urllib.parse.urlparse(mcp_url)
    hostname = parsed.hostname or ""
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.lower() == "localhost"
    if parsed.scheme != "http" or not loopback:
        return {
            "success": False,
            "status": "probe_unsafe_url",
            "message": "Painter readiness probes require a loopback HTTP registry URL.",
        }
    probe = probe_sidecar_tool(mcp_url, _READINESS_TOOL, timeout_secs=timeout_secs)
    if probe.get("status") == "probe_http_error" and probe.get("http_status") == 406:
        return _probe_streamable_tool(mcp_url, _READINESS_TOOL, timeout_secs)
    return probe


def _verify(ctx: InstallContext, environ: Mapping[str, str]) -> Tuple[Dict[str, Any], Sequence[Dict[str, Any]]]:
    if not ctx.receipt_path.is_file():
        return (
            {
                "directly_usable": False,
                "failure_stage": "receipt",
                "failure_reason": "No Painter install receipt exists.",
            },
            [],
        )
    receipt = _load_json(ctx.receipt_path)
    for item in receipt.get("files", []):
        path = Path(str(item.get("path", "")))
        if not path.is_file():
            return (
                {
                    "directly_usable": False,
                    "failure_stage": "artifact",
                    "failure_reason": f"Receipted file is missing: {path}",
                },
                [],
            )
        if _hash_file(path) != item.get("sha256"):
            return (
                {
                    "directly_usable": False,
                    "failure_stage": "artifact",
                    "failure_reason": f"Receipted file digest changed: {path}",
                },
                [],
            )
    try:
        _query_python(ctx.python_path)
    except LifecycleFailure as exc:
        return (
            {
                "directly_usable": False,
                "failure_stage": "import",
                "failure_reason": str(exc),
            },
            [],
        )

    installed_at = float(receipt.get("installed_at_epoch", 0.0))
    recent_logs = (
        [path for path in ctx.bootstrap_log_dir.glob("*.host-errors.log") if path.stat().st_mtime >= installed_at]
        if ctx.bootstrap_log_dir.is_dir()
        else []
    )
    if recent_logs:
        return (
            {
                "directly_usable": False,
                "failure_stage": "bootstrap",
                "failure_reason": f"Painter captured a startup error in {recent_logs[-1]}",
            },
            [],
        )

    timeout = max(0.1, float(environ.get("DCC_MCP_INSTALL_VERIFY_TIMEOUT", "2.0")))
    runtime_state = query_runtime_state(
        environ.get("DCC_MCP_REGISTRY_DIR"),
        dcc_type=DCC_TYPE,
        include_dead=False,
    )
    entries = [entry for entry in runtime_state.get("entries", []) if entry.get("mcp_url")]
    if len(entries) != 1:
        reason = (
            "No live Painter adapter is registered."
            if not entries
            else "Multiple live Painter adapters are registered; stop all but the target instance."
        )
        return (
            {
                "directly_usable": False,
                "failure_stage": "readiness",
                "failure_reason": reason,
                "probe_tool": _READINESS_TOOL,
            },
            _readiness_next_steps(ctx),
        )
    probe = _probe_runtime_tool(str(entries[0]["mcp_url"]), timeout)
    if not probe.get("success"):
        reason = str(probe.get("message") or probe.get("reason") or probe.get("status") or "Painter ping failed")
        return (
            {
                "directly_usable": False,
                "failure_stage": "readiness",
                "failure_reason": reason,
                "probe_tool": _READINESS_TOOL,
            },
            _readiness_next_steps(ctx),
        )
    return (
        {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
            "probe_tool": _READINESS_TOOL,
        },
        [],
    )


def _rollback_path(current: Path, backup: Path) -> None:
    if current.is_dir():
        safe_remove_tree(current)
    elif current.exists():
        current.unlink()
    if backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(backup), str(current))


def _execute_install(ctx: InstallContext, environ: Mapping[str, str]) -> LifecycleOutcome:
    if ctx.state == "partial":
        raise LifecycleFailure(
            "partial",
            "Painter adapter files exist without a matching receipt; refusing to overwrite them.",
        )
    inspection = inspect_install_root(ctx.bootstrap_root)
    if inspection.get("requires_restart"):
        result = _base_result(ctx, status="requires_restart")
        result["steps"] = [{"id": "preflight", "status": "requires_restart"}]
        result["next_steps"] = [
            {
                "id": "restart_painter",
                "description": "Close Painter and repeat the install command.",
                "command": list(_command(ctx, "install", execute=True)),
                "why": "A loaded native artifact prevents safe replacement.",
            }
        ]
        return LifecycleOutcome(result, INSTALL_EXIT_REQUIRES_RESTART)

    transaction_root = ctx.profile / ".dcc-mcp" / "staging" / uuid.uuid4().hex
    staged_bootstrap = transaction_root / "payload" / _BOOTSTRAP_PACKAGE
    staged_loader = transaction_root / "payload" / _LOADER_NAME
    backup_bootstrap = transaction_root / "backup" / _BOOTSTRAP_PACKAGE
    backup_loader = transaction_root / "backup" / _LOADER_NAME
    backup_receipt = transaction_root / "backup" / ctx.receipt_path.name
    transaction_root.mkdir(parents=True, exist_ok=False)
    staged_bootstrap.mkdir(parents=True)
    staged_bootstrap.joinpath("__init__.py").write_text(_bootstrap_source(ctx), encoding="utf-8")
    staged_loader.write_text(_loader_source(), encoding="utf-8")
    installed_at = time.time()

    try:
        ctx.bootstrap_root.parent.mkdir(parents=True, exist_ok=True)
        ctx.loader_path.parent.mkdir(parents=True, exist_ok=True)
        backup_bootstrap.parent.mkdir(parents=True, exist_ok=True)
        if ctx.bootstrap_root.exists():
            os.replace(str(ctx.bootstrap_root), str(backup_bootstrap))
        if ctx.loader_path.exists():
            os.replace(str(ctx.loader_path), str(backup_loader))
        if ctx.receipt_path.exists():
            backup_receipt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(ctx.receipt_path), str(backup_receipt))

        replaced = safe_replace_tree(staged_bootstrap, ctx.bootstrap_root)
        if not replaced.get("success"):
            if replaced.get("requires_restart"):
                raise LifecycleFailure("install", str(replaced.get("message")), INSTALL_EXIT_REQUIRES_RESTART)
            raise LifecycleFailure("install", str(replaced.get("message")), INSTALL_EXIT_INSTALL)
        loader_temporary = ctx.loader_path.with_name(f".{ctx.loader_path.name}.{uuid.uuid4().hex}.tmp")
        shutil.copy2(str(staged_loader), str(loader_temporary))
        os.replace(str(loader_temporary), str(ctx.loader_path))
        _write_json_atomic(ctx.receipt_path, _receipt(ctx, installed_at))
    except BaseException:
        _rollback_path(ctx.bootstrap_root, backup_bootstrap)
        _rollback_path(ctx.loader_path, backup_loader)
        if backup_receipt.exists():
            ctx.receipt_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(backup_receipt), str(ctx.receipt_path))
        elif ctx.receipt_path.exists():
            ctx.receipt_path.unlink()
        raise
    finally:
        safe_remove_tree(transaction_root)

    verify, next_steps = _verify(ctx, environ)
    directly_usable = bool(verify["directly_usable"])
    result = _base_result(ctx, status="ok" if directly_usable else "partial", verify=verify)
    result["steps"] = [
        {"id": "preflight", "status": "ok"},
        {"id": "install", "status": "ok"},
        {"id": "receipt", "status": "ok"},
        {"id": "verify", "status": "ok" if directly_usable else "failed"},
    ]
    result["next_steps"] = list(next_steps)
    return LifecycleOutcome(result, INSTALL_EXIT_OK if directly_usable else INSTALL_EXIT_VERIFY)


def _execute_uninstall(ctx: InstallContext) -> LifecycleOutcome:
    if not ctx.receipt_path.exists():
        if ctx.loader_path.exists() or ctx.bootstrap_root.exists():
            raise LifecycleFailure(
                "partial",
                "Painter adapter files exist without a receipt; refusing ambiguous removal.",
            )
        result = _base_result(ctx, status="ok")
        result["steps"] = [{"id": "uninstall", "status": "already_absent"}]
        return LifecycleOutcome(result, INSTALL_EXIT_OK)

    receipt = _load_json(ctx.receipt_path)
    expected_paths = {ctx.loader_path.resolve(), ctx.bootstrap_path.resolve()}
    recorded_paths = {Path(str(item.get("path", ""))).resolve() for item in receipt.get("files", [])}
    if recorded_paths != expected_paths:
        raise LifecycleFailure(
            "receipt", "Receipt ownership does not match the Painter adapter paths.", INSTALL_EXIT_INSTALL
        )
    for item in receipt["files"]:
        path = Path(str(item["path"]))
        if path.exists() and _hash_file(path) != item.get("sha256"):
            raise LifecycleFailure(
                "receipt",
                f"Receipted file was modified; preserving it: {path}",
                INSTALL_EXIT_INSTALL,
            )

    removed = safe_remove_tree(ctx.bootstrap_root)
    if not removed.get("success"):
        result = _base_result(ctx, status="requires_restart" if removed.get("requires_restart") else "failed")
        result["steps"] = [{"id": "uninstall", "status": result["status"]}]
        result["next_steps"] = [
            {
                "id": "retry_uninstall",
                "description": "Close Painter and repeat receipt-driven uninstall.",
                "command": list(_command(ctx, "uninstall", execute=True)),
                "why": str(removed.get("message") or "The adapter payload could not be removed safely."),
            }
        ]
        exit_code = INSTALL_EXIT_REQUIRES_RESTART if removed.get("requires_restart") else INSTALL_EXIT_INSTALL
        return LifecycleOutcome(result, exit_code)
    if ctx.loader_path.exists():
        ctx.loader_path.unlink()
    ctx.receipt_path.unlink()
    result = _base_result(ctx, status="ok")
    result["steps"] = [
        {"id": "receipt", "status": "consumed"},
        {"id": "uninstall", "status": "ok"},
    ]
    return LifecycleOutcome(result, INSTALL_EXIT_OK)


def _status(ctx: InstallContext) -> LifecycleOutcome:
    incomplete = ctx.state in {"partial", "repair"}
    result = _base_result(ctx, status="partial" if incomplete else "ok")
    result["steps"] = [
        {"id": "receipt", "status": "ok" if ctx.receipt_path.exists() else "absent"},
        {
            "id": "artifacts",
            "status": "present" if ctx.loader_path.exists() and ctx.bootstrap_path.exists() else "absent",
        },
    ]
    return LifecycleOutcome(result, INSTALL_EXIT_PREFLIGHT if incomplete else INSTALL_EXIT_OK)


def _verify_outcome(ctx: InstallContext, environ: Mapping[str, str]) -> LifecycleOutcome:
    verify, next_steps = _verify(ctx, environ)
    result = _base_result(ctx, status="ok" if verify["directly_usable"] else "failed", verify=verify)
    result["steps"] = [{"id": "verify", "status": "ok" if verify["directly_usable"] else "failed"}]
    result["next_steps"] = list(next_steps)
    return LifecycleOutcome(result, INSTALL_EXIT_OK if verify["directly_usable"] else INSTALL_EXIT_VERIFY)


def _failure_result(
    dcc_path: Optional[str],
    python_path: Optional[str],
    environ: Mapping[str, str],
    failure: LifecycleFailure,
) -> LifecycleOutcome:
    profile = Path(environ.get(_PROFILE_ENV) or _default_profile()).expanduser().resolve()
    retry_command = [COMMAND, "status"]
    if dcc_path:
        retry_command.extend(["--dcc-path", dcc_path])
    retry_command.extend(["--python", python_path or environ.get(_PYTHON_ENV) or sys.executable, "--json"])
    result = {
        "schema_version": INSTALL_SOP_SCHEMA_VERSION,
        "status": "failed",
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": str(getattr(dcc_mcp_core, "__version__", "unknown")),
        "steps": [{"id": "preflight", "status": "failed", "message": str(failure)}],
        "next_steps": [
            {
                "id": "retry_preflight",
                "description": "Repeat the command with the exact Painter host and target interpreter.",
                "command": retry_command,
                "why": str(failure),
            }
        ],
        "receipt_path": str(profile / ".dcc-mcp" / "receipts" / f"{DCC_TYPE}.json"),
        "verify": {
            "directly_usable": False,
            "failure_stage": failure.stage,
            "failure_reason": str(failure),
        },
    }
    return LifecycleOutcome(result, failure.exit_code)


def run_lifecycle(
    verb: str,
    *,
    dcc_path: Optional[str],
    python_path: Optional[str],
    yes: bool,
    dry_run: bool,
    environ: Optional[Mapping[str, str]] = None,
) -> LifecycleOutcome:
    """Execute one public Painter lifecycle verb."""
    resolved_environ = os.environ if environ is None else environ
    try:
        ctx = _resolve_context(dcc_path, python_path, resolved_environ)
        if verb == "status":
            return _status(ctx)
        if verb == "verify":
            return _verify_outcome(ctx, resolved_environ)
        if verb == "uninstall":
            if dry_run or not yes:
                return _plan(ctx, verb)
            return _execute_uninstall(ctx)
        if verb in {"install", "upgrade"}:
            if dry_run or not yes:
                return _plan(ctx, verb)
            return _execute_install(ctx, resolved_environ)
        raise LifecycleFailure("verb", f"Unsupported lifecycle verb: {verb}")
    except LifecycleFailure as exc:
        return _failure_result(dcc_path, python_path, resolved_environ, exc)
    except BaseException as exc:
        failure = LifecycleFailure("install", f"Lifecycle operation failed: {exc}", INSTALL_EXIT_INSTALL)
        return _failure_result(dcc_path, python_path, resolved_environ, failure)


__all__ = [
    "COMMAND",
    "DCC_TYPE",
    "MIN_CORE_VERSION",
    "MIN_PAINTER_VERSION",
    "LifecycleOutcome",
    "run_lifecycle",
]
