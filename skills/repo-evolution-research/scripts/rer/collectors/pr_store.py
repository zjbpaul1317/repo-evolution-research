"""Authoritative per-PR detail documents.

``raw/pr-details/<number>.json`` is the only persisted representation of a
deep read. The entity ``raw_state.deep_read`` pointer is the only derived view;
it is rebuilt from the documents, never written independently.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..errors import StoreError
from ..models import entity_id, validate_document
from ..store import SnapshotStore
from .entity_store import (
    PR_DETAIL_DIR,
    deep_read_state,
    detail_path,
    project_pr_detail,
    prune_deep_read,
)
from .status import PR_DETAIL_SECTIONS, derive_detail_status, derive_limitations


def validate_pr_document(document: Mapping[str, Any]) -> None:
    validate_document(document, "pr_detail")
    expected = derive_detail_status(
        sections={key: document[key] for key in PR_DETAIL_SECTIONS},
        head_changed=bool(document["head_changed"]),
        head_unverified=document["head_sha"] is None or document["head_sha_end"] is None,
        diff_status=str(document["diff"]["status"]),
    )
    if document["coverage"]["detail"] != expected:
        raise StoreError(
            "pr_detail.coverage.detail 与 section 状态不一致",
            source="pr_store.invariant",
            details={
                "number": document.get("number"),
                "stored": document["coverage"]["detail"],
                "derived": expected,
            },
            recovery="修正文档的 coverage/limitations，不要静默写入",
        )
    for key in PR_DETAIL_SECTIONS:
        if document["coverage"][key] != document[key]["status"]:
            raise StoreError(
                f"pr_detail.coverage.{key} 与 section 状态不一致",
                source="pr_store.invariant",
                details={"number": document.get("number")},
            )
    if document["limitations"] != derive_limitations(document):
        raise StoreError(
            "pr_detail.limitations 不是从文档派生",
            source="pr_store.invariant",
            details={"number": document.get("number")},
            recovery="使用 derive_limitations(document) 重新生成",
        )


def write_pr_document(
    store: SnapshotStore,
    document: Mapping[str, Any],
) -> Path:
    validate_pr_document(document)
    number = int(document["number"])
    return store.write_json(
        detail_path(number),
        document,
        schema_name="pr_detail",
    )


def load_pr_document(store: SnapshotStore, number: int) -> dict[str, Any] | None:
    path = store.secure_path(detail_path(number))
    if not path.is_file():
        return None
    document = store.read_json(detail_path(number))
    validate_pr_document(document)
    return document


def iter_pr_documents(store: SnapshotStore) -> Iterator[dict[str, Any]]:
    directory = store.secure_path(PR_DETAIL_DIR)
    if not directory.is_dir():
        return
    paths = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".json" and path.stem.isdigit()
    ]
    for path in sorted(paths, key=lambda item: int(item.stem)):
        document = store.read_json(f"{PR_DETAIL_DIR}/{path.name}")
        validate_pr_document(document)
        yield document


def load_pr_documents(store: SnapshotStore) -> list[dict[str, Any]]:
    return list(iter_pr_documents(store))


def summaries_from_documents(
    documents: Sequence[Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    return {
        int(document["number"]): {
            "number": int(document["number"]),
            "status": document["coverage"]["detail"],
            "head_sha": document["head_sha"],
        }
        for document in documents
    }


def load_pr_summaries(store: SnapshotStore) -> dict[int, dict[str, Any]]:
    """Resume metadata is derived from authoritative documents."""
    return summaries_from_documents(load_pr_documents(store))


def reconcile_entity_projection(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """Repair the entity deep-read pointer when it disagrees with a document."""
    materialized = (
        list(documents)
        if documents is not None
        else load_pr_documents(store)
    )
    numbers = {int(document["number"]) for document in materialized}
    entities = {
        entity["id"]: entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if isinstance(entity, dict) and isinstance(entity.get("id"), str)
    }
    repaired = False
    for document in materialized:
        entity = entities.get(_entity_identifier(document))
        raw_state = entity.get("raw_state") if isinstance(entity, Mapping) else None
        deep_read = (
            raw_state.get("deep_read") if isinstance(raw_state, Mapping) else None
        )
        if isinstance(deep_read, Mapping) and dict(deep_read) == deep_read_state(document):
            continue
        repository = document["repository"]
        project_pr_detail(
            store,
            str(repository["host"]),
            str(repository["owner"]),
            str(repository["name"]),
            document,
        )
        repaired = True
    if prune_deep_read(store, numbers):
        repaired = True
    return repaired


def _entity_identifier(document: Mapping[str, Any]) -> str:
    repository = document["repository"]
    return entity_id(
        str(repository["host"]),
        str(repository["owner"]),
        str(repository["name"]),
        "pull_request",
        str(document["number"]),
    )


__all__ = [
    "PR_DETAIL_DIR",
    "detail_path",
    "iter_pr_documents",
    "load_pr_document",
    "load_pr_documents",
    "load_pr_summaries",
    "reconcile_entity_projection",
    "summaries_from_documents",
    "validate_pr_document",
    "write_pr_document",
]
