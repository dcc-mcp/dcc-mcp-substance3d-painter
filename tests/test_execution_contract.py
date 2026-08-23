from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_SKILL = (
    Path(__file__).resolve().parents[1] / "src" / "dcc_mcp_substance3d_painter" / "skills" / "painter-project"
)


def test_main_thread_project_tools_advertise_the_job_envelope_contract():
    manifest = yaml.safe_load(PROJECT_SKILL.joinpath("tools.yaml").read_text(encoding="utf-8"))

    incorrectly_synchronous = [
        tool["name"] for tool in manifest["tools"] if tool["affinity"] == "main" and tool["execution"] != "async"
    ]

    assert incorrectly_synchronous == []
