from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from tools.release_integrity import (
    validate_lock_sync_workflow,
    validate_release_workflow,
    verify_version_anchors,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
LOCK_SYNC_WORKFLOW = ROOT / ".github" / "workflows" / "release-lock-sync.yml"


def _parsed_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_release_workflow_has_one_verified_artifact_handoff() -> None:
    validate_release_workflow(_parsed_workflow())


def test_release_workflow_rejects_comment_decoys_and_job_drift() -> None:
    document = _parsed_workflow()
    build_steps = document["jobs"]["build-release-artifact"]["steps"]
    document["jobs"]["build-release-artifact"]["steps"] = [
        step for step in build_steps if step.get("name") != "Verify release source"
    ]
    comment_decoy = yaml.safe_dump(document) + "\n# name: Verify release source\n"
    with pytest.raises(ValueError, match="workflow|step|job"):
        validate_release_workflow(yaml.safe_load(comment_decoy))

    document = _parsed_workflow()
    changed = copy.deepcopy(document)
    attach = changed["jobs"]["attach-github-assets"]["steps"]
    upload = next(step for step in attach if step.get("name") == "Upload GitHub release assets")
    attach.remove(upload)
    changed["jobs"]["publish-pypi"]["steps"].append(upload)
    with pytest.raises(ValueError, match="workflow|step|job"):
        validate_release_workflow(changed)


def test_release_lock_sync_is_exact_and_fail_closed() -> None:
    document = yaml.safe_load(LOCK_SYNC_WORKFLOW.read_text(encoding="utf-8"))
    validate_lock_sync_workflow(document)

    changed = copy.deepcopy(document)
    changed["jobs"]["sync-release-lock"]["steps"][-1]["run"] = "git push origin HEAD"
    with pytest.raises(ValueError, match="workflow|lease|push"):
        validate_lock_sync_workflow(changed)

    changed = copy.deepcopy(document)
    changed["jobs"]["sync-release-lock"]["steps"].insert(-1, {"name": "Comment decoy", "run": "# git add -- uv.lock"})
    with pytest.raises(ValueError, match="workflow|step|job"):
        validate_lock_sync_workflow(changed)


def test_release_version_anchors_require_one_synchronized_editable_lock_root(tmp_path: Path) -> None:
    for relative in (
        ".release-please-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "src/dcc_mcp_substance3d_painter/__version__.py",
    ):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    verify_version_anchors(tmp_path, require_lock=True)
    lock = tmp_path / "uv.lock"
    original = lock.read_text(encoding="utf-8")
    lock.write_text(
        original.replace(
            'name = "dcc-mcp-substance3d-painter"\nversion = "0.4.0"',
            'name = "dcc-mcp-substance3d-painter"\nversion = "0.4.1"',
            1,
        ),
        encoding="utf-8",
    )
    verify_version_anchors(tmp_path, require_lock=False)
    with pytest.raises(ValueError, match="version|lock"):
        verify_version_anchors(tmp_path, require_lock=True)

    lock.write_text(
        original
        + '\n[[package]]\nname = "dcc-mcp-substance3d-painter"\nversion = "0.4.0"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="lock|root"):
        verify_version_anchors(tmp_path, require_lock=True)
