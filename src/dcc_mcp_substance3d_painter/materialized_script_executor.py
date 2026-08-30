"""Execute only intact Python FileRefs produced by Core materialization."""

from __future__ import annotations
import __future__

import ast
import hashlib
import json
import logging
import math
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import FunctionType
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from dcc_mcp_core.cancellation import (
    DccMcpCancelledError,
    current_cancel_token,
    current_job,
    set_cancel_token,
    set_current_job,
)
from dcc_mcp_core.script_materialization import default_script_materialization_root, sanitize_materialization_segment

logger = logging.getLogger(__name__)

MAX_MATERIALIZED_SCRIPT_BYTES = 1024 * 1024
MAX_SCRIPT_RESULT_DEPTH = 64
MAX_SCRIPT_RESULT_NODES = 10_000
MAX_SCRIPT_RESULT_JSON_BYTES = 256 * 1024
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
    """Stable, redacted rejection with explicit source-entry semantics."""

    def __init__(self, code: str, *, source_entered: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.source_entered = source_entered


# Keep exception identities stable even when materialized source imports this
# module and rebinds its public names.  These aliases are host-owned and are
# never placed in the frame that invokes ``main()``.
_REJECTED_TYPE = MaterializedScriptRejected
_CANCELLED_TYPE = DccMcpCancelledError
_SYSTEM_EXIT_TYPES = (SystemExit, KeyboardInterrupt, GeneratorExit)
_FUNCTION_TYPE = FunctionType
_DICT_TYPE = dict
_BOOL_TYPE = bool


def _reject(code: str, *, source_entered: bool = False) -> None:
    raise MaterializedScriptRejected(code, source_entered=source_entered)


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


def _future_flags(syntax: ast.Module) -> int:
    flags = 0
    for node in syntax.body:
        if not isinstance(node, ast.ImportFrom) or node.module != "__future__":
            continue
        for imported in node.names:
            feature = getattr(__future__, imported.name, None)
            if feature is None:
                _reject("script_source_invalid")
            flags |= feature.compiler_flag
    return flags


def _invoke_entrypoint(entrypoint: FunctionType) -> Any:
    """Invoke materialized code from a frame with no validator locals."""

    return entrypoint()


def _execute_suffix(compiled_suffix: Any, namespace: dict[str, Any]) -> None:
    """Run suffix code from a frame with no executor state in its locals."""

    exec(compiled_suffix, namespace, namespace)


def _normalize_result(
    value: Any,
    depth: int = 1,
    *,
    enforce_shape_budget: bool = True,
    _result_depth_limit: int = MAX_SCRIPT_RESULT_DEPTH,
    _result_node_limit: int = MAX_SCRIPT_RESULT_NODES,
    _result_byte_limit: int = MAX_SCRIPT_RESULT_JSON_BYTES,
) -> tuple[Any, int]:
    """Normalize one strict portable result in a host-only call boundary.

    The nested walk and its mutable node counter only exist after ``main()``
    has returned.  Materialized code therefore cannot obtain a live validator
    closure through ``sys._getframe(1)`` while it is executing.
    """

    function_type = type
    dict_type = dict
    list_type = list
    string_type = str
    bool_type = bool
    int_type = int
    float_type = float
    length = len
    ordinal = ord
    render_int = str
    render_float = repr
    finite_number = math.isfinite
    node_count = 0

    def reject_result() -> None:
        raise _REJECTED_TYPE("script_result_invalid", source_entered=True)

    def json_string_size(item: str) -> int:
        size = 2
        try:
            for character in item:
                codepoint = ordinal(character)
                if character in {'"', "\\"} or character in {"\b", "\t", "\n", "\f", "\r"}:
                    size += 2
                elif codepoint < 0x20:
                    size += 6
                else:
                    size += length(character.encode("utf-8"))
                if size > _result_byte_limit:
                    reject_result()
        except UnicodeEncodeError:
            reject_result()
        return size

    def normalize(item: Any, item_depth: int = 1, *, enforce_budget: bool = True) -> tuple[Any, int]:
        nonlocal node_count
        value_kind = function_type(item)
        if enforce_budget and value_kind in {dict_type, list_type} and item_depth > _result_depth_limit:
            reject_result()
        if enforce_budget:
            node_count += 1
            if node_count > _result_node_limit:
                reject_result()
        if item is None:
            return None, 4
        if value_kind is string_type:
            return item, json_string_size(item)
        if value_kind is bool_type:
            return item, 4 if item else 5
        if value_kind is int_type:
            try:
                return item, length(render_int(item).encode("ascii"))
            except (UnicodeEncodeError, ValueError):
                reject_result()
        if value_kind is float_type:
            if not finite_number(item):
                reject_result()
            return item, length(render_float(item).encode("ascii"))
        if value_kind is list_type:
            normalized_list: list[Any] = []
            byte_size = 2
            for index, child in enumerate(item):
                normalized_item, item_size = normalize(child, item_depth + 1, enforce_budget=enforce_budget)
                normalized_list.append(normalized_item)
                byte_size += item_size + (1 if index else 0)
                if byte_size > _result_byte_limit:
                    reject_result()
            return normalized_list, byte_size
        if value_kind is dict_type:
            normalized_dict: dict[str, Any] = {}
            byte_size = 2
            for index, (key, child) in enumerate(item.items()):
                if function_type(key) is not string_type:
                    reject_result()
                normalized_item, item_size = normalize(child, item_depth + 1, enforce_budget=enforce_budget)
                normalized_dict[key] = normalized_item
                byte_size += json_string_size(key) + 1 + item_size + (1 if index else 0)
                if byte_size > _result_byte_limit:
                    reject_result()
            return normalized_dict, byte_size
        reject_result()

    return normalize(value, depth, enforce_budget=enforce_shape_budget)


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
    entrypoint_index = syntax.body.index(entrypoints[0])
    flags = _future_flags(syntax)
    prefix = ast.Module(body=syntax.body[: entrypoint_index + 1], type_ignores=syntax.type_ignores)
    suffix = ast.Module(body=syntax.body[entrypoint_index + 1 :], type_ignores=syntax.type_ignores)
    try:
        compile(syntax, "<materialized-script>", "exec", dont_inherit=True)
        compiled_prefix = compile(prefix, "<materialized-script>", "exec", flags=flags, dont_inherit=True)
        compiled_suffix = compile(suffix, "<materialized-script>", "exec", flags=flags, dont_inherit=True)
    except (SyntaxError, ValueError, TypeError, MemoryError):
        _reject("script_source_invalid")
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "__file__": "<materialized-script>",
        "__name__": "__dcc_mcp_materialized_script__",
        "__package__": None,
    }

    # Capture host cancellation state before entering materialized source.  No
    # result validator or its mutable state is present in this executor frame.
    host_cancel_token = current_cancel_token()
    host_job = current_job.get()

    dispatch_path, dispatch_body, dispatch_sha256, dispatch_stat = _validate_contract(file_ref)
    if (
        dispatch_path != path
        or dispatch_body != body
        or dispatch_sha256 != sha256
        or _identity(dispatch_stat) != _identity(captured_stat)
    ):
        _reject("file_ref_identity_drift")
    result_rejection: BaseException | None = None
    host_cancellation: BaseException | None = None
    source_entered = False
    normalized_snapshot: str | None = None
    try:
        exec(compiled_prefix, namespace, namespace)
        exposed_entrypoint = namespace.get("main")
        if not isinstance(exposed_entrypoint, _FUNCTION_TYPE):
            raise TypeError
        entrypoint_globals = _DICT_TYPE(exposed_entrypoint.__globals__)
        entrypoint = _FUNCTION_TYPE(
            exposed_entrypoint.__code__,
            entrypoint_globals,
            exposed_entrypoint.__name__,
            exposed_entrypoint.__defaults__,
            exposed_entrypoint.__closure__,
        )
        entrypoint_globals["main"] = entrypoint
        source_entered = True
        try:
            result = _invoke_entrypoint(entrypoint)
        except _CANCELLED_TYPE as exc:
            cancellation_is_host_owned = (
                current_cancel_token() is host_cancel_token
                and current_job.get() is host_job
                and (
                    (host_cancel_token is not None and bool(host_cancel_token.cancelled))
                    or (host_job is not None and bool(host_job.cancelled))
                )
            )
            if cancellation_is_host_owned:
                host_cancellation = exc
            else:
                result_rejection = _REJECTED_TYPE("script_execution_failed", source_entered=True)
        except _SYSTEM_EXIT_TYPES:
            result_rejection = _REJECTED_TYPE("script_execution_failed", source_entered=True)
        except BaseException:
            result_rejection = _REJECTED_TYPE("script_execution_failed", source_entered=True)
        if result_rejection is None and host_cancellation is None:
            try:
                if type(result) is not _DICT_TYPE or not isinstance(result.get("success"), _BOOL_TYPE):
                    raise _REJECTED_TYPE("script_result_invalid", source_entered=True)
                normalized, _ = _normalize_result(result)
                normalized_snapshot = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
            except _REJECTED_TYPE as exc:
                result_rejection = exc
            except BaseException:
                result_rejection = _REJECTED_TYPE("script_result_invalid", source_entered=True)
    except _CANCELLED_TYPE:
        cancellation_is_host_owned = (
            current_cancel_token() is host_cancel_token
            and current_job.get() is host_job
            and (
                (host_cancel_token is not None and bool(host_cancel_token.cancelled))
                or (host_job is not None and bool(host_job.cancelled))
            )
        )
        if cancellation_is_host_owned:
            raise
        raise _REJECTED_TYPE("script_execution_failed", source_entered=True) from None
    except _SYSTEM_EXIT_TYPES:
        raise _REJECTED_TYPE("script_execution_failed", source_entered=True) from None
    except _REJECTED_TYPE:
        raise
    except BaseException:
        raise _REJECTED_TYPE("script_execution_failed", source_entered=True) from None
    finally:
        set_cancel_token(host_cancel_token)
        set_current_job(host_job)

    # Issue #38 guarantees one suffix pass after a source-entered main attempt,
    # including invalid results and exceptions.  It is side-effect-only: no
    # result is read from its namespace and failures are recorded only in logs.
    if source_entered:
        try:
            _execute_suffix(compiled_suffix, namespace)
        except BaseException:
            logger.warning("Materialized suffix failed after source entry; preserving main outcome.")

    if host_cancellation is not None:
        raise host_cancellation
    if result_rejection is not None:
        raise result_rejection
    if normalized_snapshot is None:
        raise _REJECTED_TYPE("script_result_invalid", source_entered=True) from None
    normalized = json.loads(normalized_snapshot)
    context = normalized.get("context")
    if type(context) is not _DICT_TYPE:
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
    try:
        normalized, _ = _normalize_result(normalized, enforce_shape_budget=False)
    except _REJECTED_TYPE:
        raise
    except BaseException:
        raise _REJECTED_TYPE("script_result_invalid", source_entered=True) from None
    return normalized


__all__ = [
    "MAX_MATERIALIZED_SCRIPT_BYTES",
    "MAX_SCRIPT_RESULT_DEPTH",
    "MAX_SCRIPT_RESULT_JSON_BYTES",
    "MAX_SCRIPT_RESULT_NODES",
    "MaterializedScriptRejected",
    "execute_materialized_file_ref",
]
