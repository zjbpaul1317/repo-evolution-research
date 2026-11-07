from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rer.collectors.activity import collect_activity
from rer.collectors.progress import CollectProgress, has_blocking_gaps
from rer.errors import BudgetExceededError
from rer.github import CommandResult, GitHubClient
from rer.models import make_entity
from rer.store import SnapshotStore, create_run, new_run_id

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
WINDOW = {
    "start": "2026-08-01T00:00:00Z",
    "end": "2026-11-01T00:00:00Z",
    "timezone": "UTC",
}
BUDGET = {
    "max_requests": 100,
    "max_discovery_entities": 100,
    "max_deep_entities": 10,
    "max_relation_hops": 2,
    "max_external_entities": 5,
    "max_response_bytes": 1024 * 1024,
    "max_total_bytes": 8 * 1024 * 1024,
    "max_runtime_minutes": 35,
    "max_pack_chars": 100000,
}
REPOSITORY = {"host": "github.com", "owner": "acme", "name": "demo"}
PROFILE = {
    "discovery": {
        "discussions": {
            "enabled": True,
            "status": "complete",
        }
    }
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


def parse_params(argv: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for index, value in enumerate(argv):
        if value in ("-f", "-F") and index + 1 < len(argv):
            key, item = argv[index + 1].split("=", 1)
            params[key] = item
    return params


def search_body(
    items: list[dict[str, Any]],
    *,
    total: int | None = None,
    incomplete: bool = False,
) -> CommandResult:
    return http_response(
        200,
        {
            "total_count": len(items) if total is None else total,
            "incomplete_results": incomplete,
            "items": items,
        },
    )


def empty_search() -> CommandResult:
    return search_body([])


def pr_item(
    number: int = 10,
    *,
    created_at: str = "2026-08-15T10:00:00Z",
    updated_at: str = "2026-09-20T10:00:00Z",
    merged_at: str | None = None,
) -> dict[str, Any]:
    pull_request: dict[str, Any] = {"merged_at": merged_at}
    return {
        "id": number,
        "node_id": f"PR_{number}",
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/acme/demo/pull/{number}",
        "state": "closed" if merged_at else "open",
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": updated_at if merged_at else None,
        "labels": [{"name": "design"}],
        "comments": 3,
        "reactions": {"total_count": 2},
        "user": {"login": "alice"},
        "author_association": "MEMBER",
        "draft": False,
        "pull_request": pull_request,
    }


def issue_item(
    number: int = 7,
    *,
    created_at: str = "2026-06-01T00:00:00Z",
    updated_at: str = "2026-09-15T00:00:00Z",
) -> dict[str, Any]:
    return {
        "id": 1000 + number,
        "node_id": f"I_{number}",
        "number": number,
        "title": f"Issue {number}",
        "html_url": f"https://github.com/acme/demo/issues/{number}",
        "state": "open",
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": None,
        "labels": [{"name": "rfc"}],
        "comments": 5,
        "reactions": {"total_count": 1},
        "user": {"login": "bob"},
        "author_association": "CONTRIBUTOR",
    }


def discussion_node(
    number: int = 3,
    *,
    created_at: str = "2026-06-01T00:00:00Z",
    updated_at: str = "2026-09-01T00:00:00Z",
) -> dict[str, Any]:
    return {
        "id": f"D_{number}",
        "number": number,
        "title": f"Discussion {number}",
        "url": f"https://github.com/acme/demo/discussions/{number}",
        "createdAt": created_at,
        "updatedAt": updated_at,
        "closed": False,
        "closedAt": None,
        "category": {"name": "Ideas"},
        "author": {"login": "carol"},
        "comments": {"totalCount": 4},
        "upvoteCount": 2,
    }


def make_runner(
    *,
    search_handler=None,
    list_handler=None,
    discussion_nodes: list[dict[str, Any]] | None = None,
    graphql_calls: list[str] | None = None,
):
    def runner(argv, input_text):
        if argv[1:3] == ["api", "graphql"]:
            if graphql_calls is not None:
                graphql_calls.append(input_text or "")
            return http_response(
                200,
                {
                    "data": {
                        "repository": {
                            "discussions": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": discussion_nodes or [],
                            }
                        }
                    }
                },
            )
        endpoint = argv[argv.index("--include") + 1]
        params = parse_params(argv)
        if endpoint == "search/issues":
            if search_handler is None:
                return empty_search()
            return search_handler(params.get("q", ""))
        if endpoint.endswith("/pulls") or endpoint.endswith("/issues"):
            if list_handler is None:
                return http_response(200, [])
            return list_handler(endpoint, params)
        return http_response(404, {"message": "unhandled"})

    return runner


def make_client(
    tmp_path: Path,
    runner,
    *,
    budget: dict[str, Any] | None = None,
    max_attempts: int = 1,
) -> tuple[SnapshotStore, GitHubClient]:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    client = GitHubClient(
        store,
        budget or BUDGET,
        gh_path="gh",
        runner=runner,
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=max_attempts,
    )
    return store, client


def run_activity(
    tmp_path: Path,
    runner,
    *,
    budget: dict[str, Any] | None = None,
    include_discussions: bool = True,
):
    store, client = make_client(tmp_path, runner, budget=budget)
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        source_profile=PROFILE,
        include_discussions=include_discussions,
        clock=lambda: "2026-11-01T08:00:00Z",
    )
    return store, result


def entity_by_id(store: SnapshotStore, identifier: str) -> dict[str, Any]:
    return next(
        entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if entity["id"] == identifier
    )


def test_old_pr_merged_in_window_is_discovered(tmp_path: Path) -> None:
    item = pr_item(merged_at="2026-09-20T10:00:00Z")

    def search(query: str):
        return search_body([item]) if "is:merged" in query else empty_search()

    store, result = run_activity(tmp_path, make_runner(search_handler=search))
    entity = entity_by_id(store, "github.com/acme/demo/pull/10")

    assert entity["kind"] == "pull_request"
    assert entity["created_at"] == "2026-08-15T10:00:00Z"
    assert entity["source_event_at"] == "2026-09-20T10:00:00Z"
    assert entity["discovery_reasons"] == ["pr_merged"]
    assert entity["normalized_state"]["merged"] is True
    assert store.read_jsonl("raw/prs.jsonl") == [item]

    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_prs"]["coverage_status"] == "complete"
    assert result.overview["views"][0]["unique_count"] == 1
    assert result.overview["views"][0]["candidate_ids"] == [
        "github.com/acme/demo/pull/10"
    ]
    assert "historical_updated_window" in result.overview["limitations"]
    assert result.candidate_count == 1


def test_old_issue_updated_in_window_is_discovered(tmp_path: Path) -> None:
    item = issue_item()

    def search(query: str):
        return search_body([item]) if "is:issue updated:" in query else empty_search()

    store, result = run_activity(tmp_path, make_runner(search_handler=search))
    entity = entity_by_id(store, "github.com/acme/demo/issue/7")

    assert entity["created_at"] == "2026-06-01T00:00:00Z"
    assert entity["source_event_at"] == "2026-09-15T00:00:00Z"
    assert entity["discovery_reasons"] == ["issue_updated"]
    assert result.overview["counts"]["issue_updated"] == 1
    assert store.read_jsonl("raw/issues.jsonl") == [item]


def test_old_discussion_updated_in_window_is_discovered(tmp_path: Path) -> None:
    node = discussion_node()
    calls: list[str] = []
    store, result = run_activity(
        tmp_path,
        make_runner(discussion_nodes=[node], graphql_calls=calls),
    )
    entity = entity_by_id(store, "github.com/acme/demo/discussion/3")

    assert entity["kind"] == "discussion"
    assert entity["created_at"] == "2026-06-01T00:00:00Z"
    assert entity["source_event_at"] == "2026-09-01T00:00:00Z"
    assert entity["discovery_reasons"] == ["discussion_updated"]
    assert store.read_jsonl("raw/discussions.jsonl") == [node]
    assert result.overview["views"][-1]["status"] == "complete"
    assert calls and "rateLimit" in calls[0]


def test_same_pr_from_created_and_merged_views_is_deduplicated(tmp_path: Path) -> None:
    item = pr_item(merged_at="2026-09-20T10:00:00Z")

    def search(query: str):
        if "is:pr" in query and "created:" in query:
            return search_body([item])
        if "is:merged" in query:
            return search_body([item])
        return empty_search()

    store, result = run_activity(tmp_path, make_runner(search_handler=search))
    entity = entity_by_id(store, "github.com/acme/demo/pull/10")

    assert entity["discovery_reasons"] == ["pr_created", "pr_merged"]
    assert entity["normalized_state"]["created_in_window"] is True
    assert entity["normalized_state"]["updated_in_window"] is True
    assert entity["normalized_state"]["merged_in_window"] is True
    assert len(store.read_jsonl("raw/prs.jsonl")) == 1
    assert result.candidate_count == 1
    assert result.overview["counts"]["pr_created"] == 1
    assert result.overview["counts"]["pr_merged"] == 1


def test_search_cap_is_split_into_smaller_date_shards(tmp_path: Path) -> None:
    window = {
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-08-11T00:00:00Z",
        "timezone": "UTC",
    }
    item = pr_item(created_at="2026-08-03T00:00:00Z", updated_at="2026-08-03T00:00:00Z")
    queries: list[str] = []

    def search(query: str):
        queries.append(query)
        if "is:pr" in query and "created:2026-08-01..2026-08-10" in query:
            return search_body([], total=1000)
        if "is:pr" in query and "created:" in query:
            return search_body([item])
        return empty_search()

    store, client = make_client(tmp_path, make_runner(search_handler=search))
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=window,
        source_profile=PROFILE,
    )

    assert len([query for query in queries if "is:pr" in query and "created:" in query]) == 3
    view = next(
        item for item in result.overview["views"] if item["view"] == "pr_created"
    )
    assert view["status"] == "complete"
    assert view["shards"] == 3
    assert view["total_count"] == 2
    assert [shard["status"] for shard in view["shards_detail"]] == [
        "split",
        "complete",
        "complete",
    ]
    assert "search_sharded" in result.overview["limitations"]
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_prs"]["expected_count"] is None
    assert result.candidate_count == 1


def test_incomplete_single_day_search_falls_back_to_list(tmp_path: Path) -> None:
    window = {
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-08-02T00:00:00Z",
        "timezone": "UTC",
    }
    item = pr_item(
        created_at="2026-08-01T10:00:00Z",
        updated_at="2026-08-01T10:00:00Z",
        merged_at="2026-08-01T10:00:00Z",
    )

    def search(_query: str):
        return search_body([item], incomplete=True)

    def lists(_endpoint: str, _params: dict[str, str]):
        return http_response(200, [item])

    store, client = make_client(
        tmp_path,
        make_runner(search_handler=search, list_handler=lists),
    )
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=window,
        source_profile=PROFILE,
    )
    view = next(
        item for item in result.overview["views"] if item["view"] == "pr_created"
    )
    assert view["strategy"] == "mixed"
    assert view["fallback"] is True
    assert view["incomplete"] is True
    assert view["status"] == "complete"
    assert result.candidate_count == 1
    assert "list_fallback_used" in result.overview["limitations"]
    assert "search_index_visibility" in result.overview["limitations"]
    assert "search_incomplete_recovered" in result.overview["limitations"]


def test_pr_merged_fallback_skips_closed_unmerged(tmp_path: Path) -> None:
    window = {
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-08-02T00:00:00Z",
        "timezone": "UTC",
    }
    merged = pr_item(
        10,
        created_at="2026-08-01T09:00:00Z",
        updated_at="2026-08-01T12:00:00Z",
        merged_at="2026-08-01T12:00:00Z",
    )
    unmerged = pr_item(
        11,
        created_at="2026-08-01T09:00:00Z",
        updated_at="2026-08-01T11:00:00Z",
        merged_at=None,
    )
    unmerged["state"] = "closed"
    unmerged["closed_at"] = unmerged["updated_at"]

    def search(query: str):
        if "is:pr is:merged" in query:
            return search_body([], incomplete=True)
        return empty_search()

    def lists(endpoint: str, params: dict[str, str]):
        assert endpoint.endswith("/pulls")
        assert params.get("state") == "closed"
        return http_response(200, [unmerged, merged])

    store, client = make_client(
        tmp_path,
        make_runner(search_handler=search, list_handler=lists),
    )
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=window,
        source_profile=PROFILE,
    )

    views = {item["view"]: item for item in result.overview["views"]}
    assert views["pr_merged"]["status"] == "complete"
    assert views["pr_merged"]["fallback"] is True
    assert "invalid_timestamps" not in result.overview["limitations"]
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_prs"]["coverage_status"] == "complete"
    assert not has_blocking_gaps(result.coverage)
    assert result.candidate_count == 1
    entities = store.read_jsonl("normalized/entities.jsonl")
    assert [entity["id"] for entity in entities] == [
        "github.com/acme/demo/pull/10"
    ]


def test_rate_limit_does_not_trigger_list_fallback(tmp_path: Path) -> None:
    list_calls: list[str] = []

    def search(_query: str):
        return http_response(429, {"message": "rate limited"})

    def lists(endpoint: str, _params: dict[str, str]):
        list_calls.append(endpoint)
        return http_response(200, [])

    store, client = make_client(
        tmp_path,
        make_runner(search_handler=search, list_handler=lists),
    )
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        source_profile=PROFILE,
    )
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_prs"]["coverage_status"] == "partial"
    assert has_blocking_gaps(result.coverage)
    assert list_calls == []
    view = next(
        item for item in result.overview["views"] if item["view"] == "pr_merged"
    )
    assert "github_rate_limited" in (view["truncation_reason"] or "")


def test_failed_fallback_marks_activity_partial(tmp_path: Path) -> None:
    window = {
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-08-02T00:00:00Z",
        "timezone": "UTC",
    }
    item = pr_item(
        created_at="2026-08-01T10:00:00Z",
        updated_at="2026-08-01T10:00:00Z",
        merged_at="2026-08-01T10:00:00Z",
    )

    def search(_query: str):
        return search_body([item], incomplete=True)

    def lists(_endpoint: str, _params: dict[str, str]):
        return http_response(500, {"message": "boom"})

    store, client = make_client(
        tmp_path,
        make_runner(search_handler=search, list_handler=lists),
    )
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=window,
        source_profile=PROFILE,
    )
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_prs"]["coverage_status"] == "partial"
    assert has_blocking_gaps(result.coverage)
    view = next(
        item for item in result.overview["views"] if item["view"] == "pr_created"
    )
    assert view["status"] == "partial"
    assert "github_unavailable" in (view["truncation_reason"] or "")


def test_window_boundaries_are_exact(tmp_path: Path) -> None:
    start_item = pr_item(1, created_at="2026-08-01T00:00:00Z", updated_at="2026-08-01T00:00:00Z")
    end_item = pr_item(2, created_at="2026-11-01T00:00:00Z", updated_at="2026-11-01T00:00:00Z")

    def search(query: str):
        if "is:pr created:" in query:
            return search_body([start_item, end_item])
        return empty_search()

    store, result = run_activity(tmp_path, make_runner(search_handler=search))
    assert result.candidate_count == 1
    assert entity_by_id(store, "github.com/acme/demo/pull/1")["number"] == 1
    assert len(store.read_jsonl("raw/prs.jsonl")) == 1


def test_activity_view_quotas_prevent_pr_starvation(tmp_path: Path) -> None:
    budget = {**BUDGET, "max_discovery_entities": 12}

    def search(query: str):
        if "is:pr" in query:
            if "is:merged" in query:
                base = 100
            elif "created:" in query:
                base = 200
            else:
                base = 300
            return search_body(
                [
                    pr_item(
                        base + index,
                        created_at="2026-08-02T00:00:00Z",
                        updated_at="2026-09-02T00:00:00Z",
                        merged_at="2026-09-02T00:00:00Z",
                    )
                    for index in range(6)
                ]
            )
        if "is:issue" in query:
            base = 20 if "created:" in query else 40
            return search_body(
                [
                    issue_item(
                        base + index,
                        created_at="2026-08-15T00:00:00Z",
                        updated_at="2026-09-15T00:00:00Z",
                    )
                    for index in range(6)
                ]
            )
        return empty_search()

    store, result = run_activity(
        tmp_path,
        make_runner(
            search_handler=search,
            discussion_nodes=[discussion_node(3)],
        ),
        budget=budget,
    )
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_prs"]["fetched_count"] > 0
    assert coverage["activity_issues"]["fetched_count"] > 0
    assert coverage["activity_discussions"]["fetched_count"] > 0
    views = {item["view"]: item for item in result.overview["views"]}
    assert views["pr_merged"]["truncation_reason"] == "discovery_view_quota"
    assert views["issue_created"]["unique_count"] > 0
    assert views["discussion_updated"]["unique_count"] > 0
    assert "discovery_view_quota" in result.overview["limitations"]


def test_entity_limit_is_visible_and_blocking(tmp_path: Path) -> None:
    budget = {**BUDGET, "max_discovery_entities": 1}
    items = [
        pr_item(1, created_at="2026-08-02T00:00:00Z", updated_at="2026-08-02T00:00:00Z"),
        pr_item(2, created_at="2026-08-03T00:00:00Z", updated_at="2026-08-03T00:00:00Z"),
    ]

    def search(query: str):
        if "is:pr created:" in query:
            return search_body(items)
        return empty_search()

    store, result = run_activity(
        tmp_path,
        make_runner(search_handler=search),
        budget=budget,
    )
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_prs"]["coverage_status"] == "partial"
    assert has_blocking_gaps(result.coverage)
    assert result.candidate_count == 1
    view = next(
        item for item in result.overview["views"] if item["view"] == "pr_created"
    )
    assert view["status"] == "partial"
    assert view["truncation_reason"] == "discovery_entity_limit"
    assert view["remaining_cursor"] == "rescan-page:1"
    assert [item["number"] for item in store.read_jsonl("raw/prs.jsonl")] == [1]
    views = result.overview["views"]
    start = next(index for index, item in enumerate(views) if item["view"] == "pr_created")
    later = views[start + 1 :]
    assert later
    assert all(
        item["truncation_reason"] == "discovery_entity_limit_unstarted"
        for item in later
    )


def test_discussion_capability_gap_is_not_mislabeled(tmp_path: Path) -> None:
    for capability_status, expected in (
        ("unavailable", "unavailable"),
        ("partial", "partial"),
    ):
        calls: list[str] = []
        profile = {
            "discovery": {
                "discussions": {
                    "enabled": True,
                    "status": capability_status,
                }
            }
        }
        store, client = make_client(
            tmp_path / capability_status,
            make_runner(discussion_nodes=[discussion_node()], graphql_calls=calls),
        )
        result = collect_activity(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            source_profile=profile,
        )
        coverage = {entry["channel"]: entry for entry in result.coverage}
        assert coverage["activity_discussions"]["coverage_status"] == expected
        assert has_blocking_gaps(result.coverage)
        assert calls == []
        view = result.overview["views"][-1]
        assert view["status"] == expected
        assert "discussions_capability_" in (view["truncation_reason"] or "")


def test_discussion_transport_failure_marks_partial(tmp_path: Path) -> None:
    def runner(argv, input_text):
        if argv[1:3] == ["api", "graphql"]:
            return http_response(500, {"message": "graphql unavailable"})
        endpoint = argv[argv.index("--include") + 1]
        if endpoint == "search/issues":
            return empty_search()
        return http_response(404, {"message": "unhandled"})

    store, client = make_client(tmp_path, runner)
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        source_profile=PROFILE,
    )
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_discussions"]["coverage_status"] == "partial"
    assert has_blocking_gaps(result.coverage)


def test_activity_raw_stream_preserves_deep_fields(tmp_path: Path) -> None:
    item = pr_item(10, merged_at="2026-09-20T10:00:00Z")

    def search(query: str):
        return search_body([item]) if "is:merged" in query else empty_search()

    store, client = make_client(tmp_path, make_runner(search_handler=search))
    store.upsert_jsonl(
        "raw/prs.jsonl",
        [
            {
                "id": 10,
                "number": 10,
                "body": "full body from deep read",
                "files": [{"filename": "src/cache.py"}],
                "state": "closed",
                "updated_at": "2026-09-20T10:00:00Z",
            }
        ],
        key="number",
    )
    collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        source_profile=PROFILE,
    )
    raw = store.read_jsonl("raw/prs.jsonl")
    assert len(raw) == 1
    assert raw[0]["body"] == "full body from deep read"
    assert raw[0]["files"] == [{"filename": "src/cache.py"}]


def test_current_window_is_not_historical(tmp_path: Path) -> None:
    window = {
        "start": "2026-08-01T00:00:00Z",
        "end": "2026-11-01T07:55:00Z",
        "timezone": "UTC",
    }
    store, client = make_client(tmp_path, make_runner())
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=window,
        source_profile=PROFILE,
        clock=lambda: "2026-11-01T08:00:00Z",
    )
    assert "historical_updated_window" not in result.overview["limitations"]


def test_activity_preserves_deep_entity_fields(tmp_path: Path) -> None:
    item = pr_item(10, merged_at="2026-09-20T10:00:00Z")

    def search(query: str):
        return search_body([item]) if "is:merged" in query else empty_search()

    store, client = make_client(tmp_path, make_runner(search_handler=search))
    existing = make_entity(
        entity_id="github.com/acme/demo/pull/10",
        kind="pull_request",
        title="Deep PR",
        url="https://github.com/acme/demo/pull/10",
        observed_at="2026-10-01T00:00:00Z",
        raw_state={"body": "deep body", "files": [{"filename": "src/cache.py"}]},
        normalized_state={"files_changed": 1},
        roles=["implementation"],
        evidence_ids=["ev-deep"],
        discovery_reasons=["deep_read"],
        number=10,
    )
    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [existing],
        key="id",
        schema_name="entity",
    )
    collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        source_profile=PROFILE,
    )
    entity = entity_by_id(store, "github.com/acme/demo/pull/10")
    assert entity["raw_state"]["files"] == [{"filename": "src/cache.py"}]
    assert entity["raw_state"]["body"] == "deep body"
    assert entity["normalized_state"]["files_changed"] == 1
    assert "implementation" in entity["roles"]
    assert "ev-deep" in entity["evidence_ids"]
    assert "deep_read" in entity["discovery_reasons"]
    assert "pr_merged" in entity["discovery_reasons"]


def test_activity_raw_merge_refreshes_metadata_but_keeps_details(tmp_path: Path) -> None:
    item = pr_item(
        10,
        created_at="2026-08-02T00:00:00Z",
        updated_at="2026-09-21T00:00:00Z",
    )
    item["state"] = "open"
    item["title"] = "reopened and renamed"
    item["labels"] = [{"name": "new-label"}]
    item["body"] = "new body"

    def search(query: str):
        if "is:pr" in query and "created:" in query:
            return search_body([item])
        return empty_search()

    store, client = make_client(tmp_path, make_runner(search_handler=search))
    store.upsert_jsonl(
        "raw/prs.jsonl",
        [
            {
                "id": 10,
                "number": 10,
                "state": "closed",
                "title": "old title",
                "labels": [{"name": "old-label"}],
                "body": "old body",
                "closed_at": "2026-09-20T00:00:00Z",
                "updated_at": "2026-09-20T00:00:00Z",
                "files": [{"filename": "src/cache.py"}],
            }
        ],
        key="number",
    )
    collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        source_profile=PROFILE,
    )
    raw = store.read_jsonl("raw/prs.jsonl")[0]
    assert raw["state"] == "open"
    assert raw["title"] == "reopened and renamed"
    assert raw["labels"] == [{"name": "new-label"}]
    assert raw["body"] == "new body"
    assert raw["closed_at"] is None
    assert raw["files"] == [{"filename": "src/cache.py"}]


def test_activity_clock_records_distinct_view_end(tmp_path: Path) -> None:
    ticks = iter([f"2026-11-01T08:00:{index:02d}Z" for index in range(30)])
    store, client = make_client(tmp_path, make_runner())
    result = collect_activity(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        source_profile=PROFILE,
        clock=lambda: next(ticks),
    )
    view = result.overview["views"][0]
    assert view["observed_start"] == "2026-11-01T08:00:01Z"
    assert view["observed_end"] == "2026-11-01T08:00:02Z"
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_prs"]["observed_end"] > coverage["activity_prs"][
        "observed_start"
    ]


def test_activity_budget_exhaustion_keeps_partial_outputs(tmp_path: Path) -> None:
    budget = {**BUDGET, "max_requests": 1}
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    client = GitHubClient(
        store,
        budget,
        gh_path="gh",
        runner=make_runner(),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
    )
    progress = CollectProgress(store, "a" * 64)
    try:
        collect_activity(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            source_profile=PROFILE,
            progress=progress,
        )
    except BudgetExceededError:
        pass
    else:
        raise AssertionError("activity should exhaust the request budget")

    assert progress.budget_exhausted is True
    overview = json.loads(
        (store.run_dir / "analysis/activity-overview.json").read_text(encoding="utf-8")
    )
    assert any(view["status"] == "partial" for view in overview["views"])
    checkpoint = store.read_checkpoint("collect")
    coverage = {item["channel"]: item for item in checkpoint["coverage"]}
    assert coverage["activity_prs"]["coverage_status"] == "partial"


def test_discussions_can_be_disabled(tmp_path: Path) -> None:
    calls: list[str] = []
    store, result = run_activity(
        tmp_path,
        make_runner(graphql_calls=calls),
        include_discussions=False,
    )
    coverage = {entry["channel"]: entry for entry in result.coverage}
    assert coverage["activity_discussions"]["coverage_status"] == "not_requested"
    assert calls == []
    assert not has_blocking_gaps(result.coverage)
    assert result.overview["views"][-1]["status"] == "not_requested"


def test_invalid_window_reports_precise_error(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, make_runner())
    with pytest.raises(ValueError, match="missing or invalid"):
        collect_activity(
            client,
            store,
            REPOSITORY,
            activity_window={
                "start": "not-a-time",
                "end": "2026-11-01T00:00:00Z",
                "timezone": "UTC",
            },
            source_profile=PROFILE,
        )
    with pytest.raises(ValueError, match="must be after"):
        collect_activity(
            client,
            store,
            REPOSITORY,
            activity_window={
                "start": "2026-11-02T00:00:00Z",
                "end": "2026-11-01T00:00:00Z",
                "timezone": "UTC",
            },
            source_profile=PROFILE,
        )
