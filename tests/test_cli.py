from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from rer import cli
from rer.models import validate_document
from rer.store import SnapshotStore

SCRIPT = Path(__file__).resolve().parents[1] / "skills/repo-evolution-research/scripts/research.py"


def run_cli(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


def test_help_works_from_any_directory(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "--help")
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "init" in result.stdout


def test_invalid_repository_does_not_create_output(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "init", "--repo", "https://gitlab.com/a/b")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"
    assert not (tmp_path / ".repo-research").exists()


def test_conflicting_window_arguments_do_not_create_output(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "init", "--repo", "example/demo", "--days", "30", "--months", "1")
    assert result.returncode == 2
    assert not (tmp_path / ".repo-research").exists()


def test_oversized_window_returns_exit_2(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "init", "--repo", "example/demo", "--days", "999999999")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"
    assert not (tmp_path / ".repo-research").exists()


def test_collect_requires_an_existing_run(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "collect", "--run", str(tmp_path / "missing"))
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"


def test_collect_help_is_available(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "collect", "--help")
    assert result.returncode == 0
    assert "--run" in result.stdout


def test_unknown_subcommand_returns_usage_error(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "unknown-command")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage_error"


def test_init_creates_run_and_valid_artifacts(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "init",
        "--repo",
        "https://github.com/example/demo",
        "--mode",
        "quick",
        "--days",
        "30",
        "--timezone",
        "UTC",
        "--output-root",
        ".repo-research",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    run_dir = Path(payload["result"]["run"])
    assert run_dir.is_dir()
    assert (run_dir / "config.resolved.json").is_file()
    assert (run_dir / "manifest.json").is_file()
    assert not (run_dir / ".initializing").exists()

    config = json.loads((run_dir / "config.resolved.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    validate_document(config, "config")
    validate_document(manifest, "manifest")
    assert config["window"]["duration_days"] == 30.0
    assert config["budget"]["max_deep_entities"] == 12
    assert manifest["run_id"] == payload["result"]["run_id"]


def test_init_warns_when_language_is_reserved(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "init",
        "--repo",
        "example/demo",
        "--language",
        "en",
        "--output-root",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["warnings"]
    assert "language is reserved" in payload["warnings"][0]
    manifest = json.loads(
        (Path(payload["result"]["run"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert any("language" in note and "reserved" in note for note in manifest["notes"])


def test_init_warns_when_compare_previous_is_reserved(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "init",
        "--repo",
        "example/demo",
        "--compare-previous",
        "--output-root",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["warnings"]
    assert "compare_previous is reserved" in payload["warnings"][0]
    manifest = json.loads(
        (Path(payload["result"]["run"]) / "manifest.json").read_text(encoding="utf-8")
    )
    assert any("compare_previous" in note for note in manifest["notes"])


def test_init_failure_marks_manifest_failed(tmp_path: Path, monkeypatch) -> None:
    args = cli.build_parser().parse_args(
        ["init", "--repo", "example/demo", "--output-root", str(tmp_path)]
    )
    original = SnapshotStore.write_json

    def failing_write_json(self, relpath, document, *, schema_name=None):
        if str(relpath) == "config.resolved.json":
            raise RuntimeError("simulated write failure")
        return original(self, relpath, document, schema_name=schema_name)

    monkeypatch.setattr(SnapshotStore, "write_json", failing_write_json)
    with pytest.raises(RuntimeError):
        cli.command_init(args)

    manifests = list(tmp_path.rglob("manifest.json"))
    assert manifests
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["state"] == "failed"
    assert (manifests[0].parent / ".initializing").is_file()


def test_last_format_argument_wins(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "init",
        "--repo",
        "example/demo",
        "--format",
        "text",
        "--format",
        "json",
        "--timezone",
        "UTC",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


def test_format_after_subcommand_is_supported(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "init",
        "--repo",
        "example/demo",
        "--format",
        "text",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("run: ")


@pytest.mark.parametrize(
    ("option", "value"),
    [("--timezone", ""), ("--language", ""), ("--output-root", "")],
)
def test_empty_string_options_are_explicit_errors(
    tmp_path: Path,
    option: str,
    value: str,
) -> None:
    result = run_cli(tmp_path, "init", "--repo", "example/demo", option, value)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "usage_error"
    assert not (tmp_path / ".repo-research").exists()


def test_init_text_format(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "--format",
        "text",
        "init",
        "--repo",
        "example/demo",
        "--timezone",
        "UTC",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("run: ")
    assert "initialized run" in result.stderr


def test_doctor_degraded_uses_a_structured_error(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["PATH"] = ""
    result = run_cli(tmp_path, "doctor", env=env)
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "environment_degraded"
    assert payload["result"]["status"] == "degraded"


def test_doctor_reports_without_leaking_tokens(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "doctor")
    assert result.returncode in (0, 3)
    payload = json.loads(result.stdout)
    assert payload["ok"] is (result.returncode == 0)
    if result.returncode == 3:
        assert payload["error"]["code"] == "environment_degraded"
    names = {check["name"] for check in payload["result"]["checks"]}
    assert {"python", "jsonschema", "format_checkers", "gh", "gh_auth", "git"} <= names
    assert not re.search(r"\bgh[opsu]_[A-Za-z0-9_]+", result.stdout)


def test_doctor_redacts_auth_failure(monkeypatch) -> None:
    from rer import cli

    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("git", "gh") else None,
    )

    def fake_run(command):
        if command[0].endswith("gh") and "auth" in command:
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "Token: gho_SECRET12345678\n",
            )
        if command[0].endswith("gh"):
            return subprocess.CompletedProcess(command, 0, "gh version 2.0.0\n", "")
        return subprocess.CompletedProcess(command, 0, "git version 2.0.0\n", "")

    monkeypatch.setattr(cli, "_run_command", fake_run)
    args = cli.build_parser().parse_args(["doctor"])
    result, _ = cli.command_doctor(args)
    serialized = json.dumps(result, ensure_ascii=False)
    assert "gho_SECRET" not in serialized
    assert "<redacted>" in serialized
    assert result["result"]["status"] == "degraded"


def test_init_applies_deep_read_budget(tmp_path: Path) -> None:
    result = run_cli(
        tmp_path,
        "init",
        "--repo",
        "https://github.com/example/demo",
        "--timezone",
        "UTC",
        "--max-deep-prs",
        "5",
        "--max-deep-entities",
        "9",
        "--max-discovery-entities",
        "1234",
        "--max-total-bytes",
        "5000000",
        "--output-root",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    run_dir = Path(payload["result"]["run"])
    config = json.loads((run_dir / "config.resolved.json").read_text(encoding="utf-8"))
    assert config["budget"]["max_deep_prs"] == 5
    assert config["budget"]["max_deep_entities"] == 9
    assert config["budget"]["max_discovery_entities"] == 1234
    assert config["budget"]["max_total_bytes"] == 5000000
    validate_document(config, "config")
