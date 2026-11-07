"""Imported source identity and artifact consistency audit."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .errors import RERError

IMPORTED_SOURCE_KINDS = frozenset({"external_page", "api_response"})


def imported_source_key(
    source_kind: str,
    entity_id: str,
    source_url: str,
    source_revision: str | None,
) -> str:
    material = "\0".join(
        [source_kind, entity_id, source_url, source_revision or ""]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def imported_artifact_path(
    source_kind: str,
    entity_id: str,
    source_url: str,
    source_revision: str | None,
    content_sha256: str,
) -> str:
    key = imported_source_key(
        source_kind,
        entity_id,
        source_url,
        source_revision,
    )
    return f"imported/{key}/{content_sha256}.json"


def legacy_imported_artifact_path(content_sha256: str) -> str:
    return f"imported/{content_sha256}.json"


def _expected_artifact_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    source_kind = str(item.get("source_kind") or "")
    entity_id = str(item.get("entity_id") or "")
    source_url = str(item.get("source_url") or "")
    source_revision = item.get("source_revision")
    blob_sha256 = str(item.get("blob_sha256") or "")
    return {
        "source_key": imported_source_key(
            source_kind,
            entity_id,
            source_url,
            str(source_revision) if source_revision is not None else None,
        ),
        "source_url": source_url,
        "entity_id": entity_id,
        "source_kind": source_kind,
        "source_revision": source_revision,
        "content_sha256": blob_sha256,
    }


def audit_imported_sources(
    store: Any,
    *,
    evidence: list[dict[str, Any]],
    relations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        if item.get("source_kind") not in IMPORTED_SOURCE_KINDS:
            continue
        source_revision = item.get("source_revision")
        if source_revision is not None and not isinstance(source_revision, str):
            problems.append(
                {
                    "evidence_id": item.get("id"),
                    "problem": "imported_source_mismatch",
                    "message": "imported evidence 的 source_revision 类型非法",
                    "details": {
                        "field": "source_revision",
                        "expected": "string|null",
                        "actual": type(source_revision).__name__,
                    },
                }
            )
            continue
        expected_fields = _expected_artifact_fields(item)
        artifact = imported_artifact_path(
            str(expected_fields["source_kind"]),
            str(expected_fields["entity_id"]),
            str(expected_fields["source_url"]),
            expected_fields["source_revision"],
            str(expected_fields["content_sha256"]),
        )
        if not store.secure_path(artifact).is_file():
            legacy = legacy_imported_artifact_path(
                str(expected_fields["content_sha256"])
            )
            if store.secure_path(legacy).is_file():
                problems.append(
                    {
                        "evidence_id": item.get("id"),
                        "artifact": legacy,
                        "problem": "legacy_imported_source",
                        "message": (
                            "imported evidence 仍指向旧 artifact 路径，"
                            "需要重新导入或迁移来源记录"
                        ),
                    }
                )
            else:
                problems.append(
                    {
                        "evidence_id": item.get("id"),
                        "artifact": artifact,
                        "problem": "missing_imported_source",
                        "message": "imported evidence 缺少来源 artifact",
                    }
                )
            continue
        try:
            document = store.read_json(artifact)
        except RERError as exc:
            problems.append(
                {
                    "evidence_id": item.get("id"),
                    "artifact": artifact,
                    "problem": "imported_source_unreadable",
                    "message": f"无法读取来源 artifact：{exc.message}",
                }
            )
            continue
        if not isinstance(document, Mapping):
            problems.append(
                {
                    "evidence_id": item.get("id"),
                    "artifact": artifact,
                    "problem": "imported_source_unreadable",
                    "message": "来源 artifact 不是 JSON 对象",
                }
            )
            continue
        mismatched = {
            field: {"expected": value, "actual": document.get(field)}
            for field, value in expected_fields.items()
            if document.get(field) != value
        }
        content_mismatch = mismatched.pop("content_sha256", None)
        if mismatched:
            problems.append(
                {
                    "evidence_id": item.get("id"),
                    "artifact": artifact,
                    "problem": "imported_source_mismatch",
                    "message": "来源 artifact 与 evidence 的溯源字段不一致",
                    "details": mismatched,
                }
            )
        if content_mismatch is not None:
            problems.append(
                {
                    "evidence_id": item.get("id"),
                    "artifact": artifact,
                    "problem": "imported_source_content_mismatch",
                    "message": "来源 artifact 的 content_sha256 与 evidence 不一致",
                    "details": {"content_sha256": content_mismatch},
                }
            )
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        scope = relation.get("scope")
        if not isinstance(scope, Mapping):
            continue
        if scope.get("generator") != "import-source":
            continue
        relation_id = relation.get("id")
        artifacts: set[str] = set()
        scope_artifact = scope.get("artifact")
        if isinstance(scope_artifact, str) and scope_artifact:
            artifacts.add(scope_artifact)
        else:
            problems.append(
                {
                    "relation_id": relation_id,
                    "problem": "missing_imported_relation_artifact",
                    "message": "import-source relation 缺少 scope.artifact",
                    "details": {"field": "scope.artifact"},
                }
            )
        raw_observations = scope.get("observations")
        observations = (
            [item for item in raw_observations if isinstance(item, Mapping)]
            if isinstance(raw_observations, list)
            else []
        )
        if not observations:
            problems.append(
                {
                    "relation_id": relation_id,
                    "problem": "imported_observation_mismatch",
                    "message": "import-source relation 缺少可用的 scope.observations",
                    "details": {"field": "scope.observations"},
                }
            )
        elif len(observations) != len(raw_observations):
            problems.append(
                {
                    "relation_id": relation_id,
                    "problem": "imported_observation_mismatch",
                    "message": "scope.observations 含非对象条目",
                    "details": {"field": "scope.observations"},
                }
            )
        raw_relation_evidence = relation.get("evidence_ids")
        relation_evidence = (
            {str(value) for value in raw_relation_evidence}
            if isinstance(raw_relation_evidence, (list, tuple))
            else set()
        )
        for observation in observations:
            observation_artifact = observation.get("artifact")
            artifact_ok = (
                isinstance(observation_artifact, str) and bool(observation_artifact)
            )
            if not artifact_ok:
                problems.append(
                    {
                        "relation_id": relation_id,
                        "problem": "imported_observation_mismatch",
                        "message": "来源观察缺少 artifact",
                        "details": {"field": "observations[].artifact"},
                    }
                )
            else:
                artifacts.add(observation_artifact)
            raw_observation_evidence = observation.get("evidence_ids")
            if raw_observation_evidence is None:
                observation_evidence: set[str] = set()
            elif isinstance(raw_observation_evidence, (list, tuple)):
                observation_evidence = {
                    str(value) for value in raw_observation_evidence
                }
            else:
                observation_evidence = set()
                problems.append(
                    {
                        "relation_id": relation_id,
                        "problem": "imported_observation_evidence_mismatch",
                        "message": "来源观察的 evidence_ids 不是数组",
                        "details": {"field": "observations[].evidence_ids"},
                    }
                )
            extra = sorted(observation_evidence - relation_evidence)
            if extra:
                problems.append(
                    {
                        "relation_id": relation_id,
                        "evidence_ids": extra,
                        "problem": "imported_observation_evidence_mismatch",
                        "message": "来源观察引用了 relation 之外的 evidence",
                    }
                )
            if not artifact_ok:
                continue
            observation_hash = observation.get("content_sha256")
            if not (isinstance(observation_hash, str) and observation_hash):
                problems.append(
                    {
                        "relation_id": relation_id,
                        "problem": "imported_observation_mismatch",
                        "message": "来源观察缺少 content_sha256",
                        "details": {"field": "observations[].content_sha256"},
                    }
                )
                continue
            if not store.secure_path(observation_artifact).is_file():
                continue
            try:
                artifact_document = store.read_json(observation_artifact)
            except RERError as exc:
                problems.append(
                    {
                        "relation_id": relation_id,
                        "artifact": observation_artifact,
                        "problem": "imported_observation_mismatch",
                        "message": f"无法读取来源观察 artifact：{exc.message}",
                    }
                )
                continue
            if (
                isinstance(artifact_document, Mapping)
                and artifact_document.get("content_sha256") != observation_hash
            ):
                problems.append(
                    {
                        "relation_id": relation_id,
                        "artifact": observation_artifact,
                        "problem": "imported_observation_mismatch",
                        "message": "来源观察的 content_sha256 与 artifact 不一致",
                    }
                )
        for artifact in sorted(artifacts):
            if not store.secure_path(artifact).is_file():
                problems.append(
                    {
                        "relation_id": relation_id,
                        "artifact": artifact,
                        "problem": "missing_imported_relation_artifact",
                        "message": "import-source relation 引用的 artifact 不存在",
                    }
                )
    return problems


__all__ = [
    "IMPORTED_SOURCE_KINDS",
    "audit_imported_sources",
    "imported_artifact_path",
    "imported_source_key",
    "legacy_imported_artifact_path",
]
