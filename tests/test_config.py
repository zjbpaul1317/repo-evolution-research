from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rer.config import (
    BUDGET_KEYS,
    build_config,
    compute_config_hash,
    default_config,
    parse_repository,
    read_resolved_config,
    resolve_window,
    subtract_months,
)
from rer.errors import ArtifactError, ConfigHashError, UsageError

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures" / "example"


@pytest.mark.parametrize(
    ("value", "owner", "name"),
    [
        ("https://github.com/vllm-project/vllm", "vllm-project", "vllm"),
        ("https://github.com/vllm-project/vllm.git", "vllm-project", "vllm"),
        ("https://github.com/vllm-project/vllm/", "vllm-project", "vllm"),
        ("git@github.com:vllm-project/vllm.git", "vllm-project", "vllm"),
        ("ssh://git@github.com/vllm-project/vllm.git", "vllm-project", "vllm"),
        ("vllm-project/vllm", "vllm-project", "vllm"),
    ],
)
def test_parse_repository_forms(value: str, owner: str, name: str) -> None:
    repo = parse_repository(value)
    assert (repo.host, repo.owner, repo.name) == ("github.com", owner, name)
    assert repo.slug == f"{owner}/{name}"
    assert repo.canonical_url == f"https://github.com/{owner}/{name}"


def test_repository_identity_is_lowercase_and_hash_stable() -> None:
    upper = parse_repository("https://github.com/Acme/Demo")
    lower = parse_repository("https://github.com/acme/demo")
    assert (upper.owner, upper.name) == ("acme", "demo")
    assert upper.slug == lower.slug
    assert upper.canonical_url == lower.canonical_url
    assert upper.input == "https://github.com/Acme/Demo"

    upper_config = build_config(repository="https://github.com/Acme/Demo", now=NOW)
    lower_config = build_config(repository="https://github.com/acme/demo", now=NOW)
    assert upper_config["repository"]["owner"] == "acme"
    assert upper_config["repository"]["name"] == "demo"
    assert upper_config["config_hash"] == lower_config["config_hash"]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "https://gitlab.com/a/b",
        "https://github.com/onlyowner",
        "https://github.com/a/b/tree/main",
        "https://github.com/a b/c",
        "a/../b",
        "owner//repo",
        "https://user:token@github.com/a/b",
        "https://github.com/a/b?token=secret",
        "https://github.com/a/b#readme",
        "o\n/r",
    ],
)
def test_parse_repository_rejects_invalid(value: str) -> None:
    with pytest.raises(UsageError):
        parse_repository(value)


def test_default_config_has_no_ignored_fields() -> None:
    defaults = default_config()
    assert set(defaults) == {
        "mode",
        "window",
        "focus",
        "compare_previous",
        "include_discussions",
        "include_docs",
        "language",
        "output_root",
        "budget",
    }
    assert set(defaults["window"]) == {"timezone"}
    assert set(defaults["budget"]) == set(BUDGET_KEYS)


def test_agent_pr_reserve_is_mode_derived_and_overridable() -> None:
    assert build_config(repository="acme/demo", mode="quick", now=NOW)["budget"][
        "agent_pr_reserve"
    ] == 2
    assert build_config(repository="acme/demo", mode="standard", now=NOW)["budget"][
        "agent_pr_reserve"
    ] == 4
    assert build_config(repository="acme/demo", mode="deep", now=NOW)["budget"][
        "agent_pr_reserve"
    ] == 8
    overridden = build_config(
        repository="acme/demo",
        mode="standard",
        now=NOW,
        budget_overrides={"agent_pr_reserve": 6},
    )
    assert overridden["budget"]["agent_pr_reserve"] == 6


def test_window_days_uses_exact_duration() -> None:
    window = resolve_window(mode="standard", days=90, now=NOW)
    assert window["start"] == "2026-08-03T08:00:00Z"
    assert window["end"] == "2026-11-01T08:00:00Z"
    assert window["duration_days"] == 90.0
    assert window["expression"] == {"kind": "days", "days": 90, "months": None}


def test_window_months_clamps_month_end() -> None:
    window = resolve_window(
        months=1,
        end="2026-03-31T12:00:00+08:00",
        timezone_name="Asia/Shanghai",
        now=NOW,
    )
    assert window["start"] == "2026-02-28T04:00:00Z"
    assert window["end"] == "2026-03-31T04:00:00Z"
    assert window["expression"]["kind"] == "months"


def test_subtract_months_clamps_leap_and_short_months() -> None:
    assert subtract_months(datetime(2026, 3, 31, 12), 1) == datetime(2026, 2, 28, 12)
    assert subtract_months(datetime(2024, 3, 31, 12), 1) == datetime(2024, 2, 29, 12)
    assert subtract_months(datetime(2026, 5, 31, 12), 3) == datetime(2026, 2, 28, 12)


def test_window_explicit_times_take_priority_over_days() -> None:
    window = resolve_window(
        days=7,
        start="2026-01-01T00:00:00Z",
        end="2026-02-01T00:00:00Z",
        now=NOW,
    )
    assert window["start"] == "2026-01-01T00:00:00Z"
    assert window["end"] == "2026-02-01T00:00:00Z"
    assert window["expression"] == {"kind": "explicit", "days": None, "months": None}


def test_window_rejects_oversized_inputs() -> None:
    with pytest.raises(UsageError):
        resolve_window(days=999999999, now=NOW)
    with pytest.raises(UsageError):
        resolve_window(months=100000, now=NOW)
    with pytest.raises(UsageError):
        resolve_window(days=999999999, end="2026-01-01T00:00:00Z", now=NOW)


def test_window_overflow_is_a_usage_error() -> None:
    with pytest.raises(UsageError):
        resolve_window(days=3650, end="0001-01-01T00:00:00Z", now=NOW)


def test_window_days_survives_dst() -> None:
    window = resolve_window(
        days=5,
        end="2026-03-10T12:00:00-04:00",
        timezone_name="America/New_York",
        now=NOW,
    )
    start = datetime.fromisoformat(window["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(window["end"].replace("Z", "+00:00"))
    assert (end - start).total_seconds() == 5 * 86400


def test_window_naive_explicit_time_uses_configured_timezone() -> None:
    window = resolve_window(
        start="2026-01-01 00:00:00",
        end="2026-01-02 00:00:00",
        timezone_name="Asia/Shanghai",
        now=NOW,
    )
    assert window["start"] == "2025-12-31T16:00:00Z"
    assert window["end"] == "2026-01-01T16:00:00Z"


def test_mode_defaults() -> None:
    quick = resolve_window(mode="quick", now=NOW)
    deep = resolve_window(mode="deep", now=NOW)
    assert quick["duration_days"] == 30.0
    assert deep["duration_days"] == 180.0


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"days": 0}, "days"),
        ({"months": 0}, "months"),
        ({"days": 30, "months": 1}, "不能同时指定"),
        ({"timezone_name": "Mars/Olympus"}, "未知时区"),
        ({"start": "2026-02-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"}, "start"),
    ],
)
def test_window_rejects_invalid_inputs(kwargs: dict[str, object], expected: str) -> None:
    with pytest.raises(UsageError) as error:
        resolve_window(now=NOW, **kwargs)
    assert expected in error.value.message


def test_build_config_is_deterministic_and_mode_aware() -> None:
    first = build_config(repository="example/demo", mode="quick", now=NOW)
    second = build_config(repository="example/demo", mode="quick", now=NOW)
    assert first["config_hash"] == second["config_hash"]
    assert first["budget"]["max_deep_entities"] == 12
    assert first["window"]["duration_days"] == 30.0


def test_build_config_applies_budget_overrides() -> None:
    config = build_config(
        repository="https://github.com/example/demo",
        budget_overrides={"max_requests": 9, "max_runtime_minutes": 3, "max_deep_entities": 5},
        now=NOW,
    )
    assert config["budget"]["max_requests"] == 9
    assert config["budget"]["max_runtime_minutes"] == 3
    assert config["budget"]["max_deep_entities"] == 5


def test_build_config_rejects_bad_budget() -> None:
    with pytest.raises(UsageError):
        build_config(repository="example/demo", budget_overrides={"max_requests": 0}, now=NOW)
    with pytest.raises(UsageError):
        build_config(repository="example/demo", budget_overrides={"unknown": 1}, now=NOW)


def test_config_hash_normalizes_repository_spelling() -> None:
    forms = [
        "example/demo",
        "https://github.com/example/demo",
        "git@github.com:example/demo.git",
        "https://www.github.com/example/demo/",
    ]
    hashes = {
        build_config(repository=form, timezone_name="UTC", now=NOW)["config_hash"]
        for form in forms
    }
    assert len(hashes) == 1


def test_config_hash_ignores_machine_specific_output_root() -> None:
    first = build_config(repository="example/demo", output_root=".repo-research", now=NOW)
    second = build_config(repository="example/demo", output_root="/tmp/elsewhere", now=NOW)
    assert first["config_hash"] == second["config_hash"]
    assert first["config_hash"] == compute_config_hash(first)


def test_read_resolved_config_verifies_hash(tmp_path: Path) -> None:
    source = FIXTURES / "config.sample.json"
    document = json.loads(source.read_text(encoding="utf-8"))
    assert read_resolved_config(source)["config_hash"] == document["config_hash"]

    tampered = copy.deepcopy(document)
    tampered["mode"] = "deep"
    target = tmp_path / "tampered.json"
    target.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ConfigHashError):
        read_resolved_config(target)
    with pytest.raises(ArtifactError) as error:
        read_resolved_config(target, artifact=True)
    assert error.value.exit_code == 6


def test_legacy_1_2_config_is_readable_for_artifacts(tmp_path: Path) -> None:
    document = json.loads(
        (FIXTURES / "config.sample.json").read_text(encoding="utf-8")
    )
    document["schema_version"] = "1.2"
    document["config_hash"] = compute_config_hash(document)
    target = tmp_path / "legacy.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UsageError, match="schema_version"):
        read_resolved_config(target)
    assert read_resolved_config(target, artifact=True)["schema_version"] == "1.2"


def test_old_025_config_is_rejected_with_reinit_guidance() -> None:
    source = FIXTURES / "config.0.2.5.json"
    with pytest.raises(UsageError, match="schema_version"):
        read_resolved_config(source)
    with pytest.raises(ArtifactError) as error:
        read_resolved_config(source, artifact=True)
    assert error.value.source == "config.schema_version"
    assert "旧快照不兼容" in error.value.message
    assert "init" in (error.value.recovery or "")


def test_build_config_deduplicates_focus() -> None:
    config = build_config(
        repository="example/demo",
        focus=["KV Cache", "KV Cache", " 调度器 "],
        now=NOW,
    )
    assert config["focus"] == ["KV Cache", "调度器"]
