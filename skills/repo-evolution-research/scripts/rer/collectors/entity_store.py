"""Shared entity merge and deep-read projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import entity_id, make_entity, validate_document
from ..store import SnapshotStore

PR_DETAIL_DIR = "raw/pr-details"

_DETAIL_FIELDS = frozenset(
    {"files", "commits", "reviews", "review_comments", "diff", "timeline"}
)
_LEGACY_DEEP_READ_FIELDS = ("deep_read", "files_truncated", "commits_truncated")


def detail_path(number: int) -> str:
    return f"{PR_DETAIL_DIR}/{number}.json"


def merge_entity(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in (
        "id",
        "kind",
        "title",
        "url",
        "observed_at",
        "source_event_at",
        "number",
        "node_id",
        "created_at",
        "updated_at",
        "external",
    ):
        value = incoming.get(key)
        if value is not None or key in ("title", "url", "observed_at", "external"):
            merged[key] = value
    for key in ("roles", "evidence_ids", "discovery_reasons"):
        merged[key] = sorted(set(existing.get(key, [])) | set(incoming.get(key, [])))
    merged["raw_state"] = merge_raw_state(
        existing.get("raw_state") if isinstance(existing.get("raw_state"), dict) else {},
        incoming.get("raw_state") if isinstance(incoming.get("raw_state"), dict) else {},
    )
    merged["normalized_state"] = {
        **(existing.get("normalized_state") or {}),
        **(incoming.get("normalized_state") or {}),
    }
    return merged


def merge_raw_state(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key in _DETAIL_FIELDS and existing.get(key) not in (None, "", [], {}):
            continue
        if key == "body" and value in (None, "") and existing.get("body"):
            continue
        if key in ("merged_at", "closed_at") and value is None and existing.get(key):
            if key == "closed_at" and incoming.get("state") == "open":
                merged[key] = None
                continue
            continue
        merged[key] = value
    existing_pull_request = merged.get("pull_request")
    incoming_pull_request = incoming.get("pull_request")
    if isinstance(incoming_pull_request, Mapping):
        combined = (
            dict(existing_pull_request)
            if isinstance(existing_pull_request, dict)
            else {}
        )
        for key, value in incoming_pull_request.items():
            if key == "merged_at" and value is None and combined.get("merged_at"):
                continue
            combined[key] = value
        merged["pull_request"] = combined
    return merged


def merge_entity_stream(
    store: SnapshotStore,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing = {
        entity["id"]: entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if isinstance(entity, dict) and isinstance(entity.get("id"), str)
    }
    merged: list[dict[str, Any]] = []
    for entity in incoming:
        previous = existing.get(entity["id"])
        merged.append(merge_entity(previous, entity) if previous else entity)
    return sorted(merged, key=lambda item: item["id"])


def project_pr_detail(
    store: SnapshotStore,
    host: str,
    owner: str,
    name: str,
    document: Mapping[str, Any],
) -> None:
    target = entity_id(host, owner, name, "pull_request", str(document["number"]))
    for entity in store.read_jsonl("normalized/entities.jsonl"):
        if not isinstance(entity, dict) or entity.get("id") != target:
            continue
        merged = dict(entity)
        merged["raw_state"] = {
            **(entity.get("raw_state") or {}),
            "deep_read": deep_read_state(document),
        }
        normalized_state = dict(entity.get("normalized_state") or {})
        for key in _LEGACY_DEEP_READ_FIELDS:
            normalized_state.pop(key, None)
        merged["normalized_state"] = normalized_state
        validate_document(merged, "entity")
        store.upsert_jsonl(
            "normalized/entities.jsonl",
            [merged],
            key="id",
            schema_name="entity",
        )
        return
    created = entity_from_detail(host, owner, name, document)
    validate_document(created, "entity")
    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [created],
        key="id",
        schema_name="entity",
    )


def prune_deep_read(store: SnapshotStore, numbers: set[int]) -> bool:
    """Drop deep-read projections whose authoritative document no longer exists."""
    changed = False
    for entity in store.read_jsonl("normalized/entities.jsonl"):
        if not isinstance(entity, dict) or entity.get("kind") != "pull_request":
            continue
        number = entity.get("number")
        if isinstance(number, int) and not isinstance(number, bool) and number in numbers:
            continue
        raw_state = dict(entity.get("raw_state") or {})
        normalized_state = dict(entity.get("normalized_state") or {})
        if "deep_read" not in raw_state and not any(
            key in normalized_state for key in _LEGACY_DEEP_READ_FIELDS
        ):
            continue
        raw_state.pop("deep_read", None)
        for key in _LEGACY_DEEP_READ_FIELDS:
            normalized_state.pop(key, None)
        merged = {
            **entity,
            "raw_state": raw_state,
            "normalized_state": normalized_state,
        }
        validate_document(merged, "entity")
        store.upsert_jsonl(
            "normalized/entities.jsonl",
            [merged],
            key="id",
            schema_name="entity",
        )
        changed = True
    return changed


def entity_from_detail(
    host: str,
    owner: str,
    name: str,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    number = int(document["number"])
    deep_read = deep_read_state(document)
    return make_entity(
        entity_id=entity_id(host, owner, name, "pull_request", str(number)),
        kind="pull_request",
        title=str(document["title"]),
        url=str(document["url"]),
        observed_at=str(document["observed_at"]),
        raw_state={
            "state": document["state"],
            "merged": document["merged"],
            "merged_at": document["merged_at"],
            "closed_at": document["closed_at"],
            "deep_read": deep_read,
        },
        normalized_state={
            "state": document["state"],
            "merged": document["merged"],
            "merged_at": document["merged_at"],
        },
        number=number,
        created_at=document["created_at"],
        updated_at=document["updated_at"],
        source_event_at=document["updated_at"] or document["created_at"],
        discovery_reasons=["pr_detail"],
        external=False,
    )


def deep_read_state(document: Mapping[str, Any]) -> dict[str, Any]:
    """Pointer only: status, counts and limitations live in the document."""
    return {
        "detail_path": detail_path(int(document["number"])),
        "head_sha": document["head_sha"],
        "observed_at": document["observed_at"],
    }


__all__ = [
    "PR_DETAIL_DIR",
    "deep_read_state",
    "detail_path",
    "entity_from_detail",
    "merge_entity",
    "merge_entity_stream",
    "merge_raw_state",
    "project_pr_detail",
    "prune_deep_read",
]
