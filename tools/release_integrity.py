#!/usr/bin/env python3
"""Fail-closed release source, artifact, and asset verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


def _normalize_digest(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("artifact digest must be a SHA-256 string")
    normalized = value.removeprefix("sha256:")
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError("artifact digest must be a lowercase SHA-256 string")
    return normalized


def _require_commit(value: object, description: str) -> str:
    if not isinstance(value, str) or _COMMIT_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{description} must be a lowercase 40-character commit SHA")
    return value


def verify_workflow_artifact(
    metadata: object,
    *,
    expected_id: int,
    expected_digest: str,
    expected_name: str,
    expected_run_id: int,
    expected_head_sha: str,
    expected_repository: str,
) -> None:
    """Bind one artifact API response to its reviewed workflow output."""
    if not isinstance(metadata, dict):
        raise TypeError("artifact metadata must be an object")
    if not isinstance(expected_id, int) or expected_id <= 0:
        raise ValueError("expected artifact ID must be positive")
    if not isinstance(expected_run_id, int) or expected_run_id <= 0:
        raise ValueError("expected workflow run ID must be positive")
    _require_commit(expected_head_sha, "expected workflow head")
    if _REPOSITORY_PATTERN.fullmatch(expected_repository) is None:
        raise ValueError("expected artifact repository is invalid")

    if metadata.get("id") != expected_id:
        raise ValueError("artifact ID does not match the workflow output")
    if metadata.get("name") != expected_name:
        raise ValueError("artifact name does not match the workflow output")
    if metadata.get("expired") is not False:
        raise ValueError("artifact expiry state is not the strict boolean false")
    if _normalize_digest(metadata.get("digest")) != _normalize_digest(expected_digest):
        raise ValueError("artifact digest does not match the workflow output")
    expected_url = f"https://api.github.com/repos/{expected_repository}/actions/artifacts/{expected_id}/zip"
    if metadata.get("archive_download_url") != expected_url:
        raise ValueError("artifact archive URL does not match its exact identity")

    workflow_run = metadata.get("workflow_run")
    if not isinstance(workflow_run, dict):
        raise TypeError("artifact metadata has no owning workflow run")
    if workflow_run.get("id") != expected_run_id:
        raise ValueError("artifact belongs to a different workflow run")
    if workflow_run.get("head_sha") != expected_head_sha:
        raise ValueError("artifact belongs to a different workflow head")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_regular_unlinked_file(path: Path) -> bool:
    status = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISREG(status.st_mode) and not (attributes & reparse_flag)


def distribution_names(version: str) -> tuple[str, str]:
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("distribution version must use stable semantic versioning")
    stem = f"dcc_mcp_substance3d_painter-{version}"
    return (f"{stem}-py3-none-any.whl", f"{stem}.tar.gz")


def _checksum_manifest(directory: Path, version: str) -> str:
    return "".join(
        f"{_sha256(directory / 'dist' / name)}  dist/{name}\n" for name in sorted(distribution_names(version))
    )


def write_checksum_manifest(directory: Path, version: str) -> None:
    """Write the canonical checksum manifest after validating the exact dist set."""
    dist = directory / "dist"
    expected = set(distribution_names(version))
    if not dist.is_dir() or {entry.name for entry in dist.iterdir()} != expected:
        raise ValueError("distribution set is missing files or contains extras")
    if any(not _is_regular_unlinked_file(dist / name) for name in expected):
        raise ValueError("distribution set must contain regular unlinked files")
    directory.joinpath("SHA256SUMS").write_bytes(_checksum_manifest(directory, version).encode("utf-8"))


def verify_distribution_bundle(directory: Path, *, version: str) -> None:
    """Validate the exact wheel, sdist, and canonical checksum manifest."""
    entries = list(directory.iterdir())
    if {entry.name for entry in entries} != {"dist", "SHA256SUMS"}:
        raise ValueError("distribution bundle has missing or extra root entries")
    dist = directory / "dist"
    checksum = directory / "SHA256SUMS"
    if not dist.is_dir() or dist.is_symlink():
        raise ValueError("distribution bundle dist entry must be a real directory")
    if not _is_regular_unlinked_file(checksum):
        raise ValueError("checksum manifest must be a regular unlinked file")
    expected = set(distribution_names(version))
    dist_entries = list(dist.iterdir())
    if {entry.name for entry in dist_entries} != expected:
        raise ValueError("distribution set is missing files or contains extras")
    if any(not _is_regular_unlinked_file(entry) for entry in dist_entries):
        raise ValueError("distribution set must contain regular unlinked files")
    if checksum.read_text(encoding="utf-8") != _checksum_manifest(directory, version):
        raise ValueError("checksum manifest does not match the distribution bytes")


def _validated_archive_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if not any(not member.is_dir() for member in members):
        raise ValueError("artifact archive has no regular files")
    seen: set[str] = set()
    for member in members:
        normalized = member.filename.replace("\\", "/")
        relative = PurePosixPath(normalized)
        if (
            not normalized
            or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)
            or any(_WINDOWS_DRIVE_PATTERN.match(part) for part in relative.parts)
        ):
            raise ValueError("artifact archive contains an unsafe path")
        identity = relative.as_posix().casefold()
        if identity in seen:
            raise ValueError("artifact archive contains duplicate paths")
        seen.add(identity)
        unix_mode = member.external_attr >> 16
        if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
            raise ValueError("artifact archive contains a symbolic link")
    return members


def verify_and_extract_artifact(archive_path: Path, *, expected_digest: str, output_directory: Path) -> None:
    """Verify artifact transport bytes before atomically exposing extracted files."""
    if output_directory.exists():
        raise ValueError("artifact output directory already exists")
    if _sha256(archive_path) != _normalize_digest(expected_digest):
        raise ValueError("artifact transport digest does not match its immutable identity")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validated_archive_members(archive)
            with tempfile.TemporaryDirectory(
                prefix="painter-release-artifact-", dir=output_directory.parent
            ) as temporary:
                staged = Path(temporary) / "bundle"
                staged.mkdir()
                for member in members:
                    relative = PurePosixPath(member.filename.replace("\\", "/"))
                    destination = staged.joinpath(*relative.parts)
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
                staged.replace(output_directory)
    except ValueError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ValueError("artifact archive is invalid") from exc


def verify_release_source(
    *,
    head_sha: str,
    tag_sha: str,
    release_target: str,
    expected_sha: str,
    tag_name: str,
    version: str,
) -> None:
    """Require checkout, peeled tag, and Release target to be one commit."""
    if _VERSION_PATTERN.fullmatch(version) is None or tag_name != f"v{version}":
        raise ValueError("release tag does not match the stable package version")
    for description, value in (
        ("release HEAD", head_sha),
        ("release tag source", tag_sha),
        ("release target", release_target),
        ("expected release source", expected_sha),
    ):
        _require_commit(value, description)
    if {head_sha, tag_sha, release_target} != {expected_sha}:
        raise ValueError("release source identities do not match one exact commit")


def verify_release_target(
    metadata: object,
    *,
    expected_tag: str,
    expected_source_sha: str,
    require_empty_assets: bool,
) -> None:
    """Validate the immutable identity and publication state of a GitHub Release."""
    _require_commit(expected_source_sha, "expected release source")
    if not isinstance(metadata, dict):
        raise TypeError("release metadata must be an object")
    if metadata.get("tagName") != expected_tag:
        raise ValueError("release tag does not match")
    if metadata.get("targetCommitish") != expected_source_sha:
        raise ValueError("release target does not match the exact source commit")
    if metadata.get("isDraft") is not False:
        raise ValueError("release draft state must be the strict boolean false")
    if metadata.get("isPrerelease") is not False:
        raise ValueError("release prerelease state must be the strict boolean false")
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise TypeError("release assets must be a list")
    if require_empty_assets and assets:
        raise ValueError("release already contains assets")


def _local_release_assets(directory: Path, version: str) -> dict[str, Path]:
    wheel, sdist = distribution_names(version)
    return {
        wheel: directory / "dist" / wheel,
        sdist: directory / "dist" / sdist,
        "SHA256SUMS": directory / "SHA256SUMS",
    }


def verify_published_release(
    metadata: object,
    directory: Path,
    *,
    version: str,
    expected_tag: str,
    expected_source_sha: str,
) -> None:
    """Read back exact uploaded GitHub asset names, sizes, and digests."""
    verify_distribution_bundle(directory, version=version)
    verify_release_target(
        metadata,
        expected_tag=expected_tag,
        expected_source_sha=expected_source_sha,
        require_empty_assets=False,
    )
    if not isinstance(metadata, dict) or not isinstance(metadata.get("assets"), list):
        raise TypeError("release assets must be a list")
    assets = metadata["assets"]
    by_name: dict[str, dict] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise TypeError("release asset metadata is malformed")
        name = asset["name"]
        if name in by_name:
            raise ValueError("release asset names must be unique")
        by_name[name] = asset
    local_assets = _local_release_assets(directory, version)
    if set(by_name) != set(local_assets):
        raise ValueError("release asset set has missing or extra files")
    for name, path in local_assets.items():
        asset = by_name[name]
        if asset.get("state") != "uploaded":
            raise ValueError("release asset is not terminally uploaded")
        if asset.get("size") != path.stat().st_size:
            raise ValueError("release asset size does not match local bytes")
        if _normalize_digest(asset.get("digest")) != _sha256(path):
            raise ValueError("release asset digest does not match local bytes")


def _step_identity(step: object) -> str:
    if not isinstance(step, dict):
        raise TypeError("release workflow step must be an object")
    if isinstance(step.get("name"), str):
        return step["name"]
    if isinstance(step.get("uses"), str):
        return f"uses:{step['uses']}"
    raise ValueError("release workflow step must have a reviewed name or action")


def _named_steps(job: dict) -> dict[str, dict]:
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise TypeError("release workflow job steps must be a list")
    named: dict[str, dict] = {}
    for step in steps:
        identity = _step_identity(step)
        if identity in named:
            raise ValueError("release workflow step identities must be unique per job")
        named[identity] = step
    return named


def _require_fragments(step: dict, *fragments: str) -> None:
    run = step.get("run")
    if not isinstance(run, str) or any(fragment not in run for fragment in fragments):
        raise ValueError("release workflow step is missing a reviewed command")


def validate_release_workflow(workflow: object) -> None:
    """Validate the parsed release workflow, ignoring comment-only decoys."""
    if not isinstance(workflow, dict):
        raise TypeError("release workflow must be an object")
    triggers = workflow.get("on", workflow.get(True))
    if triggers != {"push": {"branches": ["main"]}}:
        raise ValueError("release workflow must only run from main pushes")
    if workflow.get("permissions") != {}:
        raise ValueError("release workflow root permissions must be empty")
    if workflow.get("concurrency") != {
        "group": "release-${{ github.repository }}",
        "cancel-in-progress": False,
    }:
        raise ValueError("release workflow concurrency must protect an in-flight release")
    jobs = workflow.get("jobs")
    expected_job_names = {
        "release-please",
        "build-release-artifact",
        "publish-pypi",
        "attach-github-assets",
    }
    if not isinstance(jobs, dict) or set(jobs) != expected_job_names:
        raise ValueError("release workflow job set has drifted")

    release = jobs["release-please"]
    build = jobs["build-release-artifact"]
    pypi = jobs["publish-pypi"]
    github = jobs["attach-github-assets"]
    if release.get("permissions") != {"contents": "write", "pull-requests": "write"}:
        raise ValueError("release workflow release-please permissions have drifted")
    if build.get("permissions") != {"contents": "read"}:
        raise ValueError("release workflow build permissions have drifted")
    if pypi.get("permissions") != {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }:
        raise ValueError("release workflow PyPI permissions have drifted")
    if github.get("permissions") != {"actions": "read", "contents": "write"}:
        raise ValueError("release workflow GitHub permissions have drifted")
    if build.get("needs") != "release-please":
        raise ValueError("release workflow build dependency has drifted")
    if pypi.get("needs") != ["release-please", "build-release-artifact"]:
        raise ValueError("release workflow PyPI dependency has drifted")
    if github.get("needs") != [
        "release-please",
        "build-release-artifact",
        "publish-pypi",
    ]:
        raise ValueError("release workflow GitHub dependency has drifted")

    expected_steps = {
        "release-please": ["uses:googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7"],
        "build-release-artifact": [
            "uses:actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            "uses:actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
            "Verify release source",
            "Install build tools",
            "Build distributions once",
            "Check distribution metadata",
            "Stage exact distribution bundle",
            "Upload immutable release artifact",
        ],
        "publish-pypi": [
            "uses:actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            "Download verified release artifact",
            "Re-fetch and verify before PyPI upload",
            "Upload verified distributions to PyPI",
        ],
        "attach-github-assets": [
            "uses:actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            "Download verified release artifact",
            "Re-fetch and verify before GitHub upload",
            "Upload GitHub release assets",
            "Verify published GitHub release assets",
        ],
    }
    named_by_job: dict[str, dict[str, dict]] = {}
    for name, expected in expected_steps.items():
        steps = jobs[name].get("steps")
        if not isinstance(steps, list) or [_step_identity(step) for step in steps] != expected:
            raise ValueError(f"release workflow step order has drifted in job {name}")
        named_by_job[name] = _named_steps(jobs[name])

    build_steps = named_by_job["build-release-artifact"]
    _require_fragments(
        build_steps["Verify release source"],
        "verify-source",
        "verify-release-target",
        "--require-empty-assets",
    )
    _require_fragments(
        build_steps["Stage exact distribution bundle"],
        "write-checksums",
        "verify-bundle",
    )
    upload = build_steps["Upload immutable release artifact"]
    if upload.get("id") != "upload" or upload.get("with") != {
        "name": "release-distributions",
        "path": "release-bundle",
        "if-no-files-found": "error",
        "retention-days": 7,
    }:
        raise ValueError("release workflow artifact upload identity has drifted")

    for job_name, before_name in (
        ("publish-pypi", "Re-fetch and verify before PyPI upload"),
        ("attach-github-assets", "Re-fetch and verify before GitHub upload"),
    ):
        steps = named_by_job[job_name]
        _require_fragments(
            steps["Download verified release artifact"],
            "actions/artifacts/$ARTIFACT_ID",
            "actions/artifacts/$ARTIFACT_ID/zip",
            "verify-artifact",
            "verify-extract",
            "verify-bundle",
        )
        _require_fragments(
            steps[before_name],
            "actions/artifacts/$ARTIFACT_ID",
            "verify-artifact",
            "gh release view",
            "verify-release-target",
            "verify-bundle",
        )

    pypi_upload = named_by_job["publish-pypi"]["Upload verified distributions to PyPI"]
    if (
        pypi_upload.get("uses") != ("pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33")
        or pypi_upload.get("with", {}).get("packages-dir") != "release-bundle/dist"
    ):
        raise ValueError("release workflow PyPI upload has drifted")
    github_upload = named_by_job["attach-github-assets"]["Upload GitHub release assets"]
    _require_fragments(
        github_upload,
        "gh release upload",
        "release-bundle/dist/*",
        "release-bundle/SHA256SUMS",
    )
    if "--clobber" in github_upload["run"]:
        raise ValueError("release workflow must not overwrite existing assets")
    _require_fragments(
        named_by_job["attach-github-assets"]["Verify published GitHub release assets"],
        "gh release view",
        "verify-published-release",
    )


def validate_lock_sync_workflow(workflow: object) -> None:
    """Validate the parsed release-lock synchronizer and its mutation lease."""
    if not isinstance(workflow, dict):
        raise TypeError("release lock workflow must be an object")
    triggers = workflow.get("on", workflow.get(True))
    expected_paths = [
        ".release-please-manifest.json",
        "pyproject.toml",
        "src/dcc_mcp_substance3d_painter/__version__.py",
        "uv.lock",
    ]
    if triggers != {
        "pull_request": {
            "types": ["opened", "reopened", "synchronize"],
            "branches": ["main"],
            "paths": expected_paths,
        }
    }:
        raise ValueError("release lock workflow trigger has drifted")
    if workflow.get("permissions") != {"contents": "write", "pull-requests": "read"}:
        raise ValueError("release lock workflow permissions have drifted")
    if workflow.get("concurrency") != {
        "group": "release-lock-${{ github.event.pull_request.number }}",
        "cancel-in-progress": False,
    }:
        raise ValueError("release lock workflow concurrency has drifted")
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or set(jobs) != {"sync-release-lock"}:
        raise ValueError("release lock workflow job set has drifted")
    job = jobs["sync-release-lock"]
    condition = job.get("if")
    required_condition_fragments = (
        "github.event.pull_request.head.repo.full_name == github.repository",
        "github.event.pull_request.base.ref == 'main'",
        "github.event.pull_request.head.ref == 'release-please--branches--main--components--dcc-mcp-substance3d-painter'",
        "startsWith(github.event.pull_request.title, 'chore(main): release ')",
    )
    if not isinstance(condition, str) or any(fragment not in condition for fragment in required_condition_fragments):
        raise ValueError("release lock workflow job guard has drifted")
    steps = job.get("steps")
    expected_steps = [
        "uses:actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
        "uses:actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
        "Install pinned uv",
        "Validate exact release PR lease",
        "Synchronize root uv.lock",
        "Verify only reviewed lock change",
        "Commit and push exact uv.lock",
    ]
    if not isinstance(steps, list) or [_step_identity(step) for step in steps] != expected_steps:
        raise ValueError("release lock workflow step order has drifted")
    named = _named_steps(job)
    checkout = named[expected_steps[0]]
    if checkout.get("with") != {
        "ref": "${{ github.event.pull_request.head.sha }}",
        "fetch-depth": 0,
        "fetch-tags": False,
        "token": "${{ secrets.PERSONAL_ACCESS_TOKEN || github.token }}",
    }:
        raise ValueError("release lock workflow checkout identity has drifted")
    if named["Install pinned uv"].get("run") != 'python -m pip install "uv==0.11.19"':
        raise ValueError("release lock workflow uv version has drifted")
    _require_fragments(
        named["Validate exact release PR lease"],
        "git ls-remote --exit-code --heads origin",
        "refs/heads/main",
        "git fetch --no-tags origin",
        "git merge-base",
        "release PR contains an unreviewed file set",
        "verify-version-anchors --root .",
        "git status --porcelain=v1 --untracked-files=all",
    )
    if named["Synchronize root uv.lock"].get("run") != "uv lock":
        raise ValueError("release lock workflow generation command has drifted")
    _require_fragments(
        named["Verify only reviewed lock change"],
        "verify-version-anchors --root . --require-lock",
        'changed not in ([], [" M uv.lock"])',
    )
    push = named["Commit and push exact uv.lock"]
    _require_fragments(
        push,
        "git ls-remote --exit-code --heads origin",
        'test "$remote_head" = "$EXPECTED_HEAD_SHA"',
        'test "$remote_base" = "$EXPECTED_BASE_SHA"',
        'test "$(git status --porcelain=v1 --untracked-files=all)" = " M uv.lock"',
        "git add -- uv.lock",
        'test "$(git diff --cached --name-only)" = "uv.lock"',
        'git config user.name "loonghao"',
        'git config user.email "hal.long@outlook.com"',
        'git commit -m "chore(ci): sync release lock"',
        'git push --force-with-lease="refs/heads/${HEAD_REF}:${EXPECTED_HEAD_SHA}"',
        'origin "HEAD:refs/heads/${HEAD_REF}"',
    )


def verify_version_anchors(root: Path, *, require_lock: bool) -> str:
    """Verify release-please anchors and, when requested, the editable lock root."""
    try:
        manifest = json.loads(root.joinpath(".release-please-manifest.json").read_text(encoding="utf-8"))
        pyproject = root.joinpath("pyproject.toml").read_text(encoding="utf-8")
        version_module = root.joinpath("src", "dcc_mcp_substance3d_painter", "__version__.py").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("release version anchors are unreadable") from exc
    project_versions = re.findall(r'(?m)^version = "([^"]+)"$', pyproject)
    module_versions = re.findall(r'(?m)^__version__ = "([^"]+)"\s+# x-release-please-version$', version_module)
    if len(project_versions) != 1 or len(module_versions) != 1:
        raise ValueError("release version anchors must each contain one version")
    version = project_versions[0]
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("release version anchor must use stable semantic versioning")
    if manifest != {".": version} or module_versions != [version]:
        raise ValueError("release version anchors do not match")
    if not require_lock:
        return version

    try:
        lock = root.joinpath("uv.lock").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("release lock is unreadable") from exc
    roots: list[str] = []
    for block in lock.split("[[package]]"):
        lines = {line.strip() for line in block.splitlines()}
        if 'name = "dcc-mcp-substance3d-painter"' in lines and 'source = { editable = "." }' in lines:
            versions = [
                match.group(1) for line in lines if (match := re.fullmatch(r'version = "([^"]+)"', line)) is not None
            ]
            if len(versions) != 1:
                raise ValueError("release lock root has an invalid version")
            roots.append(versions[0])
    if roots != [version] or lock.count('source = { editable = "." }') != 1:
        raise ValueError("release lock must contain one synchronized editable root")
    return version


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("release metadata is not valid JSON") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    source = commands.add_parser("verify-source")
    for name in ("head-sha", "tag-sha", "release-target", "expected-sha", "tag-name", "version"):
        source.add_argument(f"--{name}", required=True)

    artifact = commands.add_parser("verify-artifact")
    artifact.add_argument("--metadata", required=True, type=Path)
    artifact.add_argument("--expected-id", required=True, type=int)
    artifact.add_argument("--expected-digest", required=True)
    artifact.add_argument("--expected-name", required=True)
    artifact.add_argument("--expected-run-id", required=True, type=int)
    artifact.add_argument("--expected-head-sha", required=True)
    artifact.add_argument("--expected-repository", required=True)

    extract = commands.add_parser("verify-extract")
    extract.add_argument("--archive", required=True, type=Path)
    extract.add_argument("--expected-digest", required=True)
    extract.add_argument("--output", required=True, type=Path)

    for name in ("write-checksums", "verify-bundle"):
        command = commands.add_parser(name)
        command.add_argument("--directory", required=True, type=Path)
        command.add_argument("--version", required=True)

    target = commands.add_parser("verify-release-target")
    target.add_argument("--metadata", required=True, type=Path)
    target.add_argument("--expected-tag", required=True)
    target.add_argument("--expected-source-sha", required=True)
    target.add_argument("--require-empty-assets", action="store_true")

    published = commands.add_parser("verify-published-release")
    published.add_argument("--metadata", required=True, type=Path)
    published.add_argument("--directory", required=True, type=Path)
    published.add_argument("--version", required=True)
    published.add_argument("--expected-tag", required=True)
    published.add_argument("--expected-source-sha", required=True)

    anchors = commands.add_parser("verify-version-anchors")
    anchors.add_argument("--root", required=True, type=Path)
    anchors.add_argument("--require-lock", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "verify-source":
            verify_release_source(
                head_sha=args.head_sha,
                tag_sha=args.tag_sha,
                release_target=args.release_target,
                expected_sha=args.expected_sha,
                tag_name=args.tag_name,
                version=args.version,
            )
        elif args.command == "verify-artifact":
            verify_workflow_artifact(
                _load_json(args.metadata),
                expected_id=args.expected_id,
                expected_digest=args.expected_digest,
                expected_name=args.expected_name,
                expected_run_id=args.expected_run_id,
                expected_head_sha=args.expected_head_sha,
                expected_repository=args.expected_repository,
            )
        elif args.command == "verify-extract":
            verify_and_extract_artifact(
                args.archive,
                expected_digest=args.expected_digest,
                output_directory=args.output,
            )
        elif args.command == "write-checksums":
            write_checksum_manifest(args.directory, args.version)
        elif args.command == "verify-bundle":
            verify_distribution_bundle(args.directory, version=args.version)
        elif args.command == "verify-release-target":
            verify_release_target(
                _load_json(args.metadata),
                expected_tag=args.expected_tag,
                expected_source_sha=args.expected_source_sha,
                require_empty_assets=args.require_empty_assets,
            )
        elif args.command == "verify-published-release":
            verify_published_release(
                _load_json(args.metadata),
                args.directory,
                version=args.version,
                expected_tag=args.expected_tag,
                expected_source_sha=args.expected_source_sha,
            )
        elif args.command == "verify-version-anchors":
            verify_version_anchors(args.root, require_lock=args.require_lock)
        else:  # pragma: no cover - argparse owns command selection.
            raise ValueError("unsupported release integrity command")
    except (OSError, TypeError, ValueError) as exc:
        print(f"release integrity verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
