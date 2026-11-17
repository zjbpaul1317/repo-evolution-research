from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/repo-evolution-research"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_pyproject_version_matches_runtime_version() -> None:
    from rer import TOOL_VERSION

    project = tomllib.loads(_read(ROOT / "pyproject.toml"))["project"]
    assert project["version"] == TOOL_VERSION


def test_user_facing_documents_track_runtime_versions() -> None:
    from rer import SCHEMA_VERSION, TOOL_VERSION

    readme = _read(ROOT / "README.md")
    readme_zh = _read(ROOT / "README.zh-CN.md")
    workflow = _read(SKILL / "references" / "workflow.md")

    assert re.search(rf"Tool version: `{re.escape(TOOL_VERSION)}`", readme)
    assert re.search(rf"Snapshot schema: `{re.escape(SCHEMA_VERSION)}`", readme)
    assert re.search(rf"当前工具版本：`{re.escape(TOOL_VERSION)}`", readme_zh)
    assert re.search(rf"快照 schema：`{re.escape(SCHEMA_VERSION)}`", readme_zh)
    assert f"TOOL_VERSION={TOOL_VERSION}" in workflow
    assert f"SCHEMA_VERSION={SCHEMA_VERSION}" in workflow
