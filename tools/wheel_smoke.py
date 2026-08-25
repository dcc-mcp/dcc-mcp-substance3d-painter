"""Validate the installed wheel against the released Core install contract."""

from __future__ import annotations

import contextlib
import io
import json

from dcc_mcp_core.deployment import INSTALL_EXIT_PREFLIGHT, load_install_sop_schema
from jsonschema import Draft202012Validator

from dcc_mcp_substance3d_painter.install_cli import main


def run() -> int:
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
