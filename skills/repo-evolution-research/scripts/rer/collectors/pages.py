"""Shared REST page fetching and section construction."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..errors import GitHubError, GitHubNotFoundError
from ..github import GitHubClient, parse_link_header
from .base import FATAL_ERRORS
from .values import as_optional_int

DEFAULT_PER_PAGE = 100


@dataclass(frozen=True)
class FetchResult:
    items: list[Any]
    status: str
    pages: int
    reason: str | None
    duplicates: int = 0


@dataclass(frozen=True)
class SectionSpec:
    name: str
    endpoint: str
    parse_item: Callable[[Mapping[str, Any]], dict[str, Any]]
    cap: int | None = None
    cap_reason: str | None = None
    total_known_key: str | None = None
    mismatch_reason: str | None = None
    dedupe_key: Callable[[Any], Any] | None = None


def dedupe_by_field(field: str) -> Callable[[Any], Any]:
    def key(item: Any) -> Any:
        if not isinstance(item, Mapping):
            return None
        value = item.get(field)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, str)) and value != "":
            return value
        return None

    return key


def fetch_rest_list(
    client: GitHubClient,
    endpoint: str,
    *,
    params: Mapping[str, Any] | None = None,
    cap: int | None = None,
    cap_reason: str | None = None,
    source: str = "collector.response",
    stop_after: Callable[[Sequence[Any]], bool] | None = None,
    stop_reason: str | None = None,
    dedupe_key: Callable[[Any], Any] | None = None,
) -> FetchResult:
    items: list[Any] = []
    pages = 0
    duplicates = 0
    reason: str | None = None
    seen: set[Any] = set()
    query = {"per_page": DEFAULT_PER_PAGE, **dict(params or {})}
    try:
        for page in client.iter_rest_pages(endpoint, params=query):
            pages += 1
            data = page.data
            if isinstance(data, Mapping):
                data = data.get("items")
            if not isinstance(data, list):
                raise GitHubError(
                    "GitHub 列表响应格式异常",
                    source=source,
                    details={"endpoint": endpoint},
                )
            for item in data:
                if dedupe_key is not None:
                    key = dedupe_key(item)
                    if key is not None:
                        if key in seen:
                            duplicates += 1
                            continue
                        seen.add(key)
                items.append(item)
            if cap is not None and len(items) >= cap:
                overflow = len(items) > cap
                items = items[:cap]
                has_next = bool(parse_link_header(page.headers.get("link")).get("next"))
                if overflow or has_next:
                    reason = cap_reason
                break
            if (
                stop_after is not None
                and data
                and stop_after(data)
            ):
                reason = reason or stop_reason
                break
    except GitHubNotFoundError:
        status = "partial" if items else "unavailable"
        return FetchResult(items, status, pages, "not_found", duplicates)
    except FATAL_ERRORS:
        raise
    except GitHubError:
        status = "partial" if items else "unavailable"
        return FetchResult(items, status, pages, "github_error", duplicates)
    status = "partial" if reason else "complete"
    return FetchResult(items, status, pages, reason, duplicates)


def build_section(
    result: FetchResult,
    items: list[dict[str, Any]],
    *,
    total_known: int | None,
    cap: int | None = None,
    cap_reason: str | None = None,
    mismatch_reason: str | None = None,
) -> dict[str, Any]:
    truncated = False
    reason = result.reason
    if total_known is not None and total_known > len(items):
        truncated = True
        if cap is not None and len(items) >= cap and cap_reason:
            reason = cap_reason
        else:
            reason = reason or mismatch_reason
    elif reason and result.status == "partial":
        truncated = True
    elif reason == "not_found":
        truncated = False
    status = result.status
    if truncated and status == "complete":
        status = "partial"
    if cap_reason is not None and reason == cap_reason and status != "unavailable":
        status = "partial"
    return {
        "status": status,
        "fetched": len(items),
        "total_known": total_known,
        "truncated": truncated,
        "truncation_reason": reason if truncated else (reason if status == "unavailable" else None),
        "duplicates": result.duplicates,
        "items": items,
    }


def fetch_section(
    client: GitHubClient,
    spec: SectionSpec,
    *,
    owner: str,
    name: str,
    number: int,
    parent: Mapping[str, Any] | None = None,
    stop_after: Callable[[Sequence[Any]], bool] | None = None,
    stop_reason: str | None = None,
    dedupe_key: Callable[[Any], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    section, result = fetch_section_result(
        client,
        spec,
        owner=owner,
        name=name,
        number=number,
        parent=parent,
        stop_after=stop_after,
        stop_reason=stop_reason,
        dedupe_key=dedupe_key,
    )
    return section, result.pages


def fetch_section_result(
    client: GitHubClient,
    spec: SectionSpec,
    *,
    owner: str,
    name: str,
    number: int,
    parent: Mapping[str, Any] | None = None,
    stop_after: Callable[[Sequence[Any]], bool] | None = None,
    stop_reason: str | None = None,
    dedupe_key: Callable[[Any], Any] | None = None,
) -> tuple[dict[str, Any], FetchResult]:
    endpoint = spec.endpoint.format(owner=owner, name=name, number=number)
    resolved_key = dedupe_key if dedupe_key is not None else spec.dedupe_key
    result = fetch_rest_list(
        client,
        endpoint,
        cap=spec.cap,
        cap_reason=spec.cap_reason,
        source=f"{spec.name}.response",
        stop_after=stop_after,
        stop_reason=stop_reason,
        dedupe_key=resolved_key,
    )
    items = [
        spec.parse_item(item)
        for item in result.items
        if isinstance(item, Mapping)
    ]
    total_known = (
        as_optional_int(parent.get(spec.total_known_key))
        if parent is not None and spec.total_known_key is not None
        else None
    )
    section = build_section(
        result,
        items,
        total_known=total_known,
        cap=spec.cap,
        cap_reason=spec.cap_reason,
        mismatch_reason=spec.mismatch_reason,
    )
    return section, result


__all__ = [
    "DEFAULT_PER_PAGE",
    "FetchResult",
    "SectionSpec",
    "build_section",
    "dedupe_by_field",
    "fetch_rest_list",
    "fetch_section",
    "fetch_section_result",
]
