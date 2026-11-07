"""Deep-read selected pull requests: files, commits, comments and reviews."""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .. import SCHEMA_VERSION
from ..errors import (
    GitHubError,
    GitHubNotFoundError,
    InternalError,
    StoreError,
    ValidationFailure,
)
from ..github import GitHubClient
from ..models import canonical_json, validate_document
from ..store import SnapshotStore
from .base import FATAL_ERRORS, error_code
from .entity_store import project_pr_detail
from .pages import SectionSpec, dedupe_by_field, fetch_section
from .pr_store import (
    detail_path,
    load_pr_documents,
    reconcile_entity_projection,
    write_pr_document,
)
from .progress import CollectProgress, coverage_entry
from .selection import (
    Candidate,
    build_candidates,
    choose,
    derive_fetched_count,
    derive_selection_counts,
    quota_for_budget,
)
from .status import (
    PR_DETAIL_SECTIONS,
    derive_detail_status,
    derive_limitations,
    derive_pr_detail_channel_status,
    derive_selection_limitations,
)
from .texts import iter_text_refs, text_ref, text_ref_problem
from .values import (
    as_int,
    as_optional_int,
    as_optional_str,
    get_in,
    iso_or_none,
    now_iso,
    window_bounds,
)

FILES_CAP = 3000
COMMITS_CAP = 250
_CODE_SECTIONS = ("files", "commits")
_NON_MATERIAL_FIELDS = frozenset({"observed_at", "selection_reason"})
_NON_FATAL_GITHUB = (GitHubNotFoundError,)
_LOCAL_ERRORS = (StoreError, ValidationFailure, InternalError)


@dataclass
class _Read:
    document: dict[str, Any]
    pages: int
    changed: bool


@dataclass
class PullRequestCollection:
    coverage: list[dict[str, Any]]
    selection: dict[str, Any]
    details: list[dict[str, Any]]
    counts: dict[str, int]


def collect_pull_requests(
    client: GitHubClient,
    store: SnapshotStore,
    repository: Mapping[str, Any],
    *,
    activity_window: Mapping[str, Any],
    activity_overview: Mapping[str, Any] | None = None,
    focus: list[str] | tuple[str, ...] = (),
    progress: CollectProgress | None = None,
    clock: Callable[[], str] | None = None,
    force: bool = False,
) -> PullRequestCollection:
    now = clock or now_iso
    observed_at = now()
    host = str(repository.get("host") or "github.com")
    owner = str(repository["owner"])
    name = str(repository["name"])
    overview = (
        activity_overview
        if activity_overview is not None
        else _read_overview(store)
    )
    overview_missing = overview is None
    start, end = _parse_window(activity_window)
    # Repair the entity projection before selection reads discovery_reasons.
    documents = load_pr_documents(store)
    existing = {int(document["number"]): document for document in documents}
    reconcile_entity_projection(store, documents)
    candidates = build_candidates(
        store,
        overview,
        focus,
        start=start,
        end=end,
        host=host,
        owner=owner,
        name=name,
    )
    pr_quota, merged_quota, open_quota = quota_for_budget(
        client.budget, client.deep_entities_used()
    )
    choose(candidates, pr_quota, merged_quota, open_quota)
    selected = [candidate for candidate in candidates if candidate.selected]
    selected_numbers = {candidate.number for candidate in selected}
    completed: set[int] = set()
    pages = 0
    results: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []

    def flush_stage(status: str) -> None:
        if progress is None:
            return
        progress.flush_stage(
            "pr_detail",
            status=status,
            completed=sorted(completed),
            pending=sorted(selected_numbers - completed),
            observed_at=now(),
            best_effort=True,
        )

    def finalize_partial() -> None:
        try:
            _finalize(
                store,
                client,
                repository,
                activity_window,
                observed_at,
                candidates,
                results,
                pages,
                progress,
                now(),
                completed=completed,
                quota=(pr_quota, merged_quota, open_quota),
                extra_limitations=("activity_overview_missing",) if overview_missing else (),
                status="partial",
                retained=retained,
                best_effort=True,
            )
        except Exception as exc:
            print(f"warning: 无法写入 partial 收尾状态: {exc}", file=sys.stderr)

    try:
        for candidate in selected:
            requests_before = int(client.usage.get("requests", 0))
            existing_document = existing.get(candidate.number)
            try:
                read = _read_pr(
                    client,
                    store,
                    host,
                    owner,
                    name,
                    candidate.number,
                    existing=existing_document,
                    force=force,
                    selection_reason=list(candidate.reasons) or ["deep_read_selected"],
                    observed_at=now(),
                )
            except _NON_FATAL_GITHUB as exc:
                pages += max(0, int(client.usage.get("requests", 0)) - requests_before)
                candidate.status = "failed"
                candidate.skipped_reason = error_code(exc, "github_error")
                flush_stage("in_progress")
                continue
            except FATAL_ERRORS:
                pages += max(0, int(client.usage.get("requests", 0)) - requests_before)
                flush_stage("partial")
                raise
            except GitHubError as exc:
                pages += max(0, int(client.usage.get("requests", 0)) - requests_before)
                candidate.status = "failed"
                candidate.skipped_reason = error_code(exc, "github_error")
                flush_stage("in_progress")
                continue
            except _LOCAL_ERRORS as exc:
                pages += max(0, int(client.usage.get("requests", 0)) - requests_before)
                candidate.status = "failed"
                candidate.skipped_reason = error_code(exc, "store_error")
                flush_stage("partial")
                raise
            pages += read.pages
            if (
                store.secure_path(detail_path(candidate.number)).is_file()
                and _would_downgrade(existing_document, read.document)
            ):
                candidate.status = "failed"
                candidate.skipped_reason = "refresh_incomplete"
                retained.append(dict(existing_document))
                flush_stage("in_progress")
                continue
            if not read.changed:
                candidate.status = "skipped_unchanged"
                candidate.skipped_reason = "materials_unchanged"
                completed.add(candidate.number)
                flush_stage("in_progress")
                continue
            try:
                _persist_pr(store, read.document)
            except _LOCAL_ERRORS as exc:
                candidate.status = "failed"
                candidate.skipped_reason = error_code(exc, "store_error")
                raise
            results.append(read.document)
            candidate.status = "selected"
            completed.add(candidate.number)
            if existing_document is None:
                client.usage["deep_entities"] += 1
            flush_stage("in_progress")
    except FATAL_ERRORS:
        finalize_partial()
        raise
    except _LOCAL_ERRORS:
        finalize_partial()
        raise

    return _finalize(
        store,
        client,
        repository,
        activity_window,
        observed_at,
        candidates,
        results,
        pages,
        progress,
        now(),
        completed=completed,
        quota=(pr_quota, merged_quota, open_quota),
        extra_limitations=("activity_overview_missing",) if overview_missing else (),
        retained=retained,
    )


def _read_overview(store: SnapshotStore) -> Mapping[str, Any] | None:
    try:
        return store.read_json("analysis/activity-overview.json")
    except Exception:
        return None


def _section_specs(
    store: SnapshotStore,
    head_sha: str | None,
) -> tuple[SectionSpec, ...]:
    return (
        SectionSpec(
            name="files",
            endpoint="repos/{owner}/{name}/pulls/{number}/files",
            parse_item=lambda item: _file_item(store, item),
            cap=FILES_CAP,
            cap_reason="files_limit_3000",
            total_known_key="changed_files",
            mismatch_reason="files_total_mismatch",
            dedupe_key=dedupe_by_field("filename"),
        ),
        SectionSpec(
            name="commits",
            endpoint="repos/{owner}/{name}/pulls/{number}/commits",
            parse_item=lambda item: _commit_item(store, item),
            cap=COMMITS_CAP,
            cap_reason="commits_limit_250",
            total_known_key="commits",
            mismatch_reason="commits_total_mismatch",
            dedupe_key=dedupe_by_field("sha"),
        ),
        SectionSpec(
            name="issue_comments",
            endpoint="repos/{owner}/{name}/issues/{number}/comments",
            parse_item=lambda item: _comment_item(store, item),
            total_known_key="comments",
            mismatch_reason="issue_comments_total_mismatch",
            dedupe_key=dedupe_by_field("id"),
        ),
        SectionSpec(
            name="reviews",
            endpoint="repos/{owner}/{name}/pulls/{number}/reviews",
            parse_item=lambda item: _review_item(store, item, head_sha),
            dedupe_key=dedupe_by_field("id"),
        ),
        SectionSpec(
            name="review_comments",
            endpoint="repos/{owner}/{name}/pulls/{number}/comments",
            parse_item=lambda item: _review_comment_item(store, item, head_sha),
            total_known_key="review_comments",
            mismatch_reason="review_comments_total_mismatch",
            dedupe_key=dedupe_by_field("id"),
        ),
    )


def _read_pr(
    client: GitHubClient,
    store: SnapshotStore,
    host: str,
    owner: str,
    name: str,
    number: int,
    *,
    existing: Mapping[str, Any] | None,
    force: bool,
    selection_reason: list[str],
    observed_at: str,
    read: Mapping[str, bool] | None = None,
) -> _Read:
    base = f"repos/{owner}/{name}"
    pages = 0
    detail = client.rest(f"{base}/pulls/{number}")
    pages += 1
    if not isinstance(detail, Mapping):
        raise GitHubError(
            "PR 详情响应格式异常",
            source="pr_detail.response",
            details={"number": number},
        )
    head_sha = as_optional_str(get_in(detail, "head", "sha"))
    base_sha = as_optional_str(get_in(detail, "base", "sha"))
    reused = (
        {}
        if force
        else _reusable_code_sections(
            store,
            existing,
            detail,
            head_sha=head_sha,
            base_sha=base_sha,
        )
    )

    wanted = _wanted_sections(read)
    sections: dict[str, dict[str, Any]] = {}
    for spec in _section_specs(store, head_sha):
        if spec.name not in wanted:
            sections[spec.name] = _existing_section(existing, spec.name)
            continue
        if spec.name in reused:
            sections[spec.name] = reused[spec.name]
            continue
        section, section_pages = fetch_section(
            client,
            spec,
            owner=owner,
            name=name,
            number=number,
            parent=detail,
        )
        sections[spec.name] = section
        pages += section_pages

    head_sha_end = _confirm_head(client, owner, name, number)
    pages += 1
    head_changed = bool(head_sha_end and head_sha and head_sha_end != head_sha)
    head_unverified = head_sha is None or head_sha_end is None
    diff_ref = {"status": "not_requested", "sha256": None, "bytes": 0, "truncated": False}

    coverage = {
        "detail": derive_detail_status(
            sections=sections,
            head_changed=head_changed,
            head_unverified=head_unverified,
            diff_status=diff_ref["status"],
        ),
        **{key: value["status"] for key, value in sections.items()},
    }

    body_ref = text_ref(store, detail.get("body"))
    if read is not None and not read.get("body", True):
        body_ref = _existing_body(existing) or text_ref(store, "")
    document = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repository": {"host": host, "owner": owner, "name": name},
        "number": number,
        "url": str(detail.get("html_url") or f"https://github.com/{owner}/{name}/pull/{number}"),
        "title": str(detail.get("title") or f"#{number}"),
        "author": as_optional_str(get_in(detail, "user", "login")),
        "state": str(detail.get("state") or "unknown"),
        "draft": bool(detail.get("draft")),
        "merged": bool(detail.get("merged") or detail.get("merged_at")),
        "merged_at": iso_or_none(detail.get("merged_at")),
        "created_at": iso_or_none(detail.get("created_at")),
        "updated_at": iso_or_none(detail.get("updated_at")),
        "closed_at": iso_or_none(detail.get("closed_at")),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "head_sha_end": head_sha_end,
        "head_changed": head_changed,
        "merge_commit_sha": as_optional_str(detail.get("merge_commit_sha")),
        "comparator": "github_pr_files",
        "additions": as_int(detail.get("additions")),
        "deletions": as_int(detail.get("deletions")),
        "changed_files": as_int(detail.get("changed_files")),
        "commits_count": as_int(detail.get("commits")),
        "comments_count": as_int(detail.get("comments")),
        "review_comments_count": as_int(detail.get("review_comments")),
        "selection_reason": list(selection_reason),
        "body": body_ref,
        "files": sections["files"],
        "commits": sections["commits"],
        "issue_comments": sections["issue_comments"],
        "reviews": sections["reviews"],
        "review_comments": sections["review_comments"],
        "diff": diff_ref,
        "coverage": coverage,
        "limitations": [],
    }
    document["limitations"] = derive_limitations(document)
    validate_document(document, "pr_detail")
    return _Read(
        document=document,
        pages=pages,
        changed=(
            force
            or not store.secure_path(detail_path(number)).is_file()
            or not _same_material(existing, document)
        ),
    )


_PR_SECTION_FLAGS = {
    "files": "files",
    "issue_comments": "comments",
    "reviews": "reviews",
    "review_comments": "comments",
}


def _wanted_sections(read: Mapping[str, bool] | None) -> set[str]:
    wanted = {"commits"}
    if read is None:
        wanted.update(_PR_SECTION_FLAGS)
        return wanted
    for section, flag in _PR_SECTION_FLAGS.items():
        if read.get(flag, True):
            wanted.add(section)
    return wanted


def _not_requested_section() -> dict[str, Any]:
    return {
        "status": "not_requested",
        "fetched": 0,
        "total_known": None,
        "truncated": False,
        "truncation_reason": None,
        "duplicates": 0,
        "items": [],
    }


def _existing_section(
    existing: Mapping[str, Any] | None,
    name: str,
) -> dict[str, Any]:
    if isinstance(existing, Mapping):
        section = existing.get(name)
        if isinstance(section, Mapping):
            copied = dict(section)
            copied.setdefault("duplicates", 0)
            return copied
    return _not_requested_section()


def _existing_body(existing: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(existing, Mapping):
        return None
    body = existing.get("body")
    if not isinstance(body, Mapping):
        return None
    if body.get("sha256") is not None or body.get("bytes") or body.get("preview"):
        return dict(body)
    return None


def _reusable_code_sections(
    store: SnapshotStore,
    existing: Mapping[str, Any] | None,
    detail: Mapping[str, Any],
    *,
    head_sha: str | None,
    base_sha: str | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(existing, Mapping):
        return {}
    number = existing.get("number")
    if not isinstance(number, int) or isinstance(number, bool):
        return {}
    if not store.secure_path(detail_path(number)).is_file():
        return {}
    if not head_sha or not base_sha:
        return {}
    if existing.get("head_sha") != head_sha or existing.get("base_sha") != base_sha:
        return {}
    if existing.get("head_sha_end") != head_sha or existing.get("head_changed"):
        return {}
    reused: dict[str, dict[str, Any]] = {}
    for name in _CODE_SECTIONS:
        section = existing.get(name)
        if not isinstance(section, Mapping):
            continue
        if section.get("status") != "complete" or section.get("truncated"):
            continue
        fetched = section.get("fetched")
        items = section.get("items")
        total_known = section.get("total_known")
        if (
            not isinstance(fetched, int)
            or isinstance(fetched, bool)
            or not isinstance(items, list)
            or fetched != len(items)
        ):
            continue
        if total_known is not None and total_known != fetched:
            continue
        fresh_total = _detail_total(detail, name)
        if fresh_total is not None and fresh_total != fetched:
            continue
        if not _text_refs_valid(store, section):
            continue
        copied = dict(section)
        copied.setdefault("duplicates", 0)
        reused[name] = copied
    return reused


def _detail_total(detail: Mapping[str, Any], section: str) -> int | None:
    if section == "files":
        return as_optional_int(detail.get("changed_files"))
    if section == "commits":
        return as_optional_int(detail.get("commits"))
    return None


def _text_refs_valid(store: SnapshotStore, value: Any) -> bool:
    return all(
        text_ref_problem(store, ref) is None
        for _path, ref in iter_text_refs(value)
    )


def _same_material(
    existing: Mapping[str, Any] | None,
    document: Mapping[str, Any],
) -> bool:
    if not isinstance(existing, Mapping):
        return False
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


def _would_downgrade(
    existing: Mapping[str, Any] | None,
    document: Mapping[str, Any],
) -> bool:
    if not isinstance(existing, Mapping):
        return False
    old_coverage = existing.get("coverage")
    if not isinstance(old_coverage, Mapping) or old_coverage.get("detail") != "complete":
        return False
    new_coverage = document.get("coverage")
    return (
        not isinstance(new_coverage, Mapping)
        or new_coverage.get("detail") != "complete"
    )


def _confirm_head(
    client: GitHubClient,
    owner: str,
    name: str,
    number: int,
) -> str | None:
    try:
        detail = client.rest(f"repos/{owner}/{name}/pulls/{number}")
    except FATAL_ERRORS:
        raise
    except GitHubError:
        return None
    return as_optional_str(get_in(detail, "head", "sha"))


def _file_item(store: SnapshotStore, raw: Mapping[str, Any]) -> dict[str, Any]:
    patch = raw.get("patch")
    if isinstance(patch, str):
        patch_status = "available" if patch else "empty"
        patch_ref = text_ref(store, patch)
    else:
        patch_status = "missing"
        patch_ref = None
    return {
        "filename": str(raw.get("filename") or ""),
        "status": str(raw.get("status") or "modified"),
        "additions": as_int(raw.get("additions")),
        "deletions": as_int(raw.get("deletions")),
        "changes": as_int(raw.get("changes")),
        "patch_status": patch_status,
        "patch": patch_ref,
        "previous_filename": as_optional_str(raw.get("previous_filename")),
        "sha": as_optional_str(raw.get("sha")),
    }


def _commit_item(store: SnapshotStore, raw: Mapping[str, Any]) -> dict[str, Any]:
    commit = raw.get("commit") if isinstance(raw.get("commit"), Mapping) else {}
    author = commit.get("author") if isinstance(commit.get("author"), Mapping) else {}
    return {
        "sha": str(raw.get("sha") or ""),
        "message": text_ref(store, commit.get("message")),
        "author": as_optional_str(author.get("name")),
        "date": iso_or_none(author.get("date")),
    }


def _comment_item(store: SnapshotStore, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": as_int(raw.get("id")),
        "user": as_optional_str(get_in(raw, "user", "login")),
        "created_at": iso_or_none(raw.get("created_at")),
        "updated_at": iso_or_none(raw.get("updated_at")),
        "author_association": as_optional_str(raw.get("author_association")),
        "body": text_ref(store, raw.get("body")),
    }


def _review_item(
    store: SnapshotStore,
    raw: Mapping[str, Any],
    head_sha: str | None,
) -> dict[str, Any]:
    commit_id = as_optional_str(raw.get("commit_id"))
    return {
        "id": as_int(raw.get("id")),
        "user": as_optional_str(get_in(raw, "user", "login")),
        "state": str(raw.get("state") or "UNKNOWN"),
        "submitted_at": iso_or_none(raw.get("submitted_at")),
        "commit_id": commit_id,
        "outdated": bool(head_sha and commit_id and commit_id != head_sha),
        "resolution": "unknown",
        "body": text_ref(store, raw.get("body")),
    }


def _review_comment_item(
    store: SnapshotStore,
    raw: Mapping[str, Any],
    head_sha: str | None,
) -> dict[str, Any]:
    commit_id = as_optional_str(raw.get("commit_id"))
    return {
        "id": as_int(raw.get("id")),
        "user": as_optional_str(get_in(raw, "user", "login")),
        "path": as_optional_str(raw.get("path")),
        "line": as_optional_int(raw.get("line")),
        "original_line": as_optional_int(raw.get("original_line")),
        "commit_id": commit_id,
        "outdated": bool(head_sha and commit_id and commit_id != head_sha),
        "in_reply_to_id": as_optional_int(raw.get("in_reply_to_id")),
        "pull_request_review_id": as_optional_int(raw.get("pull_request_review_id")),
        "diff_hunk": text_ref(store, raw.get("diff_hunk")),
        "body": text_ref(store, raw.get("body")),
    }


def _persist_pr(
    store: SnapshotStore,
    document: Mapping[str, Any],
) -> None:
    # The document is the commit point. A crash before the entity projection is
    # written is repaired by reconcile_entity_projection() on the next collect.
    write_pr_document(store, document)
    repository = document["repository"]
    project_pr_detail(
        store,
        str(repository["host"]),
        str(repository["owner"]),
        str(repository["name"]),
        document,
    )


def _finalize(
    store: SnapshotStore,
    client: GitHubClient,
    repository: Mapping[str, Any],
    activity_window: Mapping[str, Any],
    observed_at: str,
    candidates: list[Candidate],
    results: list[dict[str, Any]],
    pages: int,
    progress: CollectProgress | None,
    observed_end: str,
    *,
    completed: set[int] | None = None,
    quota: tuple[int, int, int] = (0, 0, 0),
    extra_limitations: tuple[str, ...] = (),
    status: str | None = None,
    retained: list[dict[str, Any]] | None = None,
    best_effort: bool = False,
) -> PullRequestCollection:
    selected = [candidate for candidate in candidates if candidate.selected]
    candidate_records = [
        {
            "number": candidate.number,
            "entity_id": candidate.entity_id,
            "bucket": candidate.bucket,
            "score": candidate.score,
            "score_components": dict(candidate.components),
            "selection_reason": list(candidate.reasons),
            "selected": candidate.selected,
            "skipped_reason": candidate.skipped_reason,
            "status": candidate.status,
        }
        for candidate in candidates
    ]
    counts = derive_selection_counts(candidate_records)
    counts["details_written"] = len(results)
    counts["pages"] = pages
    coverage_results = [*results, *(retained or ())]
    coverage_status = status or derive_pr_detail_channel_status(
        selected=counts["selected"],
        failed=counts["failed"],
        skipped_unchanged=counts["skipped_unchanged"],
        results=coverage_results,
        not_attempted=counts["not_attempted"],
    )
    entry = coverage_entry(
        channel="pr_detail",
        query_scope="deep_read_selection",
        query=", ".join(f"#{candidate.number}" for candidate in selected),
        expected=counts["selected"],
        fetched=derive_fetched_count(candidate_records),
        pages=pages,
        exhausted=coverage_status == "complete",
        truncation_reason=_coverage_reason(candidates, coverage_results),
        coverage_status=coverage_status,
        discovery_status="complete",
        detail_status=coverage_status,
        observed_start=observed_at,
        observed_end=observed_end,
    )
    selection = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repository": {
            "host": str(repository.get("host") or "github.com"),
            "owner": str(repository["owner"]),
            "name": str(repository["name"]),
        },
        "activity_window": {
            "start": activity_window["start"],
            "end": activity_window["end"],
            "timezone": activity_window["timezone"],
        },
        "quota": {
            "pr_quota": quota[0],
            "merged_quota": quota[1],
            "open_quota": quota[2],
            "max_deep_entities": int(client.budget.get("max_deep_entities", 0)),
            "deep_entities_used": client.deep_entities_used(),
            "agent_pr_reserve": int(client.budget.get("agent_pr_reserve", 0)),
        },
        "candidates": candidate_records,
        "counts": {
            "candidates": counts["candidates"],
            "selected": counts["selected"],
            "merged_selected": counts["merged_selected"],
            "open_selected": counts["open_selected"],
            "skipped_quota": counts["skipped_quota"],
            "skipped_unchanged": counts["skipped_unchanged"],
            "failed": counts["failed"],
            "not_attempted": counts["not_attempted"],
        },
        "limitations": derive_selection_limitations(
            has_quota_skip=any(
                item.status == "skipped_quota" for item in candidates
            ),
            coverage_status=coverage_status,
            extra=extra_limitations,
        ),
    }
    try:
        validate_document(selection, "pr_selection")
        store.write_json(
            "analysis/pr-selection.json",
            selection,
            schema_name="pr_selection",
        )
    except Exception as exc:
        if not best_effort:
            raise
        print(f"warning: 无法写入 pr-selection: {exc}", file=sys.stderr)
    if progress is not None:
        completed_numbers = set(completed or ())
        progress.finish_channel(
            "pr_detail",
            entry,
            completed=sorted(completed_numbers),
            pending=sorted(
                candidate.number
                for candidate in selected
                if candidate.number not in completed_numbers
            ),
            observed_at=observed_end,
            best_effort=True,
        )
    return PullRequestCollection(
        coverage=[entry],
        selection=selection,
        details=results,
        counts=counts,
    )


def _coverage_reason(
    candidates: list[Candidate],
    results: list[dict[str, Any]],
) -> str | None:
    reasons: dict[str, list[str]] = {}
    for candidate in candidates:
        if candidate.status == "failed":
            reasons.setdefault(candidate.skipped_reason or "failed", []).append(
                f"#{candidate.number}"
            )
    for item in results:
        number = f"#{item['number']}"
        for key in ("files", "commits", "issue_comments", "review_comments"):
            if item[key]["truncated"]:
                reasons.setdefault(f"{key}_truncated", []).append(number)
        duplicates = sum(
            int(item[key].get("duplicates") or 0)
            for key in PR_DETAIL_SECTIONS
            if isinstance(item.get(key), Mapping)
        )
        if duplicates:
            reasons.setdefault("pagination_duplicates", []).append(number)
        if item["head_changed"]:
            reasons.setdefault("head_changed", []).append(number)
        if item["coverage"]["detail"] == "partial":
            reasons.setdefault("detail_partial", []).append(number)
    if not reasons:
        return None
    return "; ".join(
        f"{reason}:{','.join(numbers)}" for reason, numbers in sorted(reasons.items())
    )


def _parse_window(window: Mapping[str, Any]) -> tuple[datetime, datetime]:
    try:
        return window_bounds(window)
    except ValueError as exc:
        raise GitHubError(
            "活动窗口缺少合法边界",
            source="pr_detail.window",
        ) from exc
