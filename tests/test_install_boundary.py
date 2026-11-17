from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/repo-evolution-research"

REAL_UV = os.environ.get("RER_TEST_REAL_UV") == "1"


def _snapshot(root: Path) -> dict[str, tuple[str, int, int]]:
    entries: dict[str, tuple[str, int, int]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_dir():
            entries[relative + "/"] = ("dir", 0, 0)
            continue
        stat_result = path.stat()
        entries[relative] = ("file", stat_result.st_size, stat_result.st_mtime_ns)
    return entries


def _set_writable(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)
    root.chmod(0o755)


def _set_readonly(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def _run(install: Path, project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if REAL_UV:
        environment.pop("RER_PYTHON", None)
    else:
        environment["RER_PYTHON"] = sys.executable
    return subprocess.run(
        ["bash", str(install / "rer"), *args],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _payload(completed: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(completed.stdout)


@pytest.fixture()
def installed(tmp_path: Path) -> tuple[Path, Path]:
    """Copy only the Skill directory, the way an installer would."""
    install = tmp_path / "installed skill 中文"
    project = tmp_path / "user project"
    shutil.copytree(SKILL, install, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    project.mkdir()
    return install, project


def test_installed_skill_runs_without_touching_the_install(installed: tuple[Path, Path]) -> None:
    install, project = installed
    before = _snapshot(install)
    _set_readonly(install)
    try:
        doctor = _run(install, project, "doctor")
        assert doctor.returncode in (0, 3), doctor.stderr
        doctor_payload = _payload(doctor)
        runtime = doctor_payload["result"]["runtime"]
        checks = {check["name"]: check["status"] for check in doctor_payload["result"]["checks"]}
        assert checks["python"] == "ok"
        assert checks["jsonschema"] == "ok"
        interpreter = Path(runtime["interpreter"]).resolve()
        assert not interpreter.is_relative_to(install.resolve())
        assert runtime["launcher"] == ("uv-locked" if REAL_UV else "python-override")

        init = _run(
            install,
            project,
            "init",
            "--repo",
            "acme/demo",
            "--days",
            "7",
            "--mode",
            "quick",
        )
        assert init.returncode == 0, init.stderr
        run = Path(_payload(init)["result"]["run"])
        run_path = run if run.is_absolute() else project / run
        assert run_path.is_dir()
        assert run_path.resolve().is_relative_to(project.resolve())
        assert not run_path.resolve().is_relative_to(install.resolve())

        audit = _run(install, project, "audit", "--run", str(run_path))
        assert audit.returncode == 0, audit.stderr
    finally:
        _set_writable(install)

    after = _snapshot(install)
    assert after == before, "the installed Skill directory must stay unchanged"
    assert not any("__pycache__" in name for name in after)


def test_launcher_explains_missing_uv(installed: tuple[Path, Path]) -> None:
    install, project = installed
    bin_dir = project / "bin"
    bin_dir.mkdir()
    bash_path = shutil.which("bash")
    assert bash_path
    (bin_dir / "bash").symlink_to(bash_path)

    environment = dict(os.environ)
    environment.pop("RER_PYTHON", None)
    environment["PATH"] = str(bin_dir)
    completed = subprocess.run(
        ["bash", str(install / "rer"), "doctor"],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 3
    assert "uv" in completed.stderr
    assert "0.9" in completed.stderr
    assert "references/setup.md" in completed.stderr
