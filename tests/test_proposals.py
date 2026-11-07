from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rer.collectors.base import CollectContext
from rer.collectors.pr_store import write_pr_document
from rer.collectors.progress import CollectProgress
from rer.collectors.proposal_store import (
    load_discussion_documents,
    load_issue_documents,
    validate_discussion_document,
    validate_issue_document,
)
from rer.collectors.proposals import (
    build_candidates,
    choose,
    collect_proposals,
    quota_for_budget,
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


def issue_body(
    number: int = 42,
    *,
    title: str = "RFC: explicit cache invalidation",
    state: str = "open",
    state_reason: str | None = None,
    labels: list[str] | None = None,
    body: str = "We should add an explicit cache invalidation API.",
    **overrides: Any,
) -> dict[str, Any]:
    result = {
        "number": number,
        "html_url": f"https://github.com/acme/demo/issues/{number}",
        "title": title,
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "locked": False,
        "created_at": "2026-05-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "closed_at": None,
        "user": {"login": "alice"},
        "labels": [{"name": label} for label in (labels or ["rfc"])],
        "comments": 0,
        "reactions": {"total_count": 3},
    }
    result.update(overrides)
    return result


def comment_body(identifier: int = 1) -> dict[str, Any]:
    return {
        "id": identifier,
        "user": {"login": "bob"},
        "author_association": "MEMBER",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-01T00:00:00Z",
        "body": "Sounds good.",
    }


def timeline_body(event: str = "closed") -> dict[str, Any]:
    return {
        "id": 9001,
        "event": event,
        "actor": {"login": "bob"},
        "created_at": "2026-10-02T00:00:00Z",
        "commit_id": None,
    }


def discussion_raw(number: int = 7, **overrides: Any) -> dict[str, Any]:
    result = {
        "number": number,
        "title": "Design: scheduler ownership",
        "url": f"https://github.com/acme/demo/discussions/{number}",
        "closed": False,
        "closedAt": None,
        "stateReason": None,
        "createdAt": "2026-06-01T00:00:00Z",
        "updatedAt": "2026-10-01T00:00:00Z",
        "category": {"name": "Architecture"},
        "upvoteCount": 12,
        "comments": {"totalCount": 1},
        "author": {"login": "alice"},
    }
    result.update(overrides)
    return result


def discussion_payload(
    number: int = 7,
    *,
    comments: list[dict[str, Any]] | None = None,
    has_next: bool = False,
    errors: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    nodes = comments if comments is not None else [
        {
            "id": "DC_1",
            "databaseId": 1,
            "body": "The scheduler should own it.",
            "createdAt": "2026-06-02T00:00:00Z",
            "updatedAt": "2026-06-02T00:00:00Z",
            "isAnswer": True,
            "author": {"login": "bob"},
            "replies": {
                "totalCount": 0,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            },
        }
    ]
    return {
        "data": {
            "repository": {
                "discussion": {
                    **discussion_raw(number),
                    **overrides,
                    "id": "D_1",
                    "body": "Who owns the scheduler state?",
                    "isAnswered": True,
                    "answer": {"id": "DC_1"},
                    "comments": {
                        "totalCount": len(nodes),
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": "cursor-1" if has_next else None,
                        },
                        "nodes": nodes,
                    },
                }
            }
        },
        "errors": errors or [],
    }


def make_store(tmp_path: Path) -> SnapshotStore:
    return create_run(tmp_path, REPOSITORY, new_run_id(NOW))


def make_client(
    store: SnapshotStore,
    handler,
    *,
    budget: dict[str, int] | None = None,
) -> GitHubClient:
    return GitHubClient(
        store,
        budget or BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: handler(argv, input_text),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )


def context_for(
    client: GitHubClient,
    store: SnapshotStore,
    clock=lambda: "2026-11-01T08:00:10Z",
    *,
    progress: CollectProgress | None = None,
) -> CollectContext:
    return CollectContext(
        client=client,
        store=store,
        repository=REPOSITORY,
        clock=clock,
        progress=progress,
    )


def write_issue_stream(store: SnapshotStore, records: list[dict[str, Any]]) -> None:
    store.write_jsonl("raw/issues.jsonl", records, key="number")


def write_discussion_stream(store: SnapshotStore, records: list[dict[str, Any]]) -> None:
    store.write_jsonl("raw/discussions.jsonl", records, key="number")


def write_selected_pr(store: SnapshotStore, number: int, body_text: str) -> None:
    from rer.collectors.status import (
        PR_DETAIL_SECTIONS,
        derive_detail_status,
        derive_limitations,
    )
    from rer.collectors.texts import text_ref

    document = json.loads(
        (Path(__file__).parent / "fixtures/example/pr_detail.sample.json").read_text(
            encoding="utf-8"
        )
    )
    document["number"] = number
    document["body"] = text_ref(store, body_text)
    sections = {key: document[key] for key in PR_DETAIL_SECTIONS}
    document["coverage"]["detail"] = derive_detail_status(
        sections=sections,
        head_changed=bool(document["head_changed"]),
        head_unverified=document["head_sha"] is None
        or document["head_sha_end"] is None,
        diff_status=str(document["diff"]["status"]),
    )
    for key in PR_DETAIL_SECTIONS:
        document["coverage"][key] = document[key]["status"]
    document["limitations"] = derive_limitations(document)
    write_pr_document(store, document)


def rest_handler(
    issues: dict[int, dict[str, Any]],
    comments: dict[int, list[dict[str, Any]]] | None = None,
    timeline: dict[int, list[dict[str, Any]]] | None = None,
):
    comments = comments or {}
    timeline = timeline or {}

    def handler(argv: list[str], input_text: str):
        if argv[1:3] == ["api", "graphql"]:
            raise AssertionError("unexpected graphql call")
        endpoint = argv[argv.index("--include") + 1]
        if "/issues/" in endpoint and endpoint.endswith("/comments"):
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            return http_response(200, comments.get(number, []))
        if "/issues/" in endpoint and endpoint.endswith("/timeline"):
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            return http_response(200, timeline.get(number, []))
        if "/issues/" in endpoint:
            number = int(endpoint.rsplit("/", 1)[1])
            if number in issues:
                return http_response(200, issues[number])
            return http_response(404, {"message": "not found"})
        return http_response(404, {"message": "unhandled"})

    return handler


def test_collects_issue_rfc_and_writes_selection(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    client = make_client(store, rest_handler({42: issue_body()}))

    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
    )

    assert result.counts["candidates"] == 1
    assert result.counts["selected"] == 1
    assert result.counts["issue_selected"] == 1
    document = load_issue_documents(store)[0]
    assert document["number"] == 42
    assert document["role_hints"] == ["rfc"]
    assert document["scope"] == "in_window"
    assert document["coverage"]["detail"] == "complete"
    channels = {item["channel"]: item for item in result.coverage}
    assert channels["issue_detail"]["coverage_status"] == "complete"
    assert channels["discussion_detail"]["coverage_status"] == "not_requested"
    selection = store.read_json("analysis/proposal-selection.json")
    assert selection["counts"]["selected"] == 1
    assert selection["candidates"][0]["detail_path"] == "raw/issues/42.json"
    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["kind"] == "issue"
    )
    assert entity["raw_state"]["proposal_read"]["detail_path"] == "raw/issues/42.json"


def test_issue_comment_pagination_duplicates_are_deduped(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    comment = {
        "id": 9001,
        "user": {"login": "bob"},
        "created_at": "2026-10-02T08:00:00Z",
        "updated_at": "2026-10-02T08:00:00Z",
        "author_association": "MEMBER",
        "body": "Looks reasonable.",
    }

    def handler(argv: list[str], input_text: str):
        if argv[1:3] == ["api", "graphql"]:
            raise AssertionError("unexpected graphql call")
        endpoint = argv[argv.index("--include") + 1]
        if endpoint == "repos/acme/demo/issues/42/comments":
            return http_response(
                200,
                [comment],
                {
                    "Link": (
                        "<https://api.github.com/repos/acme/demo/issues/42/"
                        'comments?page=2>; rel="next"'
                    )
                },
            )
        if endpoint.endswith("/issues/42/comments?page=2"):
            return http_response(200, [comment])
        if endpoint.endswith("/issues/42/timeline"):
            return http_response(200, [])
        if endpoint.endswith("/issues/42"):
            return http_response(200, issue_body(42, comments=1))
        return http_response(404, {"message": "unhandled"})

    client = make_client(store, handler)
    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
    )

    document = load_issue_documents(store)[0]
    assert document["comments"]["fetched"] == 1
    assert document["comments"]["duplicates"] == 1
    assert document["comments"]["items"][0]["id"] == 9001
    assert document["coverage"]["detail"] == "complete"
    assert "pagination_duplicates:1" in document["limitations"]
    channels = {item["channel"]: item for item in result.coverage}
    assert "pagination_duplicates:#42" in (
        channels["issue_detail"]["truncation_reason"] or ""
    )


def test_closed_not_planned_is_recorded_as_limitation(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    body = issue_body(
        state="closed",
        state_reason="not_planned",
        closed_at="2026-09-01T00:00:00Z",
    )
    write_issue_stream(store, [body])
    client = make_client(store, rest_handler({42: body}))

    collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_issue_documents(store)[0]
    assert document["state_reason"] == "not_planned"
    assert "closed_not_planned" in document["limitations"]


def test_context_reference_from_selected_pr(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [])
    write_selected_pr(store, 101, "Follow-up to #77.")
    store.write_jsonl("raw/prs.jsonl", [{"number": 101, "title": "PR 101"}], key="number")
    client = make_client(store, rest_handler({77: issue_body(77, title="Old RFC")}))

    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
        pr_selection={
            "candidates": [{"number": 101, "selected": True}],
        },
    )

    assert result.counts["candidates"] == 1
    assert result.counts["context_selected"] == 1
    document = load_issue_documents(store)[0]
    assert document["number"] == 77
    assert document["scope"] == "context"
    assert "out_of_window_context" in document["limitations"]


def test_referenced_pull_request_is_skipped(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [])
    body = issue_body(77, pull_request={"url": "https://example"})
    client = make_client(store, rest_handler({77: body}))

    write_selected_pr(store, 101, "Follow-up to #77.")
    store.write_jsonl("raw/prs.jsonl", [{"number": 101}], key="number")

    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
        pr_selection={"candidates": [{"number": 101, "selected": True}]},
    )

    assert result.counts["failed"] == 0
    assert result.counts["selected"] == 0
    assert load_issue_documents(store) == []
    candidate = result.selection["candidates"][0]
    assert candidate["status"] == "skipped_reason"
    assert candidate["skipped_reason"] == "referenced_pull_request"
    assert candidate["selected"] is False


def test_referenced_pr_number_is_not_a_context_candidate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [])
    store.write_jsonl("raw/prs.jsonl", [{"number": 77}], key="number")
    write_selected_pr(store, 101, "Follow-up to #77.")

    candidates = build_candidates(
        store,
        pr_selection={"candidates": [{"number": 101, "selected": True}]},
        focus=[],
        start=datetime(2026, 8, 1, tzinfo=UTC),
        end=datetime(2026, 11, 1, tzinfo=UTC),
        host="github.com",
        owner="acme",
        name="demo",
    )

    assert candidates == []


def test_issue_unchanged_is_skipped(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    client = make_client(store, rest_handler({42: issue_body()}))
    context = context_for(client, store)
    collect_proposals(context, activity_window=WINDOW)
    observed = load_issue_documents(store)[0]["observed_at"]

    client2 = make_client(store, rest_handler({42: issue_body()}))
    result = collect_proposals(
        context_for(client2, store, clock=lambda: "2026-11-01T09:00:00Z"),
        activity_window=WINDOW,
    )

    assert result.counts["skipped_unchanged"] == 1
    assert load_issue_documents(store)[0]["observed_at"] == observed


def test_comment_unavailable_keeps_complete_document(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    client = make_client(store, rest_handler({42: issue_body()}))
    collect_proposals(context_for(client, store), activity_window=WINDOW)

    def failing(argv: list[str], input_text: str):
        endpoint = argv[argv.index("--include") + 1]
        if endpoint.endswith("/comments"):
            return http_response(404, {"message": "gone"})
        if endpoint.endswith("/timeline"):
            return http_response(200, [])
        return http_response(200, issue_body())

    client2 = make_client(store, failing)
    result = collect_proposals(
        context_for(client2, store, clock=lambda: "2026-11-01T09:00:00Z"),
        activity_window=WINDOW,
    )

    document = load_issue_documents(store)[0]
    assert document["coverage"]["detail"] == "complete"
    assert result.counts["failed"] == 1
    assert result.coverage[0]["coverage_status"] == "partial"
    assert "refresh_incomplete" in result.coverage[0]["truncation_reason"]


def test_local_write_failure_finalizes_partial_then_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from rer.collectors import proposals as proposals_module
    from rer.errors import StoreError

    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body(42), issue_body(43, title="RFC two")])
    client = make_client(
        store,
        rest_handler({42: issue_body(42), 43: issue_body(43, title="RFC two")}),
    )
    progress = CollectProgress(store, "a" * 64)
    real_write = proposals_module.write_issue_document
    calls = {"count": 0}

    def fail_second(store_arg, document) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise StoreError("disk full", source="test.write")
        real_write(store_arg, document)

    monkeypatch.setattr(proposals_module, "write_issue_document", fail_second)

    with pytest.raises(StoreError):
        collect_proposals(
            context_for(client, store, progress=progress),
            activity_window=WINDOW,
        )

    checkpoint = store.read_checkpoint("collect")
    entries = {item["channel"]: item for item in checkpoint["coverage"]}
    assert entries["issue_detail"]["coverage_status"] == "partial"
    assert entries["issue_detail"]["fetched_count"] == 1


def test_quota_reserves_discussion_slots() -> None:
    assert quota_for_budget({"max_deep_entities": 40}, 0) == (8, 6, 2)
    assert quota_for_budget({"max_deep_entities": 40}, 22) == (8, 6, 2)
    assert quota_for_budget({"max_deep_entities": 40}, 38) == (2, 2, 0)
    assert quota_for_budget({"max_deep_entities": 40}, 40) == (0, 0, 0)
    assert quota_for_budget(
        {"max_deep_entities": 12, "agent_pr_reserve": 2}, 6
    ) == (4, 4, 0)


def test_chooses_discussion_when_issue_slots_are_full() -> None:
    from rer.collectors.proposals import ProposalCandidate

    candidates = [
        ProposalCandidate(
            carrier="issue",
            number=index,
            entity_id=f"github.com/acme/demo/issue/{index}",
            title=f"issue {index}",
            scope="in_window",
            raw={},
            score=100 - index,
            components={},
            role_hints=[],
            reasons=[],
        )
        for index in range(1, 4)
    ]
    candidates.append(
        ProposalCandidate(
            carrier="discussion",
            number=7,
            entity_id="github.com/acme/demo/discussion/7",
            title="discussion 7",
            scope="in_window",
            raw={},
            score=10,
            components={},
            role_hints=[],
            reasons=[],
        )
    )
    choose(candidates, quota=3, issue_quota=2, discussion_quota=1)

    assert sum(1 for item in candidates if item.selected) == 3
    assert any(item.selected and item.carrier == "discussion" for item in candidates)


def test_discussion_deep_read_collects_comments_and_replies(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def handler(argv: list[str], input_text: str):
        if argv[1:3] != ["api", "graphql"]:
            raise AssertionError("unexpected rest call")
        return http_response(200, discussion_payload())

    client = make_client(store, handler)
    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
    )

    document = load_discussion_documents(store)[0]
    assert document["number"] == 7
    assert document["category"] == "Architecture"
    assert document["comments"]["fetched"] == 1
    assert document["comments"]["items"][0]["is_answer"] is True
    assert document["coverage"]["detail"] == "complete"
    channels = {item["channel"]: item for item in result.coverage}
    assert channels["discussion_detail"]["coverage_status"] == "complete"
    assert result.counts["discussion_selected"] == 1


def test_discussion_graphql_errors_are_partial_not_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def handler(argv: list[str], input_text: str):
        return http_response(
            200,
            {
                "data": {"repository": {"discussion": None}},
                "errors": [{"message": "discussion unavailable"}],
            },
        )

    client = make_client(store, handler)
    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
    )

    assert load_discussion_documents(store) == []
    assert result.counts["failed"] == 1
    candidate = result.selection["candidates"][0]
    assert candidate["skipped_reason"] == "github_error"
    channels = {item["channel"]: item for item in result.coverage}
    assert channels["discussion_detail"]["coverage_status"] == "unavailable"


def test_discussion_not_found_uses_not_found_error(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def handler(argv: list[str], input_text: str):
        return http_response(
            200,
            {
                "data": {"repository": {"discussion": None}},
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "message": "Could not resolve to a Discussion",
                    }
                ],
            },
        )

    client = make_client(store, handler)
    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
    )

    assert result.counts["failed"] == 0
    assert result.counts["selected"] == 0
    candidate = result.selection["candidates"][0]
    assert candidate["status"] == "skipped_reason"
    assert candidate["skipped_reason"] == "proposal_not_found"
    assert candidate["selected"] is False
    channels = {item["channel"]: item for item in result.coverage}
    assert channels["discussion_detail"]["coverage_status"] == "not_requested"


def test_discussion_partial_graphql_data_is_kept(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def handler(argv: list[str], input_text: str):
        payload = discussion_payload(
            errors=[{"message": "comments partially unavailable"}]
        )
        return http_response(200, payload)

    client = make_client(store, handler)
    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
    )

    document = load_discussion_documents(store)[0]
    assert document["comments"]["fetched"] == 1
    assert document["comments"]["status"] == "partial"
    assert document["coverage"]["detail"] == "partial"
    assert "graphql_errors" in document["comments"]["truncation_reason"]
    channels = {item["channel"]: item for item in result.coverage}
    assert channels["discussion_detail"]["coverage_status"] == "partial"


def test_discussion_replies_are_continued(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def first_payload():
        return discussion_payload(
            comments=[
                {
                    "id": "DC_1",
                    "databaseId": 1,
                    "body": "Top level",
                    "createdAt": "2026-06-02T00:00:00Z",
                    "updatedAt": "2026-06-02T00:00:00Z",
                    "isAnswer": False,
                    "author": {"login": "bob"},
                    "replies": {
                        "totalCount": 2,
                        "pageInfo": {"hasNextPage": True, "endCursor": "r1"},
                        "nodes": [
                            {
                                "id": "DC_2",
                                "databaseId": 2,
                                "body": "First reply",
                                "createdAt": "2026-06-03T00:00:00Z",
                                "updatedAt": "2026-06-03T00:00:00Z",
                                "author": {"login": "carol"},
                            }
                        ],
                    },
                }
            ]
        )

    def second_payload():
        return {
            "data": {
                "node": {
                    "replies": {
                        "totalCount": 2,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "DC_3",
                                "databaseId": 3,
                                "body": "Second reply",
                                "createdAt": "2026-06-04T00:00:00Z",
                                "updatedAt": "2026-06-04T00:00:00Z",
                                "author": {"login": "dave"},
                            }
                        ],
                    }
                }
            },
            "errors": [],
        }

    def handler(argv: list[str], input_text: str):
        payload = json.loads(input_text)
        if "node(id:" in payload["query"]:
            return http_response(200, second_payload())
        return http_response(200, first_payload())

    client = make_client(store, handler)
    collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_discussion_documents(store)[0]
    replies = document["comments"]["items"][0]["replies"]
    assert [reply["id"] for reply in replies] == ["DC_2", "DC_3"]
    assert document["comments"]["status"] == "complete"


def test_discussion_pagination_duplicates_are_deduped(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def reply_node(identifier: str, body: str) -> dict[str, Any]:
        return {
            "id": identifier,
            "databaseId": int(identifier.removeprefix("DC_")),
            "body": body,
            "createdAt": "2026-06-03T00:00:00Z",
            "updatedAt": "2026-06-03T00:00:00Z",
            "author": {"login": "carol"},
        }

    def comment_node() -> dict[str, Any]:
        return {
            "id": "DC_1",
            "databaseId": 1,
            "body": "Top level",
            "createdAt": "2026-06-02T00:00:00Z",
            "updatedAt": "2026-06-02T00:00:00Z",
            "isAnswer": False,
            "author": {"login": "bob"},
            "replies": {
                "totalCount": 2,
                "pageInfo": {"hasNextPage": True, "endCursor": "r1"},
                "nodes": [reply_node("DC_2", "First reply")],
            },
        }

    def handler(argv: list[str], input_text: str):
        payload = json.loads(input_text)
        if "node(id:" in payload["query"]:
            return http_response(
                200,
                {
                    "data": {
                        "node": {
                            "replies": {
                                "totalCount": 2,
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    reply_node("DC_2", "First reply"),
                                    reply_node("DC_3", "Second reply"),
                                ],
                            }
                        }
                    },
                    "errors": [],
                },
            )
        cursor = payload.get("variables", {}).get("cursor")
        return http_response(
            200,
            discussion_payload(
                comments=[comment_node()],
                has_next=cursor is None,
            ),
        )

    client = make_client(store, handler)
    result = collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_discussion_documents(store)[0]
    assert document["comments"]["fetched"] == 1
    assert document["comments"]["duplicates"] == 2
    assert [reply["id"] for reply in document["comments"]["items"][0]["replies"]] == [
        "DC_2",
        "DC_3",
    ]
    assert document["coverage"]["detail"] == "complete"
    assert "pagination_duplicates:2" in document["limitations"]
    channels = {item["channel"]: item for item in result.coverage}
    assert "pagination_duplicates:#7" in (
        channels["discussion_detail"]["truncation_reason"] or ""
    )


def test_discussion_not_requested_when_disabled(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def handler(argv: list[str], input_text: str):
        raise AssertionError("must not call GitHub")

    client = make_client(store, handler)
    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
        include_discussions=False,
    )

    assert result.selection["counts"]["candidates"] == 0
    channels = {item["channel"]: item for item in result.coverage}
    assert channels["discussion_detail"]["coverage_status"] == "not_requested"


def test_documents_validate(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    write_discussion_stream(store, [discussion_raw()])

    def handler(argv: list[str], input_text: str):
        if argv[1:3] == ["api", "graphql"]:
            return http_response(200, discussion_payload())
        endpoint = argv[argv.index("--include") + 1]
        if endpoint.endswith("/comments") or endpoint.endswith("/timeline"):
            return http_response(200, [])
        return http_response(200, issue_body())

    client = make_client(store, handler)
    collect_proposals(context_for(client, store), activity_window=WINDOW)

    for document in load_issue_documents(store):
        validate_issue_document(document)
    for document in load_discussion_documents(store):
        validate_discussion_document(document)


def test_issue_comments_and_timeline_are_recorded(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    body = issue_body(comments=1)
    write_issue_stream(store, [body])
    timeline = [
        {
            "id": 9002,
            "event": "cross-referenced",
            "actor": {"login": "dave"},
            "created_at": "2026-09-15T00:00:00Z",
            "commit_id": None,
            "source": {
                "issue": {
                    "number": 101,
                    "pull_request": {"url": "https://example"},
                }
            },
        }
    ]
    client = make_client(
        store,
        rest_handler(
            {42: body},
            comments={42: [comment_body()]},
            timeline={42: timeline},
        ),
    )

    collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_issue_documents(store)[0]
    assert document["comments"]["fetched"] == 1
    assert document["comments"]["items"][0]["body"]["preview"] == "Sounds good."
    assert document["timeline"]["items"][0] == {
        "event": "cross-referenced",
        "id": 9002,
        "actor": "dave",
        "created_at": "2026-09-15T00:00:00Z",
        "commit_id": None,
        "source_number": 101,
        "source_kind": "pull_request",
        "label": None,
    }
    assert document["coverage"]["detail"] == "complete"


def test_issue_timeline_unavailable_marks_partial(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])

    def handler(argv: list[str], input_text: str):
        endpoint = argv[argv.index("--include") + 1]
        if endpoint.endswith("/timeline"):
            return http_response(404, {"message": "gone"})
        if endpoint.endswith("/comments"):
            return http_response(200, [])
        return http_response(200, issue_body())

    client = make_client(store, handler)
    result = collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_issue_documents(store)[0]
    assert document["timeline"]["status"] == "unavailable"
    assert document["coverage"]["detail"] == "partial"
    assert "detail_partial" in document["limitations"]
    assert result.coverage[0]["coverage_status"] == "partial"


def test_context_candidate_roles_are_confirmed_from_fetched_labels(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [])
    write_selected_pr(store, 101, "Follow-up to #77.")
    store.write_jsonl("raw/prs.jsonl", [{"number": 101}], key="number")
    body = issue_body(77, title="Old cache proposal", labels=["rfc"])
    client = make_client(store, rest_handler({77: body}))

    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
        pr_selection={"candidates": [{"number": 101, "selected": True}]},
    )

    document = load_issue_documents(store)[0]
    assert document["role_hints"] == ["rfc"]
    candidate = result.selection["candidates"][0]
    assert candidate["role_hints"] == ["rfc"]


def test_proposal_label_does_not_become_entity_role(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    body = issue_body(
        title="Proposal: explicit cache invalidation",
        labels=["proposal"],
    )
    write_issue_stream(store, [body])
    client = make_client(store, rest_handler({42: body}))

    collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_issue_documents(store)[0]
    assert document["role_hints"] == ["proposal"]
    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["kind"] == "issue"
    )
    assert "proposal" not in entity["roles"]
    assert entity["normalized_state"]["role_hints"] == ["proposal"]


def test_discussion_category_proposals_does_not_become_entity_role(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    raw = discussion_raw(
        title="Scheduler ownership",
        category={"name": "Proposals"},
    )
    write_discussion_stream(store, [raw])

    def handler(argv: list[str], input_text: str):
        return http_response(
            200,
            discussion_payload(
                title="Scheduler ownership",
                category={"name": "Proposals"},
            ),
        )

    client = make_client(store, handler)
    collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_discussion_documents(store)[0]
    assert document["role_hints"] == ["proposal"]
    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["kind"] == "discussion"
    )
    assert "proposal" not in entity["roles"]
    assert entity["normalized_state"]["role_hints"] == ["proposal"]


def test_projection_does_not_roll_back_activity_timestamps(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    client = make_client(store, rest_handler({42: issue_body()}))
    collect_proposals(context_for(client, store), activity_window=WINDOW)

    entities = store.read_jsonl("normalized/entities.jsonl")
    entity = next(item for item in entities if item["kind"] == "issue")
    entity["observed_at"] = "2026-11-01T09:30:00Z"
    entity["updated_at"] = "2026-11-01T09:30:00Z"
    entity["title"] = "Activity refreshed title"
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    client2 = make_client(store, rest_handler({42: issue_body()}))
    collect_proposals(
        context_for(client2, store, clock=lambda: "2026-11-01T10:00:00Z"),
        activity_window=WINDOW,
    )

    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["kind"] == "issue"
    )
    assert entity["observed_at"] == "2026-11-01T09:30:00Z"
    assert entity["updated_at"] == "2026-11-01T09:30:00Z"
    assert entity["title"] == "Activity refreshed title"


def test_reconcile_preserves_activity_timestamps(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    client = make_client(store, rest_handler({42: issue_body()}))
    collect_proposals(context_for(client, store), activity_window=WINDOW)

    entities = store.read_jsonl("normalized/entities.jsonl")
    entity = next(item for item in entities if item["kind"] == "issue")
    entity["observed_at"] = "2026-11-01T09:45:00Z"
    entity["updated_at"] = "2026-11-01T09:45:00Z"
    entity["source_event_at"] = "2026-11-01T09:45:00Z"
    entity["title"] = "Activity refreshed title"
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    write_issue_stream(store, [])
    client2 = make_client(store, rest_handler({}))
    collect_proposals(
        context_for(client2, store, clock=lambda: "2026-11-01T10:00:00Z"),
        activity_window=WINDOW,
    )

    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["kind"] == "issue"
    )
    assert entity["observed_at"] == "2026-11-01T09:45:00Z"
    assert entity["updated_at"] == "2026-11-01T09:45:00Z"
    assert entity["source_event_at"] == "2026-11-01T09:45:00Z"
    assert entity["title"] == "Activity refreshed title"


def test_replies_continuation_limit_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from rer.collectors import discussions as discussions_module

    monkeypatch.setattr(discussions_module, "MAX_REPLY_PAGES", 100)
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def comment_payload():
        return discussion_payload(
            comments=[
                {
                    "id": "DC_1",
                    "databaseId": 1,
                    "body": "Top level",
                    "createdAt": "2026-06-02T00:00:00Z",
                    "updatedAt": "2026-06-02T00:00:00Z",
                    "isAnswer": False,
                    "author": {"login": "bob"},
                    "replies": {
                        "totalCount": 100,
                        "pageInfo": {"hasNextPage": True, "endCursor": "r1"},
                        "nodes": [],
                    },
                }
            ]
        )

    def reply_payload():
        return {
            "data": {
                "node": {
                    "replies": {
                        "totalCount": 100,
                        "pageInfo": {"hasNextPage": True, "endCursor": "r2"},
                        "nodes": [
                            {
                                "id": "DC_2",
                                "databaseId": 2,
                                "body": "Reply",
                                "createdAt": "2026-06-03T00:00:00Z",
                                "updatedAt": "2026-06-03T00:00:00Z",
                                "author": {"login": "carol"},
                            }
                        ],
                    }
                }
            },
            "errors": [],
        }

    def handler(argv: list[str], input_text: str):
        payload = json.loads(input_text)
        if "node(id:" in payload["query"]:
            return http_response(200, reply_payload())
        return http_response(200, comment_payload())

    client = make_client(store, handler)
    collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_discussion_documents(store)[0]
    reason = document["comments"]["truncation_reason"]
    assert "replies_continuation_limit:DC_1" in reason
    assert "replies_page_limit" not in reason
    assert document["comments"]["items"][0]["replies_truncated"] is True


def test_replies_page_limit_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from rer.collectors import discussions as discussions_module

    monkeypatch.setattr(discussions_module, "MAX_REPLY_PAGES", 1)
    store = make_store(tmp_path)
    write_discussion_stream(store, [discussion_raw()])

    def comment_payload():
        return discussion_payload(
            comments=[
                {
                    "id": "DC_1",
                    "databaseId": 1,
                    "body": "Top level",
                    "createdAt": "2026-06-02T00:00:00Z",
                    "updatedAt": "2026-06-02T00:00:00Z",
                    "isAnswer": False,
                    "author": {"login": "bob"},
                    "replies": {
                        "totalCount": 100,
                        "pageInfo": {"hasNextPage": True, "endCursor": "r1"},
                        "nodes": [],
                    },
                }
            ]
        )

    def reply_payload():
        return {
            "data": {
                "node": {
                    "replies": {
                        "totalCount": 100,
                        "pageInfo": {"hasNextPage": True, "endCursor": "r2"},
                        "nodes": [
                            {
                                "id": "DC_2",
                                "databaseId": 2,
                                "body": "Reply",
                                "createdAt": "2026-06-03T00:00:00Z",
                                "updatedAt": "2026-06-03T00:00:00Z",
                                "author": {"login": "carol"},
                            }
                        ],
                    }
                }
            },
            "errors": [],
        }

    def handler(argv: list[str], input_text: str):
        payload = json.loads(input_text)
        if "node(id:" in payload["query"]:
            return http_response(200, reply_payload())
        return http_response(200, comment_payload())

    client = make_client(store, handler)
    collect_proposals(context_for(client, store), activity_window=WINDOW)

    document = load_discussion_documents(store)[0]
    reason = document["comments"]["truncation_reason"]
    assert "replies_page_limit:DC_1" in reason
    assert "replies_continuation_limit" not in reason
    assert document["comments"]["items"][0]["replies_truncated"] is True


def test_budget_exhaustion_degrades_to_partial(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    client = make_client(
        store,
        rest_handler({42: issue_body()}),
        budget={**BUDGET, "max_requests": 2},
    )

    context = context_for(client, store)
    result = collect_proposals(
        context,
        activity_window=WINDOW,
    )

    assert context.budget_exhausted is True
    assert result.counts["not_attempted"] == 1
    channels = {item["channel"]: item for item in result.coverage}
    assert channels["issue_detail"]["coverage_status"] == "partial"
    assert "budget_exceeded" in channels["issue_detail"]["truncation_reason"]


def test_proposal_request_limit_stops_remaining_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from rer.collectors import proposals as proposals_module

    store = make_store(tmp_path)
    write_issue_stream(
        store,
        [issue_body(42), issue_body(43, title="RFC two")],
    )
    client = make_client(
        store,
        rest_handler(
            {
                42: issue_body(42),
                43: issue_body(43, title="RFC two"),
            }
        ),
    )
    monkeypatch.setattr(proposals_module, "MAX_PROPOSAL_REQUESTS", 1)

    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
    )

    assert result.counts["not_attempted"] == 1
    channels = {item["channel"]: item for item in result.coverage}
    assert channels["issue_detail"]["coverage_status"] == "partial"
    assert "proposal_request_limit:1" in channels["issue_detail"]["truncation_reason"]


def test_scope_change_rewrites_document(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [])
    write_selected_pr(store, 101, "Follow-up to #77.")
    store.write_jsonl("raw/prs.jsonl", [{"number": 101}], key="number")
    client = make_client(
        store,
        rest_handler({77: issue_body(77, title="Old proposal")}),
    )
    first = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
        pr_selection={"candidates": [{"number": 101, "selected": True}]},
    )
    assert first.counts["context_selected"] == 1
    assert load_issue_documents(store)[0]["scope"] == "context"

    updated = issue_body(77, title="Old proposal")
    write_issue_stream(store, [updated])
    client2 = make_client(
        store,
        rest_handler({77: updated}),
    )
    collect_proposals(
        context_for(client2, store, clock=lambda: "2026-11-01T09:00:00Z"),
        activity_window=WINDOW,
        pr_selection={"candidates": [{"number": 101, "selected": True}]},
    )

    document = load_issue_documents(store)[0]
    assert document["scope"] == "in_window"
    assert "out_of_window_context" not in document["limitations"]
    assert document["observed_at"] == "2026-11-01T09:00:00Z"


def test_invalid_existing_entity_does_not_block_collect(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    write_issue_stream(store, [issue_body()])
    store.write_jsonl(
        "normalized/entities.jsonl",
        [
            {
                "id": "github.com/acme/demo/issue/42",
                "kind": "issue",
                "roles": ["proposal"],
            }
        ],
        key="id",
    )
    client = make_client(store, rest_handler({42: issue_body()}))

    result = collect_proposals(
        context_for(client, store),
        activity_window=WINDOW,
    )

    assert result.counts["selected"] == 1
    assert load_issue_documents(store)
