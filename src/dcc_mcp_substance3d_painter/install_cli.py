"""Agent-first installer entry point for Substance 3D Painter."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

import dcc_mcp_core
from dcc_mcp_core.deployment import INSTALL_EXIT_PREFLIGHT, INSTALL_SOP_SCHEMA_VERSION

from dcc_mcp_substance3d_painter.__version__ import __version__
from dcc_mcp_substance3d_painter._installer import COMMAND, DCC_TYPE, run_lifecycle


class _ArgumentFailure(ValueError):
    pass


class _LifecycleParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentFailure("Invalid lifecycle arguments.")


def _parser() -> argparse.ArgumentParser:
    parser = _LifecycleParser(prog=COMMAND)
    parser.add_argument("verb", choices=("install", "status", "verify", "uninstall", "upgrade"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dcc-path")
    parser.add_argument("--python", dest="python_path")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the Painter lifecycle command."""
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw_arguments
    try:
        args = _parser().parse_args(raw_arguments)
    except _ArgumentFailure:
        result = {
            "schema_version": INSTALL_SOP_SCHEMA_VERSION,
            "status": "failed",
            "dcc_type": DCC_TYPE,
            "adapter_version": __version__,
            "core_version": str(dcc_mcp_core.__version__),
            "steps": [{"id": "arguments", "status": "failed", "message": "Invalid lifecycle arguments."}],
            "next_steps": [],
            "receipt_path": None,
            "verify": {
                "directly_usable": False,
                "failure_stage": "arguments",
                "failure_reason": "Invalid lifecycle arguments.",
            },
        }
        if as_json:
            print(json.dumps(result, sort_keys=True))
        else:
            print("Painter lifecycle: invalid arguments", file=sys.stderr)
        return INSTALL_EXIT_PREFLIGHT
    outcome = run_lifecycle(
        args.verb,
        dcc_path=args.dcc_path,
        python_path=args.python_path,
        yes=args.yes,
        dry_run=args.dry_run,
    )
    if args.as_json:
        print(json.dumps(outcome.result, sort_keys=True))
    else:
        print(f"Painter {args.verb}: {outcome.result['status']}")
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
