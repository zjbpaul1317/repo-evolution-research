from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from rer.collectors.pages import FetchResult, build_section, fetch_rest_list
from rer.collectors.selection import quota_for_budget
from rer.collectors.values import reaction_count, window_bounds
from rer.errors import GitHubNotFoundError, GitHubRateLimitError
from rer.github import RestPage


class FakeClient:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages

    def iter_rest_pages(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Iterator[RestPage]:
        for item in self.pages:
            if isinstance(item, BaseException):
                raise item
            yield item


def page(data: Any, *, link: str | None = None, number: int = 1) -> RestPage:
    headers = {"link": link} if link else {}
    return RestPage(data=data, status=200, headers=headers, request_id="r", page=number)


def test_cap_exact_boundary_without_next_is_complete() -> None:
    result = fetch_rest_list(
        FakeClient([page([1, 2])]),
        "items",
        cap=2,
        cap_reason="cap",
    )
    assert result.items == [1, 2]
    assert result.status == "complete"
    assert result.reason is None
    assert result.pages == 1


def test_cap_overflow_is_partial() -> None:
    result = fetch_rest_list(
        FakeClient([page([1, 2, 3])]),
        "items",
        cap=2,
        cap_reason="cap",
    )
    assert result.items == [1, 2]
    assert result.status == "partial"
    assert result.reason == "cap"


def test_cap_exact_boundary_with_next_link_is_partial() -> None:
    result = fetch_rest_list(
        FakeClient(
            [
                page(
                    [1, 2],
                    link='<https://api.github.com/items?page=2>; rel="next"',
                )
            ]
        ),
        "items",
        cap=2,
        cap_reason="cap",
    )
    assert result.items == [1, 2]
    assert result.status == "partial"
    assert result.reason == "cap"


def test_mapping_response_uses_items_key() -> None:
    result = fetch_rest_list(FakeClient([page({"items": [1, 2]})]), "items")
    assert result.items == [1, 2]
    assert result.status == "complete"


def test_404_after_items_keeps_partial_result() -> None:
    result = fetch_rest_list(
        FakeClient(
            [
                page([1]),
                GitHubNotFoundError("gone", source="test"),
            ]
        ),
        "items",
    )
    assert result.items == [1]
    assert result.status == "partial"
    assert result.reason == "not_found"


def test_fatal_error_is_rethrown() -> None:
    with pytest.raises(GitHubRateLimitError):
        fetch_rest_list(
            FakeClient([GitHubRateLimitError("slow down", source="test")]),
            "items",
        )


def test_invalid_list_payload_is_reported_as_unavailable() -> None:
    result = fetch_rest_list(FakeClient([page({"unexpected": []})]), "items")
    assert result.status == "unavailable"
    assert result.reason == "github_error"


def test_build_section_marks_total_mismatch_partial() -> None:
    section = build_section(
        FetchResult(items=[1, 2], status="complete", pages=1, reason=None),
        [{"id": 1}, {"id": 2}],
        total_known=5,
        mismatch_reason="total_mismatch",
    )
    assert section["status"] == "partial"
    assert section["truncated"] is True
    assert section["truncation_reason"] == "total_mismatch"


def test_build_section_404_without_items_is_unavailable() -> None:
    section = build_section(
        FetchResult(items=[], status="unavailable", pages=1, reason="not_found"),
        [],
        total_known=0,
    )
    assert section["status"] == "unavailable"
    assert section["truncated"] is False
    assert section["truncation_reason"] == "not_found"


def test_window_bounds_reports_invalid_timestamp_and_order_separately() -> None:
    with pytest.raises(ValueError, match="missing or invalid"):
        window_bounds({"start": "not-a-time", "end": "2026-11-01T00:00:00Z"})
    with pytest.raises(ValueError, match="must be after"):
        window_bounds(
            {
                "start": "2026-11-02T00:00:00Z",
                "end": "2026-11-01T00:00:00Z",
            }
        )


def test_reaction_count_upvote_fallback_is_opt_in() -> None:
    raw = {"upvoteCount": 3}
    assert reaction_count(raw) == 3
    assert reaction_count(raw, allow_upvote=False) is None


def test_quota_split_is_deterministic() -> None:
    assert quota_for_budget({"max_deep_prs": 22, "max_deep_entities": 40}) == (
        22,
        14,
        8,
    )
    assert quota_for_budget({"max_deep_prs": 8, "max_deep_entities": 12}) == (
        8,
        5,
        3,
    )
    assert quota_for_budget(
        {
            "max_deep_prs": 22,
            "max_deep_entities": 40,
            "agent_pr_reserve": 4,
        }
    ) == (18, 11, 7)
    assert quota_for_budget(
        {
            "max_deep_prs": 22,
            "max_deep_entities": 40,
            "agent_pr_reserve": 4,
        },
        30,
    ) == (6, 4, 2)
    assert quota_for_budget(
        {
            "max_deep_prs": 22,
            "max_deep_entities": 40,
            "agent_pr_reserve": 4,
        },
        40,
    ) == (0, 0, 0)
