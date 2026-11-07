from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rer import cli
from rer.collectors import pull_requests as pr_module
from rer.collectors.entity_store import detail_path
from rer.collectors.pr_store import load_pr_document
from rer.collectors.progress import CollectProgress, has_blocking_gaps
from rer.collectors.pull_requests import (
    _read_pr,
    _reusable_code_sections,
    _section_specs,
    collect_pull_requests,
)
from rer.collectors.selection import derive_fetched_count, derive_selection_counts
from rer.collectors.status import PR_DETAIL_SECTIONS
from rer.errors import (
    BudgetExceededError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    SchemaError,
    StoreError,
)
from rer.github import CommandResult, GitHubClient
from rer.models import make_entity, validate_document
from rer.normalize import build_evidence_set, iter_text_sources
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
HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def http_response(
    status: int,
    body: Any,
    headers: dict[str, str] | None = None,
) -> CommandResult:
    lines = [f"HTTP/2.0 {status} OK"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    lines.append("")
    if isinstance(body, str):
        lines.append(body)
    else:
        lines.append(json.dumps(body))
    return CommandResult(0 if status < 400 else 1, "\n".join(lines), "")


def parse_params(argv: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for index, value in enumerate(argv):
        if value in ("-f", "-F") and index + 1 < len(argv):
            key, item = argv[index + 1].split("=", 1)
            params[key] = item
    return params


def pr_record(
    number: int,
    *,
    merged_at: str | None = None,
    updated_at: str = "2026-10-20T10:00:00Z",
    comments: int = 0,
    reactions: int = 0,
    labels: list[str] | None = None,
    draft: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/acme/demo/pull/{number}",
        "state": "closed" if merged_at else "open",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": updated_at,
        "closed_at": merged_at,
        "labels": [{"name": label} for label in (labels or [])],
        "comments": comments,
        "reactions": {"total_count": reactions},
        "user": {"login": "alice"},
        "draft": draft,
        "pull_request": {"merged_at": merged_at},
    }


def detail_body(number: int, *, head_sha: str = HEAD, **overrides: Any) -> dict[str, Any]:
    body = {
        "number": number,
        "html_url": f"https://github.com/acme/demo/pull/{number}",
        "title": f"PR {number}",
        "body": f"Body for {number}",
        "state": "closed",
        "draft": False,
        "merged": True,
        "merged_at": "2026-10-20T10:00:00Z",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-10-20T10:00:00Z",
        "closed_at": "2026-10-20T10:00:00Z",
        "user": {"login": "alice"},
        "base": {"sha": "c" * 40},
        "head": {"sha": head_sha},
        "merge_commit_sha": "d" * 40,
        "additions": 12,
        "deletions": 3,
        "changed_files": 2,
        "commits": 1,
        "comments": 1,
        "review_comments": 1,
    }
    body.update(overrides)
    return body


def file_body(
    name: str = "src/cache.py",
    *,
    patch: Any = "@@ -1 +1 @@",
) -> dict[str, Any]:
    return {
        "filename": name,
        "status": "modified",
        "additions": 10,
        "deletions": 2,
        "changes": 12,
        "patch": patch,
        "sha": "e" * 40,
    }


def commit_body(sha: str = HEAD) -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": "Add planner",
            "author": {"name": "Alice", "date": "2026-10-19T09:00:00Z"},
        },
    }


def issue_comment_body(identifier: int = 9001) -> dict[str, Any]:
    return {
        "id": identifier,
        "user": {"login": "bob"},
        "created_at": "2026-10-02T08:00:00Z",
        "updated_at": "2026-10-02T08:00:00Z",
        "author_association": "MEMBER",
        "body": "Looks reasonable.",
    }


def review_body(identifier: int = 7001, *, commit_id: str = HEAD) -> dict[str, Any]:
    return {
        "id": identifier,
        "user": {"login": "carol"},
        "state": "APPROVED",
        "submitted_at": "2026-10-19T12:00:00Z",
        "commit_id": commit_id,
        "body": "LGTM",
    }


def review_comment_body(identifier: int = 8001, *, commit_id: str = "f" * 40) -> dict[str, Any]:
    return {
        "id": identifier,
        "user": {"login": "carol"},
        "path": "src/cache.py",
        "line": 42,
        "original_line": 42,
        "commit_id": commit_id,
        "in_reply_to_id": None,
        "pull_request_review_id": 7001,
        "diff_hunk": "@@ -40 +40 @@",
        "body": "Please add a test.",
    }


def default_handler(
    *,
    files: dict[int, list[dict[str, Any]]] | None = None,
    commits: dict[int, list[dict[str, Any]]] | None = None,
    details: dict[int, dict[str, Any]] | None = None,
    detail_sequence: dict[int, list[dict[str, Any]]] | None = None,
    calls: list[str] | None = None,
):
    details = details or {}
    sequences = detail_sequence or {}
    counter: dict[int, int] = {}

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if calls is not None:
            calls.append(endpoint)
        number = _number_from_endpoint(endpoint)
        if endpoint == f"repos/acme/demo/pulls/{number}":
            if number in sequences:
                index = min(counter.get(number, 0), len(sequences[number]) - 1)
                counter[number] = counter.get(number, 0) + 1
                return http_response(200, sequences[number][index])
            return http_response(200, details.get(number, detail_body(number)))
        if endpoint == f"repos/acme/demo/pulls/{number}/files":
            if files is not None and number in files:
                return http_response(200, files[number])
            return http_response(200, [file_body(), file_body("assets/model.bin", patch=None)])
        if endpoint == f"repos/acme/demo/pulls/{number}/commits":
            if commits is not None and number in commits:
                return http_response(200, commits[number])
            return http_response(200, [commit_body()])
        if endpoint == f"repos/acme/demo/issues/{number}/comments":
            return http_response(200, [issue_comment_body(9000 + number)])
        if endpoint == f"repos/acme/demo/pulls/{number}/reviews":
            return http_response(200, [review_body(7000 + number)])
        if endpoint == f"repos/acme/demo/pulls/{number}/comments":
            return http_response(200, [review_comment_body(8000 + number)])
        return http_response(404, {"message": "unhandled"})

    return handler


def _number_from_endpoint(endpoint: str) -> int:
    for part in endpoint.split("/"):
        if part.isdigit():
            return int(part)
    return 0


def make_client(
    tmp_path: Path,
    handler,
    *,
    budget: dict[str, Any] | None = None,
    max_attempts: int = 1,
) -> tuple[SnapshotStore, GitHubClient]:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    return store, build_client(store, handler, budget=budget, max_attempts=max_attempts)


def build_client(
    store: SnapshotStore,
    handler,
    *,
    budget: dict[str, Any] | None = None,
    max_attempts: int = 1,
    initial_usage: dict[str, Any] | None = None,
) -> GitHubClient:
    return GitHubClient(
        store,
        budget or BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: handler(
            argv[argv.index("--include") + 1],
            parse_params(argv),
            _headers(argv),
        ),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=max_attempts,
        initial_usage=initial_usage,
    )


def _headers(argv: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for index, value in enumerate(argv):
        if value == "-H" and index + 1 < len(argv):
            key, item = argv[index + 1].split(":", 1)
            headers[key] = item.strip()
    return headers


def seed(
    store: SnapshotStore,
    numbers: list[int],
    *,
    records: dict[int, dict[str, Any]] | None = None,
    entities: bool = True,
) -> None:
    records = records or {}
    raw = [
        records.get(number) or pr_record(number, merged_at="2026-10-01T00:00:00Z")
        for number in numbers
    ]
    store.write_jsonl("raw/prs.jsonl", raw, key="number")
    store.write_json(
        "analysis/activity-overview.json",
        {
            "repository": REPOSITORY,
            "views": [
                {
                    "kind": "pull_request",
                    "candidate_ids": [
                        f"github.com/acme/demo/pull/{number}" for number in numbers
                    ],
                }
            ],
        },
    )
    if entities:
        store.write_jsonl(
            "normalized/entities.jsonl",
            [
                make_entity(
                    entity_id=f"github.com/acme/demo/pull/{number}",
                    kind="pull_request",
                    title=f"PR {number}",
                    url=f"https://github.com/acme/demo/pull/{number}",
                    observed_at="2026-11-01T00:00:00Z",
                    number=number,
                    source_event_at="2026-10-20T10:00:00Z",
                    discovery_reasons=["pr_created"],
                )
                for number in numbers
            ],
            key="id",
            schema_name="entity",
        )


def test_collects_selected_pull_request_detail(tmp_path: Path) -> None:
    handler = default_handler()
    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        focus=["cache"],
        clock=lambda: "2026-11-01T08:00:10Z",
    )

    assert result.counts["selected"] == 1
    assert result.counts["details_written"] == 1
    assert result.coverage[0]["channel"] == "pr_detail"
    assert result.coverage[0]["coverage_status"] == "complete"
    assert not has_blocking_gaps(result.coverage)

    document = load_pr_document(store, 10)
    assert document is not None
    validate_document(document, "pr_detail")
    assert document["merged"] is True
    assert document["head_sha"] == HEAD
    assert document["head_changed"] is False
    assert document["files"]["fetched"] == 2
    assert document["files"]["items"][1]["patch_status"] == "missing"
    assert "patch_missing" in document["limitations"]
    assert document["review_comments"]["items"][0]["outdated"] is True
    assert document["reviews"]["items"][0]["resolution"] == "unknown"
    assert document["diff"]["status"] == "not_requested"

    assert len(document["files"]["items"]) == 2
    assert len(document["commits"]["items"]) == 1
    assert len(document["issue_comments"]["items"]) == 1
    assert len(document["reviews"]["items"]) == 1
    assert len(document["review_comments"]["items"]) == 1

    body = document["body"]
    assert body["sha256"] is not None
    assert store.read_blob(body["sha256"]).decode("utf-8") == "Body for 10"

    selection = json.loads(
        (store.run_dir / "analysis/pr-selection.json").read_text(encoding="utf-8")
    )
    validate_document(selection, "pr_selection")
    assert selection["counts"]["selected"] == 1

    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["id"] == "github.com/acme/demo/pull/10"
    )
    assert "deep_read" not in entity["normalized_state"]
    assert entity["raw_state"]["deep_read"] == {
        "detail_path": "raw/pr-details/10.json",
        "head_sha": HEAD,
        "observed_at": "2026-11-01T08:00:10Z",
    }


def test_default_quota_covers_all_candidates(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    numbers = [1, 2, 3, 4, 5, 6]
    records = {
        1: pr_record(1, merged_at="2026-10-01T00:00:00Z"),
        2: pr_record(2, merged_at="2026-10-02T00:00:00Z"),
        3: pr_record(3, merged_at="2026-10-03T00:00:00Z"),
        4: pr_record(4),
        5: pr_record(5),
        6: pr_record(6),
    }
    seed(store, numbers, records=records)
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )

    assert result.counts["selected"] == 6
    assert result.counts["merged_selected"] == 3
    assert result.counts["open_selected"] == 3
    assert result.selection["quota"] == {
        "pr_quota": 22,
        "merged_quota": 14,
        "open_quota": 8,
        "max_deep_entities": 40,
        "deep_entities_used": 6,
        "agent_pr_reserve": 0,
    }


def test_agent_pr_reserve_reduces_initial_collect_quota(tmp_path: Path) -> None:
    budget = {**BUDGET, "agent_pr_reserve": 4, "max_requests": 300}
    store, client = make_client(tmp_path, default_handler(), budget=budget)
    numbers = list(range(1, 26))
    records = {
        number: pr_record(
            number,
            merged_at="2026-10-01T00:00:00Z" if number <= 14 else None,
        )
        for number in numbers
    }
    seed(store, numbers, records=records)
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )

    assert result.selection["quota"]["pr_quota"] == 18
    assert result.selection["quota"]["agent_pr_reserve"] == 4
    assert result.counts["selected"] == 18
    assert result.counts["skipped_quota"] == 7


def test_quota_limits_selection_and_records_skips(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    numbers = [1, 2, 3, 4, 5, 6]
    records = {
        1: pr_record(1, merged_at="2026-10-01T00:00:00Z"),
        2: pr_record(2, merged_at="2026-10-02T00:00:00Z"),
        3: pr_record(3, merged_at="2026-10-03T00:00:00Z"),
        4: pr_record(4),
        5: pr_record(5),
        6: pr_record(6),
    }
    seed(store, numbers, records=records)
    client.budget["max_deep_prs"] = 3
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )

    assert result.counts["selected"] == 3
    assert result.counts["merged_selected"] == 2
    assert result.counts["open_selected"] == 1
    assert result.counts["skipped_quota"] == 3
    assert result.selection["quota"]["pr_quota"] == 3
    skipped = [
        item for item in result.selection["candidates"] if item["status"] == "skipped_quota"
    ]
    assert len(skipped) == 3
    assert all(item["skipped_reason"] == "quota_exhausted" for item in skipped)


def test_nested_pagination_follows_link_headers(tmp_path: Path) -> None:
    base = default_handler(details={10: detail_body(10, changed_files=101)})

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/pulls/10/files":
            return http_response(
                200,
                [file_body(f"src/file_{index}.py") for index in range(100)],
                {
                    "Link": (
                        "<https://api.github.com/repos/acme/demo/pulls/10/files?page=2>; "
                        'rel="next"'
                    )
                },
            )
        if endpoint.endswith("/pulls/10/files?page=2"):
            return http_response(200, [file_body("src/last.py")])
        return base(endpoint, params, headers)

    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )

    document = load_pr_document(store, 10)
    assert document is not None
    assert document["files"]["fetched"] == 101
    assert document["files"]["total_known"] == 101
    assert document["files"]["truncated"] is False
    assert document["files"]["status"] == "complete"
    assert result.coverage[0]["pages"] == 8


def test_pagination_duplicates_are_deduped_and_reported(tmp_path: Path) -> None:
    detail = detail_body(
        10,
        changed_files=1,
        commits=1,
        comments=1,
        review_comments=1,
    )
    base = default_handler(details={10: detail})
    file = file_body()
    comment = issue_comment_body(9001)
    review = review_body(7001)
    review_comment = review_comment_body(8001)

    def link(path: str) -> dict[str, str]:
        return {
            "Link": (
                f"<https://api.github.com/repos/acme/demo/{path}?page=2>; "
                'rel="next"'
            )
        }

    def handler(
        endpoint: str,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> CommandResult:
        if endpoint == "repos/acme/demo/pulls/10/files":
            return http_response(200, [file], link("pulls/10/files"))
        if endpoint.endswith("/pulls/10/files?page=2"):
            return http_response(200, [file])
        if endpoint == "repos/acme/demo/issues/10/comments":
            return http_response(200, [comment], link("issues/10/comments"))
        if endpoint.endswith("/issues/10/comments?page=2"):
            return http_response(200, [comment])
        if endpoint == "repos/acme/demo/pulls/10/reviews":
            return http_response(200, [review], link("pulls/10/reviews"))
        if endpoint.endswith("/pulls/10/reviews?page=2"):
            return http_response(200, [review])
        if endpoint == "repos/acme/demo/pulls/10/comments":
            return http_response(200, [review_comment], link("pulls/10/comments"))
        if endpoint.endswith("/pulls/10/comments?page=2"):
            return http_response(200, [review_comment])
        return base(endpoint, params, headers)

    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )

    document = load_pr_document(store, 10)
    assert document is not None
    for section in ("files", "issue_comments", "reviews", "review_comments"):
        assert document[section]["fetched"] == 1
        assert document[section]["duplicates"] == 1
    assert document["files"]["items"][0]["filename"] == "src/cache.py"
    assert document["issue_comments"]["items"][0]["id"] == 9001
    assert document["reviews"]["items"][0]["id"] == 7001
    assert document["review_comments"]["items"][0]["id"] == 8001
    assert "pagination_duplicates:4" in document["limitations"]
    entry = next(item for item in result.coverage if item["channel"] == "pr_detail")
    assert "pagination_duplicates:#10" in (entry["truncation_reason"] or "")
    records, _pairs, _truncated = build_evidence_set(
        iter_text_sources(store, host="github.com", owner="acme", name="demo")
    )
    assert len(records) == len({record["id"] for record in records})


def test_files_and_commits_caps_are_visible(tmp_path: Path) -> None:
    files = [file_body(f"src/file_{index}.py") for index in range(3000)]
    commits = [commit_body(f"{index:040x}") for index in range(250)]
    handler = default_handler(
        files={10: files},
        commits={10: commits},
        details={10: detail_body(10, changed_files=3200, commits=300)},
    )
    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )

    document = load_pr_document(store, 10)
    assert document is not None
    assert document["files"]["fetched"] == 3000
    assert document["files"]["truncated"] is True
    assert document["files"]["truncation_reason"] == "files_limit_3000"
    assert document["commits"]["fetched"] == 250
    assert document["commits"]["truncated"] is True
    assert document["commits"]["truncation_reason"] == "commits_limit_250"
    assert "files_truncated" in document["limitations"]
    assert "commits_truncated" in document["limitations"]
    assert document["coverage"]["files"] == "partial"
    assert document["coverage"]["detail"] == "partial"
    assert result.coverage[0]["coverage_status"] == "partial"
    assert not has_blocking_gaps(result.coverage)


def test_head_change_marks_partial(tmp_path: Path) -> None:
    handler = default_handler(
        detail_sequence={10: [detail_body(10, head_sha=HEAD), detail_body(10, head_sha=OTHER_HEAD)]}
    )
    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )

    document = load_pr_document(store, 10)
    assert document is not None
    assert document["head_changed"] is True
    assert document["head_sha_end"] == OTHER_HEAD
    assert document["coverage"]["detail"] == "partial"
    assert "head_changed" in document["limitations"]
    assert result.coverage[0]["coverage_status"] == "partial"


def test_rate_limit_propagates_and_writes_checkpoint(tmp_path: Path) -> None:
    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint.endswith("/files"):
            return http_response(429, {"message": "slow down"}, {"Retry-After": "120"})
        return default_handler()(endpoint, params, headers)

    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    progress = CollectProgress(store, "0" * 64)
    with pytest.raises(GitHubRateLimitError):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=progress,
            clock=lambda: "2026-11-01T08:00:10Z",
        )

    checkpoint = store.read_checkpoint("collect")
    stage = checkpoint["stages"]["pr_detail"]
    assert stage["status"] == "partial"
    assert stage["completed"] == []
    assert stage["pending"] == [10]
    assert [path.name for path in (store.run_dir / "checkpoints").iterdir()] == [
        "collect.json"
    ]


def test_budget_interrupt_then_resume_skips_completed(tmp_path: Path) -> None:
    handler = default_handler()
    store, client = make_client(tmp_path, handler, budget={**BUDGET, "max_requests": 8})
    seed(store, [10, 11])
    progress = CollectProgress(store, "0" * 64)
    with pytest.raises(BudgetExceededError):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=progress,
            clock=lambda: "2026-11-01T08:00:10Z",
        )
    checkpoint = store.read_checkpoint("collect")
    stage = checkpoint["stages"]["pr_detail"]
    assert stage["status"] == "partial"
    assert stage["completed"] == [11]
    assert stage["pending"] == [10]
    entry = next(
        item for item in checkpoint["coverage"] if item["channel"] == "pr_detail"
    )
    assert entry["pages"] >= 8

    client2 = build_client(store, handler)
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["skipped_unchanged"] == 1
    assert result.counts["details_written"] == 1
    assert result.counts["selected"] == 2
    assert result.coverage[0]["coverage_status"] == "complete"
    assert result.coverage[0]["fetched_count"] == 2


def test_force_reread_does_not_double_count_deep_entities(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    assert client.usage["deep_entities"] == 1

    client2 = build_client(
        store,
        default_handler(),
        initial_usage={"deep_entities": 1},
    )
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        force=True,
        clock=lambda: "2026-11-01T08:00:20Z",
    )

    assert result.counts["details_written"] == 1
    assert client2.usage["deep_entities"] == 0
    assert client2.deep_entities_used() == 1


def test_checkpoint_failure_does_not_mask_collector_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base = default_handler()

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]):
        if endpoint == "repos/acme/demo/pulls/10/files":
            return http_response(500, {"message": "boom"})
        return base(endpoint, params, headers)

    store, client = make_client(tmp_path, handler)
    seed(store, [10])

    def boom(*args, **kwargs):
        raise StoreError("checkpoint failed", source="store.test")

    monkeypatch.setattr(store, "write_checkpoint", boom)
    with pytest.raises(GitHubUnavailableError):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=CollectProgress(store, "0" * 64),
            clock=lambda: "2026-11-01T08:00:10Z",
        )


def test_checkpoint_failure_does_not_fail_successful_collect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])

    def boom(*args, **kwargs):
        raise StoreError("checkpoint failed", source="store.test")

    monkeypatch.setattr(store, "write_checkpoint", boom)
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        progress=CollectProgress(store, "0" * 64),
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    assert result.counts["details_written"] == 1
    assert load_pr_document(store, 10) is not None


def test_partial_document_is_not_completed_on_failure(
    tmp_path: Path,
) -> None:
    handler = default_handler(
        details={10: detail_body(10, changed_files=5)}
    )
    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        progress=CollectProgress(store, "0" * 64),
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    assert document["coverage"]["detail"] == "partial"

    def failing(endpoint: str, params: dict[str, str], headers: dict[str, str]):
        if endpoint == "repos/acme/demo/pulls/10":
            return http_response(500, {"message": "boom"})
        return default_handler()(endpoint, params, headers)

    client2 = build_client(store, failing)
    with pytest.raises(GitHubUnavailableError):
        collect_pull_requests(
            client2,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=CollectProgress(store, "0" * 64),
            clock=lambda: "2026-11-01T08:00:20Z",
        )
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage["completed"] == []
    assert stage["pending"] == [10]


@pytest.mark.parametrize(
    "failure",
    [
        "detail",
        "files",
        "commits",
        "issue_comments",
        "reviews",
        "review_comments",
        "head",
    ],
)
def test_checkpoint_only_lists_completed_documents_on_failure(
    tmp_path: Path,
    failure: str,
) -> None:
    detail_calls = 0
    base = default_handler()

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]):
        nonlocal detail_calls
        if endpoint == "repos/acme/demo/pulls/10":
            detail_calls += 1
            if failure == "detail" and detail_calls == 1:
                return http_response(500, {"message": "boom"})
            if failure == "head" and detail_calls == 2:
                return http_response(500, {"message": "boom"})
        endpoints = {
            "files": "repos/acme/demo/pulls/10/files",
            "commits": "repos/acme/demo/pulls/10/commits",
            "issue_comments": "repos/acme/demo/issues/10/comments",
            "reviews": "repos/acme/demo/pulls/10/reviews",
            "review_comments": "repos/acme/demo/pulls/10/comments",
        }
        if failure in endpoints and endpoint == endpoints[failure]:
            return http_response(500, {"message": "boom"})
        return base(endpoint, params, headers)

    store, client = make_client(tmp_path, handler)
    seed(store, [10, 11])
    progress = CollectProgress(store, "0" * 64)
    with pytest.raises(GitHubUnavailableError):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=progress,
            clock=lambda: "2026-11-01T08:00:10Z",
        )

    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage == {"status": "partial", "completed": [11], "pending": [10]}
    assert (store.run_dir / "raw/pr-details/11.json").is_file()
    assert not (store.run_dir / "raw/pr-details/10.json").exists()
    assert [path.name for path in (store.run_dir / "checkpoints").iterdir()] == [
        "collect.json"
    ]


def test_resume_trusts_documents_over_checkpoint(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        progress=CollectProgress(store, "0" * 64),
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage["completed"] == [10]

    (store.run_dir / "raw/pr-details/10.json").unlink()

    client2 = build_client(store, default_handler())
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["details_written"] == 1
    assert result.counts["skipped_unchanged"] == 0
    assert load_pr_document(store, 10) is not None


def test_force_rereads_unchanged_pull_request(tmp_path: Path) -> None:
    handler = default_handler()
    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    calls: list[str] = []
    client2 = build_client(store, default_handler(calls=calls))
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
        force=True,
    )
    assert result.counts["skipped_unchanged"] == 0
    assert result.counts["details_written"] == 1
    assert "repos/acme/demo/pulls/10/files" in calls


def test_missing_metadata_is_skipped_with_limitation(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    store.write_jsonl("raw/prs.jsonl", [], key="number")
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    assert result.counts["candidates"] == 0
    assert result.coverage[0]["coverage_status"] == "not_requested"


def test_stale_raw_rows_are_not_selected(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    overview = store.read_json("analysis/activity-overview.json")
    overview["views"][0]["candidate_ids"] = []
    store.write_json("analysis/activity-overview.json", overview)
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    assert result.counts["candidates"] == 0
    assert result.coverage[0]["coverage_status"] == "not_requested"


def _run_cli_collect(
    tmp_path: Path,
    monkeypatch,
    handler,
    *,
    numbers: list[int] | None = None,
    records: dict[int, dict[str, Any]] | None = None,
) -> tuple[int, Path, SnapshotStore]:
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
    seed(store, numbers or [10], records=records)
    overview = store.read_json("analysis/activity-overview.json")

    monkeypatch.setattr(
        cli,
        "collect_repository",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            source_profile={},
            default_branch="main",
            default_branch_sha="a" * 40,
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_activity",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            overview=overview,
            counts={"pr_created": 1},
            candidate_count=1,
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_proposals",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            selection={},
            documents=[],
            counts={
                "candidates": 0,
                "selected": 0,
                "issue_selected": 0,
                "discussion_selected": 0,
                "context_selected": 0,
                "skipped_quota": 0,
                "skipped_unchanged": 0,
                "failed": 0,
                "not_attempted": 0,
            },
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_releases",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            documents=[],
            counts={
                "listed": 0,
                "selected": 0,
                "written": 0,
                "drafts_skipped": 0,
                "outside_window": 0,
                "invalid": 0,
                "tag_unresolved": 0,
            },
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_documents",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            documents=[],
            counts={
                "selected": 0,
                "written": 0,
                "reused": 0,
                "read": 0,
                "failed": 0,
            },
        ),
    )

    def factory(client_store, budget, **kwargs):
        return GitHubClient(
            client_store,
            budget,
            gh_path="gh",
            runner=lambda argv, input_text: handler(
                argv[argv.index("--include") + 1],
                parse_params(argv),
                _headers(argv),
            ),
            sleep=lambda _seconds: None,
            now=lambda: NOW,
            initial_usage=kwargs.get("initial_usage"),
            max_attempts=1,
        )

    monkeypatch.setattr(cli, "GitHubClient", factory)
    real_collect = cli.collect_pull_requests
    monkeypatch.setattr(
        cli,
        "collect_pull_requests",
        lambda *args, **kwargs: real_collect(
            *args,
            clock=lambda: "2026-11-01T08:00:10Z",
            **kwargs,
        ),
    )
    code = cli.main(["collect", "--run", str(run_dir)])
    return code, run_dir, store


def test_cli_pr_detail_gaps_warn_without_failing(tmp_path: Path, monkeypatch) -> None:
    code, run_dir, store = _run_cli_collect(
        tmp_path,
        monkeypatch,
        default_handler(details={10: detail_body(10, changed_files=5)}),
    )
    assert code == 0
    manifest = store.read_manifest()
    channels = {item["channel"]: item for item in manifest["coverage"]}
    assert channels["pr_detail"]["coverage_status"] == "partial"
    assert manifest["stages"]["collect"] == "complete"
    document = load_pr_document(store, 10)
    assert document is not None
    assert document["files"]["truncated"] is True
    assert document["files"]["truncation_reason"] == "files_total_mismatch"


def test_cli_collect_integrates_pr_detail(tmp_path: Path, monkeypatch) -> None:
    code, run_dir, store = _run_cli_collect(tmp_path, monkeypatch, default_handler())
    assert code == 0
    manifest = store.read_manifest()
    channels = {item["channel"]: item for item in manifest["coverage"]}
    assert "pr_detail" in channels
    assert channels["pr_detail"]["coverage_status"] == "complete"
    assert manifest["stages"]["collect"] == "complete"
    assert (run_dir / "raw/pr-details/10.json").is_file()
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage["status"] == "complete"
    assert stage["completed"] == [10]
    assert stage["pending"] == []


def test_second_run_with_all_unchanged_is_complete(tmp_path: Path) -> None:
    calls: list[str] = []
    handler = default_handler(calls=calls)
    store, client = make_client(tmp_path, handler)
    seed(store, [10, 11])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    first = load_pr_document(store, 10)
    assert first is not None
    calls.clear()
    client2 = build_client(store, handler)
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        progress=CollectProgress(store, "0" * 64),
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["skipped_unchanged"] == 2
    assert result.counts["details_written"] == 0
    assert result.coverage[0]["coverage_status"] == "complete"
    assert result.coverage[0]["pages"] == 10
    assert "pr_detail_incomplete" not in result.selection["limitations"]
    assert "repos/acme/demo/pulls/10/files" not in calls
    assert "repos/acme/demo/pulls/10/commits" not in calls
    assert "repos/acme/demo/issues/10/comments" in calls
    assert "repos/acme/demo/pulls/10/reviews" in calls
    assert "repos/acme/demo/pulls/10/comments" in calls
    second = load_pr_document(store, 10)
    assert second is not None
    assert second["observed_at"] == first["observed_at"]
    candidate = next(
        item for item in result.selection["candidates"] if item["number"] == 10
    )
    assert candidate["skipped_reason"] == "materials_unchanged"
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage["status"] == "complete"
    assert stage["completed"] == [10, 11]


def test_resume_refreshes_mutable_state_without_head_change(tmp_path: Path) -> None:
    state = {"second": False}
    old_body = "body-10"
    new_body = "body-10-updated"
    first_detail = detail_body(
        10,
        state="open",
        merged=False,
        merged_at=None,
        closed_at=None,
        comments=1,
        body=old_body,
    )
    second_detail = detail_body(
        10,
        state="closed",
        merged=True,
        merged_at="2026-10-21T00:00:00Z",
        closed_at="2026-10-21T00:00:00Z",
        comments=2,
        body=new_body,
        updated_at="2026-10-21T00:00:00Z",
    )
    first_review = review_body(7001)
    second_review = {
        **review_body(7001),
        "state": "DISMISSED",
        "body": "Updated review",
    }
    base = default_handler()

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/pulls/10":
            return http_response(200, second_detail if state["second"] else first_detail)
        if endpoint == "repos/acme/demo/issues/10/comments":
            comments = [issue_comment_body(9001)]
            if state["second"]:
                comments.append(issue_comment_body(9002))
            return http_response(200, comments)
        if endpoint == "repos/acme/demo/pulls/10/reviews":
            return http_response(200, [second_review if state["second"] else first_review])
        return base(endpoint, params, headers)

    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    first = load_pr_document(store, 10)
    assert first is not None
    assert first["merged"] is False
    old_digest = first["body"]["sha256"]
    assert old_digest is not None
    assert store.read_blob(old_digest) == old_body.encode()

    state["second"] = True
    client2 = build_client(store, handler)
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    second = load_pr_document(store, 10)
    assert second is not None
    assert result.counts["details_written"] == 1
    assert result.counts["skipped_unchanged"] == 0
    assert second["state"] == "closed"
    assert second["merged"] is True
    assert second["merged_at"] == "2026-10-21T00:00:00Z"
    assert second["closed_at"] == "2026-10-21T00:00:00Z"
    assert second["updated_at"] == "2026-10-21T00:00:00Z"
    assert second["comments_count"] == 2
    assert len(second["issue_comments"]["items"]) == 2
    assert second["reviews"]["items"][0]["state"] == "DISMISSED"
    review_digest = second["reviews"]["items"][0]["body"]["sha256"]
    assert review_digest is not None
    assert store.read_blob(review_digest) == b"Updated review"
    assert second["body"]["sha256"] != old_digest
    assert second["body"]["sha256"] is not None
    assert store.read_blob(second["body"]["sha256"]) == new_body.encode()
    assert second["observed_at"] == "2026-11-01T08:00:20Z"


def test_resume_rereads_code_sections_when_base_sha_changes(tmp_path: Path) -> None:
    calls: list[str] = []
    state = {"second": False}
    base = default_handler(calls=calls)
    other_base = "e" * 40

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/pulls/10":
            detail = detail_body(10)
            if state["second"]:
                detail = detail_body(10, base={"sha": other_base})
            return http_response(200, detail)
        return base(endpoint, params, headers)

    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    calls.clear()
    state["second"] = True
    client2 = build_client(store, handler)
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    assert document["base_sha"] == other_base
    assert result.counts["details_written"] == 1
    assert "repos/acme/demo/pulls/10/files" in calls
    assert "repos/acme/demo/pulls/10/commits" in calls


def test_resume_rereads_code_sections_when_text_blob_is_missing(tmp_path: Path) -> None:
    calls: list[str] = []
    handler = default_handler(calls=calls)
    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    patch_digest = document["files"]["items"][0]["patch"]["sha256"]
    assert isinstance(patch_digest, str)
    blob = store.secure_path(Path("blobs") / patch_digest)
    blob.unlink()
    calls.clear()

    client2 = build_client(store, handler)
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["skipped_unchanged"] == 1
    assert "repos/acme/demo/pulls/10/files" in calls
    assert "repos/acme/demo/pulls/10/commits" not in calls
    assert blob.is_file()


def test_resume_does_not_downgrade_complete_document_on_comment_not_found(
    tmp_path: Path,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    old = load_pr_document(store, 10)
    assert old is not None
    old_bytes = store.secure_path(detail_path(10)).read_bytes()
    base = default_handler()

    def failing(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/issues/10/comments":
            return http_response(404, {"message": "not found"})
        return base(endpoint, params, headers)

    client2 = build_client(store, failing)
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        progress=CollectProgress(store, "0" * 64),
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["details_written"] == 0
    assert result.counts["failed"] == 1
    assert result.counts["skipped_unchanged"] == 0
    assert result.coverage[0]["coverage_status"] == "partial"
    assert store.secure_path(detail_path(10)).read_bytes() == old_bytes
    assert load_pr_document(store, 10) == old
    candidate = next(
        item for item in result.selection["candidates"] if item["number"] == 10
    )
    assert candidate["status"] == "failed"
    assert candidate["skipped_reason"] == "refresh_incomplete"
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage["status"] == "partial"
    assert stage["completed"] == []
    assert stage["pending"] == [10]


def test_resume_preserves_complete_document_on_comment_unavailable(
    tmp_path: Path,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    old_bytes = store.secure_path(detail_path(10)).read_bytes()
    base = default_handler()

    def failing(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/issues/10/comments":
            return http_response(500, {"message": "boom"})
        return base(endpoint, params, headers)

    client2 = build_client(store, failing)
    with pytest.raises(GitHubUnavailableError):
        collect_pull_requests(
            client2,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            clock=lambda: "2026-11-01T08:00:20Z",
        )
    assert store.secure_path(detail_path(10)).read_bytes() == old_bytes


def test_resume_preserves_complete_document_on_head_not_found(
    tmp_path: Path,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    old = load_pr_document(store, 10)
    assert old is not None
    old_bytes = store.secure_path(detail_path(10)).read_bytes()
    base = default_handler()
    detail_calls = {"count": 0}

    def failing(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/pulls/10":
            detail_calls["count"] += 1
            if detail_calls["count"] == 2:
                return http_response(404, {"message": "gone"})
        return base(endpoint, params, headers)

    client2 = build_client(store, failing)
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["details_written"] == 0
    assert result.counts["failed"] == 1
    assert result.coverage[0]["coverage_status"] == "partial"
    assert store.secure_path(detail_path(10)).read_bytes() == old_bytes
    candidate = next(
        item for item in result.selection["candidates"] if item["number"] == 10
    )
    assert candidate["skipped_reason"] == "refresh_incomplete"


def test_resume_preserves_complete_document_on_head_unavailable(
    tmp_path: Path,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    old_bytes = store.secure_path(detail_path(10)).read_bytes()
    base = default_handler()
    detail_calls = {"count": 0}

    def failing(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/pulls/10":
            detail_calls["count"] += 1
            if detail_calls["count"] == 2:
                return http_response(500, {"message": "boom"})
        return base(endpoint, params, headers)

    client2 = build_client(store, failing)
    with pytest.raises(GitHubUnavailableError):
        collect_pull_requests(
            client2,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            clock=lambda: "2026-11-01T08:00:20Z",
        )
    assert store.secure_path(detail_path(10)).read_bytes() == old_bytes


def test_initial_partial_document_is_written_on_comment_not_found(
    tmp_path: Path,
) -> None:
    base = default_handler()

    def failing(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/issues/10/comments":
            return http_response(404, {"message": "not found"})
        return base(endpoint, params, headers)

    store, client = make_client(tmp_path, failing)
    seed(store, [10])
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    assert document["coverage"]["detail"] == "partial"
    assert document["issue_comments"]["status"] == "unavailable"
    assert result.counts["details_written"] == 1
    assert result.coverage[0]["coverage_status"] == "partial"


def test_force_does_not_downgrade_complete_document(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    old_bytes = store.secure_path(detail_path(10)).read_bytes()
    base = default_handler()

    def failing(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/issues/10/comments":
            return http_response(404, {"message": "not found"})
        return base(endpoint, params, headers)

    client2 = build_client(store, failing)
    result = collect_pull_requests(
        client2,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        force=True,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["details_written"] == 0
    assert result.counts["skipped_unchanged"] == 0
    assert result.coverage[0]["coverage_status"] == "partial"
    assert store.secure_path(detail_path(10)).read_bytes() == old_bytes
    candidate = next(
        item for item in result.selection["candidates"] if item["number"] == 10
    )
    assert candidate["skipped_reason"] == "refresh_incomplete"


def test_reusable_code_sections_requires_authoritative_document(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    store.secure_path(detail_path(10)).unlink()

    reused = _reusable_code_sections(
        store,
        document,
        detail_body(10),
        head_sha=HEAD,
        base_sha="c" * 40,
    )

    assert reused == {}


def test_read_pr_forces_persist_when_document_file_disappears(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    store.secure_path(detail_path(10)).unlink()

    read = _read_pr(
        client,
        store,
        "github.com",
        "acme",
        "demo",
        10,
        existing=document,
        force=False,
        selection_reason=["deep_read_selected"],
        observed_at="2026-11-01T08:00:20Z",
    )

    assert read.changed is True


def test_exact_file_and_commit_caps_are_not_truncated(tmp_path: Path) -> None:
    files = [file_body(f"src/file_{index}.py") for index in range(3000)]
    commits = [commit_body(f"{index:040x}") for index in range(250)]
    handler = default_handler(
        files={10: files},
        commits={10: commits},
        details={10: detail_body(10, changed_files=3000, commits=250)},
    )
    store, client = make_client(
        tmp_path, handler, budget={**BUDGET, "max_response_bytes": 8 * 1024 * 1024}
    )
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    assert document["files"]["fetched"] == 3000
    assert document["files"]["truncated"] is False
    assert document["files"]["status"] == "complete"
    assert document["commits"]["fetched"] == 250
    assert document["commits"]["truncated"] is False
    assert document["commits"]["status"] == "complete"


def test_force_reread_drops_stale_child_records(tmp_path: Path) -> None:
    seed_store, client = make_client(tmp_path, default_handler())
    seed(seed_store, [10])
    collect_pull_requests(
        client,
        seed_store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(seed_store, 10)
    assert document is not None
    assert len(document["files"]["items"]) == 2

    handler = default_handler(
        files={10: [file_body("src/cache.py")]},
        details={10: detail_body(10, changed_files=1)},
    )
    client2 = build_client(seed_store, handler)
    collect_pull_requests(
        client2,
        seed_store,
        REPOSITORY,
        activity_window=WINDOW,
        force=True,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    document = load_pr_document(seed_store, 10)
    assert document is not None
    files = document["files"]["items"]
    assert len(files) == 1
    assert files[0]["filename"] == "src/cache.py"


def test_missing_overview_does_not_fall_back_to_stale_raw(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    (store.run_dir / "analysis/activity-overview.json").unlink()
    result = collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    assert result.counts["candidates"] == 0
    assert result.coverage[0]["coverage_status"] == "not_requested"
    assert "activity_overview_missing" in result.selection["limitations"]


def test_pr_detail_unavailable_is_blocking(tmp_path: Path) -> None:
    def unavailable(channel: str) -> dict[str, Any]:
        return {
            "channel": channel,
            "coverage_status": "unavailable",
        }

    partial = {**unavailable("pr_detail"), "coverage_status": "partial"}
    assert has_blocking_gaps([unavailable("pr_detail")])
    assert not has_blocking_gaps([partial])
    assert has_blocking_gaps([unavailable("activity_prs")])


def test_cli_all_pr_failures_exit_4(tmp_path: Path, monkeypatch) -> None:
    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint.startswith("repos/acme/demo/pulls/10"):
            return http_response(404, {"message": "not found"})
        return http_response(200, [])

    code, run_dir, store = _run_cli_collect(tmp_path, monkeypatch, handler)
    assert code == 4
    manifest = store.read_manifest()
    channels = {item["channel"]: item for item in manifest["coverage"]}
    assert channels["pr_detail"]["coverage_status"] == "unavailable"
    assert manifest["state"] == "partial"


def test_partial_page_failure_keeps_fetched_items(tmp_path: Path) -> None:
    base = default_handler(details={10: detail_body(10, changed_files=101)})

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]) -> CommandResult:
        if endpoint == "repos/acme/demo/pulls/10/files":
            return http_response(
                200,
                [file_body(f"src/file_{index}.py") for index in range(100)],
                {
                    "Link": (
                        "<https://api.github.com/repos/acme/demo/pulls/10/files?page=2>; "
                        'rel="next"'
                    )
                },
            )
        if endpoint.endswith("/pulls/10/files?page=2"):
            return http_response(404, {"message": "gone"})
        return base(endpoint, params, headers)

    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    assert document["files"]["fetched"] == 100
    assert len(document["files"]["items"]) == 100
    assert document["files"]["truncated"] is True
    assert document["files"]["truncation_reason"] == "not_found"
    assert document["files"]["status"] == "partial"


def test_missing_initial_head_marks_partial(tmp_path: Path) -> None:
    detail = detail_body(10)
    detail.pop("head")
    handler = default_handler(details={10: detail})
    store, client = make_client(tmp_path, handler)
    seed(store, [10])
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    assert document["head_sha"] is None
    assert "head_unverified" in document["limitations"]
    assert document["coverage"]["detail"] == "partial"


def test_missing_entity_is_created_from_detail(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10], entities=False)
    collect_pull_requests(
        client,
        store,
        REPOSITORY,
        activity_window=WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    entities = store.read_jsonl("normalized/entities.jsonl")
    entity = next(item for item in entities if item["id"] == "github.com/acme/demo/pull/10")
    assert entity["discovery_reasons"] == ["pr_detail"]
    assert "deep_read" not in entity["normalized_state"]
    assert entity["raw_state"]["deep_read"]["detail_path"] == "raw/pr-details/10.json"


def test_persistence_failure_propagates_and_finalizes_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])

    def boom(*args, **kwargs):
        raise StoreError("disk full", source="store.test")

    monkeypatch.setattr(pr_module, "_persist_pr", boom)
    with pytest.raises(StoreError):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=CollectProgress(store, "0" * 64),
            clock=lambda: "2026-11-01T08:00:10Z",
        )

    selection = store.read_json("analysis/pr-selection.json")
    candidate = selection["candidates"][0]
    assert candidate["status"] == "failed"
    assert candidate["skipped_reason"] == "store_error"
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage == {"status": "partial", "completed": [], "pending": [10]}
    assert not (store.run_dir / detail_path(10)).exists()


def test_persistence_failure_keeps_checkpoint_when_selection_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])
    real_write_json = store.write_json

    def boom_persist(*args, **kwargs):
        raise StoreError("disk full", source="store.test")

    def flaky_write_json(relpath, document, *, schema_name=None):
        if str(relpath) == "analysis/pr-selection.json":
            raise StoreError("selection write failed", source="store.test")
        return real_write_json(relpath, document, schema_name=schema_name)

    monkeypatch.setattr(pr_module, "_persist_pr", boom_persist)
    monkeypatch.setattr(store, "write_json", flaky_write_json)
    with pytest.raises(StoreError, match="disk full"):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=CollectProgress(store, "0" * 64),
            clock=lambda: "2026-11-01T08:00:10Z",
        )

    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage == {"status": "partial", "completed": [], "pending": [10]}


def test_blob_write_failure_propagates_and_finalizes_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])

    def boom(*args, **kwargs):
        raise StoreError("disk full", source="store.test")

    monkeypatch.setattr(store, "save_blob", boom)
    with pytest.raises(StoreError):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=CollectProgress(store, "0" * 64),
            clock=lambda: "2026-11-01T08:00:10Z",
        )

    selection = store.read_json("analysis/pr-selection.json")
    candidate = selection["candidates"][0]
    assert candidate["status"] == "failed"
    assert candidate["skipped_reason"] == "store_error"
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage == {"status": "partial", "completed": [], "pending": [10]}
    assert not (store.run_dir / detail_path(10)).exists()


def test_read_pr_schema_failure_finalizes_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])

    def boom(document, schema_name=None):
        if schema_name == "pr_detail":
            raise SchemaError("invalid pr detail", source="store.test")
        return validate_document(document, schema_name)

    monkeypatch.setattr(pr_module, "validate_document", boom)
    with pytest.raises(SchemaError):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=CollectProgress(store, "0" * 64),
            clock=lambda: "2026-11-01T08:00:10Z",
        )

    selection = store.read_json("analysis/pr-selection.json")
    candidate = selection["candidates"][0]
    assert candidate["status"] == "failed"
    assert candidate["skipped_reason"] == "schema_error"
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage == {"status": "partial", "completed": [], "pending": [10]}


def test_persist_pr_schema_failure_finalizes_partial(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = make_client(tmp_path, default_handler())
    seed(store, [10])

    def boom(store, document):
        raise SchemaError("invalid pr detail", source="store.test")

    monkeypatch.setattr(pr_module, "write_pr_document", boom)
    with pytest.raises(SchemaError):
        collect_pull_requests(
            client,
            store,
            REPOSITORY,
            activity_window=WINDOW,
            progress=CollectProgress(store, "0" * 64),
            clock=lambda: "2026-11-01T08:00:10Z",
        )

    selection = store.read_json("analysis/pr-selection.json")
    candidate = selection["candidates"][0]
    assert candidate["status"] == "failed"
    assert candidate["skipped_reason"] == "schema_error"
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage == {"status": "partial", "completed": [], "pending": [10]}


def test_cli_persistence_failure_exits_6_and_keeps_successful_documents(
    tmp_path: Path,
    monkeypatch,
) -> None:
    real_persist = pr_module._persist_pr

    def flaky(store, document):
        if document["number"] == 11:
            raise StoreError("disk full", source="store.test")
        return real_persist(store, document)

    monkeypatch.setattr(pr_module, "_persist_pr", flaky)
    code, run_dir, store = _run_cli_collect(
        tmp_path,
        monkeypatch,
        default_handler(),
        numbers=[10, 11],
        records={
            10: pr_record(10, merged_at="2026-10-01T00:00:00Z", comments=5),
            11: pr_record(11, merged_at="2026-10-02T00:00:00Z"),
        },
    )

    assert code == 6
    manifest = store.read_manifest()
    assert manifest["state"] == "failed"
    assert manifest["stages"]["collect"] == "failed"
    channels = {item["channel"]: item for item in manifest["coverage"]}
    assert channels["pr_detail"]["coverage_status"] == "partial"
    assert (run_dir / "raw/pr-details/10.json").is_file()
    assert not (run_dir / "raw/pr-details/11.json").exists()

    selection = store.read_json("analysis/pr-selection.json")
    candidates = {item["number"]: item for item in selection["candidates"]}
    assert candidates[10]["status"] == "selected"
    assert candidates[11]["status"] == "failed"
    assert candidates[11]["skipped_reason"] == "store_error"
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage == {"status": "partial", "completed": [10], "pending": [11]}


def test_selection_counts_and_fetched_count_are_derived() -> None:
    candidates = [
        {"selected": True, "status": "selected", "bucket": "merged"},
        {"selected": True, "status": "skipped_unchanged", "bucket": "open"},
        {"selected": True, "status": "failed", "bucket": "open"},
        {"selected": True, "status": "not_attempted", "bucket": "merged"},
        {"selected": False, "status": "skipped_quota", "bucket": "merged"},
    ]

    assert derive_selection_counts(candidates) == {
        "candidates": 5,
        "selected": 4,
        "merged_selected": 2,
        "open_selected": 2,
        "skipped_quota": 1,
        "skipped_unchanged": 1,
        "failed": 1,
        "not_attempted": 1,
    }
    assert derive_fetched_count(candidates) == 2


def test_pr_section_specs_match_status_contract(tmp_path: Path) -> None:
    store, _ = make_client(tmp_path, default_handler())
    specs = _section_specs(store, HEAD)
    assert tuple(spec.name for spec in specs) == PR_DETAIL_SECTIONS
