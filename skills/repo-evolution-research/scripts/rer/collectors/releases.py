"""Collect authoritative release snapshots and resolve release tag commits."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from .. import SCHEMA_VERSION
from ..errors import (
    BudgetExceededError,
    GitHubError,
    GitHubNotFoundError,
    InternalError,
    StoreError,
    ValidationFailure,
)
from ..github import GitHubClient
from ..models import (
    FULL_SHA_RE,
    canonical_json,
    entity_id,
    make_entity,
    validate_document,
)
from ..store import SnapshotStore
from .base import (
    FATAL_ERRORS,
    CollectContext,
    error_code,
    is_run_budget_error,
    mark_run_budget_exhausted,
)
from .entity_store import merge_entity
from .pages import SectionSpec, fetch_section_result
from .progress import coverage_entry
from .texts import text_ref
from .values import (
    as_optional_str,
    iso_or_none,
    parse_datetime,
    user_login,
    window_bounds,
)

RELEASE_DIR = "raw/releases"
MAX_RELEASES = 200
MAX_RELEASE_REQUESTS = 80
_LOCAL_ERRORS = (StoreError, ValidationFailure, InternalError)
_NON_MATERIAL_FIELDS = frozenset({"observed_at"})


@dataclass
class ReleaseCollection:
    coverage: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    counts: dict[str, int]


def release_path(release_id: int) -> str:
    return f"{RELEASE_DIR}/{release_id}.json"


def collect_releases(
    context: CollectContext,
    *,
    activity_window: Mapping[str, Any],
) -> ReleaseCollection:
    store = context.store
    client = context.client
    repository = context.repository
    start, end = _parse_window(activity_window)
    host = str(repository.get("host") or "github.com")
    owner = str(repository["owner"])
    name = str(repository["name"])

    documents = load_release_documents(store)
    existing = {int(document["release_id"]): document for document in documents}
    reconcile_release_projection(store, documents, host=host, owner=owner, name=name)

    started = context.now()
    section, pages, duplicates = _fetch_releases(
        context,
        owner=owner,
        name=name,
        start=start,
    )
    counts = _empty_counts()
    selected = _select_releases(section["items"], start, end, counts)
    counts["selected"] = len(selected)
    counts["duplicates"] = duplicates

    problems: list[str] = []
    current: list[dict[str, Any]] = []
    requests_before = int(client.usage.get("requests", 0))
    try:
        for index, raw in enumerate(selected):
            used = int(client.usage.get("requests", 0)) - requests_before
            if used >= MAX_RELEASE_REQUESTS:
                counts["request_limited"] = len(selected) - index
                problems.append(f"release_request_limit:{counts['request_limited']}")
                break
            _collect_release(
                context,
                raw,
                existing=existing,
                host=host,
                owner=owner,
                name=name,
                counts=counts,
                problems=problems,
                current=current,
            )
    except BudgetExceededError as exc:
        if is_run_budget_error(exc):
            mark_run_budget_exhausted(context)
        problems.append(error_code(exc, "budget_exceeded"))
    except _LOCAL_ERRORS:
        _finalize(
            context,
            counts,
            problems,
            section,
            current,
            started,
            pages,
            activity_window=activity_window,
            best_effort=True,
        )
        raise
    return _finalize(
        context,
        counts,
        problems,
        section,
        current,
        started,
        pages,
        activity_window=activity_window,
    )


def _fetch_releases(
    context: CollectContext,
    *,
    owner: str,
    name: str,
    start: datetime,
) -> tuple[dict[str, Any], int, int]:
    client = context.client
    spec = SectionSpec(
        name="releases",
        endpoint=f"repos/{owner}/{name}/releases",
        parse_item=lambda item: dict(item),
        cap=MAX_RELEASES,
        cap_reason=f"release_limit_{MAX_RELEASES}",
    )
    try:
        section, result = fetch_section_result(
            client,
            spec,
            owner=owner,
            name=name,
            number=0,
            stop_after=_page_before_window(start),
            stop_reason="release_window_reached",
            dedupe_key=_release_key,
        )
        if section.get("truncation_reason") == "release_window_reached":
            section["status"] = "complete"
        return section, result.pages, result.duplicates
    except BudgetExceededError as exc:
        if is_run_budget_error(exc):
            mark_run_budget_exhausted(context)
        return _empty_section("budget_exceeded"), 0, 0


def _empty_section(reason: str) -> dict[str, Any]:
    return {
        "status": "partial",
        "fetched": 0,
        "total_known": None,
        "truncated": True,
        "truncation_reason": reason,
        "items": [],
        "duplicates": 0,
    }


def _release_key(item: Any) -> Any:
    if not isinstance(item, Mapping):
        return None
    value = item.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return ("id", value)
    return None


def _ordering_time(item: Any) -> datetime | None:
    if not isinstance(item, Mapping):
        return None
    return parse_datetime(item.get("published_at")) or parse_datetime(
        item.get("created_at")
    )


def _page_before_window(start: datetime):
    def predicate(items: Sequence[Any]) -> bool:
        stamps = [_ordering_time(item) for item in items]
        return bool(stamps) and all(
            stamp is not None and stamp < start for stamp in stamps
        )

    return predicate


def _empty_counts() -> dict[str, int]:
    return {
        "listed": 0,
        "selected": 0,
        "written": 0,
        "skipped_unchanged": 0,
        "retained": 0,
        "duplicates": 0,
        "drafts_skipped": 0,
        "outside_window": 0,
        "invalid": 0,
        "tag_unresolved": 0,
        "request_limited": 0,
    }


def _select_releases(
    items: Sequence[Any],
    start: datetime,
    end: datetime,
    counts: dict[str, int],
) -> list[Mapping[str, Any]]:
    selected: list[Mapping[str, Any]] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        counts["listed"] += 1
        if bool(raw.get("draft")):
            counts["drafts_skipped"] += 1
            continue
        published = parse_datetime(raw.get("published_at"))
        if published is None:
            counts["invalid"] += 1
            continue
        if not (start <= published < end):
            counts["outside_window"] += 1
            continue
        if not _valid_release_identity(raw):
            counts["invalid"] += 1
            continue
        selected.append(raw)
    return selected


def _collect_release(
    context: CollectContext,
    raw: Mapping[str, Any],
    *,
    existing: Mapping[int, Mapping[str, Any]],
    host: str,
    owner: str,
    name: str,
    counts: dict[str, int],
    problems: list[str],
    current: list[dict[str, Any]],
) -> None:
    document = _build_release_document(
        context.store,
        context.client,
        raw,
        existing=existing,
        host=host,
        owner=owner,
        name=name,
        observed_at=context.now(),
    )
    release_id = int(document["release_id"])
    previous = existing.get(release_id)
    if document["tag_ref"]["status"] != "complete":
        counts["tag_unresolved"] += 1
        problems.append(f"tag_unresolved:{document['tag_name']}")
    if previous is not None and _would_downgrade(previous, document):
        counts["retained"] += 1
        problems.append(f"refresh_incomplete:{document['tag_name']}")
        current.append(dict(previous))
        return
    if previous is not None and _same_content(previous, document):
        counts["skipped_unchanged"] += 1
        current.append(previous)
        return
    write_release_document(context.store, document)
    project_release(context.store, host, owner, name, document)
    current.append(document)
    counts["written"] += 1


def _would_downgrade(
    previous: Mapping[str, Any],
    document: Mapping[str, Any],
) -> bool:
    coverage = previous.get("coverage")
    detail = coverage.get("detail") if isinstance(coverage, Mapping) else None
    return detail == "complete" and document["coverage"]["detail"] != "complete"


def _same_content(
    previous: Mapping[str, Any],
    document: Mapping[str, Any],
) -> bool:
    return _content_key(previous) == _content_key(document)


def _content_key(document: Mapping[str, Any]) -> str:
    return canonical_json(
        {
            key: value
            for key, value in document.items()
            if key not in _NON_MATERIAL_FIELDS
        }
    )


def _finalize(
    context: CollectContext,
    counts: dict[str, int],
    problems: list[str],
    section: Mapping[str, Any],
    current: list[dict[str, Any]],
    started: str,
    pages: int,
    *,
    activity_window: Mapping[str, Any],
    best_effort: bool = False,
) -> ReleaseCollection:
    finished = context.now()
    coverage_status = str(section.get("status") or "complete")
    if counts["invalid"]:
        problems.append(f"invalid_releases:{counts['invalid']}")
    if counts["duplicates"]:
        problems.append(f"pagination_duplicates:{counts['duplicates']}")
    fetched = counts["written"] + counts["skipped_unchanged"]
    if coverage_status == "complete" and (problems or fetched < counts["selected"]):
        coverage_status = "partial"
    reason_parts = []
    section_reason = section.get("truncation_reason")
    if isinstance(section_reason, str) and section_reason:
        reason_parts.append(section_reason)
    if counts["drafts_skipped"]:
        reason_parts.append(f"draft_releases_skipped:{counts['drafts_skipped']}")
    reason_parts.extend(problems)
    if fetched < counts["selected"] and not problems:
        reason_parts.append(
            f"incomplete_releases:{counts['selected'] - fetched}"
        )
    entry = coverage_entry(
        channel="releases",
        query_scope="activity_window",
        query=(
            f"GET repos/{context.repository['owner']}/{context.repository['name']}"
            f"/releases [{activity_window['start']}, {activity_window['end']})"
        ),
        expected=counts["selected"],
        fetched=fetched,
        pages=pages,
        exhausted=coverage_status == "complete",
        truncation_reason="; ".join(reason_parts) or None,
        coverage_status=coverage_status,
        discovery_status="complete",
        detail_status=coverage_status,
        observed_start=started,
        observed_end=finished,
    )
    if context.progress is not None:
        context.progress.finish_channel(
            "releases",
            entry,
            completed=sorted(int(item["release_id"]) for item in current),
            pending=[],
            observed_at=finished,
            best_effort=best_effort,
        )
    return ReleaseCollection(
        coverage=[entry],
        documents=current,
        counts=counts,
    )


def _valid_release_identity(raw: Mapping[str, Any]) -> bool:
    release_id = raw.get("id")
    tag_name = raw.get("tag_name")
    return (
        isinstance(release_id, int)
        and not isinstance(release_id, bool)
        and release_id > 0
        and isinstance(tag_name, str)
        and bool(tag_name.strip())
    )


def _build_release_document(
    store: SnapshotStore,
    client: GitHubClient,
    raw: Mapping[str, Any],
    *,
    existing: Mapping[int, Mapping[str, Any]],
    host: str,
    owner: str,
    name: str,
    observed_at: str,
) -> dict[str, Any]:
    release_id = int(raw["id"])
    tag_name = str(raw["tag_name"]).strip()
    tag_ref = _resolve_tag_ref(client, raw, owner=owner, name=name)
    previous = existing.get(release_id)
    previous_commit = None
    if isinstance(previous, Mapping) and previous.get("tag_name") == tag_name:
        previous_tag_ref = previous.get("tag_ref")
        if isinstance(previous_tag_ref, Mapping):
            value = previous_tag_ref.get("commit_sha")
            if isinstance(value, str) and FULL_SHA_RE.fullmatch(value):
                previous_commit = value
    commit_sha = tag_ref["commit_sha"]
    tag_moved = bool(
        previous_commit
        and commit_sha
        and previous_commit != commit_sha
    )
    published_at = iso_or_none(raw.get("published_at"))
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repository": {"host": host, "owner": owner, "name": name},
        "release_id": release_id,
        "tag_name": tag_name,
        "name": str(raw.get("name") or tag_name),
        "url": str(
            raw.get("html_url")
            or f"https://github.com/{owner}/{name}/releases/tag/{quote(tag_name, safe='')}"
        ),
        "author": user_login(raw),
        "draft": bool(raw.get("draft")),
        "prerelease": bool(raw.get("prerelease")),
        "created_at": iso_or_none(raw.get("created_at")),
        "published_at": published_at,
        "target_commitish": as_optional_str(raw.get("target_commitish")),
        "tag_ref": tag_ref,
        "tag_moved": tag_moved,
        "previous_commit_sha": previous_commit if tag_moved else None,
        "body": text_ref(store, raw.get("body")),
        "coverage": {
            "detail": (
                "complete"
                if tag_ref["status"] == "complete"
                else "partial"
            ),
            "tag": tag_ref["status"],
            "body": "complete",
        },
        "limitations": [],
    }
    document["limitations"] = derive_release_limitations(document)
    validate_release_document(document)
    return document


def _resolve_tag_ref(
    client: GitHubClient,
    raw: Mapping[str, Any],
    *,
    owner: str,
    name: str,
) -> dict[str, Any]:
    tag_name = str(raw["tag_name"]).strip()
    endpoint = f"repos/{owner}/{name}/git/ref/tags/{quote(tag_name, safe='')}"
    try:
        data = client.rest(endpoint)
    except GitHubNotFoundError:
        return _tag_ref("unavailable", reason="not_found")
    except FATAL_ERRORS:
        raise
    except GitHubError as exc:
        return _tag_ref("unavailable", reason=error_code(exc, "github_error"))
    obj = data.get("object") if isinstance(data, Mapping) else None
    if not isinstance(obj, Mapping):
        return _tag_ref("unavailable", reason="invalid_ref")
    sha = as_optional_str(obj.get("sha"))
    object_type = as_optional_str(obj.get("type"))
    if sha is None:
        return _tag_ref("unavailable", reason="missing_ref_sha")
    if object_type == "commit":
        return _tag_ref("complete", sha=sha, object_type="commit", commit_sha=sha)
    if object_type != "tag":
        return _tag_ref(
            "partial",
            sha=sha,
            object_type=object_type,
            reason="unsupported_object_type",
        )
    try:
        tag_data = client.rest(
            f"repos/{owner}/{name}/git/tags/{sha}",
        )
    except GitHubNotFoundError:
        return _tag_ref(
            "partial",
            sha=sha,
            object_type="tag",
            reason="tag_object_not_found",
        )
    except FATAL_ERRORS:
        raise
    except GitHubError as exc:
        return _tag_ref(
            "partial",
            sha=sha,
            object_type="tag",
            reason=error_code(exc, "github_error"),
        )
    target = tag_data.get("object") if isinstance(tag_data, Mapping) else None
    if not isinstance(target, Mapping):
        return _tag_ref(
            "partial",
            sha=sha,
            object_type="tag",
            reason="invalid_tag_object",
        )
    commit_sha = as_optional_str(target.get("sha"))
    target_type = as_optional_str(target.get("type"))
    if commit_sha is None or target_type != "commit":
        return _tag_ref(
            "partial",
            sha=sha,
            object_type="tag",
            reason=f"target_{target_type or 'unknown'}",
        )
    return _tag_ref(
        "complete",
        sha=sha,
        object_type="tag",
        commit_sha=commit_sha,
    )


def _tag_ref(
    status: str,
    *,
    sha: str | None = None,
    object_type: str | None = None,
    commit_sha: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "sha": sha,
        "object_type": object_type,
        "commit_sha": commit_sha,
        "reason": reason,
    }


def validate_release_document(document: Mapping[str, Any]) -> None:
    validate_document(document, "release_detail")
    tag_ref = document["tag_ref"]
    expected_tag_status = tag_ref["status"]
    expected_detail = (
        "complete" if expected_tag_status == "complete" else "partial"
    )
    if document["coverage"]["detail"] != expected_detail:
        raise StoreError(
            "release_detail.coverage.detail 与 tag/body 状态不一致",
            source="release_store.invariant",
            details={"release_id": document.get("release_id")},
        )
    if document["coverage"]["tag"] != expected_tag_status:
        raise StoreError(
            "release_detail.coverage.tag 与 tag_ref.status 不一致",
            source="release_store.invariant",
            details={"release_id": document.get("release_id")},
        )
    if document["coverage"]["body"] != "complete":
        raise StoreError(
            "release_detail.coverage.body 必须为 complete",
            source="release_store.invariant",
            details={"release_id": document.get("release_id")},
        )
    if expected_tag_status == "complete" and tag_ref["commit_sha"] is None:
        raise StoreError(
            "release_detail.tag_ref.status=complete 但缺少 commit_sha",
            source="release_store.invariant",
            details={"release_id": document.get("release_id")},
        )
    if expected_tag_status != "complete" and tag_ref["commit_sha"] is not None:
        raise StoreError(
            "release_detail.tag_ref 未完成却声明 commit_sha",
            source="release_store.invariant",
            details={"release_id": document.get("release_id")},
        )
    if document["tag_moved"] and document["previous_commit_sha"] is None:
        raise StoreError(
            "release_detail.tag_moved 但缺少 previous_commit_sha",
            source="release_store.invariant",
            details={"release_id": document.get("release_id")},
        )
    if not document["tag_moved"] and document["previous_commit_sha"] is not None:
        raise StoreError(
            "release_detail.previous_commit_sha 只在 tag_moved 时允许",
            source="release_store.invariant",
            details={"release_id": document.get("release_id")},
        )
    if document["limitations"] != derive_release_limitations(document):
        raise StoreError(
            "release_detail.limitations 不是从文档派生",
            source="release_store.invariant",
            details={"release_id": document.get("release_id")},
        )


def derive_release_limitations(document: Mapping[str, Any]) -> list[str]:
    limitations = {"observed_current_state"}
    if document["draft"]:
        limitations.add("draft_release")
    if document["prerelease"]:
        limitations.add("prerelease")
    if document["tag_ref"]["status"] != "complete":
        limitations.add("tag_unresolved")
    if document["tag_moved"]:
        limitations.add("tag_moved")
    if document["body"]["sha256"] is None:
        limitations.add("release_note_empty")
    if document["published_at"] is None:
        limitations.add("published_at_missing")
    return sorted(limitations)


def write_release_document(
    store: SnapshotStore,
    document: Mapping[str, Any],
) -> None:
    validate_release_document(document)
    store.write_json(
        release_path(int(document["release_id"])),
        document,
        schema_name="release_detail",
    )


def iter_release_documents(store: SnapshotStore) -> Iterator[dict[str, Any]]:
    directory = store.secure_path(RELEASE_DIR)
    if not directory.is_dir():
        return
    paths = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".json" and path.stem.isdigit()
    ]
    for path in sorted(paths, key=lambda item: int(item.stem)):
        document = store.read_json(f"{RELEASE_DIR}/{path.name}")
        validate_release_document(document)
        yield document


def load_release_documents(store: SnapshotStore) -> list[dict[str, Any]]:
    return list(iter_release_documents(store))


def reconcile_release_projection(
    store: SnapshotStore,
    documents: list[dict[str, Any]] | None = None,
    *,
    host: str,
    owner: str,
    name: str,
) -> None:
    materialized = documents if documents is not None else load_release_documents(store)
    for document in materialized:
        project_release(store, host, owner, name, document)


def project_release(
    store: SnapshotStore,
    host: str,
    owner: str,
    name: str,
    document: Mapping[str, Any],
) -> None:
    identifier = entity_id(host, owner, name, "release", str(document["tag_name"]))
    existing = next(
        (
            entity
            for entity in store.read_jsonl("normalized/entities.jsonl")
            if isinstance(entity, Mapping) and entity.get("id") == identifier
        ),
        None,
    )
    incoming = make_entity(
        entity_id=identifier,
        kind="release",
        title=str(document["name"] or document["tag_name"]),
        url=str(document["url"]),
        observed_at=str(document["observed_at"]),
        raw_state=release_pointer(document),
        normalized_state={
            "draft": document["draft"],
            "prerelease": document["prerelease"],
            "published_at": document["published_at"],
        },
        roles=["release_notes"],
        created_at=document["created_at"],
        source_event_at=document["published_at"] or document["created_at"],
        discovery_reasons=["release"],
    )
    merged = merge_entity(existing, incoming) if existing is not None else incoming
    validate_document(merged, "entity")
    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [merged],
        key="id",
        schema_name="entity",
    )


def _parse_window(window: Mapping[str, Any]) -> tuple[datetime, datetime]:
    try:
        return window_bounds(window)
    except ValueError as exc:
        raise GitHubError(
            "活动窗口缺少合法边界",
            source="releases.window",
        ) from exc


def release_pointer(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "release_id": document["release_id"],
        "tag_name": document["tag_name"],
        "commit_sha": document["tag_ref"]["commit_sha"],
        "detail_path": release_path(int(document["release_id"])),
        "observed_at": document["observed_at"],
    }


__all__ = [
    "MAX_RELEASE_REQUESTS",
    "MAX_RELEASES",
    "RELEASE_DIR",
    "ReleaseCollection",
    "collect_releases",
    "derive_release_limitations",
    "iter_release_documents",
    "load_release_documents",
    "project_release",
    "reconcile_release_projection",
    "release_path",
    "release_pointer",
    "validate_release_document",
    "write_release_document",
]
