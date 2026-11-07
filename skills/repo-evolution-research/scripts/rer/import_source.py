"""Import an external source envelope and Agent-confirmed relations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .errors import RERError, StoreError, UsageError
from .imported_source import imported_artifact_path, imported_source_key
from .models import make_evidence, make_locator, normalize_newlines, validate_document
from .normalize import Block, TextSource, evidence_id
from .relation_policy import (
    CORE_SCOPE_FIELDS,
    INTERNAL_SCOPE_FIELDS,
    choose_canonical_relation,
    merge_relation_records,
    relation_identity,
    seed_observations,
)
from .store import SnapshotStore

ENVELOPE_SCHEMA = "source_envelope"


@dataclass
class ImportPlan:
    evidence: dict[str, Any]
    payload: bytes
    artifact: str
    artifact_document: dict[str, Any]
    relation_records: list[dict[str, Any]]
    relation_ids: list[str]
    relation_aliases: dict[str, str]
    evidence_is_new: bool
    source_url: str
    prior_evidence: bytes | None
    prior_relations: bytes | None
    prior_artifact: bytes | None
    warnings: list[str]


def load_envelope(path: Path) -> dict[str, Any]:
    """Read and validate one import envelope from disk."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(
            f"无法读取来源 envelope：{path}",
            source="import_source.read",
            details={"reason": str(exc)},
        ) from exc
    if not isinstance(document, dict):
        raise UsageError(
            "来源 envelope 必须是 JSON 对象",
            source="import_source.schema",
        )
    validate_document(document, ENVELOPE_SCHEMA)
    return document


def plan_import(
    store: SnapshotStore,
    envelope: Mapping[str, Any],
) -> ImportPlan:
    """Validate an import and return a plan without writing to the snapshot."""
    entities = {
        str(item["id"]): item
        for item in _read_stream(store, "normalized/entities.jsonl")
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    entity_ids = set(entities)
    entity_id = str(envelope["entity_id"])
    if entity_id not in entity_ids:
        raise UsageError(
            f"来源 envelope 的 entity_id 不在 normalized/entities.jsonl 中：{entity_id}",
            source="import_source.entity",
            recovery="先运行 prepare，使 release/PR 实体存在后再导入外部来源",
            details={"entity_id": entity_id},
        )

    text = normalize_newlines(str(envelope["content"]))
    payload = text.encode("utf-8")
    blob_sha256 = hashlib.sha256(payload).hexdigest()
    source_kind = str(envelope.get("source_kind") or "external_page")
    source_url = str(envelope["source_url"])
    source_revision = (
        str(envelope["source_revision"])
        if envelope.get("source_revision") is not None
        else None
    )
    artifact = imported_artifact_path(
        source_kind,
        entity_id,
        source_url,
        source_revision,
        blob_sha256,
    )
    source = TextSource(
        artifact=artifact,
        document_key=f"imported:{source_url}",
        json_pointer="/content",
        entity_id=entity_id,
        source_kind=source_kind,
        source_url=source_url,
        observed_at=str(envelope["retrieved_at"]),
        source_event_at=None,
        source_revision=source_revision,
        blob_sha256=blob_sha256,
        text=text,
    )
    line_end = text.count("\n") + 1
    block = Block(start=0, end=len(text), line_start=1, line_end=line_end, text=text)
    imported_evidence_id = evidence_id(source, block)
    artifact_document = _artifact_document(
        store,
        artifact=artifact,
        source_key=imported_source_key(
            source_kind,
            entity_id,
            source_url,
            source_revision,
        ),
        source_url=source_url,
        entity_id=entity_id,
        source_kind=source_kind,
        source_revision=source_revision,
        blob_sha256=blob_sha256,
        payload=payload,
        envelope=envelope,
    )
    evidence = make_evidence(
        evidence_id=imported_evidence_id,
        entity_id=entity_id,
        source_url=source.source_url,
        source_kind=source.source_kind,
        observed_at=source.observed_at,
        text=text,
        locator=make_locator(
            "char_range",
            char_start=0,
            char_end=len(text),
            line_start=1,
            line_end=line_end,
        ),
        blob_sha256=blob_sha256,
        completeness=str(envelope.get("completeness") or "complete"),
        source_revision=source.source_revision,
        verification="verified",
    )

    existing_evidence = {
        str(item["id"]): item
        for item in _read_stream(store, "normalized/evidence.jsonl")
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    if imported_evidence_id in existing_evidence:
        existing = existing_evidence[imported_evidence_id]
        mismatched = {
            field: {"expected": expected, "actual": existing.get(field)}
            for field, expected in (
                ("blob_sha256", blob_sha256),
                ("entity_id", entity_id),
                ("source_url", source.source_url),
                ("source_kind", source.source_kind),
                ("source_revision", source.source_revision),
            )
            if existing.get(field) != expected
        }
        if mismatched:
            raise StoreError(
                "导入 evidence ID 已存在但来源不一致",
                source="import_source.evidence",
                details={
                    "evidence_id": imported_evidence_id,
                    "mismatched": mismatched,
                },
            )

    relation_records: list[dict[str, Any]] = []
    known_evidence = dict(existing_evidence)
    known_evidence.setdefault(imported_evidence_id, evidence)
    for index, item in enumerate(envelope.get("relations") or []):
        record = _relation_record(
            item,
            index=index,
            imported_evidence_id=imported_evidence_id,
            evidence_by_id=known_evidence,
            entity_ids=entity_ids,
            artifact=artifact,
            blob_sha256=blob_sha256,
            source_revision=source.source_revision,
            envelope=envelope,
        )
        relation_records.append(record)

    referenced_ids = {entity_id}
    for record in relation_records:
        referenced_ids.add(str(record["from_id"]))
        referenced_ids.add(str(record["to_id"]))
    _require_current_doc_revisions(store, entities, referenced_ids)

    relation_ids: list[str] = []
    relation_aliases: dict[str, str] = {}
    original_observation_counts: dict[str, int] = {}
    if relation_records:
        existing_relations = {
            str(item["id"]): dict(item)
            for item in _read_stream(store, "normalized/relations.jsonl")
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        original_observation_counts = {
            str(item["id"]): len(seed_observations(item))
            for item in existing_relations.values()
        }
        working = dict(existing_relations)
        by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for record in working.values():
            by_identity.setdefault(relation_identity(record), []).append(record)
        for record in relation_records:
            identity = relation_identity(record)
            identifier = str(record["id"])
            prior = working.get(identifier)
            if prior is not None and relation_identity(prior) != identity:
                raise UsageError(
                    "导入关系 ID 与已有关系冲突",
                    source="import_source.relation",
                    details={
                        "relation_id": record["id"],
                        "existing": list(relation_identity(prior)),
                        "imported": list(identity),
                    },
                )
            group = by_identity.get(identity) or []
            canonical = prior
            if canonical is None and group:
                canonical = choose_canonical_relation(group, record)
            if canonical is None:
                working[identifier] = record
                by_identity[identity] = [record]
                relation_ids.append(identifier)
                continue
            merged = canonical
            for member in list(group):
                if str(member["id"]) == str(canonical["id"]):
                    continue
                merged = merge_relation_records(
                    merged,
                    member,
                    alias=str(member["id"]),
                )
                working.pop(str(member["id"]), None)
            if identifier == str(canonical["id"]):
                merged = merge_relation_records(merged, record)
            else:
                merged = merge_relation_records(
                    merged,
                    record,
                    alias=identifier,
                )
                relation_aliases[identifier] = str(canonical["id"])
            working[str(canonical["id"])] = merged
            by_identity[identity] = [merged]
            relation_ids.append(str(canonical["id"]))
        relation_records = [working[key] for key in sorted(working)]

    validate_document(evidence, "evidence")
    for record in relation_records:
        validate_document(record, "relation")

    warnings: list[str] = []
    for record in relation_records:
        identifier = str(record.get("id"))
        before = original_observation_counts.get(identifier)
        if before is None:
            continue
        after = len(seed_observations(record))
        if after > before:
            warnings.append(
                f"merged relation observations: {identifier} ({before}->{after})"
            )

    evidence_relpath = "normalized/evidence.jsonl"
    relations_relpath = "normalized/relations.jsonl"
    return ImportPlan(
        evidence=evidence,
        payload=payload,
        artifact=artifact,
        artifact_document=artifact_document,
        relation_records=relation_records,
        relation_ids=relation_ids,
        relation_aliases=relation_aliases,
        evidence_is_new=imported_evidence_id not in existing_evidence,
        source_url=source.source_url,
        prior_evidence=_read_optional(store, evidence_relpath),
        prior_relations=_read_optional(store, relations_relpath),
        prior_artifact=_read_optional(store, artifact),
        warnings=warnings,
    )


def commit_import(store: SnapshotStore, plan: ImportPlan) -> dict[str, Any]:
    """Apply a validated import plan with best-effort rollback on failure."""
    evidence_relpath = "normalized/evidence.jsonl"
    relations_relpath = "normalized/relations.jsonl"
    try:
        if plan.evidence_is_new:
            store.write_evidence(plan.evidence, payload=plan.payload)

        if plan.relation_records:
            store.write_jsonl(
                relations_relpath,
                plan.relation_records,
                key="id",
                on_conflict="replace",
                schema_name="relation",
            )

        store.write_json(plan.artifact, plan.artifact_document)
    except Exception as exc:
        rollback_problems: list[dict[str, Any]] = []
        for relpath, prior in (
            (evidence_relpath, plan.prior_evidence),
            (relations_relpath, plan.prior_relations),
            (plan.artifact, plan.prior_artifact),
        ):
            try:
                _restore_optional(store, relpath, prior)
            except Exception as rollback_exc:
                rollback_problems.append(
                    {"artifact": relpath, "reason": str(rollback_exc)}
                )
        if rollback_problems:
            raise StoreError(
                "导入提交失败且回滚不完整",
                source="import_source.rollback",
                details={
                    "error": str(exc),
                    "rollback_problems": rollback_problems,
                },
                recovery=(
                    "运行 verify_integrity 检查快照，"
                    "并重新 import-source 或重建 run"
                ),
            ) from exc
        raise

    return {
        "evidence_id": plan.evidence["id"],
        "relation_ids": plan.relation_ids,
        "relation_aliases": plan.relation_aliases,
        "artifact": plan.artifact,
        "source_url": plan.source_url,
        "warnings": plan.warnings,
    }


def import_source(store: SnapshotStore, envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Persist external evidence and imported relations without touching the network."""
    return commit_import(store, plan_import(store, envelope))


def _read_stream(store: SnapshotStore, relpath: str) -> list[dict[str, Any]]:
    try:
        return store.read_jsonl(relpath)
    except StoreError as exc:
        raise UsageError(
            f"导入前快照不可读：{relpath}",
            source="import_source.preflight",
            recovery="先修复该 JSONL 末尾未完成写入，再重新导入",
            details={"stream": relpath, "reason": exc.message},
        ) from exc


def _read_optional(store: SnapshotStore, relpath: str) -> bytes | None:
    path = store.secure_path(relpath)
    return path.read_bytes() if path.is_file() else None


def _restore_optional(
    store: SnapshotStore,
    relpath: str,
    payload: bytes | None,
) -> None:
    if payload is not None:
        store.write_bytes(relpath, payload)
        return
    path = store.secure_path(relpath)
    store.remove_file(relpath)
    if not relpath.startswith("imported/"):
        return
    parent = path.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass


def _artifact_document(
    store: SnapshotStore,
    *,
    artifact: str,
    source_key: str,
    source_url: str,
    entity_id: str,
    source_kind: str,
    source_revision: str | None,
    blob_sha256: str,
    payload: bytes,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    retrieved_at = str(envelope["retrieved_at"])
    provider = str(envelope["provider"])
    content_type = str(envelope.get("content_type") or "text/plain")
    completeness = str(envelope.get("completeness") or "complete")
    observation = {
        "retrieved_at": retrieved_at,
        "provider": provider,
        "content_type": content_type,
        "completeness": completeness,
    }
    expected = {
        "source_key": source_key,
        "source_url": source_url,
        "entity_id": entity_id,
        "source_kind": source_kind,
        "source_revision": source_revision,
        "content_sha256": blob_sha256,
    }
    existing: Mapping[str, Any] | None = None
    if store.secure_path(artifact).is_file():
        try:
            raw = store.read_json(artifact)
        except RERError as exc:
            raise StoreError(
                "无法读取已有来源 artifact",
                source="import_source.artifact",
                details={"artifact": artifact, "reason": exc.message},
            ) from exc
        if not isinstance(raw, Mapping):
            raise StoreError(
                "已有来源 artifact 不是 JSON 对象",
                source="import_source.artifact",
                details={"artifact": artifact},
            )
        existing = raw
        mismatched = {
            field: {"expected": value, "actual": raw.get(field)}
            for field, value in expected.items()
            if field in raw and raw.get(field) != value
        }
        if mismatched:
            raise StoreError(
                "已有来源 artifact 与本次导入不一致",
                source="import_source.artifact",
                details={"artifact": artifact, "mismatched": mismatched},
            )
        if (
            "content_bytes" in raw
            and raw.get("content_bytes") != len(payload)
        ):
            raise StoreError(
                "已有来源 artifact 的 content_bytes 与正文不一致",
                source="import_source.artifact",
                details={"artifact": artifact},
            )
        document = dict(raw)
        for field, value in expected.items():
            document.setdefault(field, value)
        document.setdefault("schema_version", SCHEMA_VERSION)
        document.setdefault("provider", provider)
        document.setdefault("retrieved_at", retrieved_at)
        document.setdefault("content_type", content_type)
        document.setdefault("completeness", completeness)
        document.setdefault("content_bytes", len(payload))
    else:
        document = {
            "schema_version": SCHEMA_VERSION,
            "source_key": source_key,
            "source_url": source_url,
            "provider": provider,
            "retrieved_at": retrieved_at,
            "content_type": content_type,
            "entity_id": entity_id,
            "source_kind": source_kind,
            "source_revision": source_revision,
            "completeness": completeness,
            "content_sha256": blob_sha256,
            "content_bytes": len(payload),
        }
    raw_observations = document.get("observations")
    observations = (
        [dict(item) for item in raw_observations if isinstance(item, Mapping)]
        if isinstance(raw_observations, list)
        else []
    )
    if not observations and existing is not None:
        observations.append(
            {
                "retrieved_at": document.get("retrieved_at") or retrieved_at,
                "provider": document.get("provider") or provider,
                "content_type": document.get("content_type") or content_type,
                "completeness": document.get("completeness") or completeness,
            }
        )
    observation_key = (retrieved_at, provider, content_type, completeness)
    seen = {
        (
            str(item.get("retrieved_at") or ""),
            str(item.get("provider") or ""),
            str(item.get("content_type") or ""),
            str(item.get("completeness") or ""),
        )
        for item in observations
    }
    if observation_key not in seen:
        observations.append(dict(observation))
    document["observations"] = sorted(
        observations,
        key=lambda item: (
            str(item.get("retrieved_at") or ""),
            str(item.get("provider") or ""),
            str(item.get("content_type") or ""),
            str(item.get("completeness") or ""),
        ),
    )
    return document


def _require_current_doc_revisions(
    store: SnapshotStore,
    entities: Mapping[str, Mapping[str, Any]],
    identifiers: set[str],
) -> None:
    doc_identifiers = [
        identifier
        for identifier in sorted(identifiers)
        if isinstance(entities.get(identifier), Mapping)
        and entities[identifier].get("kind") == "doc"
    ]
    if not doc_identifiers:
        return
    try:
        manifest = store.read_manifest()
    except StoreError:
        return
    active_revision = manifest.get("default_branch_sha")
    if not isinstance(active_revision, str) or not active_revision:
        return
    for identifier in doc_identifiers:
        entity = entities[identifier]
        raw_state = entity.get("raw_state")
        revision = raw_state.get("revision") if isinstance(raw_state, Mapping) else None
        if revision != active_revision:
            raise UsageError(
                f"导入来源不能引用非当前 revision 的文档实体：{identifier}",
                source="import_source.doc_revision",
                recovery="重新 collect 后只对当前 default_branch_sha 的文档实体导入外部来源",
                details={
                    "entity_id": identifier,
                    "revision": revision,
                    "active_revision": active_revision,
                },
            )


def _relation_record(
    item: Mapping[str, Any],
    *,
    index: int,
    imported_evidence_id: str,
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    entity_ids: set[str],
    artifact: str,
    blob_sha256: str,
    source_revision: str | None,
    envelope: Mapping[str, Any],
) -> dict[str, Any]:
    from_id = str(item["from_id"])
    to_id = str(item["to_id"])
    for field, value in (("from_id", from_id), ("to_id", to_id)):
        if value not in entity_ids:
            raise UsageError(
                f"导入关系的 {field} 不在 normalized/entities.jsonl 中：{value}",
                source="import_source.relation",
                recovery="先运行 prepare，使关系两端实体存在后再导入",
                details={"index": index, "field": field, "entity_id": value},
            )
    if from_id == to_id:
        raise UsageError(
            "导入关系不能自引用",
            source="import_source.relation",
            details={"index": index, "entity_id": from_id},
        )

    raw_evidence_ids = item.get("evidence_ids")
    referenced = (
        [str(value) for value in raw_evidence_ids]
        if raw_evidence_ids is not None
        else [imported_evidence_id]
    )
    missing = sorted(set(str(value) for value in referenced) - set(evidence_by_id))
    if missing:
        raise UsageError(
            "导入关系引用了不存在的 evidence",
            source="import_source.relation",
            details={"index": index, "evidence_ids": missing},
        )
    owner_mismatch = sorted(
        evidence_id
        for evidence_id in set(str(value) for value in referenced)
        if evidence_by_id.get(evidence_id, {}).get("entity_id")
        not in (from_id, to_id)
    )
    if owner_mismatch:
        raise UsageError(
            "导入关系引用的 evidence.entity_id 不是关系端点",
            source="import_source.relation",
            details={"index": index, "evidence_ids": owner_mismatch},
        )
    verification = str(item["verification"])
    basis = str(item["basis"])
    relation_type = str(item["type"])
    if (
        verification == "verified"
        and not referenced
        and relation_type != "contains_commit"
    ):
        raise UsageError(
            "verification=verified 的导入关系必须引用至少一条 evidence",
            source="import_source.relation",
            details={"index": index},
        )
    if verification == "verified" and basis not in ("api_explicit", "text_explicit"):
        raise UsageError(
            "verification=verified 的导入关系必须使用 api_explicit 或 text_explicit basis",
            source="import_source.relation",
            recovery="Agent 推断的关系请使用 verification=candidate",
            details={"index": index, "basis": basis},
        )
    if verification == "verified":
        unverified = sorted(
            evidence_id
            for evidence_id in referenced
            if evidence_by_id.get(evidence_id, {}).get("verification") != "verified"
        )
        if unverified:
            raise UsageError(
                "verification=verified 的导入关系只能引用 verified evidence",
                source="import_source.relation",
                details={"index": index, "evidence_ids": unverified},
            )

    if (
        relation_type == "verified_in_release"
        and verification == "verified"
        and imported_evidence_id not in referenced
    ):
        raise UsageError(
            "verified_in_release 必须引用本次导入的 evidence",
            source="import_source.relation",
            details={"index": index},
        )

    observed_at = str(envelope["retrieved_at"])
    provider = str(envelope["provider"])
    source_url = str(envelope["source_url"])
    completeness = str(envelope.get("completeness") or "complete")
    scope = {
        "artifact": artifact,
        "generator": "import-source",
        "observed_at": observed_at,
        "provider": provider,
        "source_url": source_url,
        "observations": [
            {
                "artifact": artifact,
                "content_sha256": blob_sha256,
                "observed_at": observed_at,
                "provider": provider,
                "source_url": source_url,
                "source_revision": source_revision,
                "evidence_ids": sorted(
                    {str(value) for value in referenced if value is not None}
                ),
                "verification": verification,
                "basis": basis,
                "completeness": completeness,
            }
        ],
    }
    provided_scope = item.get("scope")
    if isinstance(provided_scope, Mapping):
        protected = {*CORE_SCOPE_FIELDS, *INTERNAL_SCOPE_FIELDS}
        overridden = sorted(set(map(str, provided_scope)) & protected)
        if overridden:
            raise UsageError(
                "relation.scope 不能覆盖核心溯源字段",
                source="import_source.relation",
                details={"index": index, "fields": overridden},
            )
        scope.update({str(key): value for key, value in provided_scope.items()})

    relation_id = str(
        item.get("id")
        or f"rel-{hashlib.sha256(f'{from_id}|{to_id}|{relation_type}'.encode()).hexdigest()[:16]}"
    )
    return {
        "id": relation_id,
        "from_id": from_id,
        "to_id": to_id,
        "type": relation_type,
        "basis": basis,
        "verification": verification,
        "evidence_ids": list(dict.fromkeys(str(value) for value in referenced)),
        "scope": scope,
    }


__all__ = [
    "ENVELOPE_SCHEMA",
    "ImportPlan",
    "commit_import",
    "import_source",
    "load_envelope",
    "plan_import",
]
