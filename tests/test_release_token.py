"""Release PR creation requires the organization credential so CI can start."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tools.release_integrity import validate_release_workflow

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
TOKEN = "${{ secrets.PERSONAL_ACCESS_TOKEN }}"
EXPECTED_JOB_CONDITION = "github.ref == 'refs/heads/main'"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _preflight():
    job = _workflow()["jobs"]["release-please"]
    assert " ".join(job.get("if", "").split()) == EXPECTED_JOB_CONDITION
    steps = job["steps"]
    actions = [step for step in steps if step.get("uses", "").startswith("googleapis/release-please-action@")]
    assert len(actions) == 1
    action = actions[0]
    assert steps.index(action) == 1, "Token preflight must immediately precede release-please"
    assert action["id"] == "release"
    assert action["with"] == {
        "token": TOKEN,
        "config-file": "release-please-config.json",
        "manifest-file": ".release-please-manifest.json",
    }
    assert "if" not in action
    preflight = steps[0]
    assert preflight["name"] == "Require organization release token"
    assert preflight["shell"] == "bash"
    assert preflight["env"] == {"PERSONAL_ACCESS_TOKEN": TOKEN}
    assert "if" not in preflight
    assert "continue-on-error" not in preflight
    assert "continue-on-error" not in job
    assert "continue-on-error" not in action
    return preflight["run"]


def _bash():
    if os.name == "nt":
        # Git Bash is available on Actions; the PATH bash.exe can be a WSL launcher.
        git = shutil.which("git")
        assert git, "Git for Windows is required to test the bash preflight"
        for parent in Path(git).resolve().parents:
            candidate = parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
        pytest.fail("Cannot find Git for Windows' bin/bash.exe")
    bash = shutil.which("bash")
    assert bash, "Bash is required to test the release workflow"
    return bash


def test_release_please_requires_the_organization_token_before_mutation():
    _preflight()


@pytest.mark.parametrize("token", [None, "", "synthetic-token-do-not-print", "synthetic token\nsecond line"])
def test_release_token_preflight_fails_closed_without_leaking(token):
    env = os.environ.copy()
    env.pop("PERSONAL_ACCESS_TOKEN", None)
    if token is not None:
        env["PERSONAL_ACCESS_TOKEN"] = token
    result = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", _preflight()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if token:
        assert result.returncode == 0, result.stderr
        assert token not in result.stdout + result.stderr
        assert result.stdout + result.stderr == ""
    else:
        assert result.returncode != 0
        assert "::error::" in result.stdout + result.stderr
        assert "PERSONAL_ACCESS_TOKEN" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-preflight",
        "fallback",
        "late-preflight",
        "skip-preflight",
        "ignored-failure",
        "wrong-token",
        "bypass-script",
        "moved-identity",
    ],
)
def test_strict_release_guard_rejects_token_and_identity_drift(mutation):
    document = _workflow()
    steps = document["jobs"]["release-please"]["steps"]
    if mutation == "missing-preflight":
        steps.pop(0)
    elif mutation == "fallback":
        steps[1]["with"]["token"] = "${{ secrets.PERSONAL_ACCESS_TOKEN || github.token }}"
    elif mutation == "late-preflight":
        steps[0], steps[1] = steps[1], steps[0]
    elif mutation == "skip-preflight":
        steps[0]["if"] = "false"
    elif mutation == "ignored-failure":
        steps[0]["continue-on-error"] = "true"
    elif mutation == "wrong-token":
        steps[0]["env"]["PERSONAL_ACCESS_TOKEN"] = "${{ github.token }}"
    elif mutation == "bypass-script":
        steps[0]["run"] = "true"
    elif mutation == "moved-identity":
        build = document["jobs"]["build-release-artifact"]["steps"]
        build[2], build[3] = build[3], build[2]
    with pytest.raises(ValueError):
        validate_release_workflow(document)
