"""Execute only intact Python FileRefs produced by Core materialization."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from dcc_mcp_core.script_materialization import default_script_materialization_root, sanitize_materialization_segment

MAX_MATERIALIZED_SCRIPT_BYTES = 1024 * 1024
_REPARSE_POINT_ATTRIBUTE = 0x400
_FILE_REF_KEYS = {
    "uri",
    "mime",
    "size_bytes",
    "display_name",
    "digest",
    "tool_call_id",
    "session_id",
    "correlation_id",
    "created_at",
    "expires_at",
    "metadata",
}
_METADATA_KEYS = {
    "dcc_type",
    "instance_id",
    "session_id",
    "script_id",
    "language",
    "suffix",
    "materialization_kind",
}


class MaterializedScriptRejected(ValueError):
    """Stable, redacted rejection raised before materialized source runs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject(code: str) -> None:
    raise MaterializedScriptRejected(code)


def _identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE)


def _require_plain_components(path: Path, root: Path) -> None:
    try:
        root_stat = root.lstat()
    except OSError:
        _reject("file_ref_unavailable")
    if stat.S_ISLNK(root_stat.st_mode) or _is_reparse(root_stat):
        _reject("file_ref_unsafe_link")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except OSError:
            _reject("file_ref_unavailable")
        if stat.S_ISLNK(current_stat.st_mode) or _is_reparse(current_stat):
            _reject("file_ref_unsafe_link")


def _path_from_file_ref(file_ref: Mapping[str, Any], root: Path) -> Path:
    uri = file_ref.get("uri")
    if not isinstance(uri, str):
        _reject("file_ref_invalid")
    try:
        parsed = urlsplit(uri)
    except ValueError:
        _reject("file_ref_invalid")
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        _reject("file_ref_invalid")
    raw_path = url2pathname(parsed.path)
    if os.name == "nt" and raw_path.startswith(("/", "\\")) and len(raw_path) >= 3 and raw_path[2] == ":":
        raw_path = raw_path[1:]
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        _reject("file_ref_invalid")
    try:
        candidate.relative_to(root)
    except ValueError:
        _reject("file_ref_scope_denied")
    return candidate


def _open_regular_unique(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        _reject("file_ref_unavailable")
    try:
        handle_stat = os.fstat(fd)
        path_stat = path.lstat()
        if not stat.S_ISREG(handle_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            _reject("file_ref_not_regular")
        if _is_reparse(path_stat):
            _reject("file_ref_unsafe_link")
        if _identity(handle_stat) != _identity(path_stat):
            _reject("file_ref_identity_drift")
        if int(handle_stat.st_nlink) != 1:
            _reject("file_ref_hardlink")
        return fd, handle_stat
    except BaseException:
        os.close(fd)
        raise


def _read_snapshot(fd: int, expected_size: int) -> bytes:
    if expected_size < 1 or expected_size > MAX_MATERIALIZED_SCRIPT_BYTES:
        _reject("file_ref_too_large")
    chunks: list[bytes] = []
    remaining = expected_size + 1
    while remaining:
        chunk = os.read(fd, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    body = b"".join(chunks)
    if len(body) != expected_size:
        _reject("file_ref_integrity_mismatch")
    return body


def _recapture(path: Path, handle_stat: os.stat_result) -> None:
    try:
        path_stat = path.lstat()
    except OSError:
        _reject("file_ref_identity_drift")
    if _identity(path_stat) != _identity(handle_stat):
        _reject("file_ref_identity_drift")
    if path_stat.st_size != handle_stat.st_size or path_stat.st_mtime_ns != handle_stat.st_mtime_ns:
        _reject("file_ref_identity_drift")


def _parse_expiry(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        _reject("file_ref_invalid")
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _reject("file_ref_invalid")
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        _reject("file_ref_expired")


def _validate_contract(file_ref: Mapping[str, Any]) -> tuple[Path, bytes, str, os.stat_result]:
    if set(file_ref) != _FILE_REF_KEYS:
        _reject("file_ref_invalid")
    metadata_ref = file_ref.get("metadata")
    if not isinstance(metadata_ref, Mapping) or set(metadata_ref) != _METADATA_KEYS:
        _reject("file_ref_invalid")
    expected_metadata = {
        "dcc_type": "substance3d_painter",
        "language": "python",
        "suffix": ".py",
        "materialization_kind": "script",
    }
    if any(metadata_ref.get(key) != value for key, value in expected_metadata.items()):
        _reject("file_ref_scope_denied")
    for key in ("instance_id", "session_id", "script_id"):
        if not isinstance(metadata_ref.get(key), str) or not metadata_ref[key]:
            _reject("file_ref_invalid")
        if sanitize_materialization_segment(metadata_ref[key]) != metadata_ref[key]:
            _reject("file_ref_invalid")
    if file_ref.get("session_id") != metadata_ref["session_id"]:
        _reject("file_ref_invalid")
    if file_ref.get("mime") != "text/x-python":
        _reject("file_ref_scope_denied")
    if not isinstance(file_ref.get("display_name"), str):
        _reject("file_ref_invalid")
    if file_ref["display_name"] != f"{metadata_ref['script_id']}.py":
        _reject("file_ref_invalid")
    expected_size = file_ref.get("size_bytes")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        _reject("file_ref_invalid")
    digest = file_ref.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        _reject("file_ref_invalid")
    if not isinstance(file_ref.get("created_at"), str):
        _reject("file_ref_invalid")
    for key in ("tool_call_id", "correlation_id"):
        if file_ref.get(key) is not None and not isinstance(file_ref[key], str):
            _reject("file_ref_invalid")
    _parse_expiry(file_ref.get("expires_at"))

    configured_root = default_script_materialization_root().absolute()
    try:
        configured_stat = configured_root.lstat()
    except OSError:
        _reject("file_ref_unavailable")
    if stat.S_ISLNK(configured_stat.st_mode) or _is_reparse(configured_stat):
        _reject("file_ref_unsafe_link")
    root = configured_root.resolve()
    path = _path_from_file_ref(file_ref, root)
    expected_parts = (
        str(metadata_ref["dcc_type"]),
        "temp",
        str(metadata_ref["instance_id"]),
        str(metadata_ref["session_id"]),
        str(file_ref["display_name"]),
    )
    if path.relative_to(root).parts != expected_parts:
        _reject("file_ref_scope_denied")
    metadata_path = path.with_name(path.name + ".meta.json")
    _require_plain_components(path, root)
    _require_plain_components(metadata_path, root)

    script_fd, script_stat = _open_regular_unique(path)
    metadata_fd = -1
    try:
        metadata_fd, metadata_stat = _open_regular_unique(metadata_path)
        if script_stat.st_size != expected_size or script_stat.st_size > MAX_MATERIALIZED_SCRIPT_BYTES:
            _reject(
                "file_ref_too_large"
                if script_stat.st_size > MAX_MATERIALIZED_SCRIPT_BYTES
                else "file_ref_integrity_mismatch"
            )
        if script_stat.st_mtime_ns > metadata_stat.st_mtime_ns or script_stat.st_ctime_ns > metadata_stat.st_mtime_ns:
            _reject("file_ref_independent_replacement")
        body = _read_snapshot(script_fd, expected_size)
        metadata_body = _read_snapshot(metadata_fd, metadata_stat.st_size)
        _recapture(path, script_stat)
        _recapture(metadata_path, metadata_stat)
    finally:
        os.close(script_fd)
        if metadata_fd >= 0:
            os.close(metadata_fd)

    if hashlib.sha256(body).hexdigest() != digest[7:]:
        _reject("file_ref_integrity_mismatch")
    try:
        source = body.decode("utf-8")
        metadata = json.loads(metadata_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _reject("file_ref_invalid_encoding")
    if "\x00" in source:
        _reject("file_ref_invalid_encoding")
    if not isinstance(metadata, dict) or metadata.get("file_ref") != dict(file_ref):
        _reject("file_ref_integrity_mismatch")
    expected_descriptor = {
        "file_path": str(path.resolve()),
        "path": str(path.resolve()),
        "language": "python",
        "suffix": ".py",
        "sha256": digest[7:],
        "bytes": expected_size,
        "dcc_type": metadata_ref["dcc_type"],
        "instance_id": metadata_ref["instance_id"],
        "session_id": metadata_ref["session_id"],
        "script_id": metadata_ref["script_id"],
        "tool_call_id": file_ref.get("tool_call_id"),
        "correlation_id": file_ref.get("correlation_id"),
        "created_at": file_ref.get("created_at"),
        "expires_at": file_ref.get("expires_at"),
    }
    if any(metadata.get(key) != value for key, value in expected_descriptor.items()):
        _reject("file_ref_integrity_mismatch")
    return path, body, digest[7:], script_stat


def execute_materialized_file_ref(file_ref: Mapping[str, Any]) -> dict[str, Any]:
    """Validate, snapshot, and execute one fixed Python ``main()`` contract."""
    if not isinstance(file_ref, Mapping):
        _reject("file_ref_invalid")
    path, body, sha256, captured_stat = _validate_contract(file_ref)
    try:
        syntax = ast.parse(body, filename="<materialized-script>", mode="exec")
    except (SyntaxError, ValueError, TypeError, MemoryError):
        _reject("script_source_invalid")
    entrypoints = [node for node in syntax.body if isinstance(node, ast.FunctionDef) and node.name == "main"]
    if len(entrypoints) != 1:
        _reject("script_entrypoint_invalid")
    arguments = entrypoints[0].args
    if (
        entrypoints[0].decorator_list
        or arguments.posonlyargs
        or arguments.args
        or arguments.vararg is not None
        or arguments.kwonlyargs
        or arguments.kwarg is not None
    ):
        _reject("script_entrypoint_invalid")
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "__file__": "<materialized-script>",
        "__name__": "__dcc_mcp_materialized_script__",
        "__package__": None,
    }
    try:
        exec(compile(body, "<materialized-script>", "exec"), namespace, namespace)
        entrypoint = namespace.get("main")
        if not callable(entrypoint):
            _reject("script_entrypoint_invalid")
        result = entrypoint()
    except MaterializedScriptRejected:
        raise
    except BaseException:
        _reject("script_execution_failed")
    try:
        if not isinstance(result, Mapping) or not isinstance(result.get("success"), bool):
            _reject("script_result_invalid")
        normalized = json.loads(json.dumps(dict(result)))
    except MaterializedScriptRejected:
        raise
    except BaseException:
        _reject("script_result_invalid")
    context = normalized.get("context")
    if not isinstance(context, dict):
        context = {}
        normalized["context"] = context
    execution_file = {
        "method": "validated_file_ref_snapshot",
        "sha256": sha256,
        "bytes": len(body),
    }
    context.update({"sha256": sha256, "bytes": len(body), "execution_file": execution_file})
    if normalized["success"]:
        normalized["postcondition"] = {"verified": True, **execution_file}
    _recapture(path, captured_stat)
    return normalized


__all__ = [
    "MAX_MATERIALIZED_SCRIPT_BYTES",
    "MaterializedScriptRejected",
    "execute_materialized_file_ref",
]
