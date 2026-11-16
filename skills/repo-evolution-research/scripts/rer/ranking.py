"""Deterministic candidate ranking for the next deep-read round.

The module is offline: it only reads collect/prepare artifacts and writes a
single derived index. Scores are explicit heuristics with visible components;
there is no probability, confidence, or other normalized score.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from . import SCHEMA_VERSION
from .collectors.docs import doc_path
from .collectors.entity_store import detail_path as pr_detail_path
from .collectors.pr_store import load_pr_documents
from .collectors.proposal_store import detail_path as proposal_detail_path
from .collectors.releases import release_path
from .collectors.values import labels as raw_labels
from .collectors.values import merged_at, parse_datetime
from .diversity import diverse_order, title_area
from .errors import StoreError
from .models import validate_document
from .relation_policy import collapse_relations_by_identity

CANDIDATES_PATH = "analysis/candidates.json"
RULE_VERSION = "candidates-v1"
DESIGN_ROLES = frozenset({"rfc", "design", "roadmap", "architecture"})
DOC_ROLES = frozenset({"release_notes", "documentation"})
REFERENCE_QUOTAS: tuple[tuple[str, int], ...] = (
    ("design", 8),
    ("merged_pr", 14),
    ("open_proposal", 8),
    ("release_doc", 6),
    ("exploration", 4),
)
REFERENCE_TOTAL = sum(value for _lane, value in REFERENCE_QUOTAS)
RECENT_DAYS = 30
OLDER_DAYS = 90
CANDIDATE_MULTIPLIER = 8
MIN_CANDIDATE_LIMIT = 50
_RELEASE_RELATIONS = frozenset({"release_note_mentions", "verified_in_release"})
_SOURCE_REASONS = {
    "pr_selection": "from_pr_selection",
    "proposal_selection": "from_proposal_selection",
    "entity_index": "from_entity_index",
    "activity_overview": "from_activity_overview",
    "relation_neighbor": "from_relation_neighbor",
    "agent_selection": "from_agent_selection",
}
_KIND_ORDER = {
    "pull_request": 0,
    "issue": 1,
    "discussion": 2,
    "release": 3,
    "doc": 4,
}
_CANDIDATE_KINDS = frozenset(
    {"pull_request", "issue", "discussion", "release", "doc"}
)


def candidate_id(rule_version: str, entity_identifier: str) -> str:
    material = f"{rule_version}|{entity_identifier}".encode()
    return f"cand-{hashlib.sha256(material).hexdigest()[:16]}"


def build_candidates(
    store: Any,
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    evidence: Iterable[Mapping[str, Any]] | None = None,
    relations: Iterable[Mapping[str, Any]] | None = None,
    agent_selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the candidate index from authoritative artifacts."""
    repository = config["repository"]
    host = str(repository["host"])
    owner = str(repository["owner"])
    name = str(repository["name"])
    budget = config["budget"]
    max_deep_entities = int(budget.get("max_deep_entities", 0))
    max_relations = int(budget.get("max_relation_hops", 0))
    used_deep_entities = int(manifest.get("budget_consumed", {}).get("deep_entities", 0))
    remaining = max(0, max_deep_entities - used_deep_entities)
    candidate_limit = max(
        MIN_CANDIDATE_LIMIT,
        max_deep_entities * CANDIDATE_MULTIPLIER,
    )

    entities = {
        str(entity["id"]): entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if isinstance(entity, Mapping) and isinstance(entity.get("id"), str)
    }
    evidence_by_entity: dict[str, list[str]] = defaultdict(list)
    for item in evidence if evidence is not None else store.read_jsonl(
        "normalized/evidence.jsonl"
    ):
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("entity_id")
        evidence_identifier = item.get("id")
        if isinstance(identifier, str) and isinstance(evidence_identifier, str):
            evidence_by_entity[identifier].append(evidence_identifier)
    relation_items = collapse_relations_by_identity(
        relations
        if relations is not None
        else store.read_jsonl("normalized/relations.jsonl")
    )
    relation_counts: dict[str, int] = defaultdict(int)
    relation_types: dict[str, set[str]] = defaultdict(set)
    relation_evidence: dict[str, set[str]] = defaultdict(set)
    adjacency: dict[str, set[str]] = defaultdict(set)
    for relation in relation_items:
        if relation.get("verification") == "disputed":
            continue
        relation_type = str(relation.get("type") or "")
        if relation_type == "related_candidate":
            continue
        from_id = str(relation.get("from_id") or "")
        to_id = str(relation.get("to_id") or "")
        if not from_id or not to_id or from_id == to_id:
            continue
        adjacency[from_id].add(to_id)
        adjacency[to_id].add(from_id)
        for identifier in (from_id, to_id):
            relation_counts[identifier] += 1
            relation_types[identifier].add(relation_type)
            relation_evidence[identifier].update(
                str(value)
                for value in relation.get("evidence_ids") or []
                if isinstance(value, str)
            )

    pr_documents = {
        int(document["number"]): document for document in load_pr_documents(store)
    }
    raw_prs = {
        int(record["number"]): record
        for record in store.read_jsonl("raw/prs.jsonl")
        if isinstance(record, Mapping)
        and isinstance(record.get("number"), int)
        and not isinstance(record.get("number"), bool)
    }
    raw_issues = _raw_by_number(store, "raw/issues.jsonl")
    raw_discussions = _raw_by_number(store, "raw/discussions.jsonl")
    pr_selection = _read_json_or_none(store, "analysis/pr-selection.json")
    proposal_selection = _read_json_or_none(store, "analysis/proposal-selection.json")
    overview = _read_json_or_none(store, "analysis/activity-overview.json")
    selection_items = _selection_index(agent_selection)
    max_deep_prs = int(budget.get("max_deep_prs", 0))
    deep_prs_used = len(pr_documents)
    remaining_prs = max(0, max_deep_prs - deep_prs_used)

    drafts: dict[str, dict[str, Any]] = {}
    missing_entities = 0
    missing_activity_entities = 0

    def draft(identifier: str, source: str) -> dict[str, Any] | None:
        entity = entities.get(identifier)
        if entity is None or entity.get("kind") not in _CANDIDATE_KINDS:
            return None
        record = drafts.get(identifier)
        if record is None:
            record = {
                "entity": entity,
                "sources": set(),
                "raw_refs": set(),
                "reason_hints": set(),
                "bucket": _bucket_for(entity),
                "module": "",
                "module_source": "unknown",
            }
            drafts[identifier] = record
        record["sources"].add(source)
        return record

    for candidate in _candidates(pr_selection):
        identifier = candidate.get("entity_id")
        if not isinstance(identifier, str):
            continue
        record = draft(identifier, "pr_selection")
        if record is None:
            missing_entities += 1
            continue
        number = _number_from_id(identifier)
        raw = raw_prs.get(number or -1)
        if raw is not None:
            record["reason_hints"].update(
                str(value)
                for value in candidate.get("selection_reason") or []
                if isinstance(value, str)
            )
            _add_raw_ref(store, record, "raw/prs.jsonl")
        if number is not None and store.secure_path(pr_detail_path(number)).is_file():
            _add_raw_ref(store, record, pr_detail_path(number))

    for candidate in _candidates(proposal_selection):
        identifier = candidate.get("entity_id")
        if not isinstance(identifier, str):
            continue
        record = draft(identifier, "proposal_selection")
        if record is None:
            missing_entities += 1
            continue
        carrier = str(candidate.get("carrier") or "")
        number = _number_from_id(identifier)
        if carrier in ("issue", "discussion") and number is not None:
            stream = f"raw/{carrier}s.jsonl"
            _add_raw_ref(store, record, stream)
            if store.secure_path(proposal_detail_path(carrier, number)).is_file():
                _add_raw_ref(store, record, proposal_detail_path(carrier, number))
        record["reason_hints"].update(
            str(value)
            for value in candidate.get("selection_reason") or []
            if isinstance(value, str)
        )

    for identifier, entity in sorted(entities.items()):
        kind = entity.get("kind")
        roles = {str(value) for value in entity.get("roles") or []}
        if kind not in ("release", "doc") and not roles & DESIGN_ROLES:
            continue
        record = draft(identifier, "entity_index")
        if record is None:
            continue
        _add_entity_raw_ref(store, record, entity)

    for identifier, entity in sorted(entities.items()):
        if not _deep_read(entity) or entity.get("kind") not in _CANDIDATE_KINDS:
            continue
        record = draft(identifier, "entity_index")
        if record is not None:
            _add_entity_raw_ref(store, record, entity)

    for identifier in _overview_ids(overview):
        record = draft(identifier, "activity_overview")
        if record is None:
            missing_activity_entities += 1

    for identifier in sorted(selection_items):
        record = draft(identifier, "agent_selection")
        if record is None:
            continue
        _add_entity_raw_ref(store, record, entities[identifier])

    source_drafts = list(drafts.values())
    _enrich_drafts(
        source_drafts,
        pr_documents=pr_documents,
        raw_prs=raw_prs,
        raw_issues=raw_issues,
        raw_discussions=raw_discussions,
    )
    covered_modules = _covered_modules(entities, pr_documents)
    _infer_modules(source_drafts, pr_documents, raw_prs, raw_issues, raw_discussions)
    for record in source_drafts:
        record["score"], record["components"] = _score(
            record,
            relation_counts=relation_counts,
            relation_types=relation_types,
            covered_modules=covered_modules,
            focus=[str(value) for value in config.get("focus", [])],
            window_end=_window_end(config, manifest),
        )
    source_ids = {str(record["entity"]["id"]) for record in source_drafts}
    selected_ids = set(selection_items)
    deep_ids = {
        str(record["entity"]["id"])
        for record in source_drafts
        if _deep_read(record["entity"])
    }
    ordered = _protected_order(source_drafts, selected_ids, deep_ids)
    kept = ordered[:candidate_limit]
    kept_ids = {str(record["entity"]["id"]) for record in kept}
    relation_expanded = 0
    relation_omitted = 0
    external_expanded = 0
    if max_relations >= 1:
        frontier = sorted(kept_ids)
        for identifier in frontier:
            for neighbor in sorted(adjacency.get(identifier, ())):
                if neighbor in kept_ids or neighbor not in entities:
                    continue
                external = entities[neighbor].get("external") is True
                if external and external_expanded >= int(budget["max_external_entities"]):
                    relation_omitted += 1
                    continue
                record = draft(neighbor, "relation_neighbor")
                if record is None:
                    continue
                if len(kept_ids) >= candidate_limit:
                    relation_omitted += 1
                    drafts.pop(neighbor, None)
                    continue
                record["reason_hints"].add("relation_neighbor")
                kept_ids.add(neighbor)
                relation_expanded += 1
                external_expanded += int(external)
        kept = [drafts[identifier] for identifier in kept_ids if identifier in drafts]
        _enrich_drafts(
            kept,
            pr_documents=pr_documents,
            raw_prs=raw_prs,
            raw_issues=raw_issues,
            raw_discussions=raw_discussions,
        )
        _infer_modules(kept, pr_documents, raw_prs, raw_issues, raw_discussions)
        for record in kept:
            record["score"], record["components"] = _score(
                record,
                relation_counts=relation_counts,
                relation_types=relation_types,
                covered_modules=covered_modules,
                focus=[str(value) for value in config.get("focus", [])],
                window_end=_window_end(config, manifest),
            )
        before_final = {str(record["entity"]["id"]) for record in kept}
        kept = _protected_order(kept, selected_ids, deep_ids)[:candidate_limit]
        final_ids = {str(record["entity"]["id"]) for record in kept}
        relation_omitted += sum(
            1 for identifier in before_final - final_ids if identifier not in source_ids
        )

    source_kept = sum(
        1 for record in kept if str(record["entity"]["id"]) in source_ids
    )
    omitted = max(0, len(source_drafts) - source_kept)
    selected_omitted = sum(
        1
        for identifier in selected_ids
        if identifier not in {str(record["entity"]["id"]) for record in kept}
    )
    deep_read_omitted = sum(
        1
        for identifier in deep_ids
        if identifier not in {str(record["entity"]["id"]) for record in kept}
    )
    quota, lanes, exploration_reserved, focus_cap = _quota_plan(
        remaining,
        focus_configured=bool(config.get("focus")),
    )
    recommended, lane_for, skipped = _select_recommended(
        kept,
        quota=quota,
        lanes=lanes,
        exploration_reserved=exploration_reserved,
        focus_cap=focus_cap,
        focus_configured=bool(config.get("focus")),
        remaining_prs=remaining_prs,
    )
    record_list = [
        _candidate_record(
            record,
            evidence_by_entity=evidence_by_entity,
            relation_types=relation_types,
            relation_evidence=relation_evidence,
            agent=selection_items.get(str(record["entity"]["id"])),
            recommended=str(record["entity"]["id"]) in recommended,
            lane=lane_for.get(str(record["entity"]["id"])),
            skipped_reason=skipped.get(str(record["entity"]["id"])),
            budget_fit=_budget_fit(
                record,
                remaining_entities=remaining,
                remaining_prs=remaining_prs,
                deep_read=_deep_read(record["entity"]),
            ),
        )
        for record in kept
    ]
    record_list.sort(
        key=lambda item: (
            -int(item["score"]),
            _KIND_ORDER.get(str(item["kind"]), 99),
            str(item["entity_id"]),
        )
    )
    counts = _counts(
        record_list,
        total_sources=len(source_drafts) + relation_expanded,
        omitted=omitted,
        relation_expanded=relation_expanded,
        relation_omitted=relation_omitted,
        missing_entities=missing_entities,
        missing_activity_entities=missing_activity_entities,
        agent_selected_omitted=selected_omitted,
        deep_read_omitted=deep_read_omitted,
    )
    limitations = _limitations(
        record_list,
        quota=quota,
        omitted=omitted,
        relation_omitted=relation_omitted,
        remaining=remaining,
        focus_cap_applied=counts["focus_skipped"] > 0,
        agent_selected_omitted=counts["agent_selected_omitted"],
        deep_read_omitted=counts["deep_read_omitted"],
    )
    observed_at = str(
        manifest.get("collection_finished_at")
        or manifest.get("observed", {}).get("finished_at")
        or manifest.get("created_at")
        or ""
    )
    document = {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "observed_at": observed_at,
        "repository": {"host": host, "owner": owner, "name": name},
        "activity_window": {
            "start": config["window"]["start"],
            "end": config["window"]["end"],
            "timezone": config["window"]["timezone"],
        },
        "budget": {
            "max_deep_entities": max_deep_entities,
            "deep_entities_used": used_deep_entities,
            "remaining_deep_entities": remaining,
            "candidate_limit": candidate_limit,
            "max_relation_hops": max_relations,
            "max_deep_prs": max_deep_prs,
            "deep_prs_used": deep_prs_used,
            "remaining_deep_prs": remaining_prs,
        },
        "quota": {
            "total": quota,
            "lanes": lanes,
            "exploration_reserved": exploration_reserved,
            "focus_cap": focus_cap,
        },
        "candidates": record_list,
        "counts": counts,
        "limitations": limitations,
    }
    validate_document(document, "candidates")
    return document


def write_candidates(store: Any, document: Mapping[str, Any]) -> str:
    store.write_json(CANDIDATES_PATH, document, schema_name="candidates")
    return CANDIDATES_PATH


def audit_candidates(
    store: Any,
    *,
    entities: Iterable[Mapping[str, Any]],
    evidence: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate persisted candidates against entity/evidence references."""
    path = store.secure_path(CANDIDATES_PATH)
    if not path.is_file():
        return []
    problems: list[dict[str, Any]] = []
    try:
        document = store.read_json(CANDIDATES_PATH)
        validate_document(document, "candidates")
    except Exception as exc:
        return [
            {
                "artifact": CANDIDATES_PATH,
                "problem": "invalid_candidates",
                "message": str(getattr(exc, "message", exc)),
            }
        ]
    entity_ids = {
        str(entity.get("id"))
        for entity in entities
        if isinstance(entity, Mapping) and isinstance(entity.get("id"), str)
    }
    evidence_ids = {
        str(item.get("id"))
        for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    seen: set[str] = set()
    for candidate in document.get("candidates") or []:
        identifier = str(candidate.get("candidate_id"))
        if identifier in seen:
            problems.append(
                {
                    "artifact": CANDIDATES_PATH,
                    "candidate_id": identifier,
                    "problem": "duplicate_candidate_id",
                    "message": "candidates.json 存在重复 candidate_id",
                }
            )
        seen.add(identifier)
        entity_identifier = str(candidate.get("entity_id"))
        expected = candidate_id(str(document.get("rule_version")), entity_identifier)
        if identifier != expected:
            problems.append(
                {
                    "artifact": CANDIDATES_PATH,
                    "candidate_id": identifier,
                    "problem": "candidate_id_mismatch",
                    "message": f"candidate_id 应为 {expected}",
                }
            )
        if entity_identifier not in entity_ids:
            problems.append(
                {
                    "artifact": CANDIDATES_PATH,
                    "candidate_id": identifier,
                    "entity_id": entity_identifier,
                    "problem": "missing_candidate_entity",
                    "message": "candidate.entity_id 不在 entities 中",
                }
            )
        for evidence_identifier in candidate.get("evidence_ids") or []:
            if evidence_identifier not in evidence_ids:
                problems.append(
                    {
                        "artifact": CANDIDATES_PATH,
                        "candidate_id": identifier,
                        "evidence_id": evidence_identifier,
                        "problem": "missing_candidate_evidence",
                        "message": "candidate.evidence_ids 不在 evidence 流中",
                    }
                )
        for ref in candidate.get("raw_refs") or []:
            artifact = ref.get("artifact") if isinstance(ref, Mapping) else None
            if not isinstance(artifact, str):
                continue
            if not store.secure_path(artifact).is_file():
                problems.append(
                    {
                        "artifact": CANDIDATES_PATH,
                        "candidate_id": identifier,
                        "raw_ref": artifact,
                        "problem": "missing_candidate_raw_ref",
                        "message": "candidate.raw_refs 指向不存在的文件",
                    }
                )
    return problems


def _candidates(document: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    values = document.get("candidates")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, Mapping)]


def _read_json_or_none(store: Any, relpath: str) -> Mapping[str, Any] | None:
    try:
        value = store.read_json(relpath)
    except StoreError:
        return None
    return value if isinstance(value, Mapping) else None


def _raw_by_number(store: Any, relpath: str) -> dict[int, Mapping[str, Any]]:
    result: dict[int, Mapping[str, Any]] = {}
    for record in store.read_jsonl(relpath):
        if not isinstance(record, Mapping):
            continue
        number = record.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            result[number] = record
    return result


def _number_from_id(identifier: str) -> int | None:
    tail = identifier.rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _selection_index(selection: Mapping[str, Any] | None) -> dict[str, Mapping[str, Any]]:
    if not isinstance(selection, Mapping):
        return {}
    items = selection.get("entities")
    if not isinstance(items, list):
        return {}
    return {
        str(item["entity_id"]): item
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("entity_id"), str)
    }


def _overview_ids(overview: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(overview, Mapping):
        return []
    identifiers: set[str] = set()
    views = overview.get("views")
    if not isinstance(views, list):
        return []
    for view in views:
        if not isinstance(view, Mapping):
            continue
        candidate_ids = view.get("candidate_ids")
        if isinstance(candidate_ids, list):
            identifiers.update(
                value for value in candidate_ids if isinstance(value, str)
            )
    return sorted(identifiers)


def _add_raw_ref(store: Any, record: dict[str, Any], artifact: str) -> None:
    if store.secure_path(artifact).is_file():
        record["raw_refs"].add((artifact, None))


def _add_entity_raw_ref(store: Any, record: dict[str, Any], entity: Mapping[str, Any]) -> None:
    kind = entity.get("kind")
    number = entity.get("number")
    raw_state = entity.get("raw_state")
    state = raw_state if isinstance(raw_state, Mapping) else {}
    normalized = entity.get("normalized_state")
    normalized_state = normalized if isinstance(normalized, Mapping) else {}
    if kind == "release":
        release_id = state.get("release_id")
        if isinstance(release_id, int) and not isinstance(release_id, bool):
            _add_raw_ref(store, record, release_path(release_id))
    elif kind == "doc":
        revision = state.get("revision") or normalized_state.get("revision")
        path = state.get("path") or normalized_state.get("path")
        if isinstance(revision, str) and isinstance(path, str) and revision:
            _add_raw_ref(store, record, doc_path(revision, path))
    elif kind in ("pull_request", "issue", "discussion") and isinstance(number, int):
        stream = {
            "pull_request": "raw/prs.jsonl",
            "issue": "raw/issues.jsonl",
            "discussion": "raw/discussions.jsonl",
        }[kind]
        _add_raw_ref(store, record, stream)
        if kind == "pull_request":
            _add_raw_ref(store, record, pr_detail_path(number))
        else:
            _add_raw_ref(store, record, proposal_detail_path(kind, number))


def _infer_modules(
    drafts: Iterable[dict[str, Any]],
    pr_documents: Mapping[int, Mapping[str, Any]],
    raw_prs: Mapping[int, Mapping[str, Any]],
    raw_issues: Mapping[int, Mapping[str, Any]],
    raw_discussions: Mapping[int, Mapping[str, Any]],
) -> None:
    for record in drafts:
        entity = record["entity"]
        if entity.get("external"):
            record["module"], record["module_source"] = "external", "scope"
            continue
        module, source = _module_for(
            entity,
            pr_documents=pr_documents,
            raw_prs=raw_prs,
            raw_issues=raw_issues,
            raw_discussions=raw_discussions,
        )
        record["module"] = module
        record["module_source"] = source


def _module_for(
    entity: Mapping[str, Any],
    *,
    pr_documents: Mapping[int, Mapping[str, Any]],
    raw_prs: Mapping[int, Mapping[str, Any]],
    raw_issues: Mapping[int, Mapping[str, Any]],
    raw_discussions: Mapping[int, Mapping[str, Any]],
) -> tuple[str, str]:
    kind = str(entity.get("kind") or "")
    number = entity.get("number")
    raw_state = entity.get("raw_state")
    state = raw_state if isinstance(raw_state, Mapping) else {}
    normalized = entity.get("normalized_state")
    normalized_state = normalized if isinstance(normalized, Mapping) else {}
    if kind == "pull_request" and isinstance(number, int) and not isinstance(number, bool):
        document = pr_documents.get(number)
        if document is not None:
            module = _module_from_files(document)
            if module is not None:
                return module, "pr_files"
        raw = raw_prs.get(number)
        module = _module_from_labels(_label_values(entity, raw or {}))
        if module is not None:
            return module, "labels"
        hint = title_area(str((raw or {}).get("title") or entity.get("title") or ""))
        return (hint, "title") if hint else ("unknown", "unknown")
    if kind in ("issue", "discussion"):
        raw = (raw_issues if kind == "issue" else raw_discussions).get(
            number if isinstance(number, int) else -1
        )
        module = _module_from_labels(_label_values(entity, raw or {}))
        if module is not None:
            return module, "labels"
        hints = normalized_state.get("role_hints")
        if isinstance(hints, list):
            module = _module_from_labels(hints)
            if module is not None:
                return module, "labels"
        scope = normalized_state.get("scope") or state.get("scope")
        if isinstance(scope, str) and scope:
            return scope, "scope"
        return "unknown", "unknown"
    if kind == "doc":
        path = state.get("path") or normalized_state.get("path")
        if isinstance(path, str) and path:
            return _module_from_doc_path(path), "doc_path"
        return "unknown", "unknown"
    if kind == "release":
        return "release", "release"
    return "unknown", "unknown"


def _module_from_files(document: Mapping[str, Any]) -> str | None:
    files = document.get("files")
    items = files.get("items") if isinstance(files, Mapping) else None
    if not isinstance(items, list):
        return None
    weights: dict[str, int] = defaultdict(int)
    for item in items:
        if not isinstance(item, Mapping):
            continue
        filename = item.get("filename")
        if not isinstance(filename, str) or not filename:
            continue
        module = _module_from_path(filename)
        if module == "unknown":
            continue
        additions = item.get("additions")
        deletions = item.get("deletions")
        changes = (additions if isinstance(additions, int) else 0) + (
            deletions if isinstance(deletions, int) else 0
        )
        weights[module] += max(1, changes)
    if not weights:
        return None
    return sorted(weights, key=lambda value: (-weights[value], value))[0]


def _module_from_doc_path(path: str) -> str:
    clean = path.strip().removeprefix("./").lstrip("/").replace("\\", "/")
    parts = [part for part in clean.split("/") if part]
    if not parts:
        return "unknown"
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def _module_from_path(path: str) -> str:
    clean = path.strip().removeprefix("./").lstrip("/").replace("\\", "/")
    parts = [part for part in clean.split("/") if part]
    if not parts:
        return "unknown"
    lowered = [part.casefold() for part in parts]
    if any(
        part in {"test", "tests", "testing", "docs", "doc", "examples", "example"}
        for part in lowered[:-1]
    ):
        return "unknown"
    if len(parts) == 1:
        return "unknown"
    if len(parts) >= 2 and lowered[0] in {
        "src",
        "lib",
        "pkg",
        "internal",
        "app",
        "apps",
        "packages",
        "modules",
        "components",
        "crates",
    }:
        return f"{parts[0]}/{parts[1]}"
    if lowered[0] in {"docs", "doc"}:
        return "/".join(parts[:2])
    return parts[0]


def _module_from_labels(values: Iterable[str]) -> str | None:
    for value in values:
        text = str(value).strip()
        lowered = text.casefold()
        for prefix in ("area:", "area/", "area-", "module:", "module/", "component:"):
            if lowered.startswith(prefix):
                candidate = text[len(prefix) :].strip()
                if candidate:
                    return candidate
        for prefix in ("area ", "module ", "component "):
            if lowered.startswith(prefix):
                candidate = text[len(prefix) :].strip()
                if candidate:
                    return candidate
    return None


def _enrich_drafts(
    drafts: Iterable[dict[str, Any]],
    *,
    pr_documents: Mapping[int, Mapping[str, Any]],
    raw_prs: Mapping[int, Mapping[str, Any]],
    raw_issues: Mapping[int, Mapping[str, Any]],
    raw_discussions: Mapping[int, Mapping[str, Any]],
) -> None:
    for record in drafts:
        entity = record["entity"]
        if entity.get("external"):
            record.update(merged=False, draft=False, activity=0, labels=[],
                          updated_at=entity.get("source_event_at"))
            record["reason_hints"].add("external_reference_only")
            continue
        kind = str(entity.get("kind") or "")
        number = entity.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            number = -1
        raw: Mapping[str, Any] = {}
        if kind == "pull_request":
            raw = raw_prs.get(number, {})
            document = pr_documents.get(number)
            merged = bool(
                (document or {}).get("merged")
                or merged_at(document or {})
                or merged_at(raw)
                or (entity.get("raw_state") or {}).get("merged")
                or (entity.get("normalized_state") or {}).get("merged")
            )
            draft = bool(
                (document or {}).get("draft")
                or raw.get("draft")
                or (entity.get("raw_state") or {}).get("draft")
            )
        elif kind == "issue":
            raw = raw_issues.get(number, {})
            merged = False
            draft = False
        elif kind == "discussion":
            raw = raw_discussions.get(number, {})
            merged = False
            draft = False
        else:
            merged = False
            draft = False
        record["merged"] = merged
        record["draft"] = draft
        record["activity"] = _activity_total(entity, raw)
        record["labels"] = _label_values(entity, raw)
        record["updated_at"] = (
            raw.get("updated_at")
            or entity.get("updated_at")
            or entity.get("source_event_at")
        )
        if kind == "pull_request":
            state = str(
                (pr_documents.get(number) or {}).get("state")
                or raw.get("state")
                or (entity.get("raw_state") or {}).get("state")
                or ""
            ).casefold()
            if not merged and state == "closed":
                record["bucket"] = "closed_unmerged"
            elif record.get("bucket") != "design":
                record["bucket"] = "merged_pr" if merged else "open_proposal"


def _activity_total(entity: Mapping[str, Any], raw: Mapping[str, Any]) -> int:
    raw_state = entity.get("raw_state")
    state = raw_state if isinstance(raw_state, Mapping) else {}
    merged = dict(state)
    merged.update({key: value for key, value in raw.items() if value is not None})
    total = 0
    for key in ("comments", "comments_count", "review_comments", "upvotes"):
        value = merged.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
    reactions = merged.get("reactions")
    if isinstance(reactions, Mapping):
        value = reactions.get("total_count")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            total += value
    pull_request = merged.get("pull_request")
    if isinstance(pull_request, Mapping):
        for key in ("comments", "review_comments"):
            if merged.get(key) is not None:
                continue
            value = pull_request.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                total += value
    return total


def _label_values(entity: Mapping[str, Any], raw: Mapping[str, Any]) -> list[str]:
    values = [str(value) for value in raw_labels(raw)]
    if values:
        return values
    raw_state = entity.get("raw_state")
    state = raw_state if isinstance(raw_state, Mapping) else {}
    labels = state.get("labels")
    if isinstance(labels, list):
        for value in labels:
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, Mapping) and isinstance(value.get("name"), str):
                values.append(str(value["name"]))
    return values


def _covered_modules(
    entities: Mapping[str, Mapping[str, Any]],
    pr_documents: Mapping[int, Mapping[str, Any]],
) -> set[str]:
    covered: set[str] = set()
    for entity in entities.values():
        raw_state = entity.get("raw_state")
        state = raw_state if isinstance(raw_state, Mapping) else {}
        normalized = entity.get("normalized_state")
        normalized_state = normalized if isinstance(normalized, Mapping) else {}
        read = bool(state.get("deep_read") or state.get("proposal_read"))
        if not read:
            continue
        kind = entity.get("kind")
        number = entity.get("number")
        if kind == "pull_request" and isinstance(number, int):
            document = pr_documents.get(number)
            module = _module_from_files(document) if document is not None else None
            if module:
                covered.add(module)
        elif kind in ("issue", "discussion"):
            scope = normalized_state.get("scope")
            if isinstance(scope, str) and scope:
                covered.add(scope)
    return covered


def _score(
    record: Mapping[str, Any],
    *,
    relation_counts: Mapping[str, int],
    relation_types: Mapping[str, set[str]],
    covered_modules: set[str],
    focus: list[str],
    window_end: datetime | None,
) -> tuple[int, dict[str, int]]:
    entity = record["entity"]
    identifier = str(entity["id"])
    roles = {str(value) for value in entity.get("roles") or []}
    normalized = entity.get("normalized_state")
    normalized_state = normalized if isinstance(normalized, Mapping) else {}
    hints = {
        str(value)
        for value in normalized_state.get("role_hints") or []
        if isinstance(value, str)
    }
    components = {
        "design_role": 30
        if roles & DESIGN_ROLES or hints & DESIGN_ROLES
        else (10 if roles & DOC_ROLES else 0),
        "explicit_relation": min(3, int(relation_counts.get(identifier, 0))) * 8,
        "focus_match": 0,
        "activity": 0,
        "recency": 0,
        "release_mention": 20
        if relation_types.get(identifier, set()) & _RELEASE_RELATIONS
        else 0,
        "uncovered_module": 12
        if record.get("module") not in (None, "", "unknown")
        and record.get("module") not in covered_modules
        else 0,
        "merged": 20 if record.get("merged") else 0,
        "draft": -15 if record.get("draft") else 0,
    }
    haystack = " ".join(
        [
            str(entity.get("title") or ""),
            str(record.get("module") or ""),
            " ".join(sorted(roles | hints)),
            " ".join(record.get("labels") or []),
        ]
    ).casefold()
    if _focus_hit(focus, haystack):
        components["focus_match"] = 30
    activity = int(record.get("activity") or 0)
    if activity:
        components["activity"] = min(20, int(math.log2(1 + activity) * 4))
    updated = parse_datetime(record.get("updated_at"))
    if updated is not None and window_end is not None:
        age = window_end - updated
        if age <= timedelta(days=RECENT_DAYS):
            components["recency"] = 10
        elif age <= timedelta(days=OLDER_DAYS):
            components["recency"] = 5
        else:
            components["recency"] = 1
    return sum(components.values()), components


def _focus_hit(focus: Iterable[str], haystack: str) -> bool:
    """Match focus terms on token boundaries so short terms do not hit words."""
    for term in focus:
        normalized = " ".join(str(term).split()).casefold()
        if not normalized:
            continue
        pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
        if re.search(pattern, haystack):
            return True
    return False


def _draft_order(record: Mapping[str, Any]) -> tuple[int, int, str]:
    entity = record["entity"]
    return (
        -int(record.get("score") or 0),
        _KIND_ORDER.get(str(entity.get("kind")), 99),
        str(entity.get("id")),
    )


def _quota_plan(
    total: int,
    *,
    focus_configured: bool,
) -> tuple[int, dict[str, int], int, int]:
    if total <= 0:
        return 0, {lane: 0 for lane, _value in REFERENCE_QUOTAS}, 0, 0
    scaled: dict[str, int] = {}
    remainders: list[tuple[float, int, str]] = []
    assigned = 0
    for index, (lane, value) in enumerate(REFERENCE_QUOTAS):
        exact = value * total / REFERENCE_TOTAL
        base = int(exact)
        scaled[lane] = base
        assigned += base
        remainders.append((exact - base, -index, lane))
    for _fraction, _index, lane in sorted(remainders, reverse=True)[: total - assigned]:
        scaled[lane] += 1
    exploration = scaled["exploration"]
    minimum = max(1, math.ceil(total * 0.1)) if total >= 4 else exploration
    exploration = max(exploration, minimum)
    deficit = exploration - scaled["exploration"]
    scaled["exploration"] = exploration
    if deficit > 0:
        donors = sorted(
            ("design", "merged_pr", "open_proposal", "release_doc"),
            key=lambda lane: (-scaled[lane], lane),
        )
        for lane in donors:
            if deficit <= 0:
                break
            take = min(deficit, scaled[lane])
            scaled[lane] -= take
            deficit -= take
    focus_cap = max(1, total // 2) if focus_configured else total
    return total, scaled, exploration, focus_cap


def _protected_order(
    records: Iterable[Mapping[str, Any]],
    selected_ids: set[str],
    deep_ids: set[str],
) -> list[Mapping[str, Any]]:
    materialized = list(records)
    selected = [
        record
        for record in materialized
        if str(record["entity"]["id"]) in selected_ids
    ]
    deep = [
        record
        for record in materialized
        if str(record["entity"]["id"]) not in selected_ids
        and str(record["entity"]["id"]) in deep_ids
    ]
    rest = [
        record
        for record in materialized
        if str(record["entity"]["id"]) not in selected_ids
        and str(record["entity"]["id"]) not in deep_ids
    ]
    return [
        *sorted(selected, key=_draft_order),
        *sorted(deep, key=_draft_order),
        *sorted(rest, key=_draft_order),
    ]


def _budget_fit(
    record: Mapping[str, Any],
    *,
    remaining_entities: int,
    remaining_prs: int,
    deep_read: bool,
) -> str:
    if deep_read:
        return "deep_read"
    kind = str(record["entity"].get("kind") or "")
    if kind in ("pull_request", "issue", "discussion") and remaining_entities <= 0:
        return "blocked_entity_budget"
    if kind == "pull_request":
        return "fetchable" if remaining_prs > 0 else "blocked_pr_budget"
    if kind in ("issue", "discussion"):
        return "fetchable" if remaining_entities > 0 else "blocked_entity_budget"
    if kind in ("release", "doc"):
        return "recorded"
    return "recorded"


def _select_recommended(
    drafts: Iterable[Mapping[str, Any]],
    *,
    quota: int,
    lanes: Mapping[str, int],
    exploration_reserved: int,
    focus_cap: int,
    focus_configured: bool,
    remaining_prs: int,
) -> tuple[set[str], dict[str, str], dict[str, str]]:
    items = list(drafts)
    unread = [record for record in items
              if _deep_read(record["entity"]) is False and not record["entity"].get("external")]
    by_bucket: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in unread:
        by_bucket[str(record.get("bucket") or "open_proposal")].append(record)

    def order(record: Mapping[str, Any]) -> tuple[int, int, str]:
        return _draft_order(record)

    def identifier(record: Mapping[str, Any]) -> str:
        return str(record["entity"]["id"])

    def is_pr(record: Mapping[str, Any]) -> bool:
        return str(record["entity"].get("kind") or "") == "pull_request"

    chosen: dict[str, str] = {}
    pr_remaining = remaining_prs

    def choose(record: Mapping[str, Any], lane: str) -> bool:
        nonlocal pr_remaining
        value = identifier(record)
        if value in chosen:
            return False
        if is_pr(record) and pr_remaining <= 0:
            return False
        chosen[value] = lane
        if is_pr(record):
            pr_remaining -= 1
        return True

    if quota > 0:
        exploration_limit = exploration_reserved
        for record in sorted(
            [
                item
                for item in unread
                if int(item.get("components", {}).get("uncovered_module", 0)) > 0
            ],
            key=order,
        ):
            if len(chosen) >= quota or exploration_limit <= 0:
                break
            if choose(record, "exploration"):
                exploration_limit -= 1
        for lane in ("design", "merged_pr", "open_proposal", "release_doc"):
            limit = int(lanes.get(lane, 0))
            for record in diverse_order(
                sorted(by_bucket.get(lane, []), key=order),
                key=lambda item: str(item.get("module") or "unknown"),
                reserve=max(1, limit // 3),
            ):
                if len(chosen) >= quota or limit <= 0:
                    break
                if choose(record, lane):
                    limit -= 1
        for record in sorted(unread, key=order):
            if len(chosen) >= quota:
                break
            choose(record, str(record.get("bucket") or "open_proposal"))
        if focus_configured:
            focus_selected = [
                value
                for value in chosen
                if _focus_score(items, value) > 0
            ]
            if len(focus_selected) > focus_cap:
                demote = sorted(
                    focus_selected,
                    key=lambda value: (_draft_score(items, value), value),
                )
                for value in demote[: len(focus_selected) - focus_cap]:
                    chosen.pop(value, None)
                pr_remaining = max(
                    0,
                    remaining_prs
                    - sum(
                        1
                        for value in chosen
                        if any(
                            identifier(record) == value and is_pr(record)
                            for record in items
                        )
                    ),
                )
                for record in sorted(
                    [
                        item
                        for item in unread
                        if identifier(item) not in chosen
                        and _focus_score(items, identifier(item)) == 0
                    ],
                    key=order,
                ):
                    if len(chosen) >= quota:
                        break
                    choose(record, str(record.get("bucket") or "open_proposal"))
    recommended = set(chosen)
    skipped: dict[str, str] = {}
    for record in items:
        value = identifier(record)
        if _deep_read(record["entity"]):
            skipped[value] = "already_read"
        elif value not in recommended:
            if is_pr(record) and pr_remaining <= 0:
                skipped[value] = "deep_pr_budget_exhausted"
            elif quota == 0:
                skipped[value] = "deep_entity_budget_exhausted"
            else:
                skipped[value] = "quota_exhausted"
    return recommended, chosen, skipped


def _draft_score(drafts: Iterable[Mapping[str, Any]], identifier: str) -> int:
    for record in drafts:
        if str(record["entity"]["id"]) == identifier:
            return int(record.get("score") or 0)
    return 0


def _focus_score(drafts: Iterable[Mapping[str, Any]], identifier: str) -> int:
    for record in drafts:
        if str(record["entity"]["id"]) == identifier:
            return int(record.get("components", {}).get("focus_match", 0))
    return 0


def _candidate_record(
    record: Mapping[str, Any],
    *,
    evidence_by_entity: Mapping[str, list[str]],
    relation_types: Mapping[str, set[str]],
    relation_evidence: Mapping[str, set[str]],
    agent: Mapping[str, Any] | None,
    recommended: bool,
    lane: str | None,
    skipped_reason: str | None,
    budget_fit: str,
) -> dict[str, Any]:
    entity = record["entity"]
    identifier = str(entity["id"])
    components = dict(record.get("components") or {})
    kind = str(entity.get("kind") or "")
    deep_read = _deep_read(entity)
    evidence_ids = sorted(
        set(evidence_by_entity.get(identifier, []))
        | set(relation_evidence.get(identifier, set()))
    )
    reasons = {
        _SOURCE_REASONS[source]
        for source in record.get("sources", set())
        if source in _SOURCE_REASONS
    }
    reasons.update(str(value) for value in record.get("reason_hints", set()))
    if components.get("design_role"):
        reasons.add("design_role")
    if components.get("merged"):
        reasons.add("merged")
    if components.get("focus_match"):
        reasons.add("focus_match")
    if components.get("activity"):
        reasons.add("activity")
    if components.get("recency"):
        reasons.add("recent")
    if components.get("release_mention"):
        reasons.add("release_mention")
    if components.get("uncovered_module"):
        reasons.add("uncovered_module")
    if components.get("draft"):
        reasons.add("draft")
    if agent is not None:
        reasons.add("agent_selected")
    missing = _missing_features(
        record,
        components=components,
        deep_read=deep_read,
        evidence_ids=evidence_ids,
        relation_types=relation_types.get(identifier, set()),
        agent=agent,
    )
    raw_refs = [
        {"artifact": artifact, "json_pointer": pointer}
        for artifact, pointer in sorted(
            record.get("raw_refs", set()),
            key=lambda item: (item[0], item[1] or ""),
        )
    ]
    if recommended:
        selection_status = "recommended"
    elif deep_read:
        selection_status = "already_read"
    elif skipped_reason in ("deep_pr_budget_exhausted", "deep_entity_budget_exhausted"):
        selection_status = "skipped_sub_budget"
    else:
        selection_status = "skipped_quota"
    return {
        "candidate_id": candidate_id(RULE_VERSION, identifier),
        "rule_version": RULE_VERSION,
        "entity_id": identifier,
        "kind": kind,
        "title": str(entity.get("title") or ""),
        "bucket": str(record.get("bucket") or "open_proposal"),
        "module": str(record.get("module") or "unknown"),
        "module_source": str(record.get("module_source") or "unknown"),
        "deep_read": deep_read,
        "score": int(record.get("score") or 0),
        "score_components": {key: int(value) for key, value in sorted(components.items())},
        "selection_reason": sorted(reasons),
        "missing_features": missing,
        "raw_refs": raw_refs,
        "evidence_ids": evidence_ids,
        "recommended": recommended,
        "selection_lane": lane,
        "selection_status": selection_status,
        "skipped_reason": skipped_reason,
        "agent_selected": agent is not None,
        "agent_depth": str(agent.get("depth")) if agent is not None else None,
        "agent_read": dict(agent.get("read") or {}) if agent is not None else None,
        "budget_fit": budget_fit,
    }


def _missing_features(
    record: Mapping[str, Any],
    *,
    components: Mapping[str, int],
    deep_read: bool,
    evidence_ids: list[str],
    relation_types: set[str],
    agent: Mapping[str, Any] | None,
) -> list[str]:
    missing: list[str] = []
    if record.get("module") in (None, "", "unknown"):
        missing.append("module_unknown")
    if not deep_read and not evidence_ids:
        missing.append("no_evidence_yet")
    if not relation_types:
        missing.append("no_explicit_relation")
    if not components.get("activity"):
        missing.append("no_activity_metadata")
    if not components.get("release_mention"):
        missing.append("release_mention_absent")
    if record.get("module_source") == "scope":
        missing.append("module_from_scope")
    if agent is not None and not agent.get("read"):
        missing.append("agent_read_flags_missing")
    return sorted(missing)


def _bucket_for(entity: Mapping[str, Any]) -> str:
    kind = str(entity.get("kind") or "")
    roles = {str(value) for value in entity.get("roles") or []}
    normalized = entity.get("normalized_state")
    normalized_state = normalized if isinstance(normalized, Mapping) else {}
    hints = {
        str(value)
        for value in normalized_state.get("role_hints") or []
        if isinstance(value, str)
    }
    if roles & DESIGN_ROLES or hints & DESIGN_ROLES:
        return "design"
    if kind in ("release", "doc", "source_file"):
        return "release_doc"
    return "open_proposal"


def _deep_read(entity: Mapping[str, Any]) -> bool:
    raw_state = entity.get("raw_state")
    state = raw_state if isinstance(raw_state, Mapping) else {}
    if state.get("deep_read") or state.get("proposal_read"):
        return True
    if entity.get("kind") == "release":
        return isinstance(state.get("release_id"), int)
    if entity.get("kind") == "doc":
        status = state.get("status")
        if not isinstance(status, str):
            normalized = entity.get("normalized_state")
            normalized_state = normalized if isinstance(normalized, Mapping) else {}
            status = normalized_state.get("status")
        return bool(state.get("revision") and state.get("path")) and status == "read"
    return False


def _counts(
    records: list[Mapping[str, Any]],
    *,
    total_sources: int,
    omitted: int,
    relation_expanded: int,
    relation_omitted: int,
    missing_entities: int,
    missing_activity_entities: int,
    agent_selected_omitted: int,
    deep_read_omitted: int,
) -> dict[str, int]:
    recommended = [item for item in records if item.get("recommended") is True]
    focus_skipped = sum(
        1
        for item in records
        if item.get("selection_status") == "skipped_quota"
        and int(item.get("score_components", {}).get("focus_match", 0)) > 0
    )
    return {
        "candidates_total": total_sources,
        "candidates_emitted": len(records),
        "candidates_omitted": omitted,
        "recommended": len(recommended),
        "recommended_design": sum(
            1 for item in recommended if item.get("selection_lane") == "design"
        ),
        "recommended_merged_pr": sum(
            1 for item in recommended if item.get("selection_lane") == "merged_pr"
        ),
        "recommended_open_proposal": sum(
            1 for item in recommended if item.get("selection_lane") == "open_proposal"
        ),
        "recommended_release_doc": sum(
            1 for item in recommended if item.get("selection_lane") == "release_doc"
        ),
        "recommended_exploration": sum(
            1 for item in recommended if item.get("selection_lane") == "exploration"
        ),
        "recommended_focus": sum(
            1
            for item in recommended
            if int(item.get("score_components", {}).get("focus_match", 0)) > 0
        ),
        "skipped_quota": sum(
            1 for item in records if item.get("selection_status") == "skipped_quota"
        ),
        "skipped_sub_budget": sum(
            1
            for item in records
            if item.get("selection_status") == "skipped_sub_budget"
        ),
        "already_read": sum(
            1 for item in records if item.get("selection_status") == "already_read"
        ),
        "agent_selected": sum(1 for item in records if item.get("agent_selected") is True),
        "agent_selected_omitted": agent_selected_omitted,
        "deep_read_omitted": deep_read_omitted,
        "fetchable": sum(1 for item in records if item.get("budget_fit") == "fetchable"),
        "blocked_pr_budget": sum(
            1 for item in records if item.get("budget_fit") == "blocked_pr_budget"
        ),
        "blocked_entity_budget": sum(
            1 for item in records if item.get("budget_fit") == "blocked_entity_budget"
        ),
        "unknown_module": sum(
            1 for item in records if item.get("module") == "unknown"
        ),
        "relation_expanded": relation_expanded,
        "relation_omitted": relation_omitted,
        "missing_entities": missing_entities,
        "missing_activity_entities": missing_activity_entities,
        "focus_skipped": focus_skipped,
    }


def _limitations(
    records: list[Mapping[str, Any]],
    *,
    quota: int,
    omitted: int,
    relation_omitted: int,
    remaining: int,
    focus_cap_applied: bool,
    agent_selected_omitted: int,
    deep_read_omitted: int,
) -> list[str]:
    limitations = {"observed_current_state"}
    if omitted:
        limitations.add("candidate_budget_truncated")
    if relation_omitted:
        limitations.add("relation_expansion_budget_truncated")
    if remaining == 0:
        limitations.add("deep_budget_exhausted")
    if any(item.get("module") == "unknown" for item in records):
        limitations.add("unknown_module")
    if any(
        item.get("module_source") == "scope" for item in records
    ):
        limitations.add("module_from_scope")
    if focus_cap_applied:
        limitations.add("focus_cap_applied")
    if any(item.get("selection_status") == "skipped_quota" for item in records):
        limitations.add("quota_limited_selection")
    if any(item.get("selection_status") == "skipped_sub_budget" for item in records):
        limitations.add("sub_budget_limited_selection")
    if any(item.get("budget_fit") == "blocked_pr_budget" for item in records):
        limitations.add("deep_pr_budget_exhausted")
    if any(item.get("budget_fit") == "blocked_entity_budget" for item in records):
        limitations.add("deep_budget_exhausted")
    if agent_selected_omitted:
        limitations.add("agent_selection_omitted")
    if deep_read_omitted:
        limitations.add("deep_read_candidate_omitted")
    return sorted(limitations)


def _window_end(
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> datetime | None:
    return parse_datetime(
        config.get("window", {}).get("end")
        or manifest.get("activity_window", {}).get("end")
    )


__all__ = [
    "CANDIDATES_PATH",
    "RULE_VERSION",
    "audit_candidates",
    "build_candidates",
    "candidate_id",
    "write_candidates",
]
