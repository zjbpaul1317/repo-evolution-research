"""Discover recent pull request, issue and discussion activity."""

from __future__ import annotations

import math
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .. import SCHEMA_VERSION
from ..errors import (
    BudgetExceededError,
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)
from ..github import GitHubClient, parse_link_header
from ..models import entity_id, make_entity, to_iso_z, validate_document
from ..store import SnapshotStore
from .base import is_run_budget_error
from .entity_store import merge_entity_stream, merge_raw_state
from .progress import CollectProgress, coverage_entry
from .status import combine_status
from .values import (
    comment_count,
    iso_or_none,
    now_iso,
    parse_datetime,
    parse_datetime_strict,
    reaction_count,
    user_login,
    window_bounds,
)
from .values import (
    labels as label_names,
)
from .values import (
    merged_at as raw_merged_at,
)

SEARCH_RESULT_CAP = 1000
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES = 10
ACTIVITY_WINDOW_TOLERANCE = timedelta(minutes=10)


@dataclass(frozen=True)
class _ViewSpec:
    name: str
    kind: str
    event_field: str
    search_filter: str
    list_state: str
    list_sort: str
    list_since: bool = False


_VIEWS = (
    _ViewSpec(
        "pr_merged",
        "pull_request",
        "merged_at",
        "is:pr is:merged",
        "closed",
        "updated",
    ),
    _ViewSpec("pr_created", "pull_request", "created_at", "is:pr", "all", "created"),
    _ViewSpec("pr_updated", "pull_request", "updated_at", "is:pr", "all", "updated"),
    _ViewSpec("issue_created", "issue", "created_at", "is:issue", "all", "created"),
    _ViewSpec(
        "issue_updated",
        "issue",
        "updated_at",
        "is:issue",
        "all",
        "updated",
        list_since=True,
    ),
    _ViewSpec(
        "discussion_updated",
        "discussion",
        "updated_at",
        "",
        "",
        "",
    ),
)

_ACTIVITY_CHANNELS = {
    "activity_prs": ("pull_request",),
    "activity_issues": ("issue",),
    "activity_discussions": ("discussion",),
}

_DISCUSSION_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    discussions(
      first: 100
      after: $cursor
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        url
        createdAt
        updatedAt
        closed
        closedAt
        category { name }
        author { login }
        comments { totalCount }
        upvoteCount
      }
    }
  }
}
""".strip()


class _InvalidItem(ValueError):
    pass


class _InvalidTimestamp(ValueError):
    pass


class _NeedFallback(Exception):
    def __init__(self, reason: str, *, incomplete: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.incomplete = incomplete


@dataclass(frozen=True)
class _DateShard:
    start: date
    end: date

    def split(self) -> tuple[_DateShard, _DateShard] | None:
        if self.start >= self.end:
            return None
        span = (self.end - self.start).days
        middle = self.start + timedelta(days=span // 2)
        return (
            _DateShard(self.start, middle),
            _DateShard(middle + timedelta(days=1), self.end),
        )


@dataclass
class _Candidate:
    kind: str
    number: int
    raw: dict[str, Any]
    reasons: set[str] = field(default_factory=set)
    event_times: list[str] = field(default_factory=list)


class _CandidateSink:
    def __init__(self, limit: int, view_names: list[str] | None = None) -> None:
        self.limit = max(1, limit)
        self.items: dict[tuple[str, int], _Candidate] = {}
        self.limit_reached = False
        names = list(view_names or [])
        self.base_quota = self.limit // len(names) if names else self.limit
        self.pool = self.limit - self.base_quota * len(names) if names else 0
        self.current_view: str | None = None
        self.view_available = 0
        self.view_used = 0

    def begin_view(self, name: str) -> None:
        self.current_view = name
        self.view_available = self.base_quota + self.pool
        self.pool = 0
        self.view_used = 0

    def end_view(self) -> None:
        if self.current_view is None:
            return
        self.pool += max(0, self.view_available - self.view_used)
        self.current_view = None

    def add(self, candidate: _Candidate) -> str:
        key = (candidate.kind, candidate.number)
        existing = self.items.get(key)
        if existing is not None:
            _merge_candidate(existing, candidate)
            return "merged"
        if len(self.items) >= self.limit:
            self.limit_reached = True
            return "limit"
        if self.current_view is not None and self.view_used >= self.view_available:
            return "view_limit"
        self.items[key] = candidate
        self.view_used += 1
        return "added"


@dataclass
class _ViewResult:
    spec: _ViewSpec
    query: str
    status: str = "complete"
    strategy: str = "search"
    total_count: int | None = None
    fetched_count: int = 0
    pages: int = 0
    shards: int = 0
    incomplete: bool = False
    fallback: bool = False
    fallback_reason: str | None = None
    remaining_cursor: str | None = None
    observed_start: str = ""
    observed_end: str | None = None
    invalid_timestamps: int = 0
    invalid_items: int = 0
    reasons: list[str] = field(default_factory=list)
    keys: set[tuple[str, int]] = field(default_factory=set)
    shard_details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def unique_count(self) -> int:
        return len(self.keys)


@dataclass
class ActivityCollection:
    coverage: list[dict[str, Any]]
    overview: dict[str, Any]
    counts: dict[str, int]
    candidate_count: int


def collect_activity(
    client: GitHubClient,
    store: SnapshotStore,
    repository: Mapping[str, Any],
    *,
    activity_window: Mapping[str, Any],
    source_profile: Mapping[str, Any] | None = None,
    include_discussions: bool = True,
    progress: CollectProgress | None = None,
    clock: Callable[[], str] | None = None,
) -> ActivityCollection:
    owner = str(repository["owner"])
    name = str(repository["name"])
    host = str(repository.get("host") or "github.com")
    start, end = _parse_window(activity_window)
    start_date = start.date()
    end_date = (end - timedelta(microseconds=1)).date()
    now = clock or now_iso
    observed_at = now()

    sink = _CandidateSink(
        int(client.budget.get("max_discovery_entities", 6000)),
        [spec.name for spec in _VIEWS],
    )
    views: list[_ViewResult] = []

    try:
        for spec in _VIEWS:
            view = _ViewResult(
                spec=spec,
                query=_view_query(spec, owner, name, start_date, end_date),
                strategy="graphql" if spec.kind == "discussion" else "search",
                observed_start=now(),
            )
            views.append(view)
            failure: BaseException | None = None
            if sink.limit_reached:
                _mark_partial(view, "discovery_entity_limit_unstarted")
                view.observed_end = now()
                continue
            sink.begin_view(spec.name)
            try:
                _collect_view(
                    view,
                    sink,
                    client,
                    owner,
                    name,
                    start,
                    end,
                    source_profile,
                    include_discussions,
                )
            except BudgetExceededError as exc:
                failure = exc
                if progress is not None and is_run_budget_error(exc):
                    progress.budget_exhausted = True
                reason = (
                    "pagination_max_pages"
                    if exc.source == "github.pagination"
                    else "budget_exceeded"
                )
                _mark_partial(view, reason)
                _mark_last_shard(view, "partial")
                raise
            except GitHubAuthError as exc:
                failure = exc
                view.status = "unavailable"
                _append_reason(view, "github_auth")
                raise
            except BaseException as exc:
                failure = exc
                raise
            finally:
                sink.end_view()
                view.observed_end = now()
                try:
                    _persist_view(
                        store,
                        sink,
                        view,
                        host,
                        owner,
                        name,
                        observed_at,
                        start,
                        end,
                    )
                except Exception as persist_error:
                    if failure is None:
                        raise
                    print(
                        f"warning: 无法持久化活动数据: {persist_error}",
                        file=sys.stderr,
                    )
    except Exception:
        try:
            _finalize(
                store,
                sink,
                views,
                repository,
                activity_window,
                observed_at,
                progress,
            )
        except Exception as finalize_error:
            print(
                f"warning: 无法写入活动概览: {finalize_error}",
                file=sys.stderr,
            )
        raise

    return _finalize(
        store,
        sink,
        views,
        repository,
        activity_window,
        observed_at,
        progress,
    )


def _collect_view(
    view: _ViewResult,
    sink: _CandidateSink,
    client: GitHubClient,
    owner: str,
    name: str,
    start: datetime,
    end: datetime,
    source_profile: Mapping[str, Any] | None,
    include_discussions: bool,
) -> None:
    if view.spec.kind == "discussion":
        try:
            _collect_discussions(
                view,
                sink,
                client,
                owner,
                name,
                start,
                end,
                source_profile,
                include_discussions,
            )
        except BudgetExceededError:
            raise
        except GitHubAuthError:
            raise
        except GitHubError as error:
            _mark_partial(view, error.code)
            _mark_last_shard(view, "partial")
    else:
        try:
            _run_search(view, sink, client, owner, name, start, end)
        except _NeedFallback as exc:
            _mark_last_shard(view, "fallback")
            view.fallback = True
            view.strategy = "mixed"
            view.fallback_reason = exc.reason
            view.incomplete = view.incomplete or exc.incomplete
            view.total_count = None
            try:
                _run_list_fallback(view, sink, client, owner, name, start, end)
            except BudgetExceededError:
                raise
            except GitHubAuthError:
                raise
            except GitHubError as error:
                _mark_partial(view, error.code)
        except (GitHubRateLimitError, GitHubUnavailableError) as error:
            _mark_partial(view, error.code)
            _mark_last_shard(view, "partial")
        except BudgetExceededError:
            raise
        except GitHubAuthError:
            raise
        except GitHubError as error:
            _mark_last_shard(view, "fallback")
            view.fallback = True
            view.strategy = "mixed"
            view.fallback_reason = error.code
            view.total_count = None
            try:
                _run_list_fallback(view, sink, client, owner, name, start, end)
            except BudgetExceededError:
                raise
            except GitHubAuthError:
                raise
            except GitHubError as fallback_error:
                _mark_partial(view, fallback_error.code)

    if view.invalid_timestamps:
        _mark_partial(view, "invalid_timestamp")
        _mark_last_shard(view, "partial")
    if view.invalid_items:
        _mark_partial(view, "invalid_item")
        _mark_last_shard(view, "partial")


def _run_search(
    view: _ViewResult,
    sink: _CandidateSink,
    client: GitHubClient,
    owner: str,
    name: str,
    start: datetime,
    end: datetime,
) -> None:
    pending = [_DateShard(start.date(), (end - timedelta(microseconds=1)).date())]
    leaf_totals: list[int] = []
    while pending:
        shard = pending.pop(0)
        query = _search_query(view.spec, owner, name, shard)
        sort = "created" if view.spec.event_field == "created_at" else "updated"
        iterator = client.iter_rest_pages(
            "search/issues",
            params={
                "q": query,
                "per_page": SEARCH_PAGE_SIZE,
                "sort": sort,
                "order": "desc",
            },
        )
        try:
            page = next(iterator)
        except StopIteration as exc:
            raise _NeedFallback("search_empty_response") from exc
        view.shards += 1
        view.pages += 1
        data = page.data if isinstance(page.data, dict) else {}
        total = data.get("total_count")
        if isinstance(total, bool) or not isinstance(total, int):
            raise _NeedFallback("search_total_missing")
        incomplete = bool(data.get("incomplete_results"))
        view.incomplete = view.incomplete or incomplete
        detail = {
            "strategy": "search",
            "start": shard.start.isoformat(),
            "end": shard.end.isoformat(),
            "total_count": total,
            "incomplete": incomplete,
            "pages": 1,
            "fetched_count": 0,
            "status": "complete",
        }
        view.shard_details.append(detail)
        if total >= SEARCH_RESULT_CAP or incomplete:
            split = shard.split()
            if split is not None:
                detail["status"] = "split"
                pending = [*split, *pending]
                continue
            detail["status"] = "fallback"
            reason = "search_cap" if total >= SEARCH_RESULT_CAP else "search_incomplete"
            raise _NeedFallback(reason, incomplete=incomplete)

        leaf_totals.append(total)
        page_number = 1
        while True:
            data = page.data if isinstance(page.data, dict) else {}
            items = data.get("items")
            if not isinstance(items, list):
                detail["status"] = "fallback"
                raise _NeedFallback("search_items_missing")
            detail["fetched_count"] += len(items)
            for raw in items:
                if not isinstance(raw, dict):
                    view.invalid_items += 1
                    continue
                if not _accept_item(view, sink, raw, view.spec, start, end):
                    detail["status"] = "partial"
                    view.remaining_cursor = f"rescan-page:{page.page}"
                    view.total_count = None
                    return
            if data.get("incomplete_results"):
                view.incomplete = True
                detail["status"] = "fallback"
                raise _NeedFallback("search_incomplete", incomplete=True)
            next_url = parse_link_header(page.headers.get("link")).get("next")
            if not next_url:
                break
            if page_number >= SEARCH_MAX_PAGES:
                detail["status"] = "fallback"
                raise _NeedFallback("search_cap")
            try:
                page = next(iterator)
            except StopIteration as exc:
                detail["status"] = "fallback"
                raise _NeedFallback("pagination_incomplete") from exc
            page_number += 1
            view.pages += 1
            detail["pages"] += 1

        expected_pages = max(1, math.ceil(total / SEARCH_PAGE_SIZE))
        if page_number < expected_pages:
            detail["status"] = "fallback"
            raise _NeedFallback("pagination_incomplete")

    view.total_count = sum(leaf_totals)
    view.strategy = "search"


def _run_list_fallback(
    view: _ViewResult,
    sink: _CandidateSink,
    client: GitHubClient,
    owner: str,
    name: str,
    start: datetime,
    end: datetime,
) -> None:
    spec = view.spec
    endpoint = f"repos/{owner}/{name}/" + ("pulls" if spec.kind == "pull_request" else "issues")
    params: dict[str, Any] = {
        "state": spec.list_state,
        "sort": spec.list_sort,
        "direction": "desc",
        "per_page": SEARCH_PAGE_SIZE,
    }
    if spec.list_since:
        params["since"] = to_iso_z(start)

    detail = {
        "strategy": "list_fallback",
        "start": start.date().isoformat(),
        "end": (end - timedelta(microseconds=1)).date().isoformat(),
        "total_count": None,
        "incomplete": view.incomplete,
        "pages": 0,
        "fetched_count": 0,
        "status": "complete",
    }
    view.shard_details.append(detail)
    view.shards += 1
    try:
        for page in client.iter_rest_pages(endpoint, params=params):
            view.pages += 1
            detail["pages"] += 1
            items = page.data
            if not isinstance(items, list):
                detail["status"] = "partial"
                _mark_partial(view, "list_response_invalid")
                return
            detail["fetched_count"] += len(items)
            stopped = False
            for raw in items:
                if not isinstance(raw, dict):
                    view.invalid_items += 1
                    continue
                if spec.kind == "issue" and "pull_request" in raw:
                    continue
                if _list_stop(raw, spec, start):
                    stopped = True
                    break
                if spec.name == "pr_merged" and raw_merged_at(raw) is None:
                    continue
                if not _accept_item(view, sink, raw, spec, start, end):
                    detail["status"] = "partial"
                    view.remaining_cursor = f"rescan-page:{page.page}"
                    return
            if stopped:
                break
    except (BudgetExceededError, GitHubError):
        detail["status"] = "partial"
        raise


def _collect_discussions(
    view: _ViewResult,
    sink: _CandidateSink,
    client: GitHubClient,
    owner: str,
    name: str,
    start: datetime,
    end: datetime,
    source_profile: Mapping[str, Any] | None,
    include_discussions: bool,
) -> None:
    view.strategy = "graphql"
    if not include_discussions:
        view.status = "not_requested"
        return
    capability = _discussion_capability(source_profile)
    if capability.get("enabled") is False:
        view.status = "not_applicable"
        return
    capability_status = capability.get("status")
    if capability_status == "unavailable":
        view.status = "unavailable"
        _append_reason(view, "discussions_capability_unavailable")
        return
    if capability_status == "partial":
        view.status = "partial"
        _append_reason(view, "discussions_capability_partial")
        return

    detail = {
        "strategy": "graphql",
        "start": start.date().isoformat(),
        "end": (end - timedelta(microseconds=1)).date().isoformat(),
        "total_count": None,
        "incomplete": False,
        "pages": 0,
        "fetched_count": 0,
        "status": "complete",
    }
    view.shard_details.append(detail)
    view.shards = 1
    for page in client.iter_graphql_pages(
        _DISCUSSION_QUERY,
        variables={"owner": owner, "name": name},
        connection_path=("repository", "discussions"),
    ):
        view.pages += 1
        detail["pages"] += 1
        detail["fetched_count"] += len(page.nodes)
        if page.errors:
            view.incomplete = True
            detail["incomplete"] = True
            detail["status"] = "partial"
            _mark_partial(view, "graphql_errors")
            if not page.nodes:
                break
        stopped = False
        for raw in page.nodes:
            if not isinstance(raw, dict):
                view.invalid_items += 1
                continue
            if _list_stop(raw, view.spec, start):
                stopped = True
                break
            if not _accept_item(view, sink, raw, view.spec, start, end):
                detail["status"] = "partial"
                view.remaining_cursor = f"rescan-page:{page.page}"
                return
        if stopped or not page.page_info.get("hasNextPage"):
            break
        if not page.page_info.get("endCursor"):
            detail["status"] = "partial"
            _mark_partial(view, "pagination_incomplete")
            break


def _accept_item(
    view: _ViewResult,
    sink: _CandidateSink,
    raw: dict[str, Any],
    spec: _ViewSpec,
    start: datetime,
    end: datetime,
) -> bool:
    view.fetched_count += 1
    try:
        candidate = _candidate_from_raw(raw, spec, start, end)
    except _InvalidTimestamp:
        view.invalid_timestamps += 1
        return True
    except _InvalidItem:
        view.invalid_items += 1
        return True
    if candidate is None:
        return True
    outcome = sink.add(candidate)
    if outcome == "limit":
        _mark_partial(view, "discovery_entity_limit")
        return False
    if outcome == "view_limit":
        _mark_partial(view, "discovery_view_quota")
        return False
    view.keys.add((candidate.kind, candidate.number))
    return True


def _candidate_from_raw(
    raw: Mapping[str, Any],
    spec: _ViewSpec,
    start: datetime,
    end: datetime,
) -> _Candidate | None:
    if spec.kind == "issue" and "pull_request" in raw:
        return None
    number = raw.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise _InvalidItem("invalid number")
    moment = _event_moment(raw, spec)
    if moment is None:
        raise _InvalidTimestamp("missing timestamp")
    if not (start <= moment < end):
        return None
    candidate = _Candidate(
        kind=spec.kind,
        number=number,
        raw=dict(raw),
        reasons={spec.name},
        event_times=[to_iso_z(moment)],
    )
    return candidate


def _event_moment(raw: Mapping[str, Any], spec: _ViewSpec) -> datetime | None:
    if spec.name == "pr_merged":
        value = raw_merged_at(raw)
    elif spec.event_field == "created_at":
        value = raw.get("created_at") or raw.get("createdAt")
    elif spec.event_field == "updated_at":
        value = raw.get("updated_at") or raw.get("updatedAt")
    else:
        value = raw.get(spec.event_field)
    if not isinstance(value, str) or not value:
        return None
    return _parse_timestamp(value)


def _list_stop(raw: Mapping[str, Any], spec: _ViewSpec, start: datetime) -> bool:
    if spec.event_field == "created_at":
        value = raw.get("created_at") or raw.get("createdAt")
    else:
        value = raw.get("updated_at") or raw.get("updatedAt")
    if not isinstance(value, str) or not value:
        return False
    try:
        moment = _parse_timestamp(value)
    except _InvalidTimestamp:
        return False
    return moment < start


def _merge_candidate(existing: _Candidate, incoming: _Candidate) -> None:
    existing.reasons.update(incoming.reasons)
    for moment in incoming.event_times:
        if moment not in existing.event_times:
            existing.event_times.append(moment)
    existing.raw = merge_raw_state(existing.raw, incoming.raw)


def _persist_view(
    store: SnapshotStore,
    sink: _CandidateSink,
    view: _ViewResult,
    host: str,
    owner: str,
    name: str,
    observed_at: str,
    window_start: datetime,
    window_end: datetime,
) -> None:
    if not view.keys:
        return
    records: dict[str, list[dict[str, Any]]] = {}
    entities: list[dict[str, Any]] = []
    for key in sorted(view.keys):
        candidate = sink.items.get(key)
        if candidate is None:
            continue
        records.setdefault(candidate.kind, []).append(candidate.raw)
        entities.append(
            _entity_from_candidate(
                candidate,
                host,
                owner,
                name,
                observed_at,
                window_start,
                window_end,
            )
        )
    stream_names = (
        ("pull_request", "prs"),
        ("issue", "issues"),
        ("discussion", "discussions"),
    )
    for kind, stream in stream_names:
        if kind in records:
            path = f"raw/{stream}.jsonl"
            incoming = sorted(records[kind], key=lambda item: int(item["number"]))
            store.upsert_jsonl(
                path,
                _merge_raw_stream(store, path, incoming),
                key="number",
            )
    if entities:
        store.upsert_jsonl(
            "normalized/entities.jsonl",
            merge_entity_stream(store, entities),
            key="id",
            schema_name="entity",
        )


def _merge_raw_stream(
    store: SnapshotStore,
    path: str,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}
    for record in store.read_jsonl(path):
        number = record.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            merged[number] = record
    for record in incoming:
        number = record.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            if number in merged:
                merged[number] = merge_raw_state(merged[number], record)
            else:
                merged[number] = record
    return sorted(merged.values(), key=lambda item: int(item["number"]))


def _entity_from_candidate(
    candidate: _Candidate,
    host: str,
    owner: str,
    name: str,
    observed_at: str,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    raw = candidate.raw
    number = candidate.number
    merged_moment = iso_or_none(raw_merged_at(raw))
    created_at = iso_or_none(raw.get("created_at") or raw.get("createdAt"))
    updated_at = iso_or_none(raw.get("updated_at") or raw.get("updatedAt"))
    closed_at = iso_or_none(raw.get("closed_at") or raw.get("closedAt"))
    label_list = label_names(raw)
    comment_total = comment_count(raw)
    reaction_total = reaction_count(raw)
    author_login = user_login(raw)
    merged = merged_moment is not None
    state = _state(raw, merged)
    event_times = sorted(candidate.event_times)
    source_event_at = event_times[-1] if event_times else (updated_at or created_at)
    url = raw.get("html_url") or raw.get("url")
    if not isinstance(url, str) or not url:
        suffix = {"pull_request": "pull", "issue": "issues", "discussion": "discussions"}[
            candidate.kind
        ]
        url = f"https://github.com/{owner}/{name}/{suffix}/{number}"
    title = raw.get("title")
    if not isinstance(title, str) or not title:
        title = f"#{number}"
    node_id = raw.get("node_id")
    if not isinstance(node_id, str):
        identifier = raw.get("id")
        node_id = identifier if isinstance(identifier, str) else None
    reasons = sorted(candidate.reasons)
    normalized = {
        "state": state,
        "merged": merged,
        "draft": bool(raw.get("draft")),
        "created_in_window": _timestamp_in_window(created_at, window_start, window_end),
        "merged_in_window": _timestamp_in_window(merged_moment, window_start, window_end),
        "updated_in_window": _timestamp_in_window(updated_at, window_start, window_end),
        "merged_at": merged_moment,
        "labels": label_list,
        "comments": comment_total,
        "reactions": reaction_total,
    }
    entity = make_entity(
        entity_id=entity_id(host, owner, name, candidate.kind, str(number)),
        kind=candidate.kind,
        title=title,
        url=url,
        observed_at=observed_at,
        raw_state={
            "state": state,
            "merged_at": merged_moment,
            "draft": bool(raw.get("draft")),
            "closed_at": closed_at,
            "labels": label_list,
            "comments": comment_total,
            "reactions": reaction_total,
            "author": author_login,
            "author_association": raw.get("author_association"),
        },
        normalized_state=normalized,
        number=number,
        node_id=node_id,
        created_at=created_at,
        updated_at=updated_at,
        source_event_at=source_event_at,
        discovery_reasons=reasons,
        external=False,
    )
    validate_document(entity, "entity")
    return entity


def _finalize(
    store: SnapshotStore,
    sink: _CandidateSink,
    views: list[_ViewResult],
    repository: Mapping[str, Any],
    activity_window: Mapping[str, Any],
    observed_at: str,
    progress: CollectProgress | None,
) -> ActivityCollection:
    counts = {view.spec.name: view.unique_count for view in views}
    overview = {
        "schema_version": SCHEMA_VERSION,
        "activity_window": {
            "start": activity_window["start"],
            "end": activity_window["end"],
            "timezone": activity_window["timezone"],
        },
        "observed_at": observed_at,
        "repository": {
            "host": str(repository.get("host") or "github.com"),
            "owner": str(repository["owner"]),
            "name": str(repository["name"]),
        },
        "views": [
            _view_document(
                view,
                str(repository.get("host") or "github.com"),
                str(repository["owner"]),
                str(repository["name"]),
            )
            for view in views
        ],
        "counts": counts,
        "candidate_count": len(sink.items),
        "limitations": _limitations(
            views,
            historical=_is_historical(activity_window, observed_at),
        ),
    }
    store.write_json(
        "analysis/activity-overview.json",
        overview,
        schema_name="activity_overview",
    )
    coverage = _coverage_entries(views, observed_at)
    if progress is not None:
        cursors = {
            view.spec.name: view.remaining_cursor
            for view in views
            if view.remaining_cursor
        }
        if cursors:
            progress.set_cursor("activity", cursors)
        for entry in coverage:
            progress.record(entry)
    return ActivityCollection(
        coverage=coverage,
        overview=overview,
        counts=counts,
        candidate_count=len(sink.items),
    )


def _view_document(
    view: _ViewResult,
    host: str,
    owner: str,
    name: str,
) -> dict[str, Any]:
    ids = sorted(
        entity_id(host, owner, name, kind, str(number))
        for kind, number in view.keys
    )
    return {
        "view": view.spec.name,
        "kind": view.spec.kind,
        "strategy": view.strategy,
        "status": view.status,
        "query": view.query,
        "total_count": view.total_count,
        "fetched_count": view.fetched_count,
        "unique_count": view.unique_count,
        "pages": view.pages,
        "shards": view.shards,
        "incomplete": view.incomplete,
        "fallback": view.fallback,
        "truncation_reason": _truncation_reason(view),
        "remaining_cursor": view.remaining_cursor,
        "observed_start": view.observed_start,
        "observed_end": view.observed_end or view.observed_start,
        "candidate_ids": ids,
        "shards_detail": view.shard_details,
    }


def _coverage_entries(
    views: list[_ViewResult],
    observed_at: str,
) -> list[dict[str, Any]]:
    entries = []
    for channel, kinds in _ACTIVITY_CHANNELS.items():
        group = [view for view in views if view.spec.kind in kinds]
        if not group:
            continue
        status = combine_status(view.status for view in group)
        keys: set[tuple[str, int]] = set()
        for view in group:
            keys.update(view.keys)
        reasons = [reason for view in group for reason in view.reasons]
        expected = group[0].total_count if len(group) == 1 else None
        observed_start = min((view.observed_start for view in group), default=observed_at)
        observed_end = max(
            (view.observed_end or observed_at for view in group),
            default=observed_at,
        )
        entry = coverage_entry(
            channel=channel,
            query_scope="activity_window",
            query="; ".join(view.query for view in group),
            expected=expected,
            fetched=len(keys),
            pages=sum(view.pages for view in group),
            exhausted=status == "complete",
            truncation_reason=(
                None
                if status in ("complete", "not_applicable", "not_requested")
                else ("; ".join(dict.fromkeys(reasons)) or status)
            ),
            remaining_cursor=next(
                (view.remaining_cursor for view in group if view.remaining_cursor),
                None,
            ),
            coverage_status=status,
            observed_start=observed_start,
            observed_end=observed_end,
        )
        entries.append(entry)
    return entries


def _limitations(views: list[_ViewResult], *, historical: bool) -> list[str]:
    limitations: set[str] = {"observed_current_state"}
    if historical and any(
        view.spec.name in ("pr_updated", "issue_updated", "discussion_updated")
        for view in views
    ):
        limitations.add("historical_updated_window")
    if any(view.strategy in ("search", "mixed") for view in views):
        limitations.add("search_index_visibility")
    if any(view.shards > 1 for view in views):
        limitations.add("search_sharded")
    if any(view.fallback for view in views):
        limitations.add("list_fallback_used")
    if any(view.incomplete and view.status == "complete" for view in views):
        limitations.add("search_incomplete_recovered")
    if any(view.invalid_timestamps for view in views):
        limitations.add("invalid_timestamps")
    if any("discovery_entity_limit" in view.reasons for view in views):
        limitations.add("discovery_entity_limit")
    if any("discovery_entity_limit_unstarted" in view.reasons for view in views):
        limitations.add("discovery_entity_limit_unstarted")
    if any("discovery_view_quota" in view.reasons for view in views):
        limitations.add("discovery_view_quota")
    if any("pagination_max_pages" in view.reasons for view in views):
        limitations.add("pagination_max_pages")
    if any(view.status == "partial" for view in views):
        limitations.add("partial_coverage")
    if any(view.status == "unavailable" for view in views):
        limitations.add("unavailable_coverage")
    return sorted(limitations)


def _is_historical(activity_window: Mapping[str, Any], observed_at: str) -> bool:
    try:
        end = _parse_timestamp(str(activity_window["end"]))
        observed = _parse_timestamp(observed_at)
    except (KeyError, _InvalidTimestamp):
        return False
    return end < observed - ACTIVITY_WINDOW_TOLERANCE


def _truncation_reason(view: _ViewResult) -> str | None:
    if view.status in ("complete", "not_applicable", "not_requested"):
        return None
    return "; ".join(dict.fromkeys(view.reasons)) or view.status


def _mark_last_shard(view: _ViewResult, status: str) -> None:
    if view.shard_details:
        view.shard_details[-1]["status"] = status


def _mark_partial(view: _ViewResult, reason: str) -> None:
    if view.status in ("not_requested", "not_applicable"):
        return
    view.status = "partial"
    _append_reason(view, reason)


def _append_reason(view: _ViewResult, reason: str) -> None:
    if reason not in view.reasons:
        view.reasons.append(reason)


def _search_query(
    spec: _ViewSpec,
    owner: str,
    name: str,
    shard: _DateShard,
) -> str:
    field = spec.event_field.removesuffix("_at")
    return (
        f"repo:{owner}/{name} {spec.search_filter} "
        f"{field}:{shard.start.isoformat()}..{shard.end.isoformat()}"
    )


def _view_query(
    spec: _ViewSpec,
    owner: str,
    name: str,
    start: date,
    end: date,
) -> str:
    if spec.kind == "discussion":
        return "GraphQL repository.discussions(orderBy: UPDATED_AT desc)"
    field = spec.event_field.removesuffix("_at")
    return (
        f"search/issues q=repo:{owner}/{name} {spec.search_filter} "
        f"{field}:{start.isoformat()}..{end.isoformat()}"
    )


def _parse_window(window: Mapping[str, Any]) -> tuple[datetime, datetime]:
    return window_bounds(window)


def _parse_timestamp(value: str) -> datetime:
    try:
        return parse_datetime_strict(value)
    except ValueError as exc:
        raise _InvalidTimestamp(value) from exc


def _timestamp_in_window(
    value: str | None,
    start: datetime,
    end: datetime,
) -> bool:
    if not isinstance(value, str) or not value:
        return False
    moment = parse_datetime(value)
    if moment is None:
        return False
    return start <= moment < end


def _state(raw: Mapping[str, Any], merged: bool) -> str:
    if merged:
        return "merged"
    state = raw.get("state")
    if isinstance(state, str) and state:
        return state
    if raw.get("closed") is True:
        return "closed"
    return "open"


def _discussion_capability(source_profile: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(source_profile, Mapping):
        return {}
    discovery = source_profile.get("discovery")
    if not isinstance(discovery, Mapping):
        return {}
    discussions = discovery.get("discussions")
    return dict(discussions) if isinstance(discussions, Mapping) else {}
