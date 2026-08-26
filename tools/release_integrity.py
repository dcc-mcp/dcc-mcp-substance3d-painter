#!/usr/bin/env python3
"""Fail-closed release source, artifact, and asset verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import shutil
import stat
import sys
import tempfile
import unicodedata
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


def _require_positive_integer(value: object, description: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{description} must be a positive non-boolean integer")
    return value


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
    _require_positive_integer(expected_id, "expected artifact ID")
    _require_positive_integer(expected_run_id, "expected workflow run ID")
    _require_commit(expected_head_sha, "expected workflow head")
    if _REPOSITORY_PATTERN.fullmatch(expected_repository) is None:
        raise ValueError("expected artifact repository is invalid")

    artifact_id = metadata.get("id")
    _require_positive_integer(artifact_id, "artifact ID")
    if artifact_id != expected_id:
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
    workflow_run_id = workflow_run.get("id")
    _require_positive_integer(workflow_run_id, "artifact workflow run ID")
    if workflow_run_id != expected_run_id:
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


def _validated_archive_members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    members = archive.infolist()
    if not any(not member.is_dir() for member in members):
        raise ValueError("artifact archive has no regular files")
    seen: set[str] = set()
    validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    for member in members:
        normalized = unicodedata.normalize("NFC", member.filename.replace("\\", "/"))
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
        validated.append((member, relative))
    return validated


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
                for member, relative in members:
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


def verify_live_tag_ref(refs: str, *, expected_tag: str, expected_source_sha: str) -> None:
    """Verify a fresh ls-remote result resolves and peels one exact release tag."""
    _require_commit(expected_source_sha, "expected release source")
    if re.fullmatch(r"v\d+\.\d+\.\d+", expected_tag) is None:
        raise ValueError("release tag name is invalid")
    direct_ref = f"refs/tags/{expected_tag}"
    peeled_ref = f"{direct_ref}^{{}}"
    resolved: dict[str, str] = {}
    for line in refs.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or parts[1] not in {direct_ref, peeled_ref} or parts[1] in resolved:
            raise ValueError("release tag resolution is malformed or ambiguous")
        _require_commit(parts[0], "resolved release tag object")
        resolved[parts[1]] = parts[0]
    if set(resolved) not in ({direct_ref}, {direct_ref, peeled_ref}):
        raise ValueError("release tag resolution is incomplete")
    resolved_source = resolved.get(peeled_ref, resolved[direct_ref])
    if resolved_source != expected_source_sha:
        raise ValueError("live release tag does not resolve to the exact source commit")


def verify_release_pr_snapshot(
    metadata: object,
    detail: object,
    files: object,
    *,
    expected_number: int,
    expected_version: str,
    expected_base_sha: str,
    expected_head_sha: str,
    expected_head_ref: str,
    expected_repository: str,
) -> None:
    """Bind a last-moment release lock mutation to one exact open PR."""
    _require_positive_integer(expected_number, "expected release PR number")
    _require_commit(expected_base_sha, "expected release PR base")
    _require_commit(expected_head_sha, "expected release PR head")
    if _VERSION_PATTERN.fullmatch(expected_version) is None:
        raise ValueError("expected release PR version is invalid")
    if _REPOSITORY_PATTERN.fullmatch(expected_repository) is None:
        raise ValueError("expected release PR repository is invalid")
    if not isinstance(expected_head_ref, str) or expected_head_ref != (
        "release-please--branches--main--components--dcc-mcp-substance3d-painter"
    ):
        raise ValueError("expected release PR branch is invalid")
    if not isinstance(metadata, list) or len(metadata) != 1 or not isinstance(metadata[0], dict):
        raise ValueError("release PR lookup must return exactly one open PR")
    if not isinstance(detail, dict):
        raise TypeError("release PR detail metadata is malformed")
    for item in (metadata[0], detail):
        _require_positive_integer(item.get("number"), "release PR number")
        if item.get("number") != expected_number or item.get("state") != "open":
            raise ValueError("release PR identity or state changed before mutation")
        if item.get("title") != f"chore(main): release {expected_version}":
            raise ValueError("release PR title changed before mutation")

        head = item.get("head")
        base = item.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise TypeError("release PR head or base metadata is malformed")
        head_repo = head.get("repo")
        base_repo = base.get("repo")
        if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
            raise TypeError("release PR repository metadata is malformed")
        if head_repo.get("full_name") != expected_repository or base_repo.get("full_name") != expected_repository:
            raise ValueError("release PR must not cross a repository boundary")
        if head.get("ref") != expected_head_ref or head.get("sha") != expected_head_sha:
            raise ValueError("release PR head changed before mutation")
        if base.get("ref") != "main" or base.get("sha") != expected_base_sha:
            raise ValueError("release PR base changed before mutation")

    if not isinstance(files, list) or any(not isinstance(entry, dict) for entry in files):
        raise TypeError("release PR file metadata is malformed")
    names = [entry.get("filename") for entry in files]
    if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError("release PR file metadata is malformed or duplicated")
    changed_files = detail.get("changed_files")
    _require_positive_integer(changed_files, "release PR changed file count")
    if changed_files != len(names):
        raise ValueError("release PR file snapshot is incomplete")
    required = {
        ".release-please-manifest.json",
        "CHANGELOG.md",
        "pyproject.toml",
        "src/dcc_mcp_substance3d_painter/__version__.py",
    }
    if not required.issubset(names) or not set(names).issubset(required | {"uv.lock"}):
        raise ValueError("release PR file set changed before mutation")


def verify_release_paths(paths: str) -> None:
    """Validate the exact path boundary of a generated release PR."""
    names = paths.splitlines()
    if len(names) != len(set(names)) or any(not name for name in names):
        raise ValueError("release PR path list is malformed or duplicated")
    required = {
        ".release-please-manifest.json",
        "CHANGELOG.md",
        "pyproject.toml",
        "src/dcc_mcp_substance3d_painter/__version__.py",
    }
    if not required.issubset(names) or not set(names).issubset(required | {"uv.lock"}):
        raise ValueError("release PR contains an unreviewed file set")


def verify_lock_status(status: str) -> None:
    """Allow only a clean tree or one unstaged root lock rewrite."""
    if status.splitlines() not in ([], [" M uv.lock"]):
        raise ValueError("uv lock synchronization changed an unreviewed path")


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


def _reviewed_run_step(
    name: str,
    *,
    shell: str | None = None,
    env: dict[str, object] | None = None,
) -> dict[str, object]:
    controls: dict[str, object] = {"name": name}
    if shell is not None:
        controls["shell"] = shell
    if env is not None:
        controls["env"] = env
    return controls


def _reviewed_action_step(
    uses: str,
    *,
    name: str | None = None,
    step_id: str | None = None,
    with_: dict[str, object] | None = None,
) -> dict[str, object]:
    controls: dict[str, object] = {"uses": uses}
    if name is not None:
        controls["name"] = name
    if step_id is not None:
        controls["id"] = step_id
    if with_ is not None:
        controls["with"] = with_
    return controls


def _require_reviewed_step_controls(
    named: dict[str, dict],
    expected: dict[str, dict[str, object]],
) -> None:
    if set(named) != set(expected):
        raise ValueError("release workflow reviewed step control set has drifted")
    for identity, reviewed in expected.items():
        step = named[identity]
        if "uses" in reviewed:
            actual = step
        else:
            if not isinstance(step.get("run"), str):
                raise ValueError(f"release workflow run step has drifted: {identity}")
            actual = {key: value for key, value in step.items() if key != "run"}
        if actual != reviewed:
            raise ValueError(f"release workflow step controls have drifted: {identity}")


def _shell_commands(step: dict) -> list[tuple[str, ...]]:
    run = step.get("run")
    if not isinstance(run, str):
        raise ValueError("release workflow step has no reviewed command block")
    commands: list[tuple[str, ...]] = []
    pending = ""
    heredoc: str | None = None
    for raw_line in run.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw_line.strip()
        if heredoc is not None:
            if stripped == heredoc:
                heredoc = None
            continue
        if not stripped or stripped.startswith("#"):
            continue
        logical = f"{pending} {stripped}".strip() if pending else stripped
        if logical.endswith("\\"):
            pending = logical[:-1].rstrip()
            continue
        pending = ""
        lexer = shlex.shlex(logical, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens = tuple(lexer)
        except ValueError as exc:
            raise ValueError("release workflow shell command is malformed") from exc
        if tokens:
            commands.append(tokens)
        match = re.search(r"<<[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*$", logical)
        if match is not None:
            heredoc = match.group(1)
    if pending or heredoc is not None:
        raise ValueError("release workflow shell command block is incomplete")
    return commands


def _executable_text(step: dict) -> str:
    return "\n".join(" ".join(command) for command in _shell_commands(step))


def _command_digest(step: dict) -> str:
    canonical = json.dumps(_shell_commands(step), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_reviewed_command_digests(named: dict[str, dict], expected: dict[str, str]) -> None:
    runnable = {name: step for name, step in named.items() if "run" in step}
    if set(runnable) != set(expected):
        raise ValueError("release workflow reviewed command set has drifted")
    for name, digest in expected.items():
        if _command_digest(runnable[name]) != digest:
            raise ValueError(f"release workflow reviewed command block has drifted: {name}")


def _require_fragments(step: dict, *fragments: str) -> None:
    executable = _executable_text(step)
    if any(fragment not in executable for fragment in fragments):
        raise ValueError("release workflow step is missing a reviewed command")


def _unwrapped_command(command: tuple[str, ...]) -> tuple[str, ...]:
    timeout_prefix = ("timeout", "--signal=TERM", "--kill-after=10s")
    if len(command) >= 5 and command[:3] == timeout_prefix and re.fullmatch(r"\d+s", command[3]):
        return command[4:]
    return command


def _require_bounded_commands(step: dict, *prefixes: tuple[str, ...]) -> None:
    commands = _shell_commands(step)
    for prefix in prefixes:
        matches = [command for command in commands if _unwrapped_command(command)[: len(prefix)] == prefix]
        if not matches or any(_unwrapped_command(command) == command for command in matches):
            raise ValueError("release workflow external command is missing a reviewed timeout")


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
    if any(job.get("timeout-minutes") != 15 for job in (release, build, pypi, github)):
        raise ValueError("release workflow job timeout has drifted")
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

    checkout_action = "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
    setup_python_action = "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
    release_please_action = "googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7"
    upload_artifact_action = "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    pypi_publish_action = "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
    source_env = {
        "GH_TOKEN": "${{ github.token }}",
        "SOURCE_SHA": "${{ needs.release-please.outputs.source_sha }}",
        "TAG_NAME": "${{ needs.release-please.outputs.tag_name }}",
        "VERSION": "${{ needs.release-please.outputs.version }}",
    }
    artifact_env = {
        "ARTIFACT_ID": "${{ needs.build-release-artifact.outputs.artifact_id }}",
        "ARTIFACT_DIGEST": "${{ needs.build-release-artifact.outputs.artifact_digest }}",
        "SOURCE_SHA": "${{ needs.release-please.outputs.source_sha }}",
        "VERSION": "${{ needs.release-please.outputs.version }}",
        "GH_TOKEN": "${{ github.token }}",
    }
    artifact_recheck_env = {
        **artifact_env,
        "TAG_NAME": "${{ needs.release-please.outputs.tag_name }}",
    }
    reviewed_step_controls = {
        "release-please": {
            f"uses:{release_please_action}": _reviewed_action_step(
                release_please_action,
                step_id="release",
                with_={
                    "token": "${{ secrets.PERSONAL_ACCESS_TOKEN || github.token }}",
                    "config-file": "release-please-config.json",
                    "manifest-file": ".release-please-manifest.json",
                },
            )
        },
        "build-release-artifact": {
            f"uses:{checkout_action}": _reviewed_action_step(
                checkout_action,
                with_={
                    "ref": "${{ needs.release-please.outputs.source_sha }}",
                    "fetch-depth": 0,
                    "fetch-tags": True,
                },
            ),
            f"uses:{setup_python_action}": _reviewed_action_step(
                setup_python_action,
                with_={"python-version": "3.11"},
            ),
            "Verify release source": _reviewed_run_step(
                "Verify release source",
                shell="bash",
                env=source_env,
            ),
            "Install build tools": _reviewed_run_step("Install build tools"),
            "Build distributions once": _reviewed_run_step("Build distributions once"),
            "Check distribution metadata": _reviewed_run_step("Check distribution metadata"),
            "Stage exact distribution bundle": _reviewed_run_step(
                "Stage exact distribution bundle",
                shell="bash",
                env={"VERSION": "${{ needs.release-please.outputs.version }}"},
            ),
            "Upload immutable release artifact": _reviewed_action_step(
                upload_artifact_action,
                name="Upload immutable release artifact",
                step_id="upload",
                with_={
                    "name": "release-distributions",
                    "path": "release-bundle",
                    "if-no-files-found": "error",
                    "retention-days": 7,
                },
            ),
        },
        "publish-pypi": {
            f"uses:{checkout_action}": _reviewed_action_step(
                checkout_action,
                with_={"ref": "${{ needs.release-please.outputs.source_sha }}"},
            ),
            "Download verified release artifact": _reviewed_run_step(
                "Download verified release artifact",
                shell="bash",
                env=artifact_env,
            ),
            "Re-fetch and verify before PyPI upload": _reviewed_run_step(
                "Re-fetch and verify before PyPI upload",
                shell="bash",
                env=artifact_recheck_env,
            ),
            "Upload verified distributions to PyPI": _reviewed_action_step(
                pypi_publish_action,
                name="Upload verified distributions to PyPI",
                with_={
                    "packages-dir": "release-bundle/dist",
                    "verbose": True,
                    "print-hash": True,
                },
            ),
        },
        "attach-github-assets": {
            f"uses:{checkout_action}": _reviewed_action_step(
                checkout_action,
                with_={"ref": "${{ needs.release-please.outputs.source_sha }}"},
            ),
            "Download verified release artifact": _reviewed_run_step(
                "Download verified release artifact",
                shell="bash",
                env=artifact_env,
            ),
            "Re-fetch and verify before GitHub upload": _reviewed_run_step(
                "Re-fetch and verify before GitHub upload",
                shell="bash",
                env=artifact_recheck_env,
            ),
            "Upload GitHub release assets": _reviewed_run_step(
                "Upload GitHub release assets",
                shell="bash",
                env={
                    "GH_TOKEN": "${{ github.token }}",
                    "TAG_NAME": "${{ needs.release-please.outputs.tag_name }}",
                },
            ),
            "Verify published GitHub release assets": _reviewed_run_step(
                "Verify published GitHub release assets",
                shell="bash",
                env=source_env,
            ),
        },
    }
    for job_name, expected in reviewed_step_controls.items():
        _require_reviewed_step_controls(named_by_job[job_name], expected)

    reviewed_release_commands = {
        "build-release-artifact": {
            "Verify release source": "eee7aa8235d221d1fb78fadaed0471ab40ed99c053f9a58262f9d72e6c572526",
            "Install build tools": "be80e8b51039f8dfb58c67ab59b8e60b73aa44ed9cd1456bd360ab66ed1926f6",
            "Build distributions once": "b8b030b157e6970e9c72aa209e89f82bc2390775464fca2cd17b97a34c46a39e",
            "Check distribution metadata": "0ade646acf2b32359f841dc17f28cc1f1873c548639f97db1782d99e128a3549",
            "Stage exact distribution bundle": "59cc86dac9d913ab94bd3c836500de16d21e5b55b1fece16f240a8eaacf7a56b",
        },
        "publish-pypi": {
            "Download verified release artifact": "2de35264f3b839f2ec072f8f796650be73dfde6dc712cd85864b22e5839da960",
            "Re-fetch and verify before PyPI upload": "d29c87baf6ffb5ef0f0ebc109afcb071a59b4682f4894ac5e332285ff873f4f7",
        },
        "attach-github-assets": {
            "Download verified release artifact": "2de35264f3b839f2ec072f8f796650be73dfde6dc712cd85864b22e5839da960",
            "Re-fetch and verify before GitHub upload": "b0148db70eb81cf1114d6f3c7708cd787ee8d4aad21d4557d069f9e7ca252ae3",
            "Upload GitHub release assets": "c6e40713473f98f6540ae45bfa16dd7c7005a92cf642d7a8d6c27e78362d9490",
            "Verify published GitHub release assets": "6204e887c6f901a1f6a0857cb0003b6d8447d3f36d25545405936e87c34eb4bc",
        },
    }
    for job_name, expected in reviewed_release_commands.items():
        _require_reviewed_command_digests(named_by_job[job_name], expected)

    build_steps = named_by_job["build-release-artifact"]
    _require_fragments(
        build_steps["Verify release source"],
        "verify-source",
        "verify-release-target",
        "--require-empty-assets",
        "trap",
    )
    _require_bounded_commands(
        build_steps["Verify release source"],
        ("gh", "release", "view"),
        ("python", "-B", "tools/release_integrity.py", "verify-source"),
        ("python", "-B", "tools/release_integrity.py", "verify-release-target"),
    )
    _require_bounded_commands(build_steps["Install build tools"], ("python", "-m", "pip"))
    _require_bounded_commands(build_steps["Build distributions once"], ("python", "-m", "build"))
    _require_bounded_commands(build_steps["Check distribution metadata"], ("python", "-m", "twine"))
    _require_fragments(
        build_steps["Stage exact distribution bundle"],
        "write-checksums",
        "verify-bundle",
    )
    _require_bounded_commands(
        build_steps["Stage exact distribution bundle"],
        ("python", "-B", "tools/release_integrity.py", "write-checksums"),
        ("python", "-B", "tools/release_integrity.py", "verify-bundle"),
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
            "trap",
        )
        _require_bounded_commands(
            steps["Download verified release artifact"],
            ("gh", "api"),
            ("python", "-B", "tools/release_integrity.py", "verify-artifact"),
            ("python", "-B", "tools/release_integrity.py", "verify-extract"),
            ("python", "-B", "tools/release_integrity.py", "verify-bundle"),
        )
        _require_fragments(
            steps[before_name],
            "actions/artifacts/$ARTIFACT_ID",
            "verify-artifact",
            "git ls-remote --exit-code origin",
            "verify-live-tag",
            "gh release view",
            "verify-release-target",
            "verify-bundle",
            "trap",
        )
        _require_bounded_commands(
            steps[before_name],
            ("gh", "api"),
            ("git", "ls-remote"),
            ("python", "-B", "tools/release_integrity.py", "verify-live-tag"),
            ("gh", "release", "view"),
            ("python", "-B", "tools/release_integrity.py", "verify-release-target"),
            ("python", "-B", "tools/release_integrity.py", "verify-bundle"),
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
    _require_bounded_commands(github_upload, ("gh", "release", "upload"))
    _require_fragments(
        named_by_job["attach-github-assets"]["Verify published GitHub release assets"],
        "gh release view",
        "verify-published-release",
        "trap",
    )
    _require_bounded_commands(
        named_by_job["attach-github-assets"]["Verify published GitHub release assets"],
        ("gh", "release", "view"),
        ("python", "-B", "tools/release_integrity.py", "verify-published-release"),
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
    if job.get("timeout-minutes") != 10:
        raise ValueError("release lock workflow job timeout has drifted")
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
    checkout_action = "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
    setup_python_action = "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
    _require_reviewed_step_controls(
        named,
        {
            f"uses:{checkout_action}": _reviewed_action_step(
                checkout_action,
                with_={
                    "ref": "${{ github.event.pull_request.head.sha }}",
                    "fetch-depth": 0,
                    "fetch-tags": False,
                    "token": "${{ secrets.PERSONAL_ACCESS_TOKEN || github.token }}",
                },
            ),
            f"uses:{setup_python_action}": _reviewed_action_step(
                setup_python_action,
                with_={"python-version": "3.11"},
            ),
            "Install pinned uv": _reviewed_run_step("Install pinned uv"),
            "Validate exact release PR lease": _reviewed_run_step(
                "Validate exact release PR lease",
                shell="bash",
                env={
                    "EXPECTED_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                    "EXPECTED_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
                    "HEAD_REF": "${{ github.event.pull_request.head.ref }}",
                },
            ),
            "Synchronize root uv.lock": _reviewed_run_step("Synchronize root uv.lock"),
            "Verify only reviewed lock change": _reviewed_run_step(
                "Verify only reviewed lock change",
                shell="bash",
            ),
            "Commit and push exact uv.lock": _reviewed_run_step(
                "Commit and push exact uv.lock",
                shell="bash",
                env={
                    "EXPECTED_BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                    "EXPECTED_HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
                    "HEAD_REF": "${{ github.event.pull_request.head.ref }}",
                    "EXPECTED_PR_NUMBER": "${{ github.event.pull_request.number }}",
                    "GH_TOKEN": "${{ github.token }}",
                },
            ),
        },
    )
    _require_reviewed_command_digests(
        named,
        {
            "Install pinned uv": "4be7500feacd828b12cc2d4e113225090380263d6cca5ef75052f542504b0bcc",
            "Validate exact release PR lease": "f9bcba141dc9e8d8dd911197b4c99430bfc5ce9d6e4eadc84ae6be7e1690047a",
            "Synchronize root uv.lock": "2b5f92585fe5619ffd616b10b142b0edf15fd09f083e6c8a0e79a6a88223f124",
            "Verify only reviewed lock change": "a81c1a128022db58a08b02882cc6664d7698f6262a2f964039b46f894ff6224f",
            "Commit and push exact uv.lock": "6b943ea4bfa4c88adadac4383e6fb1bc7283b99f82b7463e9e2180bbf3ac717a",
        },
    )
    checkout = named[expected_steps[0]]
    if checkout.get("with") != {
        "ref": "${{ github.event.pull_request.head.sha }}",
        "fetch-depth": 0,
        "fetch-tags": False,
        "token": "${{ secrets.PERSONAL_ACCESS_TOKEN || github.token }}",
    }:
        raise ValueError("release lock workflow checkout identity has drifted")
    if named["Install pinned uv"].get("run") != (
        'timeout --signal=TERM --kill-after=10s 300s python -m pip install "uv==0.11.19"'
    ):
        raise ValueError("release lock workflow uv version has drifted")
    _require_bounded_commands(named["Install pinned uv"], ("python", "-m", "pip"))
    _require_fragments(
        named["Validate exact release PR lease"],
        "git ls-remote --exit-code --heads origin",
        "refs/heads/main",
        "git fetch --no-tags origin",
        "git merge-base",
        "verify-release-paths --paths $RUNNER_TEMP/release-paths.txt",
        "verify-version-anchors --root .",
        "git status --porcelain=v1 --untracked-files=all",
        "trap",
    )
    _require_bounded_commands(
        named["Validate exact release PR lease"],
        ("git", "ls-remote"),
        ("git", "fetch"),
        ("python", "-B", "tools/release_integrity.py", "verify-release-paths"),
        ("python", "-B", "tools/release_integrity.py", "verify-version-anchors"),
    )
    if named["Synchronize root uv.lock"].get("run") != ("timeout --signal=TERM --kill-after=10s 180s uv lock"):
        raise ValueError("release lock workflow generation command has drifted")
    _require_bounded_commands(named["Synchronize root uv.lock"], ("uv", "lock"))
    _require_fragments(
        named["Verify only reviewed lock change"],
        "verify-version-anchors --root . --require-lock",
        "verify-lock-status --status $RUNNER_TEMP/lock-status.txt",
        "trap",
    )
    _require_bounded_commands(
        named["Verify only reviewed lock change"],
        ("python", "-B", "tools/release_integrity.py", "verify-version-anchors"),
        ("python", "-B", "tools/release_integrity.py", "verify-lock-status"),
    )
    push = named["Commit and push exact uv.lock"]
    _require_fragments(
        push,
        "verify-version-anchors --root . --require-lock",
        "repos/$GITHUB_REPOSITORY/pulls?state=open&base=main&head=${GITHUB_REPOSITORY_OWNER}:${HEAD_REF}&per_page=100",
        "repos/$GITHUB_REPOSITORY/pulls/$EXPECTED_PR_NUMBER > $RUNNER_TEMP/release-pr-detail.json",
        "repos/$GITHUB_REPOSITORY/pulls/$EXPECTED_PR_NUMBER/files?per_page=100",
        "verify-release-pr",
        "--detail $RUNNER_TEMP/release-pr-detail.json",
        "--expected-number $EXPECTED_PR_NUMBER --expected-version $version",
        "--expected-base-sha $EXPECTED_BASE_SHA --expected-head-sha $EXPECTED_HEAD_SHA",
        "git ls-remote --exit-code --heads origin",
        "test $remote_head = $EXPECTED_HEAD_SHA",
        "test $remote_base = $EXPECTED_BASE_SHA",
        "test $(git status --porcelain=v1 --untracked-files=all) =  M uv.lock",
        "git add -- uv.lock",
        "test $(git diff --cached --name-only) = uv.lock",
        "git config user.name loonghao",
        "git config user.email hal.long@outlook.com",
        "git commit -m chore(ci): sync release lock",
        "--force-with-lease=refs/heads/${HEAD_REF}:${EXPECTED_HEAD_SHA}",
        "origin HEAD:refs/heads/${HEAD_REF}",
        "trap",
    )
    _require_bounded_commands(
        push,
        ("python", "-B", "tools/release_integrity.py", "verify-version-anchors"),
        ("gh", "api"),
        ("python", "-B", "tools/release_integrity.py", "verify-release-pr"),
        ("git", "ls-remote"),
        ("git", "push"),
    )
    push_commands = [
        _unwrapped_command(command)
        for command in _shell_commands(push)
        if _unwrapped_command(command)[:2] == ("git", "push")
    ]
    expected_push = (
        "git",
        "push",
        "--force-with-lease=refs/heads/${HEAD_REF}:${EXPECTED_HEAD_SHA}",
        "origin",
        "HEAD:refs/heads/${HEAD_REF}",
    )
    if push_commands != [expected_push]:
        raise ValueError("release lock workflow push command is not the exact reviewed lease")


def verify_version_anchors(root: Path, *, require_lock: bool) -> str:
    """Verify release-please anchors and, when requested, the editable lock root."""
    try:
        manifest = json.loads(
            _read_regular_text(root.joinpath(".release-please-manifest.json"), "release manifest anchor")
        )
        pyproject = _read_regular_text(root.joinpath("pyproject.toml"), "project version anchor")
        version_module = _read_regular_text(
            root.joinpath("src", "dcc_mcp_substance3d_painter", "__version__.py"),
            "package version anchor",
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
        lock = _read_regular_text(root.joinpath("uv.lock"), "release lock")
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


def _read_regular_text(path: Path, description: str) -> str:
    try:
        if not _is_regular_unlinked_file(path):
            raise ValueError(f"{description} must be a regular unlinked file")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{description} is unreadable") from exc


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

    live_tag = commands.add_parser("verify-live-tag")
    live_tag.add_argument("--refs", required=True, type=Path)
    live_tag.add_argument("--expected-tag", required=True)
    live_tag.add_argument("--expected-source-sha", required=True)

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

    release_pr = commands.add_parser("verify-release-pr")
    release_pr.add_argument("--metadata", required=True, type=Path)
    release_pr.add_argument("--detail", required=True, type=Path)
    release_pr.add_argument("--files", required=True, type=Path)
    release_pr.add_argument("--expected-number", required=True, type=int)
    release_pr.add_argument("--expected-version", required=True)
    release_pr.add_argument("--expected-base-sha", required=True)
    release_pr.add_argument("--expected-head-sha", required=True)
    release_pr.add_argument("--expected-head-ref", required=True)
    release_pr.add_argument("--expected-repository", required=True)

    release_paths = commands.add_parser("verify-release-paths")
    release_paths.add_argument("--paths", required=True, type=Path)

    lock_status = commands.add_parser("verify-lock-status")
    lock_status.add_argument("--status", required=True, type=Path)
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
        elif args.command == "verify-live-tag":
            verify_live_tag_ref(
                args.refs.read_text(encoding="utf-8"),
                expected_tag=args.expected_tag,
                expected_source_sha=args.expected_source_sha,
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
            print(verify_version_anchors(args.root, require_lock=args.require_lock))
        elif args.command == "verify-release-pr":
            verify_release_pr_snapshot(
                _load_json(args.metadata),
                _load_json(args.detail),
                _load_json(args.files),
                expected_number=args.expected_number,
                expected_version=args.expected_version,
                expected_base_sha=args.expected_base_sha,
                expected_head_sha=args.expected_head_sha,
                expected_head_ref=args.expected_head_ref,
                expected_repository=args.expected_repository,
            )
        elif args.command == "verify-release-paths":
            verify_release_paths(args.paths.read_text(encoding="utf-8"))
        elif args.command == "verify-lock-status":
            verify_lock_status(args.status.read_text(encoding="utf-8"))
        else:  # pragma: no cover - argparse owns command selection.
            raise ValueError("unsupported release integrity command")
    except (OSError, TypeError, ValueError) as exc:
        print(f"release integrity verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
