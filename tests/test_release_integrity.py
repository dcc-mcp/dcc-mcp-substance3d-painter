from __future__ import annotations

import copy
import hashlib
import zipfile
from pathlib import Path

import pytest

from tools.release_integrity import (
    verify_and_extract_artifact,
    verify_distribution_bundle,
    verify_published_release,
    verify_release_source,
    verify_release_target,
    verify_workflow_artifact,
)

SOURCE_SHA = "1" * 40
ARTIFACT_DIGEST = "2" * 64
REPOSITORY = "dcc-mcp/dcc-mcp-substance3d-painter"


def _artifact_metadata() -> dict:
    return {
        "id": 1234,
        "name": "release-distributions",
        "expired": False,
        "digest": f"sha256:{ARTIFACT_DIGEST}",
        "archive_download_url": (f"https://api.github.com/repos/{REPOSITORY}/actions/artifacts/1234/zip"),
        "workflow_run": {"id": 5678, "head_sha": SOURCE_SHA},
    }


def test_workflow_artifact_binds_exact_immutable_identity() -> None:
    verify_workflow_artifact(
        _artifact_metadata(),
        expected_id=1234,
        expected_digest=ARTIFACT_DIGEST,
        expected_name="release-distributions",
        expected_run_id=5678,
        expected_head_sha=SOURCE_SHA,
        expected_repository=REPOSITORY,
    )

    mutations = {
        "missing-id": lambda item: item.pop("id"),
        "missing-digest": lambda item: item.pop("digest"),
        "missing-run": lambda item: item.pop("workflow_run"),
        "id": lambda item: item.update(id=1235),
        "digest": lambda item: item.update(digest=f"sha256:{'3' * 64}"),
        "name": lambda item: item.update(name="other"),
        "expired": lambda item: item.update(expired=True),
        "unknown-expiry": lambda item: item.update(expired=None),
        "archive-url": lambda item: item.update(
            archive_download_url="https://api.github.com/repos/other/repo/actions/artifacts/1234/zip"
        ),
        "run": lambda item: item["workflow_run"].update(id=5679),
        "head": lambda item: item["workflow_run"].update(head_sha="4" * 40),
    }
    for mutate in mutations.values():
        metadata = copy.deepcopy(_artifact_metadata())
        mutate(metadata)
        with pytest.raises((TypeError, ValueError), match="artifact|workflow"):
            verify_workflow_artifact(
                metadata,
                expected_id=1234,
                expected_digest=ARTIFACT_DIGEST,
                expected_name="release-distributions",
                expected_run_id=5678,
                expected_head_sha=SOURCE_SHA,
                expected_repository=REPOSITORY,
            )


def _write_distribution_bundle(root: Path, version: str = "0.4.1") -> tuple[str, str]:
    dist = root / "dist"
    dist.mkdir(parents=True)
    wheel = f"dcc_mcp_substance3d_painter-{version}-py3-none-any.whl"
    sdist = f"dcc_mcp_substance3d_painter-{version}.tar.gz"
    dist.joinpath(wheel).write_bytes(b"wheel")
    dist.joinpath(sdist).write_bytes(b"sdist")
    root.joinpath("SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256(dist.joinpath(name).read_bytes()).hexdigest()}  dist/{name}\n"
            for name in sorted((wheel, sdist))
        ),
        encoding="utf-8",
        newline="\n",
    )
    return wheel, sdist


def test_distribution_bundle_requires_exact_files_and_checksums(tmp_path: Path) -> None:
    wheel, sdist = _write_distribution_bundle(tmp_path)
    verify_distribution_bundle(tmp_path, version="0.4.1")

    cases = {
        "missing-wheel": lambda root: root.joinpath("dist", wheel).unlink(),
        "extra-dist": lambda root: root.joinpath("dist", "extra.txt").write_text("x"),
        "wrong-checksum": lambda root: root.joinpath("SHA256SUMS").write_text(
            f"{'0' * 64}  dist/{wheel}\n{'1' * 64}  dist/{sdist}\n",
            encoding="utf-8",
        ),
        "duplicate-checksum": lambda root: root.joinpath("SHA256SUMS").write_text(
            root.joinpath("SHA256SUMS").read_text(encoding="utf-8")
            + root.joinpath("SHA256SUMS").read_text(encoding="utf-8").splitlines(keepends=True)[0],
            encoding="utf-8",
        ),
        "missing-checksum": lambda root: root.joinpath("SHA256SUMS").unlink(),
        "extra-root": lambda root: root.joinpath("other").write_text("x"),
    }
    for name, mutate in cases.items():
        case = tmp_path / name
        _write_distribution_bundle(case)
        mutate(case)
        with pytest.raises((OSError, TypeError, ValueError), match="distribution|checksum|bundle"):
            verify_distribution_bundle(case, version="0.4.1")


def test_artifact_transport_is_verified_before_safe_extraction(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_distribution_bundle(source)
    archive = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(source).as_posix())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    output = tmp_path / "output"
    verify_and_extract_artifact(archive, expected_digest=digest, output_directory=output)
    verify_distribution_bundle(output, version="0.4.1")

    rejected = tmp_path / "rejected"
    with pytest.raises(ValueError, match="artifact"):
        verify_and_extract_artifact(archive, expected_digest="0" * 64, output_directory=rejected)
    assert not rejected.exists()

    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as bundle:
        bundle.writestr("../outside.txt", "escape")
    hostile_digest = hashlib.sha256(hostile.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="artifact"):
        verify_and_extract_artifact(hostile, expected_digest=hostile_digest, output_directory=rejected)
    assert not rejected.exists()
    assert not tmp_path.joinpath("outside.txt").exists()

    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as bundle:
        bundle.writestr("dist/file.whl", "one")
        bundle.writestr("DIST/FILE.WHL", "two")
    duplicate_digest = hashlib.sha256(duplicate.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="artifact"):
        verify_and_extract_artifact(duplicate, expected_digest=duplicate_digest, output_directory=rejected)
    assert not rejected.exists()


def test_release_source_and_empty_target_bind_exact_merge_sha() -> None:
    verify_release_source(
        head_sha=SOURCE_SHA,
        tag_sha=SOURCE_SHA,
        release_target=SOURCE_SHA,
        expected_sha=SOURCE_SHA,
        tag_name="v0.4.1",
        version="0.4.1",
    )
    metadata = {
        "tagName": "v0.4.1",
        "targetCommitish": SOURCE_SHA,
        "isDraft": False,
        "isPrerelease": False,
        "assets": [],
    }
    verify_release_target(
        metadata,
        expected_tag="v0.4.1",
        expected_source_sha=SOURCE_SHA,
        require_empty_assets=True,
    )

    source_mutations = {
        "head": {"head_sha": "3" * 40},
        "tag": {"tag_sha": "3" * 40},
        "target": {"release_target": "3" * 40},
        "tag-name": {"tag_name": "v0.4.2"},
    }
    defaults = {
        "head_sha": SOURCE_SHA,
        "tag_sha": SOURCE_SHA,
        "release_target": SOURCE_SHA,
        "expected_sha": SOURCE_SHA,
        "tag_name": "v0.4.1",
        "version": "0.4.1",
    }
    for mutation in source_mutations.values():
        with pytest.raises(ValueError, match="release|tag|source"):
            verify_release_source(**(defaults | mutation))

    release_mutations = {
        "tag": lambda item: item.update(tagName="v0.4.2"),
        "target": lambda item: item.update(targetCommitish="3" * 40),
        "draft": lambda item: item.update(isDraft=True),
        "prerelease": lambda item: item.update(isPrerelease=True),
        "existing-assets": lambda item: item.update(assets=[{"name": "old"}]),
    }
    for mutate in release_mutations.values():
        changed = copy.deepcopy(metadata)
        mutate(changed)
        with pytest.raises((TypeError, ValueError), match="release|asset"):
            verify_release_target(
                changed,
                expected_tag="v0.4.1",
                expected_source_sha=SOURCE_SHA,
                require_empty_assets=True,
            )


def test_published_release_requires_exact_asset_digests(tmp_path: Path) -> None:
    wheel, sdist = _write_distribution_bundle(tmp_path)
    names = (wheel, sdist, "SHA256SUMS")

    def asset(name: str) -> dict:
        path = tmp_path / (name if name == "SHA256SUMS" else f"dist/{name}")
        return {
            "name": name,
            "state": "uploaded",
            "size": path.stat().st_size,
            "digest": f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        }

    metadata = {
        "tagName": "v0.4.1",
        "targetCommitish": SOURCE_SHA,
        "isDraft": False,
        "isPrerelease": False,
        "assets": [asset(name) for name in names],
    }
    verify_published_release(
        metadata,
        tmp_path,
        version="0.4.1",
        expected_tag="v0.4.1",
        expected_source_sha=SOURCE_SHA,
    )

    mutations = {
        "missing": lambda item: item["assets"].pop(),
        "duplicate": lambda item: item["assets"].append(copy.deepcopy(item["assets"][0])),
        "digest": lambda item: item["assets"][0].update(digest=f"sha256:{'9' * 64}"),
        "size": lambda item: item["assets"][0].update(size=999),
        "state": lambda item: item["assets"][0].update(state="new"),
        "tag": lambda item: item.update(tagName="v0.4.2"),
        "target": lambda item: item.update(targetCommitish="3" * 40),
    }
    for mutate in mutations.values():
        changed = copy.deepcopy(metadata)
        mutate(changed)
        with pytest.raises((TypeError, ValueError), match="release|asset"):
            verify_published_release(
                changed,
                tmp_path,
                version="0.4.1",
                expected_tag="v0.4.1",
                expected_source_sha=SOURCE_SHA,
            )
