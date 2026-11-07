from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rer import cli
from rer.collectors.progress import CollectProgress, coverage_entry, merge_coverage
from rer.collectors.repository import collect_repository
from rer.config import build_config, compute_config_hash, read_resolved_config
from rer.discovery import classify_paths, fetch_labels, read_text_file
from rer.errors import StoreError, ToolError, UsageError
from rer.github import CommandResult, GitHubClient
from rer.store import SnapshotStore, create_run, new_run_id

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
SHA = "a" * 40
BUDGET = {
    "max_requests": 50,
    "max_discovery_entities": 100,
    "max_deep_entities": 10,
    "max_relation_hops": 2,
    "max_external_entities": 5,
    "max_response_bytes": 1024 * 1024,
    "max_total_bytes": 8 * 1024 * 1024,
    "max_runtime_minutes": 35,
    "max_pack_chars": 100000,
}

REPO = {
    "id": 1,
    "node_id": "R_demo",
    "full_name": "acme/demo",
    "html_url": "https://github.com/acme/demo",
    "description": "demo repository",
    "homepage": "https://example.com/demo",
    "topics": ["cache", "demo"],
    "default_branch": "develop",
    "archived": False,
    "language": "Python",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2026-10-01T00:00:00Z",
    "pushed_at": "2026-10-01T00:00:00Z",
}

TREE = {
    "sha": SHA,
    "truncated": False,
    "tree": [
        {"path": "README.md", "type": "blob"},
        {"path": "CONTRIBUTING.md", "type": "blob"},
        {"path": "ROADMAP.md", "type": "blob"},
        {"path": "CHANGELOG.md", "type": "blob"},
        {"path": "docs/design/cache.md", "type": "blob"},
        {"path": "docs/architecture/overview.md", "type": "blob"},
        {"path": "rfcs/0001-cache.md", "type": "blob"},
        {"path": ".github/ISSUE_TEMPLATE/bug.md", "type": "blob"},
        {"path": "src/main.py", "type": "blob"},
        {"path": "tests/test_main.py", "type": "blob"},
    ],
}

CONTENTS = {
    "README.md": "# Demo\n",
    "CONTRIBUTING.md": "# Contributing\n",
    "ROADMAP.md": "# Roadmap\n",
    "CHANGELOG.md": "# Changelog\n",
    "docs/design/cache.md": "# Cache design\n",
    "docs/architecture/overview.md": "# Architecture\n",
    "rfcs/0001-cache.md": "# RFC cache\n",
}


def http_response(
    status: int,
    body: Any,
    headers: dict[str, str] | None = None,
) -> CommandResult:
    lines = [f"HTTP/2.0 {status} OK"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append(json.dumps(body))
    return CommandResult(0 if status < 400 else 1, "\n".join(lines), "")


def make_runner(*, tree_truncated: bool = False, graphql_errors: bool = False):
    def runner(argv, input_text):
        if argv[1:3] == ["api", "graphql"]:
            if graphql_errors:
                return http_response(
                    200,
                    {"data": None, "errors": [{"message": "discussions unavailable"}]},
                )
            query = input_text or ""
            if "discussionCategories" in query:
                return http_response(
                    200,
                    {
                        "data": {
                            "repository": {
                                "hasDiscussionsEnabled": True,
                                "discussions": {"totalCount": 4},
                                "discussionCategories": {
                                    "nodes": [{"name": "Ideas"}, {"name": "Architecture"}]
                                },
                            }
                        }
                    },
                )
            return http_response(
                200,
                {
                    "data": {
                        "repository": {
                            "discussions": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [],
                            }
                        }
                    }
                },
            )

        endpoint = argv[argv.index("--include") + 1]
        if endpoint == "search/issues":
            return http_response(
                200,
                {"total_count": 0, "incomplete_results": False, "items": []},
            )
        if endpoint.endswith("repos/acme/demo"):
            return http_response(200, REPO)
        if endpoint == "repos/acme/demo/releases":
            return http_response(200, [])
        if "/git/ref/heads/" in endpoint:
            return http_response(200, {"object": {"sha": SHA}})
        if "/git/trees/" in endpoint:
            tree = {**TREE, "truncated": tree_truncated}
            return http_response(200, tree)
        if endpoint.endswith("repos/acme/demo/labels"):
            return http_response(
                200,
                [{"name": "bug"}],
                {"Link": '<https://api.github.com/repos/acme/demo/labels?page=2>; rel="next"'},
            )
        if "page=2" in endpoint:
            return http_response(200, [{"name": "rfc"}])
        if "/contents/" in endpoint:
            path = endpoint.split("/contents/", 1)[1].split("?", 1)[0]
            text = CONTENTS.get(path)
            if text is None:
                return http_response(404, {"message": "Not Found"})
            raw = text.encode("utf-8")
            return http_response(
                200,
                {
                    "path": path,
                    "encoding": "base64",
                    "size": len(raw),
                    "sha": "b" * 40,
                    "html_url": f"https://github.com/acme/demo/blob/{SHA}/{path}",
                    "content": base64.b64encode(raw).decode("ascii"),
                },
            )
        return http_response(404, {"message": f"unhandled endpoint {endpoint}"})

    return runner


def make_collection(tmp_path: Path, runner):
    store = create_run(
        tmp_path,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        new_run_id(NOW),
    )
    client = GitHubClient(
        store,
        BUDGET,
        gh_path="gh",
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    collection = collect_repository(
        client,
        store,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        "2026-11-01T08:00:00Z",
    )
    return store, collection


def test_collect_repository_discovers_sources(tmp_path: Path) -> None:
    store, collection = make_collection(tmp_path, make_runner())

    assert collection.default_branch == "develop"
    assert collection.default_branch_sha == SHA
    discovery = collection.source_profile["discovery"]
    assert discovery["tree_truncated"] is False
    assert "rfcs/0001-cache.md" in discovery["rfc_paths"]
    assert "docs/design/cache.md" in discovery["design_paths"]
    assert "docs/architecture/overview.md" in discovery["architecture_files"]
    assert "ROADMAP.md" in discovery["roadmap_files"]
    assert ".github/ISSUE_TEMPLATE/bug.md" in discovery["issue_templates"]
    assert discovery["labels"] == ["bug", "rfc"]
    assert discovery["discussions"]["status"] == "complete"
    assert discovery["discussions"]["capability_status"] == "complete"
    assert discovery["discussions"]["items_status"] == "not_requested"
    assert discovery["discussions"]["categories"] == ["Ideas", "Architecture"]
    assert discovery["deferred"] == ["releases", "docs_site", "release_habits"]
    assert discovery["truncations"]["rfc_paths"]["truncated"] is False
    assert {item["kind"] for item in discovery["files"]} >= {"readme", "rfc", "design"}
    assert all(item["status"] == "read" for item in discovery["files"])

    coverage = {item["channel"]: item for item in collection.coverage}
    assert coverage["tree"]["coverage_status"] == "complete"
    assert coverage["labels"]["coverage_status"] == "complete"
    assert coverage["labels"]["expected_count"] is None
    assert coverage["discussions_capability"]["coverage_status"] == "complete"
    assert coverage["discussions_items"]["coverage_status"] == "not_requested"
    assert coverage["files"]["coverage_status"] == "complete"
    assert coverage["files"]["detail_status"] == "complete"
    assert coverage["discovery_paths"]["coverage_status"] == "complete"
    for item in collection.coverage:
        assert item["observed_start"]
        assert item["observed_end"]

    entities = store.read_jsonl("normalized/entities.jsonl")
    assert len(entities) == 1
    entity = entities[0]
    assert entity["id"] == "github.com/acme/demo"
    assert entity["node_id"] == "R_demo"
    assert entity["created_at"] == "2024-01-01T00:00:00Z"
    assert entity["updated_at"] == "2026-10-01T00:00:00Z"

    profile_path = store.run_dir / "analysis/source-profile.json"
    assert profile_path.is_file()
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile["repository"]["default_branch"] == "develop"
    assert profile["repository"]["homepage"] == "https://example.com/demo"
    assert profile["repository"]["topics"] == ["cache", "demo"]


def test_tree_truncation_is_reported_as_partial(tmp_path: Path) -> None:
    _, collection = make_collection(tmp_path, make_runner(tree_truncated=True))
    tree = next(item for item in collection.coverage if item["channel"] == "tree")
    assert tree["coverage_status"] == "partial"
    assert tree["truncation_reason"] == "tree_truncated"
    assert tree["exhausted"] is False
    assert collection.source_profile["discovery"]["tree_truncated"] is True


def test_repository_without_docs_does_not_invent_paths(tmp_path: Path) -> None:
    runner = make_runner()
    base = runner

    def minimal(argv, input_text):
        if argv[1:3] == ["api", "graphql"]:
            return http_response(
                200,
                {
                    "data": {
                        "repository": {
                            "hasDiscussionsEnabled": False,
                            "discussions": {"totalCount": 0},
                            "discussionCategories": {"nodes": []},
                        }
                    }
                },
            )
        endpoint = argv[argv.index("--include") + 1]
        if "/git/trees/" in endpoint:
            return http_response(
                200,
                {
                    "sha": SHA,
                    "truncated": False,
                    "tree": [
                        {"path": "README.md", "type": "blob"},
                        {"path": "src/main.py", "type": "blob"},
                    ],
                },
            )
        return base(argv, input_text)

    _, collection = make_collection(tmp_path, minimal)
    discovery = collection.source_profile["discovery"]
    assert discovery["rfc_paths"] == []
    assert discovery["design_paths"] == []
    assert discovery["docs_paths"] == []
    assert discovery["discussions"]["enabled"] is False


def test_discussions_unavailable_is_not_a_hard_failure(tmp_path: Path) -> None:
    _, collection = make_collection(tmp_path, make_runner(graphql_errors=True))
    discussions = next(
        item
        for item in collection.coverage
        if item["channel"] == "discussions_capability"
    )
    assert discussions["coverage_status"] == "unavailable"
    assert collection.source_profile["discovery"]["discussions"]["status"] == "unavailable"


def test_include_docs_false_skips_rfc_file_reads(tmp_path: Path) -> None:
    runner = make_runner()
    store = create_run(
        tmp_path,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        new_run_id(NOW),
    )
    client = GitHubClient(store, BUDGET, gh_path="gh", runner=runner, now=lambda: NOW)
    collection = collect_repository(
        client,
        store,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        "2026-11-01T08:00:00Z",
        include_docs=False,
    )
    kinds = {item["kind"] for item in collection.source_profile["discovery"]["files"]}
    assert "rfc" not in kinds
    assert "design" not in kinds


def test_collect_rejects_legacy_schema_version(tmp_path: Path) -> None:
    init_args = cli.build_parser().parse_args(
        [
            "init",
            "--repo",
            "acme/demo",
            "--timezone",
            "UTC",
            "--output-root",
            str(tmp_path),
        ]
    )
    init_result, _ = cli.command_init(init_args)
    run_dir = Path(init_result["result"]["run"])
    store = SnapshotStore(run_dir)
    config = json.loads(
        (run_dir / "config.resolved.json").read_text(encoding="utf-8")
    )
    config["schema_version"] = "1.2"
    config["config_hash"] = compute_config_hash(config)
    store.write_json("config.resolved.json", config, schema_name="config")
    manifest = store.read_manifest()
    manifest["schema_version"] = "1.2"
    manifest["config_hash"] = config["config_hash"]
    store.write_manifest(manifest)

    assert (
        read_resolved_config(run_dir / "config.resolved.json", artifact=True)[
            "schema_version"
        ]
        == "1.2"
    )
    with pytest.raises(UsageError, match="schema_version"):
        cli.command_collect(
            cli.build_parser().parse_args(["collect", "--run", str(run_dir)])
        )


def test_collect_rejects_legacy_manifest_schema_version(tmp_path: Path) -> None:
    init_args = cli.build_parser().parse_args(
        [
            "init",
            "--repo",
            "acme/demo",
            "--timezone",
            "UTC",
            "--output-root",
            str(tmp_path),
        ]
    )
    init_result, _ = cli.command_init(init_args)
    run_dir = Path(init_result["result"]["run"])
    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    manifest["schema_version"] = "1.2"
    store.write_manifest(manifest)

    with pytest.raises(UsageError, match="manifest schema_version"):
        cli.command_collect(
            cli.build_parser().parse_args(["collect", "--run", str(run_dir)])
        )


def test_collect_command_updates_manifest_and_checkpoint(tmp_path: Path) -> None:
    init_args = cli.build_parser().parse_args(
        [
            "init",
            "--repo",
            "acme/demo",
            "--timezone",
            "UTC",
            "--output-root",
            str(tmp_path),
        ]
    )
    init_result, _ = cli.command_init(init_args)
    run_dir = Path(init_result["result"]["run"])

    fake_client = GitHubClient(
        SnapshotStore(run_dir),
        BUDGET,
        gh_path="gh",
        runner=make_runner(),
        now=lambda: NOW,
    )
    original = cli.GitHubClient
    cli.GitHubClient = lambda store, budget, **kwargs: fake_client
    try:
        collect_args = cli.build_parser().parse_args(["collect", "--run", str(run_dir)])
        result, _ = cli.command_collect(collect_args)
    finally:
        cli.GitHubClient = original

    assert result["result"]["default_branch"] == "develop"
    manifest = SnapshotStore(run_dir).read_manifest()
    assert manifest["stages"]["collect"] == "complete"
    assert manifest["default_branch_sha"] == SHA
    assert manifest["collection_started_at"] is not None
    assert manifest["collection_finished_at"] is not None
    assert manifest["observed"]["finished_at"] is not None
    assert manifest["budget_consumed"]["requests"] > 0
    assert any(item["channel"] == "tree" for item in manifest["coverage"])
    assert (run_dir / "analysis/activity-overview.json").is_file()
    channels = {item["channel"]: item for item in manifest["coverage"]}
    assert {"activity_prs", "activity_issues", "activity_discussions"} <= set(channels)
    assert channels["activity_prs"]["coverage_status"] == "complete"

    checkpoint = SnapshotStore(run_dir).read_checkpoint("collect")
    assert checkpoint["status"] == "complete"
    assert checkpoint["config_hash"] == manifest["config_hash"]
    assert checkpoint["budget_consumed"]["requests"] > 0
    assert "cursors" not in checkpoint
    checkpoint_channels = {item["channel"] for item in checkpoint["coverage"]}
    assert {"activity_prs", "activity_issues", "activity_discussions"} <= checkpoint_channels


def test_collect_recovers_checkpoint_budget_and_exhaustion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = init_run(tmp_path)
    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    store.write_checkpoint(
        "collect",
        {
            "status": "partial",
            "config_hash": manifest["config_hash"],
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
            "coverage": [],
            "budget_consumed": {
                "requests": 10,
                "response_bytes": 0,
                "total_bytes": 0,
                "deep_entities": 0,
                "runtime_seconds": 0,
            },
            "budget_exhausted": True,
        },
    )
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))

    assert cli.main(["collect", "--run", str(run_dir)]) == 4

    updated = store.read_manifest()
    assert updated["stages"]["collect"] == "partial"
    assert updated["budget_consumed"]["requests"] >= 10
    assert "run-level budget exhaustion" in "\n".join(updated["notes"])


def test_collect_marks_run_level_budget_exhaustion_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = init_run(tmp_path)
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))

    def exhausted(context, **kwargs):
        context.budget_exhausted = True
        if context.progress is not None:
            context.progress.budget_exhausted = True
        return SimpleNamespace(
            coverage=[],
            counts={
                "candidates": 0,
                "issue_selected": 0,
                "discussion_selected": 0,
            },
            documents=[],
            selection="",
        )

    monkeypatch.setattr(cli, "collect_proposals", exhausted)

    assert cli.main(["collect", "--run", str(run_dir)]) == 4

    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    assert manifest["stages"]["collect"] == "partial"
    assert manifest["state"] == "partial"
    assert "run-level budget exhaustion" in "\n".join(manifest["notes"])
    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["status"] == "partial"
    assert checkpoint.get("budget_exhausted") is True


def init_run(tmp_path: Path) -> Path:
    init_args = cli.build_parser().parse_args(
        [
            "init",
            "--repo",
            "acme/demo",
            "--timezone",
            "UTC",
            "--output-root",
            str(tmp_path),
        ]
    )
    init_result, _ = cli.command_init(init_args)
    return Path(init_result["result"]["run"])


def client_factory(runner, *, max_attempts: int = 3):
    def factory(store, budget, **kwargs):
        return GitHubClient(
            store,
            budget,
            gh_path="gh",
            runner=runner,
            sleep=lambda _seconds: None,
            now=lambda: NOW,
            initial_usage=kwargs.get("initial_usage"),
            max_attempts=max_attempts,
        )

    return factory


def test_collect_failure_preserves_coverage_and_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = init_run(tmp_path)
    base = make_runner()

    def runner(argv, input_text):
        endpoint = argv[argv.index("--include") + 1]
        if "/git/trees/" in endpoint:
            return http_response(500, {"message": "boom"})
        return base(argv, input_text)

    monkeypatch.setattr(cli, "GitHubClient", client_factory(runner, max_attempts=2))
    assert cli.main(["collect", "--run", str(run_dir)]) == 4

    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    assert manifest["state"] == "interrupted"
    assert manifest["stages"]["collect"] == "partial"
    assert manifest["observed"]["finished_at"] is not None
    assert manifest["collection_finished_at"] is not None
    assert manifest["budget_consumed"]["requests"] > 0
    assert manifest["default_branch"] == "develop"
    assert manifest["default_branch_sha"] == SHA
    channels = {item["channel"] for item in manifest["coverage"]}
    assert {"repository", "default_branch"} <= channels
    assert "tree" not in channels

    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["status"] == "interrupted"
    assert checkpoint["config_hash"] == manifest["config_hash"]
    assert checkpoint["exit_code"] == 4
    assert checkpoint["default_branch"] == "develop"
    assert checkpoint["default_branch_sha"] == SHA
    assert {"repository", "default_branch"} <= {
        item["channel"] for item in checkpoint["coverage"]
    }


def test_collect_command_persists_activity_candidates(tmp_path: Path, monkeypatch) -> None:
    run_dir = init_run(tmp_path)
    base = make_runner()
    item = {
        "id": 10,
        "node_id": "PR_10",
        "number": 10,
        "title": "Old PR merged in window",
        "html_url": "https://github.com/acme/demo/pull/10",
        "state": "closed",
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-09-20T10:00:00Z",
        "closed_at": "2026-09-20T10:00:00Z",
        "labels": [{"name": "design"}],
        "comments": 2,
        "reactions": {"total_count": 1},
        "user": {"login": "alice"},
        "pull_request": {"merged_at": "2026-09-20T10:00:00Z"},
    }

    def runner(argv, input_text):
        if argv[1:3] == ["api", "graphql"]:
            return base(argv, input_text)
        endpoint = argv[argv.index("--include") + 1]
        if endpoint == "search/issues" and "is:merged" in " ".join(argv):
            return http_response(
                200,
                {"total_count": 1, "incomplete_results": False, "items": [item]},
            )
        if endpoint == "search/issues":
            return http_response(
                200,
                {"total_count": 0, "incomplete_results": False, "items": []},
            )
        return base(argv, input_text)

    monkeypatch.setattr(cli, "GitHubClient", client_factory(runner))
    result, _ = cli.command_collect(
        cli.build_parser().parse_args(["collect", "--run", str(run_dir)])
    )

    assert result["result"]["activity_candidate_count"] == 1
    store = SnapshotStore(run_dir)
    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["id"] == "github.com/acme/demo/pull/10"
    )
    assert entity["discovery_reasons"] == ["pr_merged"]
    overview = json.loads(
        (run_dir / "analysis/activity-overview.json").read_text(encoding="utf-8")
    )
    assert overview["candidate_count"] == 1
    assert overview["counts"]["pr_merged"] == 1


def test_collect_activity_partial_returns_exit_4(tmp_path: Path, monkeypatch) -> None:
    run_dir = init_run(tmp_path)
    base = make_runner()

    def runner(argv, input_text):
        if argv[1:3] == ["api", "graphql"]:
            return base(argv, input_text)
        endpoint = argv[argv.index("--include") + 1]
        if (
            endpoint == "search/issues"
            or endpoint.endswith("/pulls")
            or endpoint.endswith("/issues")
        ):
            return http_response(500, {"message": "activity unavailable"})
        return base(argv, input_text)

    monkeypatch.setattr(cli, "GitHubClient", client_factory(runner, max_attempts=1))
    assert cli.main(["collect", "--run", str(run_dir)]) == 4

    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    assert manifest["state"] == "partial"
    assert manifest["stages"]["collect"] == "partial"
    coverage = {item["channel"]: item for item in manifest["coverage"]}
    assert coverage["activity_prs"]["coverage_status"] == "partial"
    overview = json.loads(
        (run_dir / "analysis/activity-overview.json").read_text(encoding="utf-8")
    )
    assert any(view["status"] == "partial" for view in overview["views"])


def test_collect_auth_failure_marks_failed(tmp_path: Path, monkeypatch) -> None:
    run_dir = init_run(tmp_path)

    def runner(argv, input_text):
        return http_response(401, {"message": "Bad credentials"})

    monkeypatch.setattr(cli, "GitHubClient", client_factory(runner))
    assert cli.main(["collect", "--run", str(run_dir)]) == 3

    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    assert manifest["state"] == "failed"
    assert manifest["stages"]["collect"] == "failed"
    assert manifest["observed"]["finished_at"] is not None
    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["status"] == "failed"
    assert checkpoint["exit_code"] == 3
    assert checkpoint["coverage"] == []


def test_collect_partial_success_returns_exit_4(tmp_path: Path, monkeypatch) -> None:
    run_dir = init_run(tmp_path)
    monkeypatch.setattr(
        cli,
        "GitHubClient",
        client_factory(make_runner(graphql_errors=True)),
    )
    assert cli.main(["collect", "--run", str(run_dir)]) == 4

    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    assert manifest["state"] == "partial"
    assert manifest["stages"]["collect"] == "partial"
    capability = next(
        item
        for item in manifest["coverage"]
        if item["channel"] == "discussions_capability"
    )
    assert capability["coverage_status"] == "unavailable"
    assert store.read_checkpoint("collect")["status"] == "partial"


def test_collect_rejects_config_manifest_mismatch(tmp_path: Path, monkeypatch) -> None:
    run_dir = init_run(tmp_path)
    other = build_config(repository="other/project", timezone_name="UTC", now=NOW)
    SnapshotStore(run_dir).write_json(
        "config.resolved.json",
        other,
        schema_name="config",
    )
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))
    assert cli.main(["collect", "--run", str(run_dir)]) == 6

    manifest = SnapshotStore(run_dir).read_manifest()
    assert manifest["state"] == "initialized"
    assert manifest["repository"]["slug"] == "acme/demo"


def test_force_preserves_entities_and_accumulates_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = init_run(tmp_path)
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))

    first_args = cli.build_parser().parse_args(["collect", "--run", str(run_dir)])
    cli.command_collect(first_args)
    store = SnapshotStore(run_dir)
    first_requests = store.read_manifest()["budget_consumed"]["requests"]

    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [{"id": "github.com/acme/demo/issue/1", "kind": "issue"}],
    )
    manifest = store.read_manifest()
    manifest["stages"]["validate"] = "complete"
    store.write_manifest(manifest)

    second_args = cli.build_parser().parse_args(
        ["collect", "--run", str(run_dir), "--force"]
    )
    cli.command_collect(second_args)

    entities = store.read_jsonl("normalized/entities.jsonl")
    assert "github.com/acme/demo/issue/1" in {item["id"] for item in entities}
    manifest = store.read_manifest()
    assert manifest["budget_consumed"]["requests"] == first_requests * 2
    assert manifest["stages"]["validate"] == "not_started"
    assert any("downstream" in note for note in manifest["notes"])


def test_classify_paths_reports_truncation() -> None:
    paths = [f"rfcs/{index:04d}.md" for index in range(250)]
    classification = classify_paths(paths)
    assert len(classification["rfc_paths"]) == 200
    assert classification["truncations"]["rfc_paths"] == {
        "total": 250,
        "returned": 200,
        "limit": 200,
        "truncated": True,
    }
    assert classification["truncations"]["readme_files"]["truncated"] is False


def test_discovery_path_truncation_makes_coverage_partial(tmp_path: Path) -> None:
    base = make_runner()
    tree = {
        "sha": SHA,
        "truncated": False,
        "tree": [{"path": f"rfcs/{index:04d}.md", "type": "blob"} for index in range(250)],
    }

    def runner(argv, input_text):
        if argv[1:3] == ["api", "graphql"]:
            return base(argv, input_text)
        endpoint = argv[argv.index("--include") + 1]
        if "/git/trees/" in endpoint:
            return http_response(200, tree)
        return base(argv, input_text)

    _, collection = make_collection(tmp_path, runner)
    discovery_paths = next(
        item for item in collection.coverage if item["channel"] == "discovery_paths"
    )
    assert discovery_paths["coverage_status"] == "partial"
    assert "capped:rfc_paths" in discovery_paths["truncation_reason"]
    truncations = collection.source_profile["discovery"]["truncations"]
    assert truncations["rfc_paths"]["truncated"] is True


def test_labels_rate_limit_is_unavailable_without_aborting(tmp_path: Path) -> None:
    base = make_runner()

    def runner(argv, input_text):
        endpoint = argv[argv.index("--include") + 1]
        if endpoint.endswith("repos/acme/demo/labels"):
            return http_response(
                403,
                {"message": "secondary rate limit"},
                {"x-ratelimit-remaining": "0"},
            )
        return base(argv, input_text)

    _, collection = make_collection(tmp_path, runner)
    labels = next(item for item in collection.coverage if item["channel"] == "labels")
    assert labels["coverage_status"] == "unavailable"
    assert labels["truncation_reason"] == "github_rate_limited"
    assert collection.source_profile["discovery"]["labels"] == []


def test_unreadable_files_mark_coverage_partial(tmp_path: Path) -> None:
    base = make_runner()

    def runner(argv, input_text):
        endpoint = argv[argv.index("--include") + 1]
        if "/contents/" in endpoint:
            path = endpoint.split("/contents/", 1)[1].split("?", 1)[0]
            if path == "rfcs/0001-cache.md":
                raw = b"\xff\xfe"
                return http_response(
                    200,
                    {
                        "path": path,
                        "encoding": "base64",
                        "size": len(raw),
                        "sha": "b" * 40,
                        "content": base64.b64encode(raw).decode("ascii"),
                    },
                )
            if path == "docs/design/cache.md":
                return http_response(404, {"message": "Not Found"})
        return base(argv, input_text)

    _, collection = make_collection(tmp_path, runner)
    files = next(item for item in collection.coverage if item["channel"] == "files")
    assert files["coverage_status"] == "partial"
    assert files["detail_status"] == "partial"
    statuses = {
        item["path"]: item["status"]
        for item in collection.source_profile["discovery"]["files"]
    }
    assert statuses["rfcs/0001-cache.md"] == "unsupported_encoding"
    assert statuses["docs/design/cache.md"] == "not_found"


def test_read_text_file_fallback_url_is_escaped(tmp_path: Path) -> None:
    store = create_run(
        tmp_path,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        new_run_id(NOW),
    )

    def runner(argv, input_text):
        raw = b"# Design\n"
        return http_response(
            200,
            {
                "path": "docs/design/a b.md",
                "encoding": "base64",
                "size": len(raw),
                "sha": "b" * 40,
                "content": base64.b64encode(raw).decode("ascii"),
            },
        )

    client = GitHubClient(store, BUDGET, gh_path="gh", runner=runner, now=lambda: NOW)
    result = read_text_file(client, store, "acme", "demo", "docs/design/a b.md", SHA)
    assert result["status"] == "read"
    assert result["url"].endswith(f"/blob/{SHA}/docs/design/a%20b.md")


def test_fetch_labels_truncation_requires_a_next_page(tmp_path: Path) -> None:
    def run_with(directory: str, link: str | None):
        def runner(argv, input_text):
            headers = {"Link": link} if link else None
            return http_response(200, [{"name": "bug"}], headers)

        store = create_run(
            tmp_path / directory,
            {"host": "github.com", "owner": "acme", "name": "demo"},
            new_run_id(NOW),
        )
        client = GitHubClient(store, BUDGET, gh_path="gh", runner=runner, now=lambda: NOW)
        return fetch_labels(client, "acme", "demo", limit=1)

    labels, truncated, _, cursor = run_with("no-link", None)
    assert labels == ["bug"]
    assert truncated is False
    assert cursor is None
    _, truncated, _, cursor = run_with(
        "with-link",
        '<https://api.github.com/repos/acme/demo/labels?page=2>; rel="next"',
    )
    assert truncated is True
    assert cursor == "https://api.github.com/repos/acme/demo/labels?page=2"


def test_empty_repository_is_an_environment_error(tmp_path: Path) -> None:
    base = make_runner()

    def runner(argv, input_text):
        endpoint = argv[argv.index("--include") + 1]
        if endpoint.endswith("repos/acme/demo"):
            return http_response(200, {**REPO, "default_branch": None})
        return base(argv, input_text)

    store = create_run(
        tmp_path,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        new_run_id(NOW),
    )
    client = GitHubClient(store, BUDGET, gh_path="gh", runner=runner, now=lambda: NOW)
    with pytest.raises(ToolError) as error:
        collect_repository(
            client,
            store,
            {"host": "github.com", "owner": "acme", "name": "demo"},
            "2026-11-01T08:00:00Z",
        )
    assert error.value.exit_code == 3


def test_collect_success_returns_exit_0(tmp_path: Path, monkeypatch) -> None:
    run_dir = init_run(tmp_path)
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))
    assert cli.main(["collect", "--run", str(run_dir)]) == 0
    manifest = SnapshotStore(run_dir).read_manifest()
    assert manifest["state"] == "collecting"
    assert manifest["stages"]["collect"] == "complete"


def test_collect_rejects_activity_window_mismatch(tmp_path: Path, monkeypatch) -> None:
    run_dir = init_run(tmp_path)
    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    manifest["activity_window"]["end"] = "2000-01-01T00:00:00Z"
    store.write_manifest(manifest)

    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))
    assert cli.main(["collect", "--run", str(run_dir)]) == 6


def test_merge_coverage_drops_legacy_discussions_channel() -> None:
    def entry(channel: str) -> dict:
        return coverage_entry(
            channel=channel,
            query_scope="repository",
            query="q",
            expected=None,
            fetched=0,
            pages=0,
            observed_start="2026-11-01T08:00:00Z",
        )

    merged = merge_coverage(
        [entry("tree"), entry("discussions")],
        [entry("discussions_capability"), entry("discussions_items")],
    )
    assert [item["channel"] for item in merged] == [
        "tree",
        "discussions_capability",
        "discussions_items",
    ]


def test_discussion_category_truncation_is_visible(tmp_path: Path) -> None:
    base = make_runner()

    def runner(argv, input_text):
        if argv[1:3] == ["api", "graphql"]:
            return http_response(
                200,
                {
                    "data": {
                        "repository": {
                            "hasDiscussionsEnabled": True,
                            "discussions": {"totalCount": 1},
                            "discussionCategories": {
                                "totalCount": 25,
                                "nodes": [{"name": f"category-{index}"} for index in range(20)],
                            },
                        }
                    }
                },
            )
        return base(argv, input_text)

    _, collection = make_collection(tmp_path, runner)
    capability = next(
        item
        for item in collection.coverage
        if item["channel"] == "discussions_capability"
    )
    assert capability["coverage_status"] == "sampled"
    assert capability["truncation_reason"] == "categories_limit"
    discussions = collection.source_profile["discovery"]["discussions"]
    assert discussions["categories_truncated"] is True


def test_auxiliary_gaps_do_not_block_the_run(tmp_path: Path, monkeypatch) -> None:
    run_dir = init_run(tmp_path)
    base = make_runner()

    def runner(argv, input_text):
        endpoint = argv[argv.index("--include") + 1]
        if endpoint.endswith("repos/acme/demo/labels"):
            return http_response(
                403,
                {"message": "secondary rate limit"},
                {"x-ratelimit-remaining": "0"},
            )
        if "/contents/" in endpoint:
            path = endpoint.split("/contents/", 1)[1].split("?", 1)[0]
            if path == "docs/design/cache.md":
                return http_response(404, {"message": "Not Found"})
        return base(argv, input_text)

    monkeypatch.setattr(cli, "GitHubClient", client_factory(runner))
    assert cli.main(["collect", "--run", str(run_dir)]) == 0

    manifest = SnapshotStore(run_dir).read_manifest()
    assert manifest["state"] == "collecting"
    assert manifest["stages"]["collect"] == "complete"
    coverage = {item["channel"]: item for item in manifest["coverage"]}
    assert coverage["labels"]["coverage_status"] == "unavailable"
    assert coverage["files"]["coverage_status"] == "partial"


@pytest.mark.parametrize(
    ("field", "limit_key", "multiplier"),
    [
        ("requests", "max_requests", 1),
        ("total_bytes", "max_total_bytes", 1),
        ("runtime_seconds", "max_runtime_minutes", 60),
    ],
)
def test_force_rejects_exhausted_budget_without_mutation(
    tmp_path: Path,
    monkeypatch,
    field: str,
    limit_key: str,
    multiplier: int,
) -> None:
    run_dir = init_run(tmp_path)
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))
    cli.command_collect(cli.build_parser().parse_args(["collect", "--run", str(run_dir)]))

    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    manifest["budget_consumed"][field] = (
        manifest["budget"][limit_key] * multiplier
    )
    manifest["state"] = "rendered"
    manifest["stages"]["validate"] = "complete"
    manifest["stages"]["render"] = "complete"
    store.write_manifest(manifest)
    previous = store.read_manifest()
    previous_checkpoint = store.read_checkpoint("collect")

    def fail_factory(*args, **kwargs):
        raise AssertionError("GitHubClient must not be created when budget is exhausted")

    monkeypatch.setattr(cli, "GitHubClient", fail_factory)
    assert cli.main(["collect", "--run", str(run_dir), "--force"]) == 4

    after = store.read_manifest()
    assert after["state"] == previous["state"]
    assert after["stages"] == previous["stages"]
    assert after["coverage"] == previous["coverage"]
    assert after["budget_consumed"] == previous["budget_consumed"]
    assert after["observed"] == previous["observed"]
    assert after["default_branch"] == previous["default_branch"]
    assert after["default_branch_sha"] == previous["default_branch_sha"]
    assert store.read_checkpoint("collect") == previous_checkpoint


def test_force_zero_progress_failure_restores_previous_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = init_run(tmp_path)
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))
    cli.command_collect(cli.build_parser().parse_args(["collect", "--run", str(run_dir)]))

    store = SnapshotStore(run_dir)
    previous = store.read_manifest()
    previous_checkpoint = store.read_checkpoint("collect")

    def runner(argv, input_text):
        return http_response(401, {"message": "Bad credentials"})

    monkeypatch.setattr(cli, "GitHubClient", client_factory(runner))
    assert cli.main(["collect", "--run", str(run_dir), "--force"]) == 3

    after = store.read_manifest()
    assert after["state"] == previous["state"]
    assert after["stages"] == previous["stages"]
    assert after["coverage"] == previous["coverage"]
    assert after["default_branch"] == previous["default_branch"]
    assert after["default_branch_sha"] == previous["default_branch_sha"]
    assert after["budget_consumed"]["requests"] == previous["budget_consumed"]["requests"] + 1
    assert any("previous snapshot preserved" in note for note in after["notes"])
    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["status"] == "complete"
    assert checkpoint["coverage"] == previous_checkpoint["coverage"]
    assert checkpoint["budget_consumed"] == after["budget_consumed"]


def test_force_zero_progress_failure_restores_from_manifest_without_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = init_run(tmp_path)
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))
    cli.command_collect(cli.build_parser().parse_args(["collect", "--run", str(run_dir)]))

    store = SnapshotStore(run_dir)
    previous = store.read_manifest()
    (run_dir / "checkpoints/collect.json").unlink()

    def runner(argv, input_text):
        return http_response(401, {"message": "Bad credentials"})

    monkeypatch.setattr(cli, "GitHubClient", client_factory(runner))
    assert cli.main(["collect", "--run", str(run_dir), "--force"]) == 3

    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["status"] == "complete"
    assert checkpoint["coverage"] == previous["coverage"]
    assert checkpoint["default_branch"] == previous["default_branch"]
    assert checkpoint["default_branch_sha"] == previous["default_branch_sha"]


def test_collect_progress_restore_keeps_diagnostics(tmp_path: Path) -> None:
    store = create_run(
        tmp_path,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        new_run_id(NOW),
    )
    progress = CollectProgress(store, "a" * 64)
    progress.restore(
        {
            "status": "failed",
            "config_hash": "a" * 64,
            "observed_at": "2026-11-01T00:00:00Z",
            "stages": {
                "pr_detail": {
                    "status": "partial",
                    "completed": [10],
                    "pending": [11],
                }
            },
            "coverage": [],
            "default_branch": "main",
            "default_branch_sha": "b" * 40,
            "error": "github_auth",
            "exit_code": 3,
        }
    )
    progress.flush(observed_at="2026-11-01T00:00:01Z")

    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["status"] == "failed"
    assert checkpoint["error"] == "github_auth"
    assert checkpoint["exit_code"] == 3
    assert checkpoint["default_branch"] == "main"
    assert checkpoint["default_branch_sha"] == "b" * 40
    assert checkpoint["stages"]["pr_detail"] == {
        "status": "partial",
        "completed": [10],
        "pending": [11],
    }


def test_checkpoint_writes_are_best_effort_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = create_run(
        tmp_path,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        new_run_id(NOW),
    )
    entry = coverage_entry(
        channel="repository",
        query_scope="metadata",
        query="x",
        expected=1,
        fetched=1,
        pages=1,
        observed_start="2026-11-01T00:00:00Z",
    )

    def boom(*args, **kwargs):
        raise StoreError("checkpoint failed", source="store.test")

    monkeypatch.setattr(store, "write_checkpoint", boom)
    progress = CollectProgress(store, "a" * 64)
    progress.record(entry)
    progress.flush(status="complete")
    with pytest.raises(StoreError):
        progress.flush(status="complete", best_effort=False)


def test_legacy_uppercase_manifest_requires_reinit(tmp_path: Path, capsys) -> None:
    run_dir = init_run(tmp_path)
    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    manifest["repository"]["owner"] = "Acme"
    store.write_manifest(manifest)

    assert cli.main(["collect", "--run", str(run_dir)]) == 6
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["source"] == "cli.run_binding"
    assert "legacy" in payload["error"]["message"]


def test_collect_progress_writes_cursor_and_state(tmp_path: Path) -> None:
    store = create_run(
        tmp_path,
        {"host": "github.com", "owner": "acme", "name": "demo"},
        new_run_id(NOW),
    )
    progress = CollectProgress(store, "a" * 64)
    progress.set_cursor(
        "labels",
        {"next": "https://api.github.com/repos/acme/demo/labels?page=2"},
    )
    progress.set_state("default_branch", "main")
    progress.set_state("default_branch_sha", "b" * 40)
    progress.flush()

    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["cursors"]["labels"]["next"].endswith("page=2")
    assert checkpoint["default_branch"] == "main"
    assert checkpoint["default_branch_sha"] == "b" * 40


def test_default_branch_sha_keeps_slashes_in_branch_names() -> None:
    from rer.discovery import fetch_default_branch_sha

    class FakeClient:
        endpoint = ""

        def rest(self, endpoint: str) -> dict:
            self.endpoint = endpoint
            return {"object": {"sha": SHA}}

    client = FakeClient()

    sha = fetch_default_branch_sha(client, "acme", "demo", "release/1.0")

    assert sha == SHA
    assert client.endpoint == "repos/acme/demo/git/ref/heads/release/1.0"


def test_zero_progress_collect_failure_preserves_previous_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = init_run(tmp_path)
    monkeypatch.setattr(cli, "GitHubClient", client_factory(make_runner()))
    cli.command_collect(cli.build_parser().parse_args(["collect", "--run", str(run_dir)]))

    store = SnapshotStore(run_dir)
    manifest = store.read_manifest()
    manifest["state"] = "rendered"
    manifest["stages"]["validate"] = "complete"
    manifest["stages"]["render"] = "complete"
    store.write_manifest(manifest)
    report = store.write_text("report.md", "# previous report\n")

    def runner(argv, input_text):
        return http_response(401, {"message": "Bad credentials"})

    monkeypatch.setattr(cli, "GitHubClient", client_factory(runner))
    assert cli.main(["collect", "--run", str(run_dir), "--force"]) == 3

    assert report.is_file()
    after = store.read_manifest()
    assert after["state"] == "rendered"
    assert after["stages"]["render"] == "complete"
