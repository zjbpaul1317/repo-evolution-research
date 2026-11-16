"""Shared relation identity, verification and observation merge policy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

CORE_SCOPE_FIELDS = ("artifact", "generator", "observed_at", "provider", "source_url")
INTERNAL_SCOPE_FIELDS = ("observations", "aliases", "resolution")
_BASIS_RANK = {"agent_inferred": 0, "text_explicit": 1, "api_explicit": 2}
_OBSERVATION_FIELDS = (
    "artifact",
    "content_sha256",
    "observed_at",
    "provider",
    "source_url",
    "source_revision",
    "evidence_ids",
    "verification",
    "basis",
    "completeness",
)


def relation_identity(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(record.get("from_id") or ""),
        str(record.get("to_id") or ""),
        str(record.get("type") or ""),
    )


def merge_relation_verification(left: str, right: str) -> str:
    if "disputed" in (left, right):
        return "disputed"
    if "verified" in (left, right):
        return "verified"
    return "candidate"


def merge_relation_evidence(
    left: Iterable[Any],
    right: Iterable[Any],
) -> list[str]:
    return sorted(
        {
            str(value)
            for value in [*(left or []), *(right or [])]
            if value is not None
        }
    )


def _normalize_observation(raw: Mapping[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = {}
    for key in _OBSERVATION_FIELDS:
        value = raw.get(key)
        if key == "evidence_ids":
            observation[key] = sorted(
                {str(item) for item in value or [] if item is not None}
            )
        elif key in ("artifact", "observed_at", "provider", "source_url"):
            observation[key] = None if value is None else str(value)
        else:
            observation[key] = value
    return observation


def seed_observations(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    scope = record.get("scope")
    scope_map = scope if isinstance(scope, Mapping) else {}
    raw_observations = scope_map.get("observations")
    if isinstance(raw_observations, list):
        observations = [
            _normalize_observation(item)
            for item in raw_observations
            if isinstance(item, Mapping)
        ]
        if observations:
            return observations
    return [
        _normalize_observation(
            {
                "artifact": scope_map.get("artifact"),
                "content_sha256": None,
                "observed_at": scope_map.get("observed_at"),
                "provider": scope_map.get("provider"),
                "source_url": scope_map.get("source_url"),
                "source_revision": scope_map.get("source_revision"),
                "evidence_ids": record.get("evidence_ids") or [],
                "verification": record.get("verification") or "candidate",
                "basis": record.get("basis") or "agent_inferred",
                "completeness": scope_map.get("completeness"),
            }
        )
    ]


def _observation_key(observation: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        observation.get("artifact"),
        observation.get("content_sha256"),
        observation.get("observed_at"),
        observation.get("provider"),
        observation.get("source_url"),
        observation.get("source_revision"),
        observation.get("verification"),
        observation.get("basis"),
        observation.get("completeness"),
        tuple(observation.get("evidence_ids") or []),
    )


def _observation_sort_key(observation: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(observation.get("observed_at") or ""),
        str(observation.get("artifact") or ""),
        str(observation.get("provider") or ""),
        str(observation.get("source_url") or ""),
        str(observation.get("source_revision") or ""),
        str(observation.get("verification") or ""),
        str(observation.get("basis") or ""),
    )


def merge_observations(
    left: Iterable[Mapping[str, Any]],
    right: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in [*left, *right]:
        observation = _normalize_observation(raw)
        merged[_observation_key(observation)] = observation
    return sorted(merged.values(), key=_observation_sort_key)


def select_basis(
    observations: Iterable[Mapping[str, Any]],
    verification: str,
) -> str:
    items = list(observations)
    candidates = [
        item for item in items if str(item.get("verification")) == verification
    ]
    if not candidates:
        candidates = items
    if not candidates:
        return "agent_inferred"
    selected = max(
        candidates,
        key=lambda item: (
            _BASIS_RANK.get(str(item.get("basis")), -1),
            str(item.get("observed_at") or ""),
        ),
    )
    return str(selected.get("basis") or "agent_inferred")


def merge_relation_scope(
    prior_scope: Any,
    incoming_scope: Any,
    observations: Iterable[Mapping[str, Any]],
    *,
    alias: str | None = None,
) -> dict[str, Any]:
    prior = dict(prior_scope) if isinstance(prior_scope, Mapping) else {}
    incoming = dict(incoming_scope) if isinstance(incoming_scope, Mapping) else {}
    merged = dict(prior)
    for key, value in incoming.items():
        if key in CORE_SCOPE_FIELDS or key in INTERNAL_SCOPE_FIELDS:
            continue
        merged.setdefault(key, value)
    for key in CORE_SCOPE_FIELDS:
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    merged["observations"] = sorted(
        (dict(item) for item in observations),
        key=_observation_sort_key,
    )
    aliases: set[str] = set()
    for source in (prior, incoming):
        raw_aliases = source.get("aliases")
        if isinstance(raw_aliases, list):
            aliases.update(
                str(value)
                for value in raw_aliases
                if isinstance(value, str) and value
            )
    if alias:
        aliases.add(alias)
    if aliases:
        merged["aliases"] = sorted(aliases)
    if (
        prior.get("generator") == "import-source"
        or incoming.get("generator") == "import-source"
    ):
        merged["generator"] = "import-source"
    return merged


def merge_relation_records(
    prior: Mapping[str, Any],
    incoming: Mapping[str, Any],
    *,
    alias: str | None = None,
) -> dict[str, Any]:
    merged = dict(prior)
    prior_observations = seed_observations(prior)
    incoming_observations = seed_observations(incoming)
    observations = merge_observations(prior_observations, incoming_observations)
    merged["evidence_ids"] = merge_relation_evidence(
        prior.get("evidence_ids") or [],
        incoming.get("evidence_ids") or [],
    )
    merged["verification"] = merge_relation_verification(
        str(prior.get("verification") or "candidate"),
        str(incoming.get("verification") or "candidate"),
    )
    merged["basis"] = select_basis(observations, str(merged["verification"]))
    merged["scope"] = merge_relation_scope(
        prior.get("scope"),
        incoming.get("scope"),
        observations,
        alias=alias,
    )
    return merged


def collapse_relations_by_identity(
    relations: Iterable[Mapping[str, Any]],
    *,
    merge_scope: bool = False,
) -> list[dict[str, Any]]:
    ordered = sorted(
        (dict(item) for item in relations if isinstance(item, Mapping)),
        key=lambda item: str(item.get("id") or ""),
    )
    collapsed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in ordered:
        identity = relation_identity(record)
        current = collapsed.get(identity)
        if current is None:
            collapsed[identity] = record
            continue
        if merge_scope:
            collapsed[identity] = merge_relation_records(
                current,
                record,
                alias=str(record.get("id") or ""),
            )
            continue
        current["evidence_ids"] = merge_relation_evidence(
            current.get("evidence_ids") or [],
            record.get("evidence_ids") or [],
        )
        current["verification"] = merge_relation_verification(
            str(current.get("verification") or "candidate"),
            str(record.get("verification") or "candidate"),
        )
    return sorted(collapsed.values(), key=lambda item: str(item.get("id") or ""))


def choose_canonical_relation(
    group: Iterable[Mapping[str, Any]],
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    records = [dict(item) for item in group if isinstance(item, Mapping)]
    incoming_id = str(incoming.get("id") or "")
    for record in records:
        if str(record.get("id") or "") == incoming_id:
            return record
    imported = [
        record
        for record in records
        if isinstance(record.get("scope"), Mapping)
        and record["scope"].get("generator") == "import-source"
    ]
    pool = imported or records
    return min(pool, key=lambda record: str(record.get("id") or ""))


__all__ = [
    "CORE_SCOPE_FIELDS",
    "INTERNAL_SCOPE_FIELDS",
    "choose_canonical_relation",
    "collapse_relations_by_identity",
    "merge_observations",
    "merge_relation_evidence",
    "merge_relation_records",
    "merge_relation_scope",
    "merge_relation_verification",
    "relation_identity",
    "seed_observations",
    "select_basis",
]
