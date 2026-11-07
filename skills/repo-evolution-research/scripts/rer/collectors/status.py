"""Single status algebra for coverage, detail documents and channels.

Activity view/shard state machines still own their local status strings; folding
them into this module is deferred (see references/evidence-and-state.md known
exceptions). This module is the only place that derives PR detail, channel and
selection limitations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..errors import EXIT_PARTIAL

PR_DETAIL_SECTIONS = (
    "files",
    "commits",
    "issue_comments",
    "reviews",
    "review_comments",
)
ISSUE_DETAIL_SECTIONS = ("comments", "timeline")
DISCUSSION_DETAIL_SECTIONS = ("comments",)

_DIFF_DEGRADED = ("unavailable", "budget_capped")


def combine_status(statuses: Iterable[str]) -> str:
    values = set(statuses)
    if not values or values == {"not_requested"}:
        return "not_requested"
    if values <= {"not_applicable"}:
        return "not_applicable"
    if "partial" in values:
        return "partial"
    if "unavailable" in values:
        return "partial" if "complete" in values else "unavailable"
    if "sampled" in values:
        return "sampled"
    if "complete" in values:
        return "complete"
    return "unavailable"


def derive_detail_status(
    *,
    sections: Mapping[str, Mapping[str, Any]],
    head_changed: bool,
    head_unverified: bool,
    diff_status: str,
) -> str:
    active = [
        section["status"]
        for section in sections.values()
        if section["status"] not in ("not_requested", "not_applicable")
    ]
    if not active:
        return "not_requested"
    # Even when every requested section is unavailable the detail response
    # itself (title, body ref, timestamps) remains usable, so the document is
    # partial rather than unavailable.
    detail = "complete" if all(status == "complete" for status in active) else "partial"
    if head_changed or head_unverified:
        detail = "partial"
    if diff_status in _DIFF_DEGRADED:
        detail = "partial"
    return detail


def derive_detail_channel_status(
    *,
    selected: int,
    failed: int,
    skipped_unchanged: int,
    results: Iterable[Mapping[str, Any]],
    not_attempted: int = 0,
    detail_of: Callable[[Mapping[str, Any]], str],
) -> str:
    """Derive a deep-read channel status from section truth only.

    `coverage.detail` is a derived view inside the document; channel status is
    recomputed from the sections so an inconsistent stored view cannot mask a
    broken invariant. Each collector supplies `detail_of`, which must rebuild
    the document's detail status from its own sections.
    """
    materialized = list(results)
    if selected == 0:
        return "not_requested"
    if not_attempted:
        return "partial"
    if not materialized:
        if failed:
            return "unavailable" if failed == selected else "partial"
        if skipped_unchanged == selected:
            return "complete"
        return "partial"
    if failed:
        return "partial"
    for item in materialized:
        if detail_of(item) != "complete":
            return "partial"
    return "complete"


def derive_pr_detail_channel_status(
    *,
    selected: int,
    failed: int,
    skipped_unchanged: int,
    results: Iterable[Mapping[str, Any]],
    not_attempted: int = 0,
) -> str:
    return derive_detail_channel_status(
        selected=selected,
        failed=failed,
        skipped_unchanged=skipped_unchanged,
        results=results,
        not_attempted=not_attempted,
        detail_of=_pr_detail_status,
    )


def _pr_detail_status(document: Mapping[str, Any]) -> str:
    return derive_detail_status(
        sections={key: document[key] for key in PR_DETAIL_SECTIONS},
        head_changed=bool(document["head_changed"]),
        head_unverified=document["head_sha"] is None
        or document["head_sha_end"] is None,
        diff_status=str(document["diff"]["status"]),
    )


def derive_issue_detail_status(document: Mapping[str, Any]) -> str:
    sections = {
        key: document[key] for key in ISSUE_DETAIL_SECTIONS
    }
    sections["body"] = {"status": _body_coverage(document)}
    return _proposal_detail_status(sections)


def derive_discussion_detail_status(document: Mapping[str, Any]) -> str:
    sections = {
        key: document[key] for key in DISCUSSION_DETAIL_SECTIONS
    }
    sections["body"] = {"status": _body_coverage(document)}
    return _proposal_detail_status(sections)


def _proposal_detail_status(sections: Mapping[str, Mapping[str, Any]]) -> str:
    status = derive_detail_status(
        sections=sections,
        head_changed=False,
        head_unverified=False,
        diff_status="not_requested",
    )
    return status if status in ("complete", "partial") else "partial"


def _body_coverage(document: Mapping[str, Any]) -> str:
    coverage = document.get("coverage")
    if isinstance(coverage, Mapping):
        status = coverage.get("body")
        if isinstance(status, str):
            return status
    return "complete"


def derive_stage_status(*, exit_code: int, has_coverage: bool) -> tuple[str, str]:
    """Map an interrupted run to (manifest state, stage status)."""
    if exit_code == EXIT_PARTIAL:
        return "interrupted", "partial" if has_coverage else "failed"
    return "failed", "failed"


def _duplicate_count(document: Mapping[str, Any], keys: Iterable[str]) -> int:
    total = 0
    for key in keys:
        section = document.get(key)
        if isinstance(section, Mapping):
            total += int(section.get("duplicates") or 0)
    return total


def derive_limitations(document: Mapping[str, Any]) -> list[str]:
    limitations: list[str] = ["observed_current_state"]
    if any(
        item.get("patch_status") == "missing"
        for item in document["files"]["items"]
    ):
        limitations.append("patch_missing")
    for key in ("files", "commits", "issue_comments", "review_comments"):
        if document[key]["truncated"]:
            limitations.append(f"{key}_truncated")
    duplicates = _duplicate_count(document, PR_DETAIL_SECTIONS)
    if duplicates:
        limitations.append(f"pagination_duplicates:{duplicates}")
    if document["head_changed"]:
        limitations.append("head_changed")
    if document["head_sha"] is None or document["head_sha_end"] is None:
        limitations.append("head_unverified")
    if document["reviews"]["items"] or document["review_comments"]["items"]:
        limitations.append("review_resolution_unknown")
    # The document's merged flag is `merged or merged_at`; this is the
    # research-model meaning and is intentionally used even when raw flags
    # disagree.
    if document["merged"]:
        limitations.append("squash_rebase_unverified")
    diff_status = document["diff"]["status"]
    if diff_status == "budget_capped":
        limitations.append("diff_budget_capped")
    elif diff_status == "unavailable":
        limitations.append("diff_unavailable")
    return sorted(dict.fromkeys(limitations))


def derive_selection_limitations(
    *,
    has_quota_skip: bool,
    coverage_status: str,
    extra: Iterable[str] = (),
) -> list[str]:
    limitations = {"observed_current_state", *extra}
    if has_quota_skip:
        limitations.add("quota_limited_selection")
    if coverage_status != "complete":
        limitations.add("pr_detail_incomplete")
    return sorted(limitations)


def derive_issue_limitations(document: Mapping[str, Any]) -> list[str]:
    limitations = {"observed_current_state"}
    if document["scope"] == "context":
        limitations.add("out_of_window_context")
    if document["state_reason"] == "not_planned":
        limitations.add("closed_not_planned")
    elif document["state_reason"] == "duplicate":
        limitations.add("closed_as_duplicate")
    elif document["state"] == "closed" and document["state_reason"] is None:
        limitations.add("state_reason_unknown")
    for key in ISSUE_DETAIL_SECTIONS:
        if document[key]["truncated"]:
            limitations.add(f"{key}_truncated")
    duplicates = _duplicate_count(document, ISSUE_DETAIL_SECTIONS)
    if duplicates:
        limitations.add(f"pagination_duplicates:{duplicates}")
    if derive_issue_detail_status(document) != "complete":
        limitations.add("detail_partial")
    return sorted(limitations)


def derive_discussion_limitations(document: Mapping[str, Any]) -> list[str]:
    limitations = {"observed_current_state"}
    if document["scope"] == "context":
        limitations.add("out_of_window_context")
    if document["comments"]["truncated"]:
        limitations.add("comments_truncated")
    if any(
        item.get("replies_truncated")
        for item in document["comments"]["items"]
    ):
        limitations.add("discussion_replies_truncated")
    duplicates = _duplicate_count(document, DISCUSSION_DETAIL_SECTIONS)
    if duplicates:
        limitations.add(f"pagination_duplicates:{duplicates}")
    if document["state_reason"] in ("outdated", "duplicate"):
        limitations.add(f"closed_{document['state_reason']}")
    if derive_discussion_detail_status(document) != "complete":
        limitations.add("detail_partial")
    return sorted(limitations)


def derive_proposal_selection_limitations(
    *,
    has_quota_skip: bool,
    issue_status: str,
    discussion_status: str,
    extra: Iterable[str] = (),
) -> list[str]:
    limitations = {"observed_current_state", *extra}
    if has_quota_skip:
        limitations.add("quota_limited_selection")
    if "partial" in (issue_status, discussion_status):
        limitations.add("proposal_detail_incomplete")
    if "unavailable" in (issue_status, discussion_status):
        limitations.add("proposal_detail_unavailable")
    return sorted(limitations)


__all__ = [
    "DISCUSSION_DETAIL_SECTIONS",
    "ISSUE_DETAIL_SECTIONS",
    "PR_DETAIL_SECTIONS",
    "combine_status",
    "derive_detail_channel_status",
    "derive_detail_status",
    "derive_discussion_detail_status",
    "derive_discussion_limitations",
    "derive_issue_detail_status",
    "derive_issue_limitations",
    "derive_limitations",
    "derive_pr_detail_channel_status",
    "derive_proposal_selection_limitations",
    "derive_selection_limitations",
    "derive_stage_status",
]
