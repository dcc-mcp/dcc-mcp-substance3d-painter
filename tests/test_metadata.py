from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

import dcc_mcp_substance3d_painter as adapter

ROOT = Path(__file__).resolve().parents[1]


def test_release_metadata_is_synchronized():
    manifest = json.loads(ROOT.joinpath(".release-please-manifest.json").read_text(encoding="utf-8"))
    version = re.search(r'(?m)^version = "([^"]+)"$', ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8"))
    assert version is not None
    assert version.group(1) == adapter.__version__ == manifest["."]


def test_uv_lock_root_matches_release_version():
    pyproject = ROOT.joinpath("pyproject.toml").read_text(encoding="utf-8")
    release_version = re.search(r'(?m)^version = "([^"]+)"$', pyproject)
    assert release_version is not None

    package_blocks = ROOT.joinpath("uv.lock").read_text(encoding="utf-8").split("[[package]]")
    editable_roots = [
        block
        for block in package_blocks
        if re.search(r'(?m)^name = "dcc-mcp-substance3d-painter"$', block)
        and re.search(r'(?m)^source = \{ editable = "\." \}$', block)
    ]
    assert len(editable_roots) == 1
    locked_version = re.search(r'(?m)^version = "([^"]+)"$', editable_roots[0])
    assert locked_version is not None
    assert locked_version.group(1) == release_version.group(1)


def test_release_workflow_uses_pinned_actions_and_scoped_permissions():
    workflow = yaml.safe_load(ROOT.joinpath(".github", "workflows", "release.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]

    assert workflow["permissions"] == {}
    assert jobs["release-please"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }
    assert jobs["build-and-publish"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    assert [step["uses"] for job in jobs.values() for step in job["steps"] if "uses" in step] == [
        "googleapis/release-please-action@45996ed1f6d02564a971a2fa1b5860e934307cf7",
        "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
        "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1",
        "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
    ]


def test_release_notes_include_effective_color_management():
    current_release = ROOT.joinpath("CHANGELOG.md").read_text(encoding="utf-8").split("## [0.3.0]", 1)[0]

    assert "report effective Painter color management" in current_release
    assert "c371b025d53d18163948d7a23e074083e7860192" in current_release


def test_plugin_and_skill_contract_files_exist():
    package = ROOT / "src" / "dcc_mcp_substance3d_painter"
    for required_directory in ("plugins", "startup", "modules"):
        assert package.joinpath("painter", required_directory).is_dir()
    startup_entry = package.joinpath("painter", "startup", f"{adapter.STARTUP_PLUGIN_MODULE}.py")
    assert startup_entry.exists()
    assert adapter.STARTUP_PLUGIN_MODULE != adapter.__name__
    assert not package.joinpath("painter", "plugins", f"{adapter.STARTUP_PLUGIN_MODULE}.py").exists()
    assert package.joinpath("skills", "painter-project", "SKILL.md").exists()
    assert package.joinpath("skills", "painter-project", "tools.yaml").exists()
