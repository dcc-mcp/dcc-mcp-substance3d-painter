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
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

import dcc_mcp_core
from dcc_mcp_core.deployment import (
    INSTALL_EXIT_INSTALL,
    INSTALL_EXIT_OK,
    INSTALL_EXIT_PREFLIGHT,
    INSTALL_EXIT_REQUIRES_RESTART,
    INSTALL_EXIT_VERIFY,
    INSTALL_SOP_SCHEMA_VERSION,
    inspect_install_root,
    probe_sidecar_tool,
    query_runtime_state,
    safe_remove_tree,
    safe_replace_tree,
)

from dcc_mcp_substance3d_painter.__version__ import __version__

DCC_TYPE = "substance3d_painter"
COMMAND = "dcc-mcp-substance3d-painter"
MIN_CORE_VERSION = "0.20.15"
MIN_PAINTER_VERSION = (7, 2)
_PROFILE_ENV = "DCC_MCP_SUBSTANCE3D_PAINTER_PROFILE"
_PYTHON_ENV = "DCC_MCP_INSTALL_PYTHON"
_LOADER_NAME = "dcc_mcp_substance3d_painter_plugin.py"
_BOOTSTRAP_PACKAGE = "dcc_mcp_substance3d_painter_bootstrap"
_READINESS_TOOL = "painter_diagnostics__ping"
_MAX_PROBE_OUTPUT_BYTES = 256 * 1024
_MAX_RECEIPT_BYTES = 256 * 1024
_MAX_VERSION_LENGTH = 32
_VERSION_COMPONENT_RE = re.compile(r"(?:0|[1-9][0-9]{0,5})")
_HOST_EXECUTABLES = {
    "adobe substance 3d painter.exe",
    "adobe substance 3d painter",
    "substance3d-painter",
}
_PROFILE_LOCK_GUARD = threading.Lock()
_PROFILE_LOCKS: set[str] = set()


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
    adapter_module_path: Optional[Path] = None
    core_module_path: Optional[Path] = None
    adapter_distribution_root: Optional[Path] = None
    core_distribution_root: Optional[Path] = None


@dataclass(frozen=True)
class LifecycleOutcome:
    result: Dict[str, Any]
    exit_code: int


class LifecycleFailure(RuntimeError):
    def __init__(self, stage: str, message: str, exit_code: int = INSTALL_EXIT_PREFLIGHT) -> None:
        super().__init__(message)
        self.stage = stage
        self.exit_code = exit_code


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _require_regular_file(path: Path, stage: str, label: str) -> Path:
    try:
        if not path.is_file() or _is_link_or_junction(path) or path.stat().st_size <= 0:
            raise LifecycleFailure(stage, f"{label} is missing, empty, or an unsupported link.")
    except OSError as exc:
        raise LifecycleFailure(stage, f"{label} could not be inspected.") from exc
    return path.resolve()


@contextmanager
def _profile_lock(profile: Path) -> Iterator[None]:
    """Fail closed when another lifecycle mutation owns this profile."""
    key = os.path.normcase(str(profile.resolve()))
    lock_path = profile / ".dcc-mcp" / "locks" / f"{DCC_TYPE}.lock"
    with _PROFILE_LOCK_GUARD:
        if key in _PROFILE_LOCKS:
            raise LifecycleFailure("busy", "A Painter lifecycle mutation is already in progress.", INSTALL_EXIT_INSTALL)
        _PROFILE_LOCKS.add(key)
    descriptor: Optional[int] = None
    identity: Optional[Tuple[int, int]] = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise LifecycleFailure(
                "busy", "A Painter lifecycle mutation is already in progress.", INSTALL_EXIT_INSTALL
            ) from exc
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        stat = os.fstat(descriptor)
        identity = (int(stat.st_dev), int(stat.st_ino))
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if identity is not None:
            try:
                # pathlib.Path.stat did not accept follow_symlinks on the
                # supported Python 3.9 floor; os.stat has supported it for the
                # entire compatibility range.
                current = os.stat(lock_path, follow_symlinks=False)
                if (int(current.st_dev), int(current.st_ino)) == identity:
                    lock_path.unlink()
            except OSError:
                pass
        with _PROFILE_LOCK_GUARD:
            _PROFILE_LOCKS.discard(key)


def _version_tuple(value: object, *, components: int = 3) -> Optional[Tuple[int, ...]]:
    """Parse a bounded canonical final version before integer conversion."""
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_VERSION_LENGTH:
        return None
    parts = value.split(".")
    if len(parts) != components or any(_VERSION_COMPONENT_RE.fullmatch(part) is None for part in parts):
        return None
    parsed = tuple(int(part) for part in parts)
    if not any(parsed):
        return None
    return parsed


class _ProcessTreeOwner:
    def terminate(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        return None


class _PosixProcessTreeOwner(_ProcessTreeOwner):
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def terminate(self) -> None:
        # The supervisor is the session/group leader and deliberately remains
        # alive until its owner terminates it. Never signal a numeric PGID once
        # that identity-bound leader handle has observed exit: the identifier
        # may already have been reused by an unrelated process group.
        if self._process.poll() is None:
            os.killpg(self._process.pid, 9)


class _WindowsProcessTreeOwner(_ProcessTreeOwner):
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _THREAD_SUSPEND_RESUME = 0x0002
    _TH32CS_SNAPTHREAD = 0x00000004

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._handle = handle
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(handle)
            self._handle = None
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        if not self._kernel32.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def terminate(self) -> None:
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise OSError(self._ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _resume_windows_process(process: subprocess.Popen[bytes]) -> None:
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(_WindowsProcessTreeOwner._TH32CS_SNAPTHREAD, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    resumed = False
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while present:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel32.OpenThread(_WindowsProcessTreeOwner._THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed = True
                    finally:
                        kernel32.CloseHandle(thread)
            present = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise OSError("No suspended supervisor thread could be resumed")


def _start_owned_process(command: Sequence[str]) -> Tuple[subprocess.Popen[bytes], _ProcessTreeOwner]:
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "posix":
        process = subprocess.Popen(list(command), start_new_session=True, **kwargs)
        return process, _PosixProcessTreeOwner(process)
    if os.name == "nt":
        owner = _WindowsProcessTreeOwner()
        process: Optional[subprocess.Popen[bytes]] = None
        try:
            process = subprocess.Popen(
                list(command), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000004, **kwargs
            )
            owner.assign(process)
            _resume_windows_process(process)
            return process, owner
        except BaseException:
            try:
                owner.terminate()
            except OSError:
                if process is not None and process.poll() is None:
                    process.kill()
            if process is not None:
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
            owner.close()
            raise
    process = subprocess.Popen(list(command), **kwargs)
    return process, _ProcessTreeOwner()


def _cleanup_owned_process(process: subprocess.Popen[bytes], owner: _ProcessTreeOwner) -> None:
    try:
        owner.terminate()
    except (NotImplementedError, OSError):
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
    finally:
        owner.close()


def _run_bounded_command(command: Sequence[str], timeout: float = 20.0) -> Dict[str, Any]:
    """Run a metadata probe under an owned supervisor tree and bounded deadline."""
    with tempfile.TemporaryDirectory(prefix="dcc-mcp-painter-probe-") as directory:
        root = Path(directory)
        status_path = root / "status.json"
        stdout_path = root / "stdout.bin"
        stderr_path = root / "stderr.bin"
        supervisor = [
            sys.executable,
            "-m",
            "dcc_mcp_substance3d_painter._probe_supervisor",
            str(status_path),
            str(stdout_path),
            str(stderr_path),
            "--",
            *list(command),
        ]
        try:
            process, owner = _start_owned_process(supervisor)
        except OSError as exc:
            return {"success": False, "reason": f"launch failed: {exc.__class__.__name__}"}
        deadline = time.monotonic() + max(0.1, min(float(timeout), 30.0))
        record: Optional[Dict[str, Any]] = None
        reason: Optional[str] = None
        try:
            while time.monotonic() < deadline:
                if any(
                    path.exists() and path.stat().st_size > _MAX_PROBE_OUTPUT_BYTES
                    for path in (stdout_path, stderr_path)
                ):
                    reason = "probe output exceeded limit"
                    break
                if status_path.is_file():
                    try:
                        record = json.loads(status_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        reason = "probe returned invalid status"
                    break
                if process.poll() is not None:
                    reason = "probe supervisor exited unexpectedly"
                    break
                time.sleep(0.02)
            else:
                reason = "probe timed out"
        finally:
            _cleanup_owned_process(process, owner)
        stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
        stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
        truncated = len(stdout) > _MAX_PROBE_OUTPUT_BYTES or len(stderr) > _MAX_PROBE_OUTPUT_BYTES
        if record is None:
            return {"success": False, "reason": reason or "probe failed", "truncated": truncated}
        returncode = int(record.get("returncode", -1))
        return {
            "success": record.get("state") == "completed" and returncode == 0 and not truncated,
            "returncode": returncode,
            "reason": None if record.get("state") == "completed" else "probe launch failed",
            "stdout": stdout[:_MAX_PROBE_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "stderr": stderr[:_MAX_PROBE_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            "truncated": truncated,
        }


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.is_file() or _is_link_or_junction(path) or not 0 < path.stat().st_size <= _MAX_RECEIPT_BYTES:
            raise ValueError("receipt is missing, linked, empty, or unbounded")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleFailure("receipt", "Painter install receipt is unreadable.") from exc
    if not isinstance(payload, dict):
        raise LifecycleFailure("receipt", "Receipt root must be a JSON object.")
    return payload


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def _query_python(python_path: Path) -> Dict[str, str]:
    script = r"""
import importlib.metadata as md
import json
import pathlib
import sys
import sysconfig
import dcc_mcp_core
import dcc_mcp_substance3d_painter as adapter

ad = md.distribution("dcc-mcp-substance3d-painter")
co = md.distribution("dcc-mcp-core")
af = {str(pathlib.Path(ad.locate_file(item)).resolve()): str(item) for item in tuple(ad.files or ())}
cf = {str(pathlib.Path(co.locate_file(item)).resolve()): str(item) for item in tuple(co.files or ())}
ap = str(pathlib.Path(adapter.__file__).resolve())
cp = str(pathlib.Path(dcc_mcp_core.__file__).resolve())
au = ad.read_text("direct_url.json")
cu = co.read_text("direct_url.json")
print(json.dumps({
    "python_version": ".".join(map(str, sys.version_info[:3])),
    "python_root": sysconfig.get_path("purelib"),
    "executable": sys.executable,
    "core_version": dcc_mcp_core.__version__,
    "core_dist_version": co.version,
    "adapter_version": adapter.__version__,
    "adapter_dist_version": ad.version,
    "adapter_file": ap,
    "core_file": cp,
    "adapter_dist_root": str(pathlib.Path(ad.locate_file("")).resolve()),
    "core_dist_root": str(pathlib.Path(co.locate_file("")).resolve()),
    "adapter_record": af.get(ap),
    "core_record": cf.get(cp),
    "adapter_direct_url": json.loads(au) if au else None,
    "core_direct_url": json.loads(cu) if cu else None,
}))
""".strip()
    completed = _run_bounded_command([str(python_path), "-c", script], timeout=20.0)
    if not completed.get("success") or completed.get("truncated"):
        reason = str(completed.get("reason") or "probe failed")
        public_reason = "probe timed out" if reason == "probe timed out" else "probe failed"
        raise LifecycleFailure("python", f"Target interpreter import check {public_reason}.")
    try:
        result = json.loads(str(completed.get("stdout") or "").strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise LifecycleFailure("python", "Target interpreter returned invalid metadata.") from exc
    if not isinstance(result, dict):
        raise LifecycleFailure("python", "Target interpreter returned invalid metadata.")
    reported_executable = Path(str(result.get("executable") or ""))
    if not reported_executable.is_file() or not _same_path(reported_executable, python_path):
        raise LifecycleFailure("python", "Target interpreter identity does not match --python.")
    adapter_version = _version_tuple(result.get("adapter_version"))
    adapter_distribution_version = _version_tuple(result.get("adapter_dist_version"))
    if (
        adapter_version is None
        or adapter_distribution_version is None
        or result.get("adapter_version") != __version__
        or result.get("adapter_version") != result.get("adapter_dist_version")
    ):
        raise LifecycleFailure("python", "Imported adapter version does not match its installed distribution.")
    core_version = _version_tuple(result.get("core_version"))
    core_distribution_version = _version_tuple(result.get("core_dist_version"))
    core_floor = _version_tuple(MIN_CORE_VERSION)
    if core_version is None or core_distribution_version is None or core_floor is None:
        raise LifecycleFailure("core_version", "dcc-mcp-core returned a noncanonical final version.")
    if result.get("core_version") != result.get("core_dist_version"):
        raise LifecycleFailure("python", "Imported Core version does not match its installed distribution.")
    if core_version < core_floor:
        raise LifecycleFailure("core_version", f"dcc-mcp-core>={MIN_CORE_VERSION} is required.")
    python_version = _version_tuple(result.get("python_version"))
    if python_version is None or python_version < (3, 9, 0):
        raise LifecycleFailure("python_version", "Python 3.9 or newer is required.")
    adapter_file = _require_regular_file(
        Path(str(result.get("adapter_file") or "")), "python", "Imported Painter adapter module"
    )
    core_file = _require_regular_file(Path(str(result.get("core_file") or "")), "python", "Imported Core module")
    _require_distribution_origin(
        adapter_file,
        result.get("adapter_dist_root"),
        result.get("adapter_record"),
        result.get("adapter_direct_url"),
        distribution="adapter",
        package="dcc_mcp_substance3d_painter",
    )
    _require_distribution_origin(
        core_file,
        result.get("core_dist_root"),
        result.get("core_record"),
        result.get("core_direct_url"),
        distribution="Core",
        package="dcc_mcp_core",
    )
    return {str(key): "" if value is None else str(value) for key, value in result.items()}


def _editable_distribution_root(value: object) -> Optional[Path]:
    if not isinstance(value, dict) or not isinstance(value.get("dir_info"), dict):
        return None
    url = value.get("url")
    if value["dir_info"].get("editable") is not True or not isinstance(url, str) or not 0 < len(url) <= 2048:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.query or parsed.fragment:
        return None
    raw_path = unquote(parsed.path)
    if re.fullmatch(r"/[A-Za-z]:/.*", raw_path):
        raw_path = raw_path[1:]
    try:
        root = Path(raw_path).resolve()
    except (OSError, ValueError):
        return None
    return root if root.is_dir() and not _is_link_or_junction(root) else None


def _require_distribution_origin(
    module_file: Path,
    root_value: object,
    record_value: object,
    direct_url: object,
    *,
    distribution: str,
    package: str,
) -> None:
    root_text = str(root_value or "")
    root = Path(root_text)
    if (
        not root_text
        or not root.is_dir()
        or _is_link_or_junction(root)
        or module_file.name != "__init__.py"
        or module_file.parent.name != package
    ):
        raise LifecycleFailure("python", f"Imported {distribution} module is shadowed outside its distribution.")
    if isinstance(record_value, str) and 0 < len(record_value) <= 1024:
        record_path = Path(record_value)
        if record_path.is_absolute() or ".." in record_path.parts:
            raise LifecycleFailure("python", f"Imported {distribution} module has invalid RECORD ownership.")
        if not _same_path(root / record_path, module_file) or not _path_within(module_file, root):
            raise LifecycleFailure("python", f"Imported {distribution} module is not owned by its RECORD.")
        return
    editable = _editable_distribution_root(direct_url)
    candidates = (
        () if editable is None else (editable / "src" / package / "__init__.py", editable / package / "__init__.py")
    )
    if not any(_same_path(module_file, candidate) for candidate in candidates):
        raise LifecycleFailure(
            "python", f"Imported {distribution} module has no validated RECORD or editable ownership."
        )


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
        if candidate.name.lower() not in _HOST_EXECUTABLES:
            raise LifecycleFailure("host", "--dcc-path must select the exact Painter executable.")
        if candidate.is_file():
            return _require_regular_file(candidate, "host", "Selected Painter executable")
        raise LifecycleFailure("host", "Painter executable does not exist.")
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
    file_version = _windows_file_version(path)
    if file_version:
        parts = file_version.split(".")
        normalized = ".".join(parts[:3]) if len(parts) >= 3 else file_version
        if _version_tuple(normalized) is not None:
            return normalized, "file_metadata"
    if path.parent.name == "MacOS" and path.parent.parent.name == "Contents":
        plist_path = path.parent.parent / "Info.plist"
        try:
            plist = plistlib.loads(plist_path.read_bytes())
            version = str(plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion") or "").strip()
        except (OSError, ValueError):
            version = ""
        if _version_tuple(version) is not None:
            return version, "app_bundle"
    for parent in tuple(path.parents)[:5]:
        match = re.fullmatch(
            r"Adobe Substance 3D Painter (?P<version>(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5})){2})", parent.name
        )
        if match and _version_tuple(match.group("version")) is not None:
            return match.group("version"), "path"
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
    parsed_host_version = _version_tuple(host_version)
    if parsed_host_version is None or parsed_host_version < MIN_PAINTER_VERSION:
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
        embedded = _version_tuple(embedded_python_version, components=2)
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
    else:
        state = "fresh"
    ctx = InstallContext(
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
        adapter_module_path=Path(python["adapter_file"]).resolve(),
        core_module_path=Path(python["core_file"]).resolve(),
        adapter_distribution_root=Path(python["adapter_dist_root"]).resolve(),
        core_distribution_root=Path(python["core_dist_root"]).resolve(),
    )
    if receipt_exists:
        receipt = _load_json(receipt_path)
        _validate_receipt(ctx, receipt, require_artifacts=False)
        intact = all(
            path.is_file() and not _is_link_or_junction(path) and _hash_file(path) == item["sha256"]
            for path, item in zip((ctx.loader_path, ctx.bootstrap_path), receipt["files"])
        )
        state = "upgrade" if receipt.get("adapter_version") != __version__ else ("current" if intact else "repair")
        ctx = replace(ctx, state=state)
    return ctx


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
            "adapter_module_path": None if ctx.adapter_module_path is None else str(ctx.adapter_module_path),
            "core_module_path": None if ctx.core_module_path is None else str(ctx.core_module_path),
            "adapter_distribution_root": (
                None if ctx.adapter_distribution_root is None else str(ctx.adapter_distribution_root)
            ),
            "core_distribution_root": None if ctx.core_distribution_root is None else str(ctx.core_distribution_root),
        },
        "files": [{"path": str(path), "sha256": _hash_file(path)} for path in files],
        "bootstrap_error_dir": str(ctx.bootstrap_log_dir),
        "installed_at": datetime.fromtimestamp(installed_at, timezone.utc).isoformat(),
        "installed_at_epoch": installed_at,
        "previous_adapter_version": prior_version,
    }


def _lexical_path(value: object) -> Optional[Path]:
    if not isinstance(value, str) or not 0 < len(value) <= 32_768:
        return None
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    return Path(os.path.abspath(str(path)))


def _validate_receipt(
    ctx: InstallContext,
    receipt: Mapping[str, Any],
    *,
    require_artifacts: bool = True,
) -> None:
    """Bind receipt ownership to this exact host, profile, interpreter, and payload."""
    if receipt.get("schema_version") != 1 or receipt.get("dcc_type") != DCC_TYPE:
        raise LifecycleFailure(
            "receipt", "Painter install receipt has the wrong contract identity.", INSTALL_EXIT_INSTALL
        )
    adapter_version = _version_tuple(receipt.get("adapter_version"))
    core_version = _version_tuple(receipt.get("core_version"))
    core_floor = _version_tuple(MIN_CORE_VERSION)
    if adapter_version is None or core_version is None or core_floor is None or core_version < core_floor:
        raise LifecycleFailure("receipt", "Painter install receipt has invalid version metadata.", INSTALL_EXIT_INSTALL)
    host = receipt.get("host")
    python = receipt.get("python")
    if not isinstance(host, dict) or not isinstance(python, dict):
        raise LifecycleFailure(
            "receipt", "Painter install receipt is missing ownership metadata.", INSTALL_EXIT_INSTALL
        )
    path_bindings = (
        (host.get("path"), ctx.host_path),
        (host.get("profile"), ctx.profile),
        (python.get("path"), ctx.python_path),
        (python.get("site_packages"), ctx.python_root),
    )
    for recorded, expected in path_bindings:
        path = _lexical_path(recorded)
        if path is None or os.path.normcase(str(path)) != os.path.normcase(str(Path(os.path.abspath(str(expected))))):
            raise LifecycleFailure(
                "receipt", "Painter install receipt ownership does not match this context.", INSTALL_EXIT_INSTALL
            )
    optional_bindings = (
        (python.get("adapter_module_path"), ctx.adapter_module_path),
        (python.get("core_module_path"), ctx.core_module_path),
        (python.get("adapter_distribution_root"), ctx.adapter_distribution_root),
        (python.get("core_distribution_root"), ctx.core_distribution_root),
    )
    for recorded, expected in optional_bindings:
        if expected is None and recorded is None:
            continue
        path = _lexical_path(recorded)
        if expected is None or path is None or not _same_path(path, expected):
            raise LifecycleFailure(
                "receipt", "Painter install receipt module origin does not match.", INSTALL_EXIT_INSTALL
            )
    if host.get("version") != ctx.host_version or python.get("version") != ctx.python_version:
        raise LifecycleFailure(
            "receipt", "Painter install receipt version binding does not match.", INSTALL_EXIT_INSTALL
        )
    files = receipt.get("files")
    if not isinstance(files, list) or len(files) != 2 or any(not isinstance(item, dict) for item in files):
        raise LifecycleFailure("receipt", "Painter install receipt file ownership is incomplete.", INSTALL_EXIT_INSTALL)
    expected_paths = (ctx.loader_path, ctx.bootstrap_path)
    for item, expected in zip(files, expected_paths):
        recorded = _lexical_path(item.get("path"))
        digest = item.get("sha256")
        if (
            recorded is None
            or os.path.normcase(str(recorded)) != os.path.normcase(str(Path(os.path.abspath(str(expected)))))
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise LifecycleFailure(
                "receipt", "Painter install receipt file ownership is invalid.", INSTALL_EXIT_INSTALL
            )
        if require_artifacts:
            if not expected.is_file() or _is_link_or_junction(expected) or _hash_file(expected) != digest:
                raise LifecycleFailure(
                    "receipt", "A receipted Painter file is missing, linked, or modified.", INSTALL_EXIT_INSTALL
                )
    if ctx.bootstrap_root.exists():
        if _is_link_or_junction(ctx.bootstrap_root) or not ctx.bootstrap_root.is_dir():
            raise LifecycleFailure("receipt", "Painter managed payload is an unsupported link.", INSTALL_EXIT_INSTALL)
        extras = []
        for path in ctx.bootstrap_root.rglob("*"):
            if (
                _is_link_or_junction(path)
                or (path.is_file() and path != ctx.bootstrap_path)
                or (path.is_dir() and path != ctx.bootstrap_root)
            ):
                extras.append(path)
        if extras:
            raise LifecycleFailure(
                "receipt", "Painter managed payload contains content not owned by the receipt.", INSTALL_EXIT_INSTALL
            )


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
    """Call one fixed tool on the local Streamable HTTP endpoint."""
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
    except (OSError, ValueError, urllib.error.HTTPError):
        return {"success": False, "status": "probe_unreachable", "message": "Painter ping was unreachable."}
    if not isinstance(payload, dict):
        return {"success": False, "status": "probe_bad_response", "message": "Ping returned no JSON-RPC result."}
    if payload.get("error"):
        return {"success": False, "status": "probe_failed", "message": "Painter ping returned an error."}
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
        loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or parsed.path.rstrip("/") != "/mcp"
    ):
        return {
            "success": False,
            "status": "probe_unsafe_url",
            "message": "Painter readiness probes require a loopback HTTP registry URL.",
        }
    probe = probe_sidecar_tool(mcp_url, _READINESS_TOOL, timeout_secs=timeout_secs)
    if probe.get("status") == "probe_http_error" and probe.get("http_status") == 406:
        return _probe_streamable_tool(mcp_url, _READINESS_TOOL, timeout_secs)
    return probe


def _observe_process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Observe executable and start identity independently from registry/probe claims."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32_768)
            length = wintypes.DWORD(len(buffer))
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return None
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return None
            started = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            return {
                "pid": pid,
                "executable": str(Path(buffer.value).resolve()),
                "start_identity": f"windows-filetime:{started}",
            }
        finally:
            kernel32.CloseHandle(handle)
    if sys.platform == "darwin":
        import ctypes

        buffer = ctypes.create_string_buffer(4096)
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            length = int(libproc.proc_pidpath(pid, buffer, len(buffer)))
        except (OSError, AttributeError):
            return None
        if length <= 0:
            return None
        started = _run_bounded_command(["ps", "-p", str(pid), "-o", "lstart="], timeout=3.0)
        start_text = str(started.get("stdout") or "").strip()
        if not started.get("success") or not start_text:
            return None
        return {
            "pid": pid,
            "executable": str(Path(buffer.value.decode("utf-8", errors="strict")).resolve()),
            "start_identity": f"darwin-lstart:{start_text}",
        }
    try:
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = stat.rfind(") ")
        start_ticks = stat[closing + 2 :].split()[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (IndexError, OSError, ValueError):
        return None
    return {
        "pid": pid,
        "executable": str(executable),
        "start_identity": f"linux:{boot_id}:{start_ticks}",
    }


def _probe_context(probe: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    result = probe.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent") or result.get("structured_content") or result
    if not isinstance(structured, dict) or structured.get("success") is not True:
        return None
    context = structured.get("context")
    return context if isinstance(context, dict) else None


def _identity_failure(reason: str) -> Tuple[Dict[str, Any], Sequence[Dict[str, Any]]]:
    return (
        {
            "directly_usable": False,
            "failure_stage": "readiness_identity",
            "failure_reason": reason,
            "probe_tool": _READINESS_TOOL,
        },
        [],
    )


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
    try:
        _validate_receipt(ctx, receipt)
    except LifecycleFailure:
        return (
            {
                "directly_usable": False,
                "failure_stage": "receipt",
                "failure_reason": "Painter install receipt or payload ownership is invalid.",
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
    entries = [entry for entry in runtime_state.get("entries", []) if isinstance(entry, dict) and entry.get("mcp_url")]
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
    entry = entries[0]
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return _identity_failure("Painter runtime identity metadata is unavailable.")
    try:
        pid = int(metadata.get("dcc_pid"))
    except (TypeError, ValueError):
        return _identity_failure("Painter runtime PID is unavailable.")
    if (
        not str(entry.get("instance_id") or "").strip()
        or entry.get("adapter_version") != __version__
        or entry.get("dcc_type") != DCC_TYPE
        or metadata.get("dcc_version") != ctx.host_version
    ):
        return _identity_failure("Painter registry identity does not match this install.")
    before = _observe_process_identity(pid)
    if before is None or not _same_path(Path(str(before.get("executable") or "")), ctx.host_path):
        return _identity_failure("Painter runtime executable identity does not match --dcc-path.")
    probe = _probe_runtime_tool(str(entry["mcp_url"]), timeout)
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
    context = _probe_context(probe)
    after = _observe_process_identity(pid)
    if context is None or after is None:
        return _identity_failure("Painter probe identity is unavailable.")
    expected_adapter = None if ctx.adapter_module_path is None else str(ctx.adapter_module_path)
    expected_core = None if ctx.core_module_path is None else str(ctx.core_module_path)
    expected_bootstrap = str(ctx.bootstrap_path)
    if (
        context.get("host_dispatch_ready") is not True
        or context.get("host") != DCC_TYPE
        or context.get("host_pid") != pid
        or context.get("adapter_version") != __version__
        or _version_tuple(context.get("core_version")) is None
        or _version_tuple(context.get("core_version")) < _version_tuple(MIN_CORE_VERSION)
        or context.get("process_start_identity") != before.get("start_identity")
        or not _same_path(Path(str(context.get("host_executable") or "")), ctx.host_path)
        or (
            expected_adapter is not None
            and not _same_path(Path(str(context.get("adapter_module_path") or "")), Path(expected_adapter))
        )
        or (
            expected_core is not None
            and not _same_path(Path(str(context.get("core_module_path") or "")), Path(expected_core))
        )
        or not _same_path(Path(str(context.get("bootstrap_module_path") or "")), Path(expected_bootstrap))
        or after.get("start_identity") != before.get("start_identity")
        or not _same_path(Path(str(after.get("executable") or "")), ctx.host_path)
    ):
        return _identity_failure("Painter runtime identity changed or did not match the installed receipt.")
    return (
        {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
            "probe_tool": _READINESS_TOOL,
        },
        [],
    )


def _remove_transaction_path(path: Path) -> None:
    if path.is_dir() and not _is_link_or_junction(path):
        removed = safe_remove_tree(path)
        if not removed.get("success"):
            raise LifecycleFailure("rollback", "Painter transaction rollback could not remove a staged payload.")
    elif path.exists() or _is_link_or_junction(path):
        path.unlink()


def _rollback_path(current: Path, backup: Path, *, previously_existed: bool) -> None:
    if backup.exists():
        _remove_transaction_path(current)
        current.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(backup), str(current))
        return
    if not previously_existed:
        _remove_transaction_path(current)
        return
    if not current.exists():
        raise LifecycleFailure("rollback", "Painter transaction rollback is missing its previous payload.")


def _rollback_transaction(paths: Sequence[Tuple[Path, Path, bool]]) -> None:
    failed = False
    for current, backup, previously_existed in paths:
        try:
            _rollback_path(current, backup, previously_existed=previously_existed)
        except (LifecycleFailure, OSError):
            failed = True
    if failed:
        raise LifecycleFailure("rollback", "Painter transaction rollback could not restore every owned artifact.")


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
    previous_bootstrap = ctx.bootstrap_root.exists()
    previous_loader = ctx.loader_path.exists()
    previous_receipt = ctx.receipt_path.exists()
    rollback_paths = (
        (ctx.bootstrap_root, backup_bootstrap, previous_bootstrap),
        (ctx.loader_path, backup_loader, previous_loader),
        (ctx.receipt_path, backup_receipt, previous_receipt),
    )
    restore_on_verify_failure = ctx.state in {"current", "upgrade"}
    verify: Optional[Dict[str, Any]] = None
    next_steps: Sequence[Dict[str, Any]] = []
    try:
        transaction_root.mkdir(parents=True, exist_ok=False)
        staged_bootstrap.mkdir(parents=True)
        staged_bootstrap.joinpath("__init__.py").write_text(_bootstrap_source(ctx), encoding="utf-8")
        staged_loader.write_text(_loader_source(), encoding="utf-8")
        installed_at = time.time()
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
        verify, next_steps = _verify(ctx, environ)
        if restore_on_verify_failure and not verify["directly_usable"]:
            _rollback_transaction(rollback_paths)
            result = _base_result(ctx, status="partial", verify=verify)
            result["previous_restored"] = True
            result["steps"] = [
                {"id": "preflight", "status": "ok"},
                {"id": "install", "status": "rolled_back"},
                {"id": "receipt", "status": "restored"},
                {"id": "verify", "status": "failed"},
            ]
            result["next_steps"] = list(next_steps)
            return LifecycleOutcome(result, INSTALL_EXIT_VERIFY)
    except BaseException:
        _rollback_transaction(rollback_paths)
        raise
    finally:
        if transaction_root.exists():
            safe_remove_tree(transaction_root)

    if verify is None:
        raise LifecycleFailure("install", "Painter install verification did not run.", INSTALL_EXIT_INSTALL)
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
    _validate_receipt(ctx, receipt)
    transaction_root = ctx.profile / ".dcc-mcp" / "staging" / uuid.uuid4().hex
    backup_bootstrap = transaction_root / "backup" / _BOOTSTRAP_PACKAGE
    backup_loader = transaction_root / "backup" / _LOADER_NAME
    backup_receipt = transaction_root / "backup" / ctx.receipt_path.name
    transaction_root.mkdir(parents=True, exist_ok=False)
    try:
        backup_bootstrap.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ctx.bootstrap_root, backup_bootstrap, symlinks=True)
        shutil.copy2(ctx.loader_path, backup_loader)
        shutil.copy2(ctx.receipt_path, backup_receipt)
        removed = safe_remove_tree(ctx.bootstrap_root)
        if not removed.get("success"):
            exit_code = INSTALL_EXIT_REQUIRES_RESTART if removed.get("requires_restart") else INSTALL_EXIT_INSTALL
            raise LifecycleFailure("uninstall", "Painter payload could not be removed safely.", exit_code)
        ctx.loader_path.unlink()
        ctx.receipt_path.unlink()
    except BaseException:
        try:
            if ctx.bootstrap_root.exists():
                safe_remove_tree(ctx.bootstrap_root)
            if backup_bootstrap.exists():
                shutil.copytree(backup_bootstrap, ctx.bootstrap_root)
            if backup_loader.exists() and not ctx.loader_path.exists():
                ctx.loader_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_loader, ctx.loader_path)
            if backup_receipt.exists() and not ctx.receipt_path.exists():
                ctx.receipt_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_receipt, ctx.receipt_path)
        except OSError as exc:
            raise LifecycleFailure(
                "uninstall", "Painter uninstall rollback could not restore the previous install.", INSTALL_EXIT_INSTALL
            ) from exc
        raise
    finally:
        if transaction_root.exists():
            safe_remove_tree(transaction_root)
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
            with _profile_lock(ctx.profile):
                ctx = _resolve_context(dcc_path, python_path, resolved_environ)
                return _execute_uninstall(ctx)
        if verb in {"install", "upgrade"}:
            if dry_run or not yes:
                return _plan(ctx, verb)
            with _profile_lock(ctx.profile):
                ctx = _resolve_context(dcc_path, python_path, resolved_environ)
                return _execute_install(ctx, resolved_environ)
        raise LifecycleFailure("verb", f"Unsupported lifecycle verb: {verb}")
    except LifecycleFailure as exc:
        return _failure_result(dcc_path, python_path, resolved_environ, exc)
    except BaseException:
        failure = LifecycleFailure("install", "Painter lifecycle operation failed safely.", INSTALL_EXIT_INSTALL)
        return _failure_result(dcc_path, python_path, resolved_environ, failure)


__all__ = [
    "COMMAND",
    "DCC_TYPE",
    "MIN_CORE_VERSION",
    "MIN_PAINTER_VERSION",
    "LifecycleOutcome",
    "run_lifecycle",
]
