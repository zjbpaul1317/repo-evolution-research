from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/repo-evolution-research"
SCRIPT = SKILL / "scripts/research.py"
LOCK = SKILL / "scripts/research.py.lock"
LAUNCHER = SKILL / "rer"


def _script_metadata() -> dict:
    text = SCRIPT.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = lines.index("# /// script")
    end = lines.index("# ///", start + 1)
    body = "\n".join(
        line[2:] if line.startswith("# ") else line[1:] for line in lines[start + 1 : end]
    )
    return tomllib.loads(body)


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_inline_metadata_matches_pyproject() -> None:
    metadata = _script_metadata()
    project = _pyproject()["project"]

    assert metadata["requires-python"] == project["requires-python"]
    assert metadata["dependencies"] == project["dependencies"]


def test_lockfile_present_and_covers_dependencies() -> None:
    assert LOCK.is_file(), "research.py.lock must be committed next to the script"
    text = LOCK.read_text(encoding="utf-8")
    for requirement in _script_metadata()["dependencies"]:
        name = requirement.split(">")[0].split("=")[0].strip()
        assert f'name = "{name}"' in text


def test_launcher_is_executable_and_uses_locked_script_run() -> None:
    assert os.access(LAUNCHER, os.X_OK), "rer must be executable"
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "--locked" in text
    assert "--no-build" in text
    assert "--script" in text
    assert "RER_PYTHON" in text


def test_skill_commands_do_not_assume_repository_layout() -> None:
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert "python skills/repo-evolution-research/scripts/research.py" not in text
    assert 'bash "$SKILL_DIR/rer"' in text
    assert "\ncompatibility:" in text


def test_launcher_runs_from_another_directory(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["RER_PYTHON"] = sys.executable
    completed = subprocess.run(
        ["bash", str(LAUNCHER), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage: repo-evolution-research" in completed.stdout


def test_launcher_passes_exit_code_through() -> None:
    environment = dict(os.environ)
    environment["RER_PYTHON"] = sys.executable
    completed = subprocess.run(
        ["bash", str(LAUNCHER), "no-such-command"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 2


def _legacy_python() -> str | None:
    for candidate in ("/usr/bin/python3", "python3.10"):
        try:
            version = subprocess.run(
                [candidate, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        major, minor = version.stdout.strip().split(".")
        if (int(major), int(minor)) < (3, 11):
            return candidate
    return None


@pytest.mark.skipif(_legacy_python() is None, reason="no Python < 3.11 available")
def test_old_interpreter_reports_structured_error() -> None:
    completed = subprocess.run(
        [_legacy_python() or "python3", str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 3
    assert '"code": "environment_error"' in completed.stdout
    assert '"source": "bootstrap"' in completed.stdout
