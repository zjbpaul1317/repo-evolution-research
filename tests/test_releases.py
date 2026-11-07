from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rer.collectors.base import CollectContext
from rer.collectors.releases import (
    collect_releases,
    derive_release_limitations,
    load_release_documents,
    release_path,
)
from rer.github import CommandResult, GitHubClient
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
    "max_deep_entities": 40,
    "max_deep_prs": 22,
    "max_relation_hops": 2,
    "max_external_entities": 5,
    "max_response_bytes": 1024 * 1024,
    "max_total_bytes": 8 * 1024 * 1024,
    "max_runtime_minutes": 35,
    "max_pack_chars": 100000,
}
REPOSITORY = {"host": "github.com", "owner": "acme", "name": "demo"}
COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
TAG_OBJECT = "c" * 40


def http_response(status: int, body: Any, headers: dict[str, str] | None = None) -> CommandResult:
    lines = [f"HTTP/2.0 {status} OK"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append(json.dumps(body))
    return CommandResult(0 if status < 400 else 1, "\n".join(lines), "")


def release_body(
    identifier: int = 1,
    *,
    tag: str = "v1.0.0",
    published_at: str = "2026-10-01T00:00:00Z",
    draft: bool = False,
    prerelease: bool = False,
    body: str = "Release notes",
    **overrides: Any,
) -> dict[str, Any]:
    result = {
        "id": identifier,
        "tag_name": tag,
        "name": tag,
        "html_url": f"https://github.com/acme/demo/releases/tag/{tag}",
        "body": body,
        "draft": draft,
        "prerelease": prerelease,
        "created_at": "2026-09-20T00:00:00Z",
        "published_at": published_at,
        "target_commitish": "main",
        "user": {"login": "alice"},
    }
    result.update(overrides)
    return result


def tag_ref_body(commit_sha: str = COMMIT_A) -> dict[str, Any]:
    return {"ref": "refs/tags/v1.0.0", "object": {"sha": commit_sha, "type": "commit"}}


def make_client(tmp_path: Path, handler) -> tuple[SnapshotStore, GitHubClient]:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    client = GitHubClient(
        store,
        BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: handler(
            argv[argv.index("--include") + 1],
            input_text,
        ),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )
    return store, client


def context_for(
    client: GitHubClient,
    store: SnapshotStore,
    clock,
) -> CollectContext:
    return CollectContext(
        client=client,
        store=store,
        repository=REPOSITORY,
        clock=clock,
    )


def simple_handler(releases: list[dict[str, Any]], *, tag_commit: str = COMMIT_A):
    def handler(endpoint: str, input_text: str) -> CommandResult:
        if endpoint == "repos/acme/demo/releases":
            return http_response(200, releases)
        if endpoint.startswith("repos/acme/demo/git/ref/tags/"):
            return http_response(200, tag_ref_body(tag_commit))
        return http_response(404, {"message": "unhandled"})

    return handler


def test_collects_stable_release_with_resolved_commit(tmp_path: Path) -> None:
    store, client = make_client(
        tmp_path,
        simple_handler([release_body()], tag_commit=COMMIT_A),
    )
    result = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    assert result.counts["selected"] == 1
    assert result.counts["written"] == 1
    assert result.coverage[0]["coverage_status"] == "complete"
    document = load_release_documents(store)[0]
    assert document["tag_ref"] == {
        "status": "complete",
        "sha": COMMIT_A,
        "object_type": "commit",
        "commit_sha": COMMIT_A,
        "reason": None,
    }
    assert document["target_commitish"] == "main"
    assert document["published_at"] == "2026-10-01T00:00:00Z"
    assert document["limitations"] == ["observed_current_state"]
    assert store.secure_path(release_path(1)).is_file()

    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["id"] == "github.com/acme/demo/release/v1.0.0"
    )
    assert entity["kind"] == "release"
    assert entity["raw_state"]["commit_sha"] == COMMIT_A
    assert entity["raw_state"]["detail_path"] == release_path(1)


def test_annotated_tag_dereferences_to_commit(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        if endpoint == "repos/acme/demo/releases":
            return http_response(200, [release_body()])
        if endpoint == "repos/acme/demo/git/ref/tags/v1.0.0":
            return http_response(
                200,
                {"ref": "refs/tags/v1.0.0", "object": {"sha": TAG_OBJECT, "type": "tag"}},
            )
        if endpoint == f"repos/acme/demo/git/tags/{TAG_OBJECT}":
            return http_response(
                200,
                {"object": {"sha": COMMIT_B, "type": "commit"}},
            )
        return http_response(404, {"message": "unhandled"})

    store, client = make_client(tmp_path, handler)
    collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    document = load_release_documents(store)[0]
    assert document["tag_ref"]["status"] == "complete"
    assert document["tag_ref"]["sha"] == TAG_OBJECT
    assert document["tag_ref"]["object_type"] == "tag"
    assert document["tag_ref"]["commit_sha"] == COMMIT_B
    assert "tag_unresolved" not in document["limitations"]


def test_prerelease_and_draft_are_distinguished(tmp_path: Path) -> None:
    releases = [
        release_body(1, tag="v1.0.0", prerelease=True),
        release_body(2, tag="v1.1.0", draft=True, published_at=None),
        release_body(3, tag="v0.9.0", published_at="2026-07-01T00:00:00Z"),
    ]
    store, client = make_client(tmp_path, simple_handler(releases))
    result = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    assert result.counts["selected"] == 1
    assert result.counts["drafts_skipped"] == 1
    assert result.counts["outside_window"] == 1
    document = load_release_documents(store)[0]
    assert document["prerelease"] is True
    assert "prerelease" in document["limitations"]


def test_no_releases_is_complete(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, simple_handler([]))
    result = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    assert result.counts["selected"] == 0
    assert result.coverage[0]["coverage_status"] == "complete"
    assert result.coverage[0]["expected_count"] == 0
    assert result.coverage[0]["fetched_count"] == 0
    assert load_release_documents(store) == []


def test_window_uses_published_at_not_created_at(tmp_path: Path) -> None:
    releases = [
        release_body(1, published_at="2026-07-31T23:59:59Z"),
        release_body(2, published_at="2026-08-01T00:00:00Z"),
        release_body(3, published_at="2026-10-31T23:59:59Z"),
        release_body(4, published_at="2026-11-01T00:00:00Z"),
    ]
    store, client = make_client(tmp_path, simple_handler(releases))
    result = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    assert result.counts["selected"] == 2
    assert result.counts["outside_window"] == 2
    assert {document["release_id"] for document in load_release_documents(store)} == {2, 3}


def test_tag_move_updates_document_and_marks_limitation(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, simple_handler([release_body()], tag_commit=COMMIT_A))
    collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    client2 = GitHubClient(
        store,
        BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: simple_handler(
            [release_body()],
            tag_commit=COMMIT_B,
        )(argv[argv.index("--include") + 1], input_text),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )
    result = collect_releases(
        context_for(client2, store, lambda: "2026-11-01T08:00:20Z"),
        activity_window=WINDOW,
    )

    document = load_release_documents(store)[0]
    assert document["tag_ref"]["commit_sha"] == COMMIT_B
    assert document["tag_moved"] is True
    assert document["previous_commit_sha"] == COMMIT_A
    assert "tag_moved" in document["limitations"]
    assert result.coverage[0]["coverage_status"] == "complete"


def test_unresolved_tag_makes_document_partial(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        if endpoint == "repos/acme/demo/releases":
            return http_response(200, [release_body()])
        return http_response(404, {"message": "not found"})

    store, client = make_client(tmp_path, handler)
    result = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    document = load_release_documents(store)[0]
    assert document["tag_ref"]["status"] == "unavailable"
    assert document["tag_ref"]["commit_sha"] is None
    assert document["coverage"]["detail"] == "partial"
    assert "tag_unresolved" in document["limitations"]
    assert result.coverage[0]["coverage_status"] == "partial"


def test_release_pagination_failure_keeps_first_page_visible(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        if endpoint == "repos/acme/demo/releases":
            return http_response(
                200,
                [release_body()],
                {
                    "Link": (
                        "<https://api.github.com/repos/acme/demo/releases?page=2>; "
                        'rel="next"'
                    )
                },
            )
        if endpoint.endswith("releases?page=2"):
            return http_response(404, {"message": "gone"})
        if endpoint.startswith("repos/acme/demo/git/ref/tags/"):
            return http_response(200, tag_ref_body())
        return http_response(404, {"message": "unhandled"})

    store, client = make_client(tmp_path, handler)
    result = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    assert result.counts["selected"] == 1
    assert result.coverage[0]["coverage_status"] == "partial"
    assert "not_found" in result.coverage[0]["truncation_reason"]
    assert load_release_documents(store)[0]["release_id"] == 1


def test_release_limitations_are_derived(tmp_path: Path) -> None:
    document = {
        "draft": False,
        "prerelease": False,
        "tag_ref": {"status": "complete"},
        "tag_moved": False,
        "body": {"sha256": "a" * 64},
        "published_at": "2026-10-01T00:00:00Z",
    }
    assert derive_release_limitations(document) == ["observed_current_state"]



def test_unchanged_release_is_not_rewritten(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, simple_handler([release_body()]))
    first = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )
    observed = load_release_documents(store)[0]["observed_at"]

    client2 = GitHubClient(
        store,
        BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: simple_handler([release_body()])(
            argv[argv.index("--include") + 1], input_text
        ),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )
    second = collect_releases(
        context_for(client2, store, lambda: "2026-11-01T09:00:00Z"),
        activity_window=WINDOW,
    )

    assert first.counts["written"] == 1
    assert second.counts["written"] == 0
    assert second.counts["skipped_unchanged"] == 1
    assert second.coverage[0]["fetched_count"] == 1
    assert second.coverage[0]["coverage_status"] == "complete"
    document = load_release_documents(store)[0]
    assert document["observed_at"] == observed
    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["kind"] == "release"
    )
    assert entity["observed_at"] == observed


def test_deleted_tag_keeps_complete_document(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, simple_handler([release_body()]))
    collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    def missing_tag(endpoint: str, input_text: str) -> CommandResult:
        if endpoint == "repos/acme/demo/releases":
            return http_response(200, [release_body()])
        return http_response(404, {"message": "gone"})

    client2 = GitHubClient(
        store,
        BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: missing_tag(
            argv[argv.index("--include") + 1], input_text
        ),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )
    result = collect_releases(
        context_for(client2, store, lambda: "2026-11-01T09:00:00Z"),
        activity_window=WINDOW,
    )

    document = load_release_documents(store)[0]
    assert document["tag_ref"]["status"] == "complete"
    assert document["tag_ref"]["commit_sha"] == COMMIT_A
    assert result.counts["retained"] == 1
    assert result.coverage[0]["coverage_status"] == "partial"
    assert "refresh_incomplete:v1.0.0" in result.coverage[0]["truncation_reason"]
    assert "tag_unresolved" not in document["limitations"]


def test_release_pagination_duplicates_are_deduped(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        if endpoint == "repos/acme/demo/releases":
            return http_response(
                200,
                [release_body(1)],
                {
                    "Link": (
                        "<https://api.github.com/repos/acme/demo/releases?page=2>; "
                        'rel="next"'
                    )
                },
            )
        if endpoint.endswith("releases?page=2"):
            return http_response(200, [release_body(1)])
        if endpoint.startswith("repos/acme/demo/git/ref/tags/"):
            return http_response(200, tag_ref_body())
        return http_response(404, {"message": "unhandled"})

    store, client = make_client(tmp_path, handler)
    result = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    assert result.counts["selected"] == 1
    assert result.counts["duplicates"] == 1
    assert result.coverage[0]["fetched_count"] == 1
    assert "pagination_duplicates:1" in result.coverage[0]["truncation_reason"]


def test_release_pagination_stops_after_window(tmp_path: Path) -> None:
    requested: list[str] = []

    def handler(endpoint: str, input_text: str) -> CommandResult:
        requested.append(endpoint)
        if endpoint == "repos/acme/demo/releases":
            return http_response(
                200,
                [release_body(1, published_at="2026-10-01T00:00:00Z")],
                {
                    "Link": (
                        "<https://api.github.com/repos/acme/demo/releases?page=2>; "
                        'rel="next"'
                    )
                },
            )
        if endpoint.endswith("releases?page=2"):
            return http_response(
                200,
                [release_body(2, published_at="2026-07-01T00:00:00Z")],
                {
                    "Link": (
                        "<https://api.github.com/repos/acme/demo/releases?page=3>; "
                        'rel="next"'
                    )
                },
            )
        if endpoint.endswith("releases?page=3"):
            raise AssertionError("collector must stop after the window")
        if endpoint.startswith("repos/acme/demo/git/ref/tags/"):
            return http_response(200, tag_ref_body())
        return http_response(404, {"message": "unhandled"})

    store, client = make_client(tmp_path, handler)
    result = collect_releases(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        activity_window=WINDOW,
    )

    assert not any("page=3" in endpoint for endpoint in requested)
    assert result.counts["selected"] == 1
    assert result.counts["outside_window"] == 1
    assert result.coverage[0]["coverage_status"] == "complete"
    assert "release_window_reached" in result.coverage[0]["truncation_reason"]


def test_budget_exhaustion_degrades_to_partial(tmp_path: Path) -> None:
    tight = {**BUDGET, "max_requests": 1}
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    client = GitHubClient(
        store,
        tight,
        gh_path="gh",
        runner=lambda argv, input_text: simple_handler([release_body()])(
            argv[argv.index("--include") + 1], input_text
        ),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )
    context = context_for(client, store, lambda: "2026-11-01T08:00:10Z")
    result = collect_releases(
        context,
        activity_window=WINDOW,
    )

    assert context.budget_exhausted is True
    assert result.counts["selected"] == 1
    assert result.counts["written"] == 0
    assert result.coverage[0]["coverage_status"] == "partial"
    assert "budget_exceeded" in result.coverage[0]["truncation_reason"]


def test_release_write_failure_finalizes_partial_then_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from rer.collectors import releases as releases_module
    from rer.collectors.progress import CollectProgress
    from rer.errors import StoreError

    releases = [release_body(1), release_body(2, tag="v1.1.0")]
    store, client = make_client(tmp_path, simple_handler(releases))
    progress = CollectProgress(store, "a" * 64)
    context = context_for(client, store, lambda: "2026-11-01T08:00:10Z")
    context.progress = progress
    real_write = releases_module.write_release_document
    calls = {"count": 0}

    def fail_second(store_arg, document) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise StoreError("disk full", source="test.write")
        real_write(store_arg, document)

    monkeypatch.setattr(releases_module, "write_release_document", fail_second)

    with pytest.raises(StoreError):
        releases_module.collect_releases(context, activity_window=WINDOW)

    checkpoint = store.read_checkpoint("collect")
    entries = {item["channel"]: item for item in checkpoint["coverage"]}
    assert entries["releases"]["coverage_status"] == "partial"
    assert entries["releases"]["fetched_count"] == 1
    assert checkpoint["stages"]["releases"]["status"] == "partial"
