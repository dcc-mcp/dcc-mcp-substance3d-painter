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


def _mutate_execution_control(step: dict, mutation: str) -> None:
    if mutation == "custom-shell":
        step["shell"] = "bash {0}; echo bypass"
    elif mutation == "continue-on-error":
        step["continue-on-error"] = True
    elif mutation == "if":
        step["if"] = "${{ always() }}"
    elif mutation == "working-directory":
        step["working-directory"] = "/tmp/unreviewed"
    elif mutation == "env":
        step.setdefault("env", {})["UNREVIEWED"] = "1"
    elif mutation == "key":
        step["timeout-minutes"] = 1
    elif mutation == "run":
        step["run"] = "true"
    else:
        raise AssertionError(f"unknown workflow mutation: {mutation}")


def test_release_workflow_has_one_verified_artifact_handoff() -> None:
    validate_release_workflow(_parsed_workflow())


def test_release_workflow_rejects_comment_decoys_and_job_drift() -> None:
    canonical = _parsed_workflow()
    canonical_step = canonical["jobs"]["publish-pypi"]["steps"][2]
    canonical_step["run"] = ("# harmless comment\n" + canonical_step["run"]).replace("\n", "\r\n")
    validate_release_workflow(canonical)

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

    document = _parsed_workflow()
    before_upload = document["jobs"]["publish-pypi"]["steps"][2]
    before_upload["run"] = before_upload["run"].replace(
        "python -B tools/release_integrity.py verify-live-tag",
        "true # python -B tools/release_integrity.py verify-live-tag",
    )
    with pytest.raises(ValueError, match="workflow|command|tag"):
        validate_release_workflow(document)

    document = _parsed_workflow()
    build_source = document["jobs"]["build-release-artifact"]["steps"][2]
    build_source["run"] = build_source["run"].replace(
        "python -B tools/release_integrity.py verify-source",
        "true # python -B tools/release_integrity.py verify-source",
    )
    with pytest.raises(ValueError, match="workflow|command|source"):
        validate_release_workflow(document)


@pytest.mark.parametrize(
    "mutation",
    ("custom-shell", "continue-on-error", "if", "working-directory", "env", "key", "run"),
)
def test_release_workflow_rejects_run_step_execution_control_drift(mutation: str) -> None:
    document = _parsed_workflow()
    step = document["jobs"]["publish-pypi"]["steps"][2]
    _mutate_execution_control(step, mutation)

    with pytest.raises(ValueError, match="workflow|step|control"):
        validate_release_workflow(document)


@pytest.mark.parametrize(
    "mutation",
    ("custom-shell", "continue-on-error", "if", "working-directory", "env", "key", "run"),
)
def test_release_workflow_rejects_action_step_execution_control_drift(mutation: str) -> None:
    document = _parsed_workflow()
    step = document["jobs"]["publish-pypi"]["steps"][0]
    _mutate_execution_control(step, mutation)

    with pytest.raises(ValueError, match="workflow|step|control"):
        validate_release_workflow(document)


def test_release_workflow_rejects_shell_comment_decoy() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    changed = source.replace("        shell: bash\n", "        # shell: bash\n", 1)
    assert changed != source

    with pytest.raises(ValueError, match="workflow|step|control"):
        validate_release_workflow(yaml.safe_load(changed))


def test_release_workflows_bind_reviewed_timeouts() -> None:
    document = _parsed_workflow()
    validate_release_workflow(document)
    for name in document["jobs"]:
        changed = copy.deepcopy(document)
        changed["jobs"][name].pop("timeout-minutes", None)
        with pytest.raises(ValueError, match="workflow|timeout"):
            validate_release_workflow(changed)

    lock_document = yaml.safe_load(LOCK_SYNC_WORKFLOW.read_text(encoding="utf-8"))
    validate_lock_sync_workflow(lock_document)
    lock_document["jobs"]["sync-release-lock"].pop("timeout-minutes", None)
    with pytest.raises(ValueError, match="workflow|timeout"):
        validate_lock_sync_workflow(lock_document)


def test_release_lock_sync_is_exact_and_fail_closed() -> None:
    document = yaml.safe_load(LOCK_SYNC_WORKFLOW.read_text(encoding="utf-8"))
    validate_lock_sync_workflow(document)

    canonical = copy.deepcopy(document)
    canonical_push = canonical["jobs"]["sync-release-lock"]["steps"][-1]
    canonical_push["run"] = ("# harmless comment\n" + canonical_push["run"]).replace("\n", "\r\n")
    validate_lock_sync_workflow(canonical)

    changed = copy.deepcopy(document)
    changed["jobs"]["sync-release-lock"]["steps"][-1]["run"] = "git push origin HEAD"
    with pytest.raises(ValueError, match="workflow|lease|push"):
        validate_lock_sync_workflow(changed)

    changed = copy.deepcopy(document)
    changed["jobs"]["sync-release-lock"]["steps"].insert(-1, {"name": "Comment decoy", "run": "# git add -- uv.lock"})
    with pytest.raises(ValueError, match="workflow|step|job"):
        validate_lock_sync_workflow(changed)

    changed = copy.deepcopy(document)
    push = changed["jobs"]["sync-release-lock"]["steps"][-1]
    push["run"] = push["run"].replace(
        "python -B tools/release_integrity.py verify-release-pr",
        "true # python -B tools/release_integrity.py verify-release-pr",
    )
    push["run"] += "\ngit push --force origin HEAD\n"
    with pytest.raises(ValueError, match="workflow|lease|push|command"):
        validate_lock_sync_workflow(changed)


@pytest.mark.parametrize(
    "mutation",
    ("custom-shell", "continue-on-error", "if", "working-directory", "env", "key", "run"),
)
def test_release_lock_sync_rejects_run_step_execution_control_drift(mutation: str) -> None:
    document = yaml.safe_load(LOCK_SYNC_WORKFLOW.read_text(encoding="utf-8"))
    step = document["jobs"]["sync-release-lock"]["steps"][-1]
    _mutate_execution_control(step, mutation)

    with pytest.raises(ValueError, match="workflow|step|control"):
        validate_lock_sync_workflow(document)


@pytest.mark.parametrize(
    "mutation",
    ("custom-shell", "continue-on-error", "if", "working-directory", "env", "key", "run"),
)
def test_release_lock_sync_rejects_action_step_execution_control_drift(mutation: str) -> None:
    document = yaml.safe_load(LOCK_SYNC_WORKFLOW.read_text(encoding="utf-8"))
    step = document["jobs"]["sync-release-lock"]["steps"][0]
    _mutate_execution_control(step, mutation)

    with pytest.raises(ValueError, match="workflow|step|control"):
        validate_lock_sync_workflow(document)


def test_release_lock_sync_rejects_shell_comment_decoy() -> None:
    source = LOCK_SYNC_WORKFLOW.read_text(encoding="utf-8")
    changed = source.replace("        shell: bash\n", "        # shell: bash\n", 1)
    assert changed != source

    with pytest.raises(ValueError, match="workflow|step|control"):
        validate_lock_sync_workflow(yaml.safe_load(changed))


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


def test_release_version_anchors_reject_non_regular_and_linked_files(tmp_path: Path) -> None:
    relatives = (
        ".release-please-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "src/dcc_mcp_substance3d_painter/__version__.py",
    )

    def copy_anchors(root: Path) -> None:
        for relative in relatives:
            source = ROOT / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    for index, relative in enumerate(relatives):
        case = tmp_path / f"directory-{index}"
        copy_anchors(case)
        target = case / relative
        target.unlink()
        target.mkdir()
        with pytest.raises(ValueError, match="anchor|lock|regular"):
            verify_version_anchors(case, require_lock=True)

    for index, relative in enumerate(relatives):
        case = tmp_path / f"symlink-{index}"
        copy_anchors(case)
        target = case / relative
        real = target.with_name(f"{target.name}.real")
        target.replace(real)
        try:
            target.symlink_to(real.name)
        except OSError:
            pytest.skip("symbolic links are unavailable on this runner")
        with pytest.raises(ValueError, match="anchor|lock|regular"):
            verify_version_anchors(case, require_lock=True)
