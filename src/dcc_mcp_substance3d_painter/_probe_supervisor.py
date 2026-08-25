"""Private process-group supervisor for bounded installer metadata probes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional


def _write_status(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(str(temporary), str(path))


def _reap_child_for_shutdown(child: subprocess.Popen, timeout: float = 1.0) -> bool:
    """Reap the direct probe child after the process group receives SIGTERM."""
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return False
    except OSError:
        return False
    return True


def main(argv: Optional[List[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 5 or arguments[3] != "--":
        return 64
    status_path = Path(arguments[0])
    stdout_path = Path(arguments[1])
    stderr_path = Path(arguments[2])
    command = arguments[4:]
    parent_pid = os.getppid()
    child: Optional[subprocess.Popen] = None
    previous_sigterm = None

    def shutdown(signum, _frame) -> None:
        if child is not None:
            _reap_child_for_shutdown(child)
        raise SystemExit(128 + int(signum))

    if os.name == "posix":
        previous_sigterm = signal.signal(signal.SIGTERM, shutdown)
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            try:
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    close_fds=True,
                )
            except OSError as exc:
                _write_status(status_path, {"state": "launch_failed", "error_type": exc.__class__.__name__})
            else:
                returncode = child.wait()
                child = None
                stdout_file.flush()
                stderr_file.flush()
                _write_status(status_path, {"state": "completed", "returncode": int(returncode)})
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if os.getppid() != parent_pid:
                if os.name == "posix":
                    os.killpg(os.getpgrp(), signal.SIGTERM)
                return 70
            time.sleep(0.05)
        return 0
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    raise SystemExit(main())
