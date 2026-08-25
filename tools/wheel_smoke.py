"""Validate the installed wheel against the released Core install contract."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from dcc_mcp_core.deployment import INSTALL_EXIT_PREFLIGHT, load_install_sop_schema
from jsonschema import Draft202012Validator

from dcc_mcp_substance3d_painter import _installer
from dcc_mcp_substance3d_painter.install_cli import main


def _write_hostile_distribution(root: Path, package: str, distribution: str, version: str) -> None:
    module = root / package / "__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    metadata = root / f"{distribution.replace('-', '_')}-{version}.dist-info"
    metadata.mkdir()
    metadata.joinpath("METADATA").write_text(
        f"Metadata-Version: 2.3\nName: {distribution}\nVersion: {version}\n",
        encoding="utf-8",
    )
    payload = module.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")
    metadata.joinpath("RECORD").write_text(
        f"{package}/__init__.py,sha256={digest},{len(payload)}\n",
        encoding="utf-8",
    )


def _verify_hostile_pythonpath_is_rejected() -> None:
    with tempfile.TemporaryDirectory(prefix="painter-wheel-shadow-") as temporary:
        hostile = Path(temporary)
        _write_hostile_distribution(
            hostile,
            "dcc_mcp_substance3d_painter",
            "dcc-mcp-substance3d-painter",
            _installer.__version__,
        )
        _write_hostile_distribution(hostile, "dcc_mcp_core", "dcc-mcp-core", _installer.MIN_CORE_VERSION)
        previous = os.environ.get("PYTHONPATH")
        os.environ["PYTHONPATH"] = str(hostile)
        try:
            try:
                _installer._query_python(Path(sys.executable).resolve())
            except _installer.LifecycleFailure:
                pass
            else:
                raise SystemExit("installed-wheel interpreter accepted a hostile PYTHONPATH distribution")
        finally:
            if previous is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = previous


def run() -> int:
    _verify_hostile_pythonpath_is_rejected()
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = main(["install", "--json", "--invalid-wheel-smoke-argument"])
    if exit_code != INSTALL_EXIT_PREFLIGHT or stderr.getvalue():
        raise SystemExit("installed-wheel CLI did not fail closed with a stable exit")
    payload = json.loads(stdout.getvalue())
    Draft202012Validator(load_install_sop_schema()).validate(payload)
    if payload["verify"]["failure_stage"] != "arguments":
        raise SystemExit("installed-wheel CLI returned an unexpected failure stage")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
