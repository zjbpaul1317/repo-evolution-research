from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from rer.collectors.progress import CHANNEL_POLICIES, has_blocking_gaps
from rer.config import BUDGET_DEFAULTS, BUDGET_FIELDS, BUDGET_KEYS, MODE_DEFAULTS

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "skills/repo-evolution-research/assets"
EXAMPLES = ROOT / "tests/fixtures/example"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _gen_assets() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_assets",
        ROOT / "scripts/gen_assets.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_budget_registry_matches_assets() -> None:
    default = load(ASSETS / "config.default.json")["budget"]
    config_budget = load(ASSETS / "schemas/config.schema.json")["$defs"]["budget"]
    manifest_budget = load(ASSETS / "schemas/manifest.schema.json")["$defs"]["budget"]

    assert (
        set(BUDGET_KEYS)
        == set(default)
        == set(config_budget["properties"])
        == set(manifest_budget["properties"])
    )
    assert BUDGET_DEFAULTS == default

    required = {field.key for field in BUDGET_FIELDS if field.required}
    assert set(config_budget["required"]) == required
    assert set(manifest_budget["required"]) == required
    for field in BUDGET_FIELDS:
        assert (field.key in config_budget["required"]) is field.required


def test_mode_derived_defaults_match_standard_mode() -> None:
    standard = MODE_DEFAULTS["standard"]
    derived = {field.key for field in BUDGET_FIELDS if field.mode_derived}
    assert derived == set(standard) - {"days"}
    for field in BUDGET_FIELDS:
        if field.mode_derived:
            assert field.default == standard[field.key]


def test_configuration_doc_is_derived_from_mode_defaults() -> None:
    module = _gen_assets()
    document = module.configuration_markdown(BUDGET_FIELDS)
    mode_fields = [field for field in BUDGET_FIELDS if field.mode_derived]
    for mode, values in MODE_DEFAULTS.items():
        cells = [mode, str(values["days"])]
        cells.extend(str(values[field.key]) for field in mode_fields)
        assert "| " + " | ".join(cells) + " |" in document

    optional = [field.key for field in BUDGET_FIELDS if not field.required]
    joined = ", ".join(f"`{key}`" for key in optional)
    assert f"Optional schema fields ({joined})" in document


def test_example_fixture_budgets_match_defaults() -> None:
    for name in ("config.sample.json", "manifest.sample.json"):
        assert load(EXAMPLES / name)["budget"] == BUDGET_DEFAULTS


def test_example_manifest_matches_config_fixture() -> None:
    config = load(EXAMPLES / "config.sample.json")
    manifest = load(EXAMPLES / "manifest.sample.json")
    assert manifest["budget"] == config["budget"]
    assert manifest["config_hash"] == config["config_hash"]


def test_generated_assets_are_current() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gen_assets.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_render_is_idempotent() -> None:
    module = _gen_assets()
    assert module.render() == module.render()
    assert module.check() == []


def test_adding_registry_field_regenerates_assets() -> None:
    module = _gen_assets()
    dummy = module.BudgetField("max_dummy", 7, "dummy field")
    fields = (*BUDGET_FIELDS, dummy)
    rendered = module.render(fields)

    default = json.loads(rendered[module.CONFIG_DEFAULT])
    assert default["budget"]["max_dummy"] == 7
    for path in (module.CONFIG_SCHEMA, module.MANIFEST_SCHEMA):
        schema = json.loads(rendered[path])["$defs"]["budget"]
        assert schema["properties"]["max_dummy"] == {"type": "integer", "minimum": 1}
        assert "max_dummy" in schema["required"]
    assert "`max_dummy`" in rendered[module.CONFIGURATION_DOC]


def test_check_detects_missing_registry_field() -> None:
    module = _gen_assets()
    fields = tuple(field for field in BUDGET_FIELDS if field.key != "max_requests")
    problems = module.check(fields)
    assert problems
    assert any("max_requests" in line for line in problems)


def test_channel_policy_registry() -> None:
    assert set(CHANNEL_POLICIES) == {
        "tree",
        "discovery_paths",
        "discussions_capability",
        "activity_prs",
        "activity_issues",
        "activity_discussions",
        "pr_detail",
        "issue_detail",
        "discussion_detail",
        "releases",
        "docs",
    }
    assert CHANNEL_POLICIES["pr_detail"].blocking_statuses == frozenset({"unavailable"})
    assert CHANNEL_POLICIES["docs"].blocking_statuses == frozenset({"unavailable"})
    assert CHANNEL_POLICIES["issue_detail"].blocking_statuses == frozenset(
        {"unavailable"}
    )
    assert CHANNEL_POLICIES["discussion_detail"].blocking_statuses == frozenset(
        {"unavailable"}
    )
    assert CHANNEL_POLICIES["releases"].blocking_statuses == frozenset(
        {"unavailable"}
    )
    assert CHANNEL_POLICIES["tree"].blocking_statuses == frozenset(
        {"partial", "unavailable"}
    )
    for policy in CHANNEL_POLICIES.values():
        assert policy.blocking_statuses <= {"partial", "unavailable"}
    assert not has_blocking_gaps(
        [{"channel": "pr_detail", "coverage_status": "partial"}]
    )
    assert has_blocking_gaps(
        [{"channel": "pr_detail", "coverage_status": "unavailable"}]
    )
    assert not has_blocking_gaps(
        [{"channel": "labels", "coverage_status": "unavailable"}]
    )
