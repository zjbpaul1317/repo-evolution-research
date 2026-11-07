"""Deterministic pull-request deep-read scoring and quota selection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..diversity import diverse_order, record_area
from ..models import entity_id
from ..store import SnapshotStore
from .values import as_int, labels, merged_at, parse_datetime, reaction_count

MERGED_SHARE = (14, 22)
RECENT_DAYS = 30


@dataclass
class Candidate:
    number: int
    entity_id: str
    raw: dict[str, Any]
    bucket: str
    score: int
    components: dict[str, int]
    reasons: list[str]
    selected: bool = False
    skipped_reason: str | None = None
    status: str = "not_attempted"


def build_candidates(
    store: SnapshotStore,
    overview: Mapping[str, Any] | None,
    focus: list[str] | tuple[str, ...],
    *,
    start: datetime,
    end: datetime,
    host: str,
    owner: str,
    name: str,
) -> list[Candidate]:
    if overview is None:
        return []
    raw_records: dict[int, dict[str, Any]] = {}
    for record in store.read_jsonl("raw/prs.jsonl"):
        number = record.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            raw_records[number] = record
    wanted = _pr_ids_by_number(overview)
    strict = _has_pr_views(overview)
    discovery: dict[str, list[str]] = {}
    for entity in store.read_jsonl("normalized/entities.jsonl"):
        if not isinstance(entity, Mapping) or not isinstance(entity.get("id"), str):
            continue
        values = entity.get("discovery_reasons")
        if isinstance(values, list):
            discovery[entity["id"]] = [
                value for value in values if isinstance(value, str) and value
            ]
    focus_tokens = [item.strip().casefold() for item in focus if item.strip()]
    recent_cutoff = end - timedelta(days=RECENT_DAYS)
    candidates: list[Candidate] = []
    for number in sorted(raw_records):
        raw = raw_records[number]
        if strict and number not in wanted:
            continue
        identifier = wanted.get(number) or entity_id(
            host, owner, name, "pull_request", str(number)
        )
        merged_moment = parse_datetime(merged_at(raw))
        bucket = (
            "merged" if merged_moment is not None
            else "closed" if str(raw.get("state") or "").casefold() == "closed"
            else "open"
        )
        components: dict[str, int] = {}
        reasons: list[str] = []
        if merged_moment is not None:
            components["merged"] = 10
            reasons.append("merged")
            if start <= merged_moment < end:
                components["merged_in_window"] = 40
                reasons.append("merged_in_window")
        discovered = discovery.get(identifier, [])
        if discovered:
            components["discovery_reasons"] = min(len(discovered), 2) * 5
            reasons.extend(f"discovered:{value}" for value in discovered)
        if focus_tokens:
            haystack = " ".join(
                [str(raw.get("title") or "")]
                + [str(label) for label in labels(raw)]
            ).casefold()
            matched = [token for token in focus_tokens if token in haystack]
            if matched:
                components["focus_match"] = 30
                reasons.extend(f"focus_match:{token}" for token in matched)
        comments = as_int(raw.get("comments"))
        if comments:
            components["comments"] = min(comments, 20)
            if comments >= 5:
                reasons.append("active_discussion")
        reactions = max(0, reaction_count(raw, allow_upvote=False) or 0)
        if reactions:
            components["reactions"] = min(reactions, 10)
            if reactions >= 5:
                reasons.append("notable_reactions")
        updated_at = parse_datetime(raw.get("updated_at"))
        if updated_at is not None and updated_at >= recent_cutoff:
            components["recent_update"] = 10
            reasons.append("recent_update")
        if bool(raw.get("draft")):
            components["draft"] = -20
            reasons.append("draft")
        score = sum(components.values())
        candidates.append(
            Candidate(
                number=number,
                entity_id=identifier,
                raw=dict(raw),
                bucket=bucket,
                score=score,
                components=components,
                reasons=sorted(dict.fromkeys(reasons)),
            )
        )
    return sorted(candidates, key=lambda item: (-item.score, -item.number))


def quota_for_budget(
    budget: Mapping[str, Any],
    used_deep_entities: int = 0,
) -> tuple[int, int, int]:
    max_prs = int(budget.get("max_deep_prs", MERGED_SHARE[1]))
    max_entities = int(budget.get("max_deep_entities", max_prs))
    reserve = max(0, int(budget.get("agent_pr_reserve", 0)))
    quota = max(0, min(max_prs, max(0, max_entities - used_deep_entities)) - reserve)
    if quota == 0:
        return 0, 0, 0
    numerator, denominator = MERGED_SHARE
    merged_quota = max(1, round(quota * numerator / denominator))
    merged_quota = min(merged_quota, quota)
    return quota, merged_quota, quota - merged_quota


def choose(
    candidates: list[Candidate],
    quota: int,
    merged_quota: int,
    open_quota: int,
) -> None:
    merged = diverse_order(
        [item for item in candidates if item.bucket == "merged"],
        key=lambda item: record_area(item.raw), reserve=max(1, (merged_quota + 1) // 2),
    )
    opened = diverse_order(
        [item for item in candidates if item.bucket == "open"],
        key=lambda item: record_area(item.raw), reserve=max(1, (open_quota + 1) // 2),
    )
    chosen = merged[:merged_quota] + opened[:open_quota]
    slots = quota - len(chosen)
    if slots > 0:
        leftovers = sorted(
            merged[merged_quota:] + opened[open_quota:]
            + [item for item in candidates if item.bucket == "closed"],
            key=lambda item: (-item.score, -item.number),
        )
        chosen.extend(leftovers[:slots])
    chosen_ids = {id(item) for item in chosen}
    for candidate in candidates:
        if id(candidate) in chosen_ids:
            candidate.selected = True
        else:
            candidate.status = "skipped_quota"
            candidate.skipped_reason = "quota_exhausted"


def derive_selection_counts(
    candidates: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    """Derive the persisted `pr_selection.counts` object from candidates."""
    materialized = list(candidates)
    selected = [item for item in materialized if item.get("selected") is True]
    return {
        "candidates": len(materialized),
        "selected": len(selected),
        "merged_selected": sum(1 for item in selected if item.get("bucket") == "merged"),
        "open_selected": sum(1 for item in selected if item.get("bucket") == "open"),
        "skipped_quota": sum(
            1 for item in materialized if item.get("status") == "skipped_quota"
        ),
        "skipped_unchanged": sum(
            1 for item in materialized if item.get("status") == "skipped_unchanged"
        ),
        "failed": sum(1 for item in materialized if item.get("status") == "failed"),
        "not_attempted": sum(
            1
            for item in materialized
            if item.get("selected") is True and item.get("status") == "not_attempted"
        ),
    }


def derive_fetched_count(candidates: Iterable[Mapping[str, Any]]) -> int:
    """Count selected PRs whose document was rebuilt or confirmed unchanged."""
    return sum(
        1
        for item in candidates
        if item.get("selected") is True
        and item.get("status") in ("selected", "skipped_unchanged")
    )


def _overview_pr_ids(overview: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(overview, Mapping):
        return set()
    ids: set[str] = set()
    views = overview.get("views")
    if not isinstance(views, list):
        return ids
    for view in views:
        if not isinstance(view, Mapping) or view.get("kind") != "pull_request":
            continue
        entries = view.get("candidate_ids")
        if isinstance(entries, list):
            ids.update(item for item in entries if isinstance(item, str))
    return ids


def _has_pr_views(overview: Mapping[str, Any] | None) -> bool:
    if not isinstance(overview, Mapping):
        return False
    views = overview.get("views")
    if not isinstance(views, list):
        return False
    return any(
        isinstance(view, Mapping) and view.get("kind") == "pull_request"
        for view in views
    )


def _pr_ids_by_number(overview: Mapping[str, Any] | None) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for identifier in _overview_pr_ids(overview):
        suffix = identifier.rsplit("/", 1)[-1]
        if suffix.isdigit():
            mapping[int(suffix)] = identifier
    return mapping


__all__ = [
    "Candidate",
    "build_candidates",
    "choose",
    "derive_fetched_count",
    "derive_selection_counts",
    "quota_for_budget",
]
