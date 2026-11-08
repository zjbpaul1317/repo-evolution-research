"""Bounded GraphQL read of pull request review thread resolution.

GitHub's REST API exposes review comments but not the resolved state of the
thread they belong to. This collector keeps review comment bodies in the
existing ``review_comments`` section and adds thread-level metadata next to it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import GitHubError
from .base import FATAL_ERRORS
from .values import as_optional_int, get_in

REVIEW_THREADS_PAGE_SIZE = 100
REVIEW_THREADS_COMMENTS_PAGE_SIZE = 100

REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: __PAGE__, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          startLine
          comments(first: __COMMENTS__) {
            totalCount
            nodes { databaseId }
          }
        }
      }
    }
  }
}
""".replace("__PAGE__", str(REVIEW_THREADS_PAGE_SIZE)).replace(
    "__COMMENTS__", str(REVIEW_THREADS_COMMENTS_PAGE_SIZE)
)


@dataclass
class ReviewThreadRead:
    section: dict[str, Any]
    pages: int


def read_review_threads(
    client: Any,
    *,
    owner: str,
    name: str,
    number: int,
) -> ReviewThreadRead:
    cursor: str | None = None
    page = 1
    pages = 0
    items: dict[str, dict[str, Any]] = {}
    total_known: int | None = None
    errors = False
    reason_parts: list[str] = []
    comments_truncated = False
    duplicates = 0

    while True:
        variables: dict[str, Any] = {
            "owner": owner,
            "name": name,
            "number": number,
        }
        if cursor is not None:
            variables["cursor"] = cursor
        try:
            result = client.graphql(
                REVIEW_THREADS_QUERY,
                variables=variables,
                page=page,
            )
        except FATAL_ERRORS:
            raise
        except GitHubError:
            return ReviewThreadRead(
                section=_section(
                    status="unavailable",
                    fetched=len(items),
                    total_known=total_known,
                    reason="review_threads_unavailable",
                    items=list(items.values()),
                ),
                pages=pages,
            )
        pages += 1
        if result.errors:
            errors = True
        connection = get_in(
            result.data,
            "repository",
            "pullRequest",
            "reviewThreads",
        )
        if not isinstance(connection, Mapping):
            if not items:
                return ReviewThreadRead(
                    section=_section(
                        status="unavailable",
                        fetched=0,
                        total_known=None,
                        reason="review_threads_connection_missing",
                        items=[],
                    ),
                    pages=pages,
                )
            reason_parts.append("review_threads_connection_missing")
            break
        if total_known is None:
            total_known = as_optional_int(connection.get("totalCount"))
        nodes = connection.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                parsed = _thread_item(node)
                if parsed is None:
                    errors = True
                    continue
                identifier = str(parsed["id"])
                if identifier in items:
                    duplicates += 1
                    continue
                items[identifier] = parsed
                if parsed["comments_truncated"]:
                    comments_truncated = True
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, Mapping) or not page_info.get("hasNextPage"):
            break
        cursor_value = page_info.get("endCursor")
        if not isinstance(cursor_value, str) or not cursor_value:
            reason_parts.append("review_threads_cursor_missing")
            break
        cursor = cursor_value
        page += 1

    if errors:
        reason_parts.append("graphql_errors")
    if total_known is not None and total_known > len(items):
        reason_parts.append("review_threads_truncated")
    if comments_truncated:
        reason_parts.append("review_threads_comments_truncated")
    if duplicates:
        reason_parts.append(f"pagination_duplicates:{duplicates}")
    reason = "; ".join(dict.fromkeys(reason_parts)) or None
    status = "partial" if reason else "complete"
    if not items and errors:
        status = "unavailable"
    return ReviewThreadRead(
        section=_section(
            status=status,
            fetched=len(items),
            total_known=total_known,
            reason=reason,
            items=[items[key] for key in sorted(items)],
            duplicates=duplicates,
        ),
        pages=pages,
    )


def _section(
    *,
    status: str,
    fetched: int,
    total_known: int | None,
    reason: str | None,
    items: list[dict[str, Any]],
    duplicates: int = 0,
) -> dict[str, Any]:
    return {
        "status": status,
        "fetched": fetched,
        "total_known": total_known,
        "truncated": status in ("partial", "unavailable"),
        "truncation_reason": reason,
        "duplicates": duplicates,
        "items": items,
    }


def _thread_item(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    comments = raw.get("comments")
    comment_ids: list[int] = []
    comments_total: int | None = None
    comments_truncated = False
    if isinstance(comments, Mapping):
        comments_total = as_optional_int(comments.get("totalCount"))
        nodes = comments.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                value = (
                    as_optional_int(get_in(node, "databaseId"))
                    if isinstance(node, Mapping)
                    else None
                )
                if value is not None:
                    comment_ids.append(value)
        if comments_total is not None and comments_total > len(comment_ids):
            comments_truncated = True
    return {
        "id": identifier,
        "is_resolved": bool(raw.get("isResolved")),
        "is_outdated": bool(raw.get("isOutdated")),
        "path": raw.get("path") if isinstance(raw.get("path"), str) else None,
        "line": as_optional_int(raw.get("line")),
        "start_line": as_optional_int(raw.get("startLine")),
        "comment_ids": sorted(comment_ids),
        "comments_total": comments_total,
        "comments_truncated": comments_truncated,
    }


__all__ = ["REVIEW_THREADS_QUERY", "ReviewThreadRead", "read_review_threads"]
