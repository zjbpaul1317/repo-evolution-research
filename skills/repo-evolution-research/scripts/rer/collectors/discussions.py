"""GraphQL deep read for GitHub Discussions.

Discussion bodies, comments and replies are mutable materials: every selected
discussion is re-read. GraphQL partial data is preserved, but a failed query is
never turned into an empty success.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .. import SCHEMA_VERSION
from ..errors import GitHubError
from ..models import canonical_json, validate_document
from ..store import SnapshotStore
from .base import CollectContext
from .status import derive_detail_status, derive_discussion_limitations
from .texts import text_ref
from .values import as_optional_int, as_optional_str, get_in, iso_or_none

MAX_COMMENT_PAGES = 20
MAX_REPLY_PAGES = 5
MAX_REPLY_CONTINUATIONS = 10
COMMENT_PAGE_SIZE = 50
_STATE_REASONS = frozenset({"resolved", "outdated", "duplicate", "reopened"})


class ProposalNotFound(GitHubError):
    """The proposal object does not exist or is not accessible."""

    code = "proposal_not_found"


@dataclass
class DiscussionRead:
    document: dict[str, Any]
    pages: int
    changed: bool = True


@dataclass
class _Comment:
    item: dict[str, Any]
    replies_truncated: bool
    replies_page: dict[str, Any] | None = None
    duplicates: int = 0


_DETAIL_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      id
      number
      title
      url
      body
      createdAt
      updatedAt
      closed
      closedAt
      stateReason
      upvoteCount
      isAnswered
      author { login }
      category { name }
      answer { id }
      comments(first: __PAGE__, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          databaseId
          body
          createdAt
          updatedAt
          isAnswer
          author { login }
          replies(first: __PAGE__) {
            totalCount
            pageInfo { hasNextPage endCursor }
            nodes { id databaseId body createdAt updatedAt author { login } }
          }
        }
      }
    }
  }
}
""".replace("__PAGE__", str(COMMENT_PAGE_SIZE))


_DETAIL_ONLY_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    discussion(number: $number) {
      id
      number
      title
      url
      body
      createdAt
      updatedAt
      closed
      closedAt
      stateReason
      upvoteCount
      isAnswered
      author { login }
      category { name }
      answer { id }
      comments(first: 1) {
        totalCount
      }
    }
  }
}
"""


_REPLIES_QUERY = """
query($commentId: ID!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  node(id: $commentId) {
    ... on DiscussionComment {
      replies(first: __PAGE__, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { id databaseId body createdAt updatedAt author { login } }
      }
    }
  }
}
""".replace("__PAGE__", str(COMMENT_PAGE_SIZE))


def read_discussion(
    context: CollectContext,
    number: int,
    *,
    scope: str,
    selection_reason: list[str],
    role_hints: list[str],
    observed_at: str,
    read: Mapping[str, bool] | None = None,
    existing: Mapping[str, Any] | None = None,
) -> DiscussionRead:
    store = context.store
    host = str(context.repository.get("host") or "github.com")
    owner = str(context.repository["owner"])
    name = str(context.repository["name"])
    want_comments = read is None or read.get("comments", True)
    if want_comments:
        (
            detail,
            pages,
            comments,
            total_known,
            list_truncated,
            errors,
            list_reason,
            comment_duplicates,
        ) = _fetch_comments(
            context,
            owner=owner,
            name=name,
            number=number,
        )
        reply_results = _fetch_reply_continuations(context, comments)
        pages += reply_results.pages
        truncated = list_truncated
        reasons = []
        if list_truncated:
            reasons.append(list_reason or "comments_truncated")
        reasons.extend(reply_results.reasons)
        if errors:
            reasons.append("graphql_errors")
        comment_status = "complete"
        if errors or truncated or any(
            item.replies_truncated for item in reply_results.comments
        ):
            comment_status = "partial"
        sections = {
            "comments": {
                "status": comment_status,
                "fetched": len(reply_results.comments),
                "total_known": total_known,
                "truncated": truncated,
                "truncation_reason": "; ".join(dict.fromkeys(reasons)) or None,
                "duplicates": comment_duplicates + reply_results.duplicates,
                "items": [item.item for item in reply_results.comments],
            }
        }
    else:
        detail, pages, total_known = _fetch_detail(
            context,
            owner=owner,
            name=name,
            number=number,
        )
        sections = {
            "comments": _existing_section(existing, "comments", total_known)
        }
    body_ref = text_ref(store, detail.get("body"))
    body_status = "complete"
    status_sections = {
        **sections,
        "body": {"status": body_status},
    }
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repository": {"host": host, "owner": owner, "name": name},
        "number": number,
        "url": str(
            detail.get("url")
            or f"https://github.com/{owner}/{name}/discussions/{number}"
        ),
        "title": str(detail.get("title") or f"#{number}"),
        "author": _login(detail.get("author")),
        "category": _category(detail),
        "closed": bool(detail.get("closed")),
        "closed_at": iso_or_none(detail.get("closedAt")),
        "state_reason": _state_reason(detail.get("stateReason")),
        "created_at": iso_or_none(detail.get("createdAt")),
        "updated_at": iso_or_none(detail.get("updatedAt")),
        "upvote_count": _count(detail.get("upvoteCount")),
        "is_answered": bool(detail.get("isAnswered")),
        "answer_comment_id": as_optional_str(get_in(detail, "answer", "id")),
        "scope": scope,
        "selection_reason": list(selection_reason),
        "role_hints": list(role_hints),
        "body": body_ref,
        "comments": sections["comments"],
        "coverage": {
            "detail": _proposal_detail_status(status_sections),
            "body": body_status,
            "comments": sections["comments"]["status"],
        },
        "limitations": [],
    }
    document["limitations"] = derive_discussion_limitations(document)
    validate_document(document, "discussion_detail")
    changed = existing is None or not _same_material(existing, document)
    return DiscussionRead(document=document, pages=pages, changed=changed)


_NON_MATERIAL_FIELDS = frozenset({"observed_at", "selection_reason", "role_hints"})


def _same_material(
    existing: Mapping[str, Any],
    document: Mapping[str, Any],
) -> bool:
    coverage = existing.get("coverage")
    if not isinstance(coverage, Mapping) or coverage.get("detail") != "complete":
        return False
    return _comparable(existing) == _comparable(document)


def _comparable(document: Mapping[str, Any]) -> str:
    return canonical_json(
        {
            key: value
            for key, value in document.items()
            if key not in _NON_MATERIAL_FIELDS
        }
    )


def _proposal_detail_status(sections: Mapping[str, Mapping[str, Any]]) -> str:
    status = derive_detail_status(
        sections=sections,
        head_changed=False,
        head_unverified=False,
        diff_status="not_requested",
    )
    return status if status in ("complete", "partial") else "partial"


def _fetch_detail(
    context: CollectContext,
    *,
    owner: str,
    name: str,
    number: int,
) -> tuple[dict[str, Any], int, int | None]:
    result = context.client.graphql(
        _DETAIL_ONLY_QUERY,
        variables={"owner": owner, "name": name, "number": number},
        page=1,
    )
    discussion = get_in(result.data, "repository", "discussion")
    if not isinstance(discussion, Mapping):
        if _is_not_found(result.errors):
            raise ProposalNotFound(
                f"Discussion #{number} 不存在或不可访问",
                source="discussion_detail.not_found",
                details={"number": number},
            )
        raise GitHubError(
            "Discussion 查询失败",
            source="discussion_detail.graphql",
            details={"number": number, "errors": result.errors[:3]},
        )
    connection = discussion.get("comments")
    total_known = (
        _optional_count(connection.get("totalCount"))
        if isinstance(connection, Mapping)
        else None
    )
    return dict(discussion), 1, total_known


def _not_requested_section(total_known: int | None = None) -> dict[str, Any]:
    return {
        "status": "not_requested",
        "fetched": 0,
        "total_known": total_known,
        "truncated": False,
        "truncation_reason": None,
        "duplicates": 0,
        "items": [],
    }


def _existing_section(
    existing: Mapping[str, Any] | None,
    name: str,
    total_known: int | None = None,
) -> dict[str, Any]:
    if isinstance(existing, Mapping):
        section = existing.get(name)
        if isinstance(section, Mapping):
            copied = dict(section)
            copied.setdefault("duplicates", 0)
            return copied
    return _not_requested_section(total_known)


def _fetch_comments(
    context: CollectContext,
    *,
    owner: str,
    name: str,
    number: int,
) -> tuple[
    dict[str, Any],
    int,
    list[_Comment],
    int | None,
    bool,
    bool,
    str | None,
    int,
]:
    cursor: str | None = None
    page = 1
    pages = 0
    detail: dict[str, Any] = {}
    comments: list[_Comment] = []
    total_known: int | None = None
    truncated = False
    errors = False
    reason: str | None = None
    duplicates = 0
    seen_comments: set[str] = set()
    while True:
        variables: dict[str, Any] = {
            "owner": owner,
            "name": name,
            "number": number,
        }
        if cursor is not None:
            variables["cursor"] = cursor
        result = context.client.graphql(_DETAIL_QUERY, variables=variables, page=page)
        pages += 1
        if result.errors:
            errors = True
        discussion = get_in(result.data, "repository", "discussion")
        if not isinstance(discussion, Mapping):
            if not detail:
                if _is_not_found(result.errors):
                    raise ProposalNotFound(
                        f"Discussion #{number} 不存在或不可访问",
                        source="discussion_detail.not_found",
                        details={"number": number},
                    )
                raise GitHubError(
                    "Discussion 查询失败",
                    source="discussion_detail.graphql",
                    details={"number": number, "errors": result.errors[:3]},
                )
            truncated = True
            reason = "graphql_errors"
            break
        if not detail:
            detail = dict(discussion)
        connection = discussion.get("comments")
        if not isinstance(connection, Mapping):
            truncated = True
            reason = "comments_connection_missing"
            break
        if total_known is None:
            total_known = _optional_count(connection.get("totalCount"))
        nodes = connection.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, Mapping):
                    errors = True
                    continue
                parsed = _comment_item(context.store, node)
                if parsed is None:
                    errors = True
                    continue
                identifier = str(parsed.item["id"])
                if identifier in seen_comments:
                    duplicates += 1
                    continue
                seen_comments.add(identifier)
                duplicates += parsed.duplicates
                comments.append(parsed)
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, Mapping) or not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or not cursor:
            truncated = True
            reason = "pagination_incomplete"
            break
        if page >= MAX_COMMENT_PAGES:
            truncated = True
            reason = "comments_page_limit"
            break
        page += 1
    if total_known is not None and len(comments) < total_known:
        truncated = True
        reason = reason or "comments_total_mismatch"
    return (
        detail,
        pages,
        comments,
        total_known,
        truncated,
        errors,
        reason,
        duplicates,
    )


@dataclass
class _ReplyBatch:
    comments: list[_Comment]
    pages: int
    reasons: list[str]
    duplicates: int = 0


def _fetch_reply_continuations(
    context: CollectContext,
    comments: list[_Comment],
) -> _ReplyBatch:
    pages = 0
    used = 0
    duplicates = 0
    reasons: list[str] = []
    for comment in comments:
        replies_conn = comment.replies_page
        if not isinstance(replies_conn, Mapping):
            continue
        page_info = replies_conn.get("pageInfo")
        if not isinstance(page_info, Mapping) or not page_info.get("hasNextPage"):
            continue
        comment_id = comment.item["id"]
        seen_replies = {
            str(reply["id"])
            for reply in comment.item["replies"]
            if isinstance(reply, Mapping) and reply.get("id")
        }
        cursor = page_info.get("endCursor")
        page = 1
        truncated_here = False
        while True:
            if used >= MAX_REPLY_CONTINUATIONS:
                reasons.append(f"replies_continuation_limit:{comment_id}")
                truncated_here = True
                break
            if not isinstance(cursor, str) or not cursor:
                reasons.append(f"replies_pagination_incomplete:{comment_id}")
                truncated_here = True
                break
            result = context.client.graphql(
                _REPLIES_QUERY,
                variables={"commentId": comment_id, "cursor": cursor},
                page=page,
            )
            pages += 1
            used += 1
            connection = get_in(result.data, "node", "replies")
            if isinstance(connection, Mapping):
                for node in connection.get("nodes") or []:
                    if not isinstance(node, Mapping):
                        continue
                    reply = _reply_item(context.store, node)
                    if reply is not None:
                        identifier = str(reply["id"])
                        if identifier in seen_replies:
                            duplicates += 1
                            continue
                        seen_replies.add(identifier)
                        comment.item["replies"].append(reply)
            if result.errors:
                reasons.append(f"replies_graphql_errors:{comment_id}")
                truncated_here = True
                break
            if not isinstance(connection, Mapping):
                reasons.append(f"replies_connection_missing:{comment_id}")
                truncated_here = True
                break
            page_info = connection.get("pageInfo")
            if not isinstance(page_info, Mapping) or not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            page += 1
            if page > MAX_REPLY_PAGES:
                reasons.append(f"replies_page_limit:{comment_id}")
                truncated_here = True
                break
        if truncated_here:
            comment.replies_truncated = True
            comment.item["replies_status"] = "partial"
            comment.item["replies_truncated"] = True
        else:
            comment.replies_truncated = False
            comment.item["replies_status"] = "complete"
            comment.item["replies_truncated"] = False
    return _ReplyBatch(
        comments=comments,
        pages=pages,
        reasons=reasons,
        duplicates=duplicates,
    )


def _comment_item(
    store: SnapshotStore,
    node: Mapping[str, Any],
) -> _Comment | None:
    identifier = as_optional_str(node.get("id"))
    if identifier is None:
        return None
    replies = node.get("replies")
    reply_items: list[dict[str, Any]] = []
    seen_replies: set[str] = set()
    duplicates = 0
    replies_status = "complete"
    replies_truncated = False
    if isinstance(replies, Mapping):
        for reply in replies.get("nodes") or []:
            if isinstance(reply, Mapping):
                parsed = _reply_item(store, reply)
                if parsed is not None:
                    reply_id = str(parsed["id"])
                    if reply_id in seen_replies:
                        duplicates += 1
                        continue
                    seen_replies.add(reply_id)
                    reply_items.append(parsed)
                else:
                    replies_status = "partial"
                    replies_truncated = True
        page_info = replies.get("pageInfo")
        if isinstance(page_info, Mapping) and page_info.get("hasNextPage"):
            replies_truncated = True
    else:
        replies_status = "partial"
        replies_truncated = True
    item = {
        "id": identifier,
        "database_id": as_optional_int(node.get("databaseId")),
        "author": _login(node.get("author")),
        "created_at": iso_or_none(node.get("createdAt")),
        "updated_at": iso_or_none(node.get("updatedAt")),
        "is_answer": bool(node.get("isAnswer")),
        "body": text_ref(store, node.get("body")),
        "replies_status": replies_status,
        "replies_truncated": replies_truncated,
        "replies": reply_items,
    }
    comment = _Comment(
        item=item,
        replies_truncated=replies_truncated,
        replies_page=replies if isinstance(replies, Mapping) else None,
        duplicates=duplicates,
    )
    if replies_truncated:
        item["replies_status"] = "partial"
    return comment


def _reply_item(
    store: SnapshotStore,
    node: Mapping[str, Any],
) -> dict[str, Any] | None:
    identifier = as_optional_str(node.get("id"))
    if identifier is None:
        return None
    return {
        "id": identifier,
        "database_id": as_optional_int(node.get("databaseId")),
        "author": _login(node.get("author")),
        "created_at": iso_or_none(node.get("createdAt")),
        "updated_at": iso_or_none(node.get("updatedAt")),
        "body": text_ref(store, node.get("body")),
    }


def _is_not_found(errors: list[dict[str, Any]]) -> bool:
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        if str(error.get("type") or "").upper() == "NOT_FOUND":
            return True
        message = str(error.get("message") or "").casefold()
        if "not found" in message or "could not resolve" in message:
            return True
    return False


def _login(value: Any) -> str | None:
    return as_optional_str(value.get("login")) if isinstance(value, Mapping) else None


def _category(detail: Mapping[str, Any]) -> str | None:
    value = get_in(detail, "category", "name")
    return as_optional_str(value)


def _state_reason(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized if normalized in _STATE_REASONS else None


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _optional_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


__all__ = [
    "DiscussionRead",
    "MAX_COMMENT_PAGES",
    "MAX_REPLY_CONTINUATIONS",
    "MAX_REPLY_PAGES",
    "ProposalNotFound",
    "read_discussion",
]
