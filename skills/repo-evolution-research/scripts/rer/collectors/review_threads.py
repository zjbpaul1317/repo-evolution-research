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
MAX_THREAD_COMMENT_PAGES = 10
MAX_THREAD_COMMENT_REQUESTS = 20

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
            pageInfo { hasNextPage endCursor }
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

REVIEW_THREAD_COMMENTS_QUERY = """
query($threadId: ID!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: __COMMENTS__, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { databaseId }
      }
    }
  }
}
""".replace("__COMMENTS__", str(REVIEW_THREADS_COMMENTS_PAGE_SIZE))


@dataclass
class ReviewThreadRead:
    section: dict[str, Any]
    pages: int


@dataclass
class _ThreadItem:
    item: dict[str, Any]
    comments_has_next: bool
    comments_cursor: str | None


@dataclass
class _CommentPagination:
    pages: int = 0
    requests: int = 0
    capped: bool = False
    errors: bool = False
    connection_missing: bool = False
    cursor_missing: bool = False


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
    comment_state = _CommentPagination()

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
                item = parsed.item
                identifier = str(item["id"])
                if identifier in items:
                    duplicates += 1
                    continue
                items[identifier] = item
                if parsed.comments_has_next:
                    if parsed.comments_cursor is None:
                        comment_state.cursor_missing = True
                        item["comments_truncated"] = True
                    else:
                        _complete_comment_pages(
                            client,
                            item,
                            parsed.comments_cursor,
                            comment_state,
                        )
                if item["comments_truncated"]:
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

    pages += comment_state.pages
    if errors:
        reason_parts.append("graphql_errors")
    if comment_state.errors:
        reason_parts.append("review_threads_comments_graphql_errors")
    if comment_state.connection_missing:
        reason_parts.append("review_threads_comments_connection_missing")
    if comment_state.cursor_missing:
        reason_parts.append("review_threads_comments_cursor_missing")
    if comment_state.capped:
        reason_parts.append("review_threads_comments_budget_capped")
    if comments_truncated:
        reason_parts.append("review_threads_comments_truncated")
    if total_known is not None and total_known > len(items):
        reason_parts.append("review_threads_truncated")
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


def _complete_comment_pages(
    client: Any,
    item: dict[str, Any],
    cursor: str,
    state: _CommentPagination,
) -> None:
    seen = set(item.get("comment_ids") or [])
    thread_pages = 0
    while True:
        if state.requests >= MAX_THREAD_COMMENT_REQUESTS:
            state.capped = True
            item["comment_ids"] = sorted(seen)
            item["comments_truncated"] = True
            return
        if thread_pages >= MAX_THREAD_COMMENT_PAGES:
            state.capped = True
            item["comment_ids"] = sorted(seen)
            item["comments_truncated"] = True
            return
        try:
            result = client.graphql(
                REVIEW_THREAD_COMMENTS_QUERY,
                variables={"threadId": item["id"], "cursor": cursor},
                page=state.requests + 1,
            )
        except FATAL_ERRORS:
            raise
        except GitHubError:
            state.errors = True
            item["comment_ids"] = sorted(seen)
            item["comments_truncated"] = True
            return
        state.requests += 1
        state.pages += 1
        thread_pages += 1
        if result.errors:
            state.errors = True
            item["comment_ids"] = sorted(seen)
            item["comments_truncated"] = True
            return
        connection = get_in(result.data, "node", "comments")
        if not isinstance(connection, Mapping):
            state.connection_missing = True
            item["comment_ids"] = sorted(seen)
            item["comments_truncated"] = True
            return
        total = as_optional_int(connection.get("totalCount"))
        if total is not None:
            item["comments_total"] = total
        nodes = connection.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                value = (
                    as_optional_int(get_in(node, "databaseId"))
                    if isinstance(node, Mapping)
                    else None
                )
                if value is not None and value not in seen:
                    seen.add(value)
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, Mapping) or not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            state.cursor_missing = True
            item["comment_ids"] = sorted(seen)
            item["comments_truncated"] = True
            return
        cursor = next_cursor
    item["comment_ids"] = sorted(seen)
    total = item.get("comments_total")
    item["comments_truncated"] = bool(total is not None and total > len(seen))


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


def _thread_item(raw: Any) -> _ThreadItem | None:
    if not isinstance(raw, Mapping):
        return None
    identifier = raw.get("id")
    if not isinstance(identifier, str) or not identifier:
        return None
    comments = raw.get("comments")
    comment_ids: list[int] = []
    comments_total: int | None = None
    comments_truncated = False
    comments_has_next = False
    comments_cursor: str | None = None
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
                if value is not None and value not in comment_ids:
                    comment_ids.append(value)
        if comments_total is not None and comments_total > len(comment_ids):
            comments_truncated = True
        page_info = comments.get("pageInfo")
        if isinstance(page_info, Mapping):
            comments_has_next = bool(page_info.get("hasNextPage"))
            cursor = page_info.get("endCursor")
            if isinstance(cursor, str) and cursor:
                comments_cursor = cursor
    item = {
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
    return _ThreadItem(
        item=item,
        comments_has_next=comments_has_next,
        comments_cursor=comments_cursor,
    )


__all__ = [
    "MAX_THREAD_COMMENT_PAGES",
    "MAX_THREAD_COMMENT_REQUESTS",
    "REVIEW_THREADS_QUERY",
    "REVIEW_THREAD_COMMENTS_QUERY",
    "ReviewThreadRead",
    "read_review_threads",
]
