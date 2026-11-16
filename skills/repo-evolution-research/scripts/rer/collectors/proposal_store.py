"""Authoritative per-issue and per-discussion detail documents.

``raw/issues/<number>.json`` and ``raw/discussions/<number>.json`` are the only
persisted representation of a proposal deep read. The entity
``raw_state.proposal_read`` pointer is a derived view and is rebuilt from the
documents.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ..errors import StoreError
from ..models import entity_id, make_entity, validate_document
from ..store import SnapshotStore
from .status import (
    DISCUSSION_DETAIL_SECTIONS,
    ISSUE_DETAIL_SECTIONS,
    derive_discussion_detail_status,
    derive_discussion_limitations,
    derive_issue_detail_status,
    derive_issue_limitations,
)

ISSUE_DIR = "raw/issues"
DISCUSSION_DIR = "raw/discussions"
CARRIERS = ("issue", "discussion")


def issue_detail_path(number: int) -> str:
    return f"{ISSUE_DIR}/{number}.json"


def discussion_detail_path(number: int) -> str:
    return f"{DISCUSSION_DIR}/{number}.json"


def detail_path(carrier: str, number: int) -> str:
    if carrier == "issue":
        return issue_detail_path(number)
    if carrier == "discussion":
        return discussion_detail_path(number)
    raise StoreError(f"未知 proposal carrier：{carrier}", source="proposal_store.carrier")


def validate_issue_document(document: Mapping[str, Any]) -> None:
    validate_document(document, "issue_detail")
    expected = derive_issue_detail_status(document)
    if document["coverage"]["detail"] != expected:
        raise StoreError(
            "issue_detail.coverage.detail 与 section 状态不一致",
            source="proposal_store.invariant",
            details={
                "number": document.get("number"),
                "stored": document["coverage"]["detail"],
                "derived": expected,
            },
        )
    for key in ISSUE_DETAIL_SECTIONS:
        if document["coverage"][key] != document[key]["status"]:
            raise StoreError(
                f"issue_detail.coverage.{key} 与 section 状态不一致",
                source="proposal_store.invariant",
                details={"number": document.get("number")},
            )
    if document["coverage"]["body"] != "complete":
        raise StoreError(
            "issue_detail.coverage.body 必须为 complete",
            source="proposal_store.invariant",
            details={"number": document.get("number")},
        )
    if document["limitations"] != derive_issue_limitations(document):
        raise StoreError(
            "issue_detail.limitations 不是从文档派生",
            source="proposal_store.invariant",
            details={"number": document.get("number")},
        )


def validate_discussion_document(document: Mapping[str, Any]) -> None:
    validate_document(document, "discussion_detail")
    expected = derive_discussion_detail_status(document)
    if document["coverage"]["detail"] != expected:
        raise StoreError(
            "discussion_detail.coverage.detail 与 section 状态不一致",
            source="proposal_store.invariant",
            details={
                "number": document.get("number"),
                "stored": document["coverage"]["detail"],
                "derived": expected,
            },
        )
    for key in DISCUSSION_DETAIL_SECTIONS:
        if document["coverage"][key] != document[key]["status"]:
            raise StoreError(
                f"discussion_detail.coverage.{key} 与 section 状态不一致",
                source="proposal_store.invariant",
                details={"number": document.get("number")},
            )
    if document["coverage"]["body"] != "complete":
        raise StoreError(
            "discussion_detail.coverage.body 必须为 complete",
            source="proposal_store.invariant",
            details={"number": document.get("number")},
        )
    if document["limitations"] != derive_discussion_limitations(document):
        raise StoreError(
            "discussion_detail.limitations 不是从文档派生",
            source="proposal_store.invariant",
            details={"number": document.get("number")},
        )


def write_issue_document(store: SnapshotStore, document: Mapping[str, Any]) -> None:
    validate_issue_document(document)
    store.write_json(
        issue_detail_path(int(document["number"])),
        document,
        schema_name="issue_detail",
    )


def write_discussion_document(store: SnapshotStore, document: Mapping[str, Any]) -> None:
    validate_discussion_document(document)
    store.write_json(
        discussion_detail_path(int(document["number"])),
        document,
        schema_name="discussion_detail",
    )


def _iter_documents(
    store: SnapshotStore,
    directory: str,
    validate,
) -> Iterator[dict[str, Any]]:
    base = store.secure_path(directory)
    if not base.is_dir():
        return
    paths = [
        path
        for path in base.iterdir()
        if path.is_file() and path.suffix == ".json" and path.stem.isdigit()
    ]
    for path in sorted(paths, key=lambda item: int(item.stem)):
        document = store.read_json(f"{directory}/{path.name}")
        validate(document)
        yield document


def iter_issue_documents(store: SnapshotStore) -> Iterator[dict[str, Any]]:
    return _iter_documents(store, ISSUE_DIR, validate_issue_document)


def iter_discussion_documents(store: SnapshotStore) -> Iterator[dict[str, Any]]:
    return _iter_documents(store, DISCUSSION_DIR, validate_discussion_document)


def load_issue_documents(store: SnapshotStore) -> list[dict[str, Any]]:
    return list(iter_issue_documents(store))


def load_discussion_documents(store: SnapshotStore) -> list[dict[str, Any]]:
    return list(iter_discussion_documents(store))


def proposal_pointer(carrier: str, document: Mapping[str, Any]) -> dict[str, Any]:
    number = int(document["number"])
    return {
        "carrier": carrier,
        "number": number,
        "detail_path": detail_path(carrier, number),
        "scope": document["scope"],
        "observed_at": document["observed_at"],
    }


def _new_proposal_entity(
    host: str,
    owner: str,
    name: str,
    carrier: str,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    number = int(document["number"])
    return make_entity(
        entity_id=entity_id(host, owner, name, carrier, str(number)),
        kind=carrier,
        title=str(document["title"] or f"#{number}"),
        url=str(document["url"]),
        observed_at=str(document["observed_at"]),
        number=number,
        created_at=document["created_at"],
        updated_at=document["updated_at"],
        source_event_at=document["updated_at"] or document["created_at"],
        # Role hints are candidate signals, not confirmed research claims.
        roles=(),
        raw_state={
            "proposal_read": proposal_pointer(carrier, document),
        },
        normalized_state={
            "state": _entity_state(carrier, document),
            "scope": document["scope"],
            "role_hints": list(document["role_hints"]),
        },
        discovery_reasons=["proposal_read"],
    )


def _entity_state(carrier: str, document: Mapping[str, Any]) -> str:
    if carrier == "issue":
        return str(document["state"])
    return "closed" if document["closed"] else "open"


def _apply_proposal_projection(
    existing: Mapping[str, Any],
    carrier: str,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Refresh only the derived pointer; activity owns entity timestamps."""
    projected = dict(existing)
    raw_state = dict(existing.get("raw_state") or {})
    raw_state["proposal_read"] = proposal_pointer(carrier, document)
    projected["raw_state"] = raw_state
    normalized = dict(existing.get("normalized_state") or {})
    normalized["scope"] = document["scope"]
    normalized["role_hints"] = list(document["role_hints"])
    projected["normalized_state"] = normalized
    return projected


def project_proposal_read(
    store: SnapshotStore,
    host: str,
    owner: str,
    name: str,
    carrier: str,
    document: Mapping[str, Any],
) -> None:
    number = int(document["number"])
    identifier = entity_id(host, owner, name, carrier, str(number))
    existing = next(
        (
            entity
            for entity in store.read_jsonl("normalized/entities.jsonl")
            if isinstance(entity, Mapping) and entity.get("id") == identifier
        ),
        None,
    )
    projected = (
        _apply_proposal_projection(existing, carrier, document)
        if isinstance(existing, Mapping)
        else _new_proposal_entity(host, owner, name, carrier, document)
    )
    validate_document(projected, "entity")
    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [projected],
        key="id",
        schema_name="entity",
    )


def reconcile_proposal_projection(
    store: SnapshotStore,
    documents: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    host: str,
    owner: str,
    name: str,
) -> None:
    """Project issue/discussion pointers in one pass over the entity stream.

    A single malformed historical document must not block the run: projection
    failures are isolated and repaired by ``verify_integrity`` later.
    """
    existing = store.read_jsonl("normalized/entities.jsonl")
    by_id = {
        entity["id"]: entity
        for entity in existing
        if isinstance(entity.get("id"), str)
    }
    changed = False
    for carrier, document in documents:
        number = int(document["number"])
        identifier = entity_id(host, owner, name, carrier, str(number))
        try:
            previous = by_id.get(identifier)
            projected = (
                _apply_proposal_projection(previous, carrier, document)
                if isinstance(previous, Mapping)
                else _new_proposal_entity(host, owner, name, carrier, document)
            )
            validate_document(projected, "entity")
        except Exception as exc:
            print(
                f"warning: 无法投影 {carrier} #{number}: {exc}",
                file=sys.stderr,
            )
            continue
        by_id[identifier] = projected
        changed = True
    if not changed:
        return
    store.write_jsonl(
        "normalized/entities.jsonl",
        list(by_id.values()),
        key="id",
        on_conflict="replace",
    )


def load_proposal_documents(
    store: SnapshotStore,
) -> list[tuple[str, dict[str, Any]]]:
    documents = [("issue", item) for item in iter_issue_documents(store)]
    documents.extend(("discussion", item) for item in iter_discussion_documents(store))
    return documents


def proposal_pointer_problems(
    document: Mapping[str, Any],
    carrier: str,
    entity: Mapping[str, Any] | None,
) -> str | None:
    """Return a problem code when the entity pointer is missing or stale."""
    expected = proposal_pointer(carrier, document)
    raw_state = entity.get("raw_state") if isinstance(entity, Mapping) else None
    pointer = raw_state.get("proposal_read") if isinstance(raw_state, Mapping) else None
    if not isinstance(pointer, Mapping):
        return "missing_proposal_projection"
    if all(pointer.get(key) == value for key, value in expected.items()):
        return None
    return "stale_proposal_projection"


__all__ = [
    "CARRIERS",
    "DISCUSSION_DIR",
    "ISSUE_DIR",
    "detail_path",
    "discussion_detail_path",
    "issue_detail_path",
    "iter_discussion_documents",
    "iter_issue_documents",
    "load_discussion_documents",
    "load_issue_documents",
    "load_proposal_documents",
    "proposal_pointer",
    "proposal_pointer_problems",
    "project_proposal_read",
    "reconcile_proposal_projection",
    "validate_discussion_document",
    "validate_issue_document",
    "write_discussion_document",
    "write_issue_document",
]
