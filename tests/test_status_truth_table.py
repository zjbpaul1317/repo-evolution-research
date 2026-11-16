from __future__ import annotations

from typing import Any

import pytest

from rer.collectors.status import (
    combine_status,
    derive_detail_status,
    derive_limitations,
    derive_pr_detail_channel_status,
    derive_selection_limitations,
)

FORCED_STATUSES = (
    "complete",
    "sampled",
    "partial",
    "unavailable",
    "not_applicable",
    "not_requested",
)


def section(
    status: str,
    *,
    truncated: bool = False,
    items: list[Any] | None = None,
) -> dict[str, Any]:
    return {"status": status, "truncated": truncated, "items": items or []}


def document(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "files": section("complete"),
        "commits": section("complete"),
        "issue_comments": section("complete"),
        "reviews": section("complete"),
        "review_comments": section("complete"),
        "head_sha": "a" * 40,
        "head_sha_end": "a" * 40,
        "head_changed": False,
        "merged": False,
        "diff": {"status": "not_requested"},
    }
    base.update(overrides)
    keys = ("files", "commits", "issue_comments", "reviews", "review_comments")
    base["coverage"] = {
        "detail": (
            "complete"
            if all(base[key]["status"] == "complete" for key in keys)
            else "partial"
        ),
        **{key: base[key]["status"] for key in keys},
    }
    return base


def sections(**overrides: Any) -> dict[str, dict[str, Any]]:
    base = {
        "files": section("complete"),
        "commits": section("complete"),
        "issue_comments": section("complete"),
        "reviews": section("complete"),
        "review_comments": section("complete"),
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), "not_requested"),
        (("not_requested",), "not_requested"),
        (("not_applicable",), "not_applicable"),
        (("complete",), "complete"),
        (("complete", "complete"), "complete"),
        (("complete", "partial"), "partial"),
        (("complete", "unavailable"), "partial"),
        (("unavailable",), "unavailable"),
        (("unavailable", "not_requested"), "unavailable"),
        (("sampled",), "sampled"),
        (("sampled", "complete"), "sampled"),
        (("sampled", "partial"), "partial"),
    ],
)
def test_combine_status_truth_table(statuses, expected: str) -> None:
    assert combine_status(statuses) == expected


def test_detail_status_complete_only_when_all_requested_sections_complete() -> None:
    assert (
        derive_detail_status(
            sections=sections(),
            head_changed=False,
            head_unverified=False,
            diff_status="not_requested",
        )
        == "complete"
    )
    assert (
        derive_detail_status(
            sections=sections(files=section("partial")),
            head_changed=False,
            head_unverified=False,
            diff_status="not_requested",
        )
        == "partial"
    )
    assert (
        derive_detail_status(
            sections=sections(commits=section("unavailable")),
            head_changed=False,
            head_unverified=False,
            diff_status="not_requested",
        )
        == "partial"
    )
    assert (
        derive_detail_status(
            sections={key: section("unavailable") for key in sections()},
            head_changed=False,
            head_unverified=False,
            diff_status="not_requested",
        )
        == "partial"
    )


def test_detail_status_ignores_unrequested_sections() -> None:
    assert (
        derive_detail_status(
            sections=sections(reviews=section("not_requested")),
            head_changed=False,
            head_unverified=False,
            diff_status="not_requested",
        )
        == "complete"
    )
    assert (
        derive_detail_status(
            sections={key: section("not_requested") for key in sections()},
            head_changed=False,
            head_unverified=False,
            diff_status="not_requested",
        )
        == "not_requested"
    )


@pytest.mark.parametrize(
    ("overrides", "diff_status", "expected"),
    [
        ({"head_changed": True}, "not_requested", "partial"),
        ({"head_sha_end": None}, "not_requested", "partial"),
        ({}, "budget_capped", "partial"),
        ({}, "unavailable", "partial"),
        ({}, "complete", "complete"),
    ],
)
def test_detail_status_head_and_diff_guards(overrides, diff_status: str, expected: str) -> None:
    base = document(**overrides)
    assert (
        derive_detail_status(
            sections={key: base[key] for key in sections()},
            head_changed=base["head_changed"],
            head_unverified=base["head_sha"] is None or base["head_sha_end"] is None,
            diff_status=diff_status,
        )
        == expected
    )


def test_all_unavailable_sections_stay_partial_because_metadata_is_usable() -> None:
    assert (
        derive_detail_status(
            sections={key: section("unavailable") for key in sections()},
            head_changed=False,
            head_unverified=False,
            diff_status="not_requested",
        )
        == "partial"
    )


def test_channel_status_treats_not_attempted_selection_as_partial() -> None:
    complete = document()
    assert (
        derive_pr_detail_channel_status(
            selected=2,
            failed=0,
            skipped_unchanged=0,
            results=[complete],
            not_attempted=1,
        )
        == "partial"
    )
    assert (
        derive_pr_detail_channel_status(
            selected=2,
            failed=0,
            skipped_unchanged=0,
            results=[],
            not_attempted=2,
        )
        == "partial"
    )


def test_channel_status_ignores_stale_coverage_detail() -> None:
    doc = document(files=section("partial"))
    doc["coverage"]["detail"] = "complete"
    assert (
        derive_pr_detail_channel_status(
            selected=1,
            failed=0,
            skipped_unchanged=0,
            results=[doc],
        )
        == "partial"
    )


def test_channel_status_truth_table() -> None:
    complete = document()
    partial = document(files=section("partial"))

    assert (
        derive_pr_detail_channel_status(
            selected=0, failed=0, skipped_unchanged=0, results=[]
        )
        == "not_requested"
    )
    assert (
        derive_pr_detail_channel_status(
            selected=2, failed=2, skipped_unchanged=0, results=[]
        )
        == "unavailable"
    )
    assert (
        derive_pr_detail_channel_status(
            selected=2, failed=1, skipped_unchanged=0, results=[]
        )
        == "partial"
    )
    assert (
        derive_pr_detail_channel_status(
            selected=2, failed=0, skipped_unchanged=2, results=[]
        )
        == "complete"
    )
    assert (
        derive_pr_detail_channel_status(
            selected=1, failed=0, skipped_unchanged=0, results=[complete]
        )
        == "complete"
    )
    assert (
        derive_pr_detail_channel_status(
            selected=1, failed=0, skipped_unchanged=0, results=[partial]
        )
        == "partial"
    )
    assert (
        derive_pr_detail_channel_status(
            selected=2, failed=1, skipped_unchanged=0, results=[complete]
        )
        == "partial"
    )


def test_limitations_are_derived_from_document() -> None:
    assert derive_limitations(document()) == ["observed_current_state"]
    assert derive_limitations(
        document(
            files=section("partial", truncated=True, items=[{"patch_status": "missing"}]),
            commits=section("partial", truncated=True),
            issue_comments=section("partial", truncated=True),
            review_comments=section("partial", truncated=True),
            head_changed=True,
            head_sha_end=None,
            merged=True,
            reviews=section("complete", items=[{"id": 1}]),
            diff={"status": "budget_capped"},
        )
    ) == [
        "commits_truncated",
        "diff_budget_capped",
        "files_truncated",
        "head_changed",
        "head_unverified",
        "issue_comments_truncated",
        "observed_current_state",
        "patch_missing",
        "review_comments_truncated",
        "review_resolution_unknown",
        "squash_rebase_unverified",
    ]


def test_selection_limitations_are_derived() -> None:
    assert derive_selection_limitations(
        has_quota_skip=False, coverage_status="complete"
    ) == ["observed_current_state"]
    assert derive_selection_limitations(
        has_quota_skip=True, coverage_status="partial", extra=("activity_overview_missing",)
    ) == [
        "activity_overview_missing",
        "observed_current_state",
        "pr_detail_incomplete",
        "quota_limited_selection",
    ]
