"""Agent selection validation and the incremental deep-read protocol."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .collectors.base import FATAL_ERRORS, CollectContext, error_code
from .collectors.discussions import ProposalNotFound, read_discussion
from .collectors.entity_store import detail_path as pr_detail_path
from .collectors.pr_store import load_pr_documents
from .collectors.proposal_store import (
    detail_path as proposal_detail_path,
)
from .collectors.proposal_store import (
    load_discussion_documents,
    load_issue_documents,
    project_proposal_read,
    write_discussion_document,
    write_issue_document,
)
from .collectors.proposals import _read_issue
from .collectors.pull_requests import _persist_pr, _read_pr
from .errors import (
    BudgetExceededError,
    GitHubError,
    StoreError,
    ValidationFailure,
)
from .models import canonical_json, validate_document
from .store import SnapshotStore

SELECTION_PATH = "analysis/selection.json"
REPORT_PATH = "analysis/selection-report.json"
FETCHABLE_KINDS = frozenset({"pull_request", "issue", "discussion"})
RECORDED_KINDS = frozenset({"release", "doc"})
SUPPORTED_KINDS = FETCHABLE_KINDS | RECORDED_KINDS


def load_agent_selection(
    store: Any,
    manifest: Mapping[str, Any],
    *,
    path: str | Path | None = None,
    entities: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Load and validate Agent-owned selection.json without modifying it."""
    selection_path = (
        store.secure_path(SELECTION_PATH) if path is None else Path(path)
    )
    if not selection_path.is_file():
        if path is not None:
            raise ValidationFailure(
                f"selection 文件不存在：{selection_path}",
                source="selection.load",
                recovery="先按 pack-index 写出 analysis/selection.json",
                details={"path": str(selection_path)},
            )
        return None
    try:
        document = json.loads(selection_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationFailure(
            "selection.json 不是合法 JSON",
            source="selection.load",
            details={"path": str(selection_path), "reason": str(exc)},
        ) from exc
    validate_document(document, "selection")
    run_id = str(manifest.get("run_id") or store.run_dir.name)
    if document.get("run_id") != run_id:
        raise ValidationFailure(
            "selection.run_id 与当前 run 不一致",
            source="selection.run_id",
            details={
                "selection_run_id": document.get("run_id"),
                "run_id": run_id,
            },
            recovery="为当前 run 重新生成 selection.json",
        )
    materialized_entities = (
        entities
        if entities is not None
        else store.read_jsonl("normalized/entities.jsonl")
    )
    known = {
        str(entity["id"])
        for entity in materialized_entities
        if isinstance(entity, Mapping) and isinstance(entity.get("id"), str)
    }
    seen: set[str] = set()
    duplicates: list[str] = []
    missing: list[str] = []
    for item in document.get("entities") or []:
        identifier = str(item["entity_id"])
        if identifier in seen:
            duplicates.append(identifier)
        seen.add(identifier)
        if identifier not in known:
            missing.append(identifier)
    if duplicates or missing:
        raise ValidationFailure(
            "selection 引用了重复或不存在的实体",
            source="selection.entities",
            details={
                "duplicates": sorted(set(duplicates)),
                "missing": sorted(set(missing)),
            },
            recovery="只选择 normalized/entities.jsonl 中存在且唯一的实体",
        )
    return document


def audit_selection_protocol(
    store: Any,
    *,
    manifest: Mapping[str, Any] | None,
    entities: list[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if store.secure_path(SELECTION_PATH).is_file():
        try:
            load_agent_selection(
                store,
                manifest or {},
                entities=entities,
            )
        except ValidationFailure as exc:
            problems.append(
                {
                    "artifact": SELECTION_PATH,
                    "problem": "invalid_selection",
                    "message": exc.message,
                    "details": exc.details,
                }
            )
    report_path = store.secure_path(REPORT_PATH)
    if report_path.is_file():
        try:
            report = store.read_json(REPORT_PATH)
            validate_document(report, "selection_report")
        except Exception as exc:
            problems.append(
                {
                    "artifact": REPORT_PATH,
                    "problem": "invalid_selection_report",
                    "message": str(getattr(exc, "message", exc)),
                }
            )
            return problems
        entity_ids = {
            str(entity.get("id"))
            for entity in entities or []
            if isinstance(entity, Mapping) and isinstance(entity.get("id"), str)
        }
        items = report.get("items") or []
        if len(items) != int(report.get("counts", {}).get("selected", -1)):
            problems.append(
                {
                    "artifact": REPORT_PATH,
                    "problem": "selection_report_count_mismatch",
                    "message": "selection-report.counts.selected 与 items 数量不一致",
                }
            )
        for item in items:
            identifier = str(item.get("entity_id"))
            if entity_ids and identifier not in entity_ids:
                problems.append(
                    {
                        "artifact": REPORT_PATH,
                        "entity_id": identifier,
                        "problem": "missing_selection_entity",
                        "message": "selection-report 引用了不存在的实体",
                    }
                )
    return problems


_NON_MATERIAL_FIELDS = frozenset({"observed_at", "selection_reason", "role_hints"})


def _material_equal(
    existing: Mapping[str, Any] | None,
    document: Mapping[str, Any],
) -> bool:
    """Compare re-read content without collector-level coverage gates."""
    if not isinstance(existing, Mapping):
        return False
    return _material_key(existing) == _material_key(document)


def _material_key(document: Mapping[str, Any]) -> str:
    return canonical_json(
        {
            key: value
            for key, value in document.items()
            if key not in _NON_MATERIAL_FIELDS
        }
    )


def collect_selection(
    store: SnapshotStore,
    client: Any,
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
    selection: Mapping[str, Any],
    *,
    selection_path: Path,
    clock: Any = None,
) -> dict[str, Any]:
    """Fetch only selected, not-yet-read PR/Issue/Discussion objects."""
    now = clock or _now_iso
    observed_at = now()
    repository = config["repository"]
    host = str(repository["host"])
    owner = str(repository["owner"])
    name = str(repository["name"])
    max_entities = int(config["budget"].get("max_deep_entities", 0))
    max_prs = int(config["budget"].get("max_deep_prs", 0))
    deep_before = int(manifest.get("budget_consumed", {}).get("deep_entities", 0))
    pr_documents = load_pr_documents(store)
    issue_documents = load_issue_documents(store)
    discussion_documents = load_discussion_documents(store)
    pr_by_number = {int(document["number"]): document for document in pr_documents}
    issue_by_number = {
        int(document["number"]): document for document in issue_documents
    }
    discussion_by_number = {
        int(document["number"]): document for document in discussion_documents
    }
    history = _selection_history(store)
    deep_prs_before = len(pr_by_number)
    entities = {
        str(entity["id"]): entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if isinstance(entity, Mapping) and isinstance(entity.get("id"), str)
    }
    context = CollectContext(
        client=client,
        store=store,
        repository=repository,
        clock=now,
    )
    items: list[dict[str, Any]] = []
    counts = {
        "selected": 0,
        "fetched": 0,
        "skipped_unchanged": 0,
        "recorded": 0,
        "not_attempted": 0,
        "failed": 0,
        "skipped_unsupported": 0,
        "pages": 0,
        "requests": 0,
    }
    used_entities = deep_before
    used_prs = deep_prs_before
    budget_exhausted = False
    fatal_error: BaseException | None = None
    for selection_item in selection.get("entities") or []:
        counts["selected"] += 1
        identifier = str(selection_item["entity_id"])
        entity = entities[identifier]
        kind = str(entity.get("kind"))
        read = dict(selection_item.get("read") or {})
        request_before = int(client.usage.get("requests", 0))
        pages = 0
        status = "skipped_unsupported"
        skipped_reason: str | None = "unsupported_kind"
        detail: str | None = None
        consumed = False
        try:
            if kind in RECORDED_KINDS:
                status = "recorded"
                skipped_reason = "not_refetched"
                detail = _recorded_path(entity)
            elif kind not in FETCHABLE_KINDS:
                status = "skipped_unsupported"
                skipped_reason = "unsupported_kind"
            elif entity.get("external") is True:
                status = "skipped_unsupported"
                skipped_reason = "external_entity"
            elif _already_read(
                kind,
                read,
                _existing_document(
                    kind,
                    entity,
                    pr_by_number=pr_by_number,
                    issue_by_number=issue_by_number,
                    discussion_by_number=discussion_by_number,
                ),
                history.get(identifier),
            ):
                status = "skipped_unchanged"
                skipped_reason = "already_read"
                detail = _detail_path_for(kind, entity)
            elif budget_exhausted or used_entities >= max_entities:
                status = "not_attempted"
                skipped_reason = "deep_entity_budget_exhausted"
                budget_exhausted = True
            elif kind == "pull_request" and used_prs >= max_prs:
                status = "not_attempted"
                skipped_reason = "deep_pr_budget_exhausted"
            elif kind == "pull_request":
                existing_document = pr_by_number.get(int(entity["number"]))
                document, pages, _ = _fetch_pr(
                    client,
                    store,
                    host,
                    owner,
                    name,
                    int(entity["number"]),
                    read=read,
                    observed_at=observed_at,
                    existing=existing_document,
                )
                changed = not _material_equal(existing_document, document)
                detail = pr_detail_path(int(entity["number"]))
                if changed:
                    _persist_pr(store, document)
                    status = "fetched"
                    skipped_reason = None
                    consumed = existing_document is None
                    if consumed:
                        used_entities += 1
                        used_prs += 1
                        client.usage["deep_entities"] += 1
                else:
                    status = "skipped_unchanged"
                    skipped_reason = "materials_unchanged"
                    consumed = False
            elif kind == "issue":
                existing_document = issue_by_number.get(int(entity["number"]))
                document, pages, _ = _fetch_issue(
                    context,
                    entity,
                    read=read,
                    observed_at=observed_at,
                    existing=existing_document,
                )
                changed = not _material_equal(existing_document, document)
                detail = proposal_detail_path("issue", int(entity["number"]))
                if changed:
                    write_issue_document(store, document)
                    project_proposal_read(
                        store, host, owner, name, "issue", document
                    )
                    status = "fetched"
                    skipped_reason = None
                    consumed = existing_document is None
                    if consumed:
                        used_entities += 1
                        client.usage["deep_entities"] += 1
                else:
                    status = "skipped_unchanged"
                    skipped_reason = "materials_unchanged"
                    consumed = False
            else:
                existing_document = discussion_by_number.get(int(entity["number"]))
                document, pages, _ = _fetch_discussion(
                    context,
                    entity,
                    read=read,
                    observed_at=observed_at,
                    existing=existing_document,
                )
                changed = not _material_equal(existing_document, document)
                detail = proposal_detail_path("discussion", int(entity["number"]))
                if changed:
                    write_discussion_document(store, document)
                    project_proposal_read(
                        store, host, owner, name, "discussion", document
                    )
                    status = "fetched"
                    skipped_reason = None
                    consumed = existing_document is None
                    if consumed:
                        used_entities += 1
                        client.usage["deep_entities"] += 1
                else:
                    status = "skipped_unchanged"
                    skipped_reason = "materials_unchanged"
                    consumed = False
        except BudgetExceededError as exc:
            status = "not_attempted"
            skipped_reason = error_code(exc, "budget_exceeded")
            budget_exhausted = True
            if fatal_error is None:
                fatal_error = exc
        except ProposalNotFound as exc:
            status = "failed"
            skipped_reason = error_code(exc, "proposal_not_found")
        except FATAL_ERRORS as exc:
            status = "failed"
            skipped_reason = error_code(exc, "github_error")
            if fatal_error is None:
                fatal_error = exc
        except GitHubError as exc:
            status = "failed"
            skipped_reason = error_code(exc, "github_error")
        except (StoreError, ValidationFailure):
            status = "failed"
            skipped_reason = "store_error"
            raise
        request_after = int(client.usage.get("requests", 0))
        request_count = max(0, request_after - request_before)
        counts["requests"] += request_count
        counts["pages"] += pages
        counts[status] += 1
        items.append(
            {
                "entity_id": identifier,
                "kind": kind,
                "depth": str(selection_item["depth"]),
                "read": read,
                "status": status,
                "skipped_reason": skipped_reason,
                "pages": pages,
                "requests": request_count,
                "detail_path": detail,
                "consumed_deep_entity": consumed,
                "budget_after": {
                    "deep_entities": used_entities,
                    "deep_prs": used_prs,
                },
            }
        )
    partial = any(
        item["status"] in ("not_attempted", "failed")
        for item in items
    )
    counts["selected"] = len(items)
    limitations = {"observed_current_state"}
    if partial:
        limitations.add("selection_partial")
    if budget_exhausted:
        limitations.add("budget_exhausted")
    if any(
        item["skipped_reason"] == "deep_pr_budget_exhausted" for item in items
    ):
        limitations.add("deep_pr_budget_exhausted")
    if any(item["status"] == "skipped_unsupported" for item in items):
        limitations.add("unsupported_selection_kind")
    if any(item["status"] == "skipped_unchanged" for item in items):
        limitations.add("selection_already_read")
    report = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "run_id": str(manifest.get("run_id") or store.run_dir.name),
        "repository": {"host": host, "owner": owner, "name": name},
        "selection_path": _display_path(store, selection_path),
        "budget": {
            "max_deep_entities": max_entities,
            "max_deep_prs": max_prs,
            "deep_entities_before": deep_before,
            "deep_entities_after": used_entities,
            "deep_prs_before": deep_prs_before,
            "deep_prs_after": used_prs,
            "requests": counts["requests"],
            "pages": counts["pages"],
            "partial": partial,
        },
        "items": items,
        "counts": counts,
        "partial": partial,
        "limitations": sorted(limitations),
    }
    validate_document(report, "selection_report")
    store.write_json(REPORT_PATH, report, schema_name="selection_report")
    if fatal_error is not None and not isinstance(fatal_error, BudgetExceededError):
        raise fatal_error
    return report


def _fetch_pr(
    client: Any,
    store: SnapshotStore,
    host: str,
    owner: str,
    name: str,
    number: int,
    *,
    read: Mapping[str, Any],
    observed_at: str,
    existing: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], int, bool]:
    result = _read_pr(
        client,
        store,
        host,
        owner,
        name,
        number,
        existing=existing,
        force=False,
        selection_reason=["agent_selection"],
        observed_at=observed_at,
        read=read,
    )
    return result.document, result.pages, result.changed


def _fetch_issue(
    context: CollectContext,
    entity: Mapping[str, Any],
    *,
    read: Mapping[str, Any],
    observed_at: str,
    existing: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], int, bool]:
    normalized = entity.get("normalized_state")
    state = normalized if isinstance(normalized, Mapping) else {}
    result = _read_issue(
        context,
        int(entity["number"]),
        scope=str(state.get("scope") or "in_window"),
        selection_reason=["agent_selection"],
        role_hints=[
            str(value)
            for value in state.get("role_hints") or []
            if isinstance(value, str)
        ],
        observed_at=observed_at,
        read=read,
        existing=existing,
    )
    return result.document, result.pages, result.changed


def _fetch_discussion(
    context: CollectContext,
    entity: Mapping[str, Any],
    *,
    read: Mapping[str, Any],
    observed_at: str,
    existing: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], int, bool]:
    normalized = entity.get("normalized_state")
    state = normalized if isinstance(normalized, Mapping) else {}
    result = read_discussion(
        context,
        int(entity["number"]),
        scope=str(state.get("scope") or "in_window"),
        selection_reason=["agent_selection"],
        role_hints=[
            str(value)
            for value in state.get("role_hints") or []
            if isinstance(value, str)
        ],
        observed_at=observed_at,
        read=read,
        existing=existing,
    )
    return result.document, result.pages, result.changed


def _selection_history(store: SnapshotStore) -> dict[str, Mapping[str, Any]]:
    try:
        report = store.read_json(REPORT_PATH)
    except StoreError:
        return {}
    if not isinstance(report, Mapping):
        return {}
    return {
        str(item["entity_id"]): item
        for item in report.get("items") or []
        if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str)
    }


def _existing_document(
    kind: str,
    entity: Mapping[str, Any],
    *,
    pr_by_number: Mapping[int, Mapping[str, Any]],
    issue_by_number: Mapping[int, Mapping[str, Any]],
    discussion_by_number: Mapping[int, Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    number = entity.get("number")
    if not isinstance(number, int):
        return None
    if kind == "pull_request":
        return pr_by_number.get(number)
    if kind == "issue":
        return issue_by_number.get(number)
    if kind == "discussion":
        return discussion_by_number.get(number)
    return None


def _already_read(
    kind: str,
    read: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    history: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(existing, Mapping):
        return False
    if kind == "pull_request":
        if read.get("body", True) and not _pr_body_read(existing, history):
            return False
        for section, flag in (
            ("files", "files"),
            ("issue_comments", "comments"),
            ("reviews", "reviews"),
            ("review_comments", "comments"),
        ):
            if read.get(flag, True) and not _section_read(existing, section):
                return False
        return True
    if kind == "issue":
        if read.get("body", True) and not _coverage_read(existing, "body"):
            return False
        return not read.get("comments", True) or _section_read(existing, "comments")
    if kind == "discussion":
        if read.get("body", True) and not _coverage_read(existing, "body"):
            return False
        return not read.get("comments", True) or _section_read(existing, "comments")
    return False


def _pr_body_read(
    existing: Mapping[str, Any],
    history: Mapping[str, Any] | None,
) -> bool:
    if _body_present(existing):
        return True
    if isinstance(history, Mapping) and history.get("status") in (
        "fetched",
        "skipped_unchanged",
    ):
        read = history.get("read")
        if isinstance(read, Mapping):
            return read.get("body") is True
    return False


def _body_present(existing: Mapping[str, Any]) -> bool:
    body = existing.get("body")
    if not isinstance(body, Mapping):
        return False
    return bool(body.get("sha256") is not None or body.get("bytes") or body.get("preview"))


def _section_read(existing: Mapping[str, Any], name: str) -> bool:
    section = existing.get(name)
    if not isinstance(section, Mapping):
        return False
    return section.get("status") in ("complete", "not_applicable")


def _coverage_read(existing: Mapping[str, Any], name: str) -> bool:
    coverage = existing.get("coverage")
    if not isinstance(coverage, Mapping):
        return False
    return coverage.get(name) in ("complete", "not_applicable")


def _detail_path_for(kind: str, entity: Mapping[str, Any]) -> str | None:
    number = entity.get("number")
    if not isinstance(number, int):
        return None
    if kind == "pull_request":
        return pr_detail_path(number)
    if kind in ("issue", "discussion"):
        return proposal_detail_path(kind, number)
    return None


def _recorded_path(entity: Mapping[str, Any]) -> str | None:
    for key in ("raw_state", "normalized_state"):
        state = entity.get(key)
        if not isinstance(state, Mapping):
            continue
        value = state.get("detail_path")
        if isinstance(value, str):
            return value
    return None


def _display_path(store: SnapshotStore, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(store.run_dir))
    except ValueError:
        return str(path)


def _now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


__all__ = [
    "REPORT_PATH",
    "SELECTION_PATH",
    "SUPPORTED_KINDS",
    "audit_selection_protocol",
    "collect_selection",
    "load_agent_selection",
]
