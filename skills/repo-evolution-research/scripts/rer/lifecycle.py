"""Pure lifecycle rule engine for capability states and formal assertions.

The module never performs network I/O and never mutates its inputs. It turns
entity/relation/evidence metadata plus optional PR and release documents into a
deterministic lifecycle result that validation can consume.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .relation_policy import collapse_relations_by_identity

PROPOSAL_PRIORITY = {
    "unknown": 0,
    "superseded": 1,
    "rejected": 2,
    "proposed": 3,
    "discussing": 4,
    "accepted": 5,
}
_LABEL_ACCEPTED = frozenset({"accepted", "accept", "approved"})
_LABEL_REJECTED = frozenset({"rejected", "declined", "wontfix"})
_LABEL_SUPERSEDED = frozenset({"superseded", "duplicate"})
_RELATION_TYPES = frozenset(
    {
        "contains_commit",
        "documents",
        "implements_part_of",
        "release_note_mentions",
        "reverts",
        "verified_in_release",
    }
)
_TRUSTED_VERIFICATIONS = frozenset({"verified"})
_MAX_HOPS = 2


def _trusted_relation(relation: Mapping[str, Any]) -> bool:
    """Only verified relations may support capability state assertions."""
    return (
        relation.get("type") in _RELATION_TYPES
        and relation.get("verification") in _TRUSTED_VERIFICATIONS
    )


def verify_capability(
    capability: Mapping[str, Any],
    entities: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    pr_documents: Mapping[Any, Mapping[str, Any]] | Iterable[Mapping[str, Any]] | None = None,
    release_documents: Mapping[Any, Mapping[str, Any]] | Iterable[Mapping[str, Any]] | None = None,
    *,
    claims: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Derive proposal/implementation/delivery/availability for one capability."""
    entity_map = _index_entities(entities)
    evidence_map = _index_by_id(evidence)
    relation_list = collapse_relations_by_identity(relations)
    claim_list = [
        dict(item) for item in (claims or []) if isinstance(item, Mapping)
    ]
    pr_map = _index_pr_documents(pr_documents)
    release_map = _index_release_documents(release_documents)
    capability = dict(capability)
    capability_id = str(capability.get("id") or "")
    relevant_claims = [
        claim
        for claim in claim_list
        if _claim_capability_id(claim) == capability_id
    ]

    direct_ids = _direct_entity_ids(capability, evidence_map, relevant_claims)
    supporting_ids = _bounded_neighbors(direct_ids, relation_list, entity_map)
    supporting = _supporting_entities(supporting_ids, entity_map)
    gaps: set[str] = set()
    conflicts: list[dict[str, Any]] = []
    evidence_ids: set[str] = set(
        str(value) for value in capability.get("evidence_ids") or []
    )
    for claim in relevant_claims:
        evidence_ids.update(
            str(value) for value in claim.get("evidence_ids") or []
        )
    for relation in relation_list:
        if (
            relation.get("from_id") in supporting_ids
            or relation.get("to_id") in supporting_ids
        ) and _trusted_relation(relation):
            evidence_ids.update(
                str(value) for value in relation.get("evidence_ids") or []
            )
    for evidence_id in sorted(evidence_ids):
        if evidence_id not in evidence_map:
            gaps.add(f"missing_evidence:{evidence_id}")

    proposal_state = _derive_proposal_state(
        supporting,
        entity_map,
        gaps,
    )
    implementation_state, target_branch, observed_revision = _derive_implementation(
        capability,
        supporting,
        entity_map,
        relation_list,
        pr_map,
        gaps,
    )
    delivery_state, delivery_verification, verified_versions = _derive_delivery(
        supporting,
        entity_map,
        relation_list,
        pr_map,
        release_map,
        gaps,
    )
    availability = _derive_availability(
        capability,
        supporting,
        entity_map,
        evidence_map,
        relation_list,
        gaps,
    )
    _record_conflicts(
        capability,
        proposal_state=proposal_state,
        implementation_state=implementation_state,
        delivery_state=delivery_state,
        availability=availability,
        conflicts=conflicts,
    )
    state_time_basis = _state_time_basis(relevant_claims)
    return {
        "capability_id": capability_id,
        "proposal_state": proposal_state,
        "implementation_state": implementation_state,
        "delivery_state": delivery_state,
        "delivery_verification": delivery_verification,
        "availability": availability,
        "target_branch": target_branch,
        "observed_revision": observed_revision,
        "verified_versions": sorted(set(verified_versions)),
        "supporting_entities": supporting,
        "evidence_ids": sorted(evidence_ids),
        "gaps": sorted(gaps),
        "conflicts": sorted(
            conflicts,
            key=lambda item: (str(item.get("field")), str(item.get("message"))),
        ),
        "state_time_basis": state_time_basis,
    }


def check_state_assertions(
    claims: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    capabilities: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
    lifecycle_results: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Formal gate for declared capability states and claim scopes."""
    capability_map = _index_capabilities(capabilities)
    lifecycle_map = _index_lifecycle(lifecycle_results)
    issues: list[dict[str, Any]] = []
    for capability_id, capability in sorted(capability_map.items()):
        result = lifecycle_map.get(capability_id)
        if result is None:
            issues.append(
                {
                    "code": "missing_lifecycle_result",
                    "capability_id": capability_id,
                    "message": "capability 缺少 lifecycle 结果",
                }
            )
            continue
        declared = str(capability.get("implementation_state") or "unknown")
        derived = str(result.get("implementation_state") or "unknown")
        if declared == "merged" and derived != "merged":
            requirements = capability.get("requirements")
            remaining = (
                requirements.get("remaining")
                if isinstance(requirements, Mapping)
                else None
            )
            code = (
                "reverted_merged_conflict"
                if derived == "reverted"
                else (
                    "merged_with_remaining_requirements"
                    if isinstance(remaining, list) and remaining
                    else "merged_state_conflict"
                )
            )
            issues.append(
                {
                    "code": code,
                    "capability_id": capability_id,
                    "declared": declared,
                    "derived": derived,
                    "message": "capability 声明 merged，但 lifecycle 不支持 merged",
                }
            )
        if declared == "reverted" and derived == "merged":
            issues.append(
                {
                    "code": "reverted_merged_conflict",
                    "capability_id": capability_id,
                    "declared": declared,
                    "derived": derived,
                    "message": "capability 声明 reverted，但 lifecycle 只看到 merged",
                }
            )
        delivery = str(capability.get("delivery_state") or "unknown")
        derived_delivery = str(result.get("delivery_state") or "unknown")
        chronology_gap = any(
            str(gap).startswith("release_before_implementation")
            for gap in result.get("gaps") or []
        )
        if delivery == "released" and (
            derived_delivery != "released"
            or result.get("delivery_verification") != "verified"
        ):
            issues.append(
                {
                    "code": (
                        "release_before_implementation"
                        if chronology_gap
                        else "released_without_verified_release"
                    ),
                    "capability_id": capability_id,
                    "declared": delivery,
                    "derived": derived_delivery,
                    "message": (
                        "release 早于实现 PR 的合并时间，不能作为交付证明"
                        if chronology_gap
                        else "capability 声明 released，但没有 verified_in_release"
                    ),
                }
            )
        elif delivery == "prerelease" and derived_delivery != "prerelease":
            issues.append(
                {
                    "code": (
                        "release_before_implementation"
                        if chronology_gap
                        else "prerelease_state_conflict"
                    ),
                    "capability_id": capability_id,
                    "declared": delivery,
                    "derived": derived_delivery,
                    "message": (
                        "release 早于实现 PR 的合并时间，不能作为交付证明"
                        if chronology_gap
                        else "capability 声明 prerelease，但 lifecycle 不支持"
                    ),
                }
            )
        availability = str(capability.get("availability") or "unknown")
        if availability in ("default_on", "deprecated", "removed"):
            if result.get("availability") != availability:
                issues.append(
                    {
                        "code": "availability_without_document_evidence",
                        "capability_id": capability_id,
                        "declared": availability,
                        "derived": result.get("availability"),
                        "message": "availability 声明缺少明确的配置/文档证据",
                    }
                )
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        claim_id = str(claim.get("id") or "")
        scope = claim.get("scope")
        scope_map = scope if isinstance(scope, Mapping) else {}
        capability_id = str(scope_map.get("capability_id") or "")
        if capability_id and capability_id not in capability_map:
            issues.append(
                {
                    "code": "missing_claim_capability",
                    "claim_id": claim_id,
                    "capability_id": capability_id,
                    "message": "claim.scope.capability_id 不存在",
                }
            )
        kind = str(claim.get("kind") or "")
        if kind == "unknown":
            if not claim.get("reason"):
                issues.append(
                    {
                        "code": "unknown_claim_missing_reason",
                        "claim_id": claim_id,
                        "message": "unknown claim 缺少 reason",
                    }
                )
            if not claim.get("missing_evidence"):
                issues.append(
                    {
                        "code": "unknown_claim_missing_evidence",
                        "claim_id": claim_id,
                        "message": "unknown claim 缺少 missing_evidence",
                    }
                )
        declared_versions: list[str] = []
        version = scope_map.get("version")
        if isinstance(version, str) and version:
            declared_versions.append(version)
        raw_versions = scope_map.get("versions")
        if isinstance(raw_versions, list):
            declared_versions.extend(
                value
                for value in raw_versions
                if isinstance(value, str) and value
            )
        result = lifecycle_map.get(capability_id) if capability_id else None
        if kind == "observed" and result is not None:
            verified_versions = set(result.get("verified_versions", []))
            for declared in dict.fromkeys(declared_versions):
                if declared not in verified_versions:
                    issues.append(
                        {
                            "code": "claim_version_not_verified",
                            "claim_id": claim_id,
                            "capability_id": capability_id,
                            "version": declared,
                            "message": "claim 引用的版本没有 verified_in_release 支持",
                        }
                    )
    return sorted(
        issues,
        key=lambda item: (
            str(item.get("code")),
            str(item.get("capability_id") or ""),
            str(item.get("claim_id") or ""),
        ),
    )


def _index_entities(
    entities: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if isinstance(entities, Mapping):
        return {
            str(key): value
            for key, value in entities.items()
            if isinstance(value, Mapping)
        }
    return {
        str(item["id"]): item
        for item in entities
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


def _index_by_id(
    records: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    values = records.values() if isinstance(records, Mapping) else records
    return {
        str(item["id"]): item
        for item in values
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


def _index_capabilities(
    capabilities: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if isinstance(capabilities, Mapping):
        return {
            str(key): value
            for key, value in capabilities.items()
            if isinstance(value, Mapping)
        }
    return {
        str(item["id"]): item
        for item in capabilities
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }


def _index_lifecycle(
    results: Mapping[str, Mapping[str, Any]] | Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if isinstance(results, Mapping):
        return {
            str(key): value
            for key, value in results.items()
            if isinstance(value, Mapping)
        }
    return {
        str(item["capability_id"]): item
        for item in results
        if isinstance(item, Mapping) and isinstance(item.get("capability_id"), str)
    }


def _index_pr_documents(
    documents: Mapping[Any, Mapping[str, Any]] | Iterable[Mapping[str, Any]] | None,
) -> dict[int, Mapping[str, Any]]:
    if documents is None:
        return {}
    if isinstance(documents, Mapping):
        values = documents.values()
    else:
        values = documents
    result: dict[int, Mapping[str, Any]] = {}
    for item in values:
        if not isinstance(item, Mapping):
            continue
        number = item.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            result[number] = item
    return result


def _index_release_documents(
    documents: Mapping[Any, Mapping[str, Any]] | Iterable[Mapping[str, Any]] | None,
) -> dict[str, Mapping[str, Any]]:
    if documents is None:
        return {}
    if isinstance(documents, Mapping):
        values = documents.values()
    else:
        values = documents
    result: dict[str, Mapping[str, Any]] = {}
    for item in values:
        if not isinstance(item, Mapping):
            continue
        tag = item.get("tag_name")
        if isinstance(tag, str) and tag:
            result[tag] = item
    return result


def _claim_capability_id(claim: Mapping[str, Any]) -> str:
    scope = claim.get("scope")
    if isinstance(scope, Mapping):
        value = scope.get("capability_id")
        if isinstance(value, str):
            return value
    return ""


def _direct_entity_ids(
    capability: Mapping[str, Any],
    evidence_map: Mapping[str, Mapping[str, Any]],
    claims: Iterable[Mapping[str, Any]],
) -> set[str]:
    identifiers: set[str] = set()
    for evidence_id in capability.get("evidence_ids") or []:
        record = evidence_map.get(str(evidence_id))
        if (
            record is not None
            and record.get("verification") == "verified"
            and isinstance(record.get("entity_id"), str)
        ):
            identifiers.add(str(record["entity_id"]))
    for claim in claims:
        scope = claim.get("scope")
        if not isinstance(scope, Mapping):
            continue
        evidence_ids = [
            str(value) for value in claim.get("evidence_ids") or []
        ]
        records = [evidence_map.get(identifier) for identifier in evidence_ids]
        if not records or any(
            record is None or record.get("verification") != "verified"
            for record in records
        ):
            continue
        for value in scope.get("entity_ids") or []:
            if isinstance(value, str):
                identifiers.add(value)
    return identifiers


def _bounded_neighbors(
    direct_ids: set[str],
    relations: Sequence[Mapping[str, Any]],
    entities: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    """Walk at most two explicit relation hops from directly mapped entities."""
    adjacency: dict[str, set[tuple[str, str]]] = {}
    for relation in relations:
        relation_type = str(relation.get("type") or "")
        if not _trusted_relation(relation):
            continue
        from_id = relation.get("from_id")
        to_id = relation.get("to_id")
        if not isinstance(from_id, str) or not isinstance(to_id, str):
            continue
        adjacency.setdefault(from_id, set()).add((to_id, relation_type))
        adjacency.setdefault(to_id, set()).add((from_id, relation_type))
    reached = {identifier for identifier in direct_ids if identifier in entities}
    frontier = set(reached)
    for _hop in range(_MAX_HOPS):
        next_frontier: set[str] = set()
        for identifier in sorted(frontier):
            entity = entities.get(identifier) or {}
            for neighbor, _relation_type in adjacency.get(identifier, set()):
                # A release can provide delivery context, but it must not act
                # as a hub between implementation entities. Otherwise one PR's
                # verified_in_release relation could verify another PR.
                if entity.get("kind") == "release":
                    continue
                if neighbor in reached or neighbor not in entities:
                    continue
                next_frontier.add(neighbor)
        if not next_frontier:
            break
        reached.update(next_frontier)
        frontier = next_frontier
    return reached


def _supporting_entities(
    identifiers: Iterable[str],
    entities: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[str]]:
    supporting: dict[str, list[str]] = {
        "proposal": [],
        "pull_request": [],
        "commit": [],
        "release": [],
        "document": [],
        "other": [],
    }
    for identifier in sorted(identifiers):
        entity = entities.get(identifier)
        if entity is None:
            continue
        kind = str(entity.get("kind") or "other")
        bucket = {
            "issue": "proposal",
            "discussion": "proposal",
            "pull_request": "pull_request",
            "commit": "commit",
            "release": "release",
            "doc": "document",
            "source_file": "document",
        }.get(kind, "other")
        supporting[bucket].append(identifier)
    return supporting


def _derive_proposal_state(
    supporting: Mapping[str, list[str]],
    entities: Mapping[str, Mapping[str, Any]],
    gaps: set[str],
) -> str:
    proposals = [
        entities[identifier]
        for identifier in supporting.get("proposal", [])
        if identifier in entities
    ]
    if not proposals:
        gaps.add("no_proposal_entity")
        return "unknown"
    states = [_proposal_state_for(entity, gaps) for entity in proposals]
    return max(states, key=lambda state: PROPOSAL_PRIORITY.get(state, 0))


def _proposal_state_for(entity: Mapping[str, Any], gaps: set[str]) -> str:
    raw_state = entity.get("raw_state")
    raw = raw_state if isinstance(raw_state, Mapping) else {}
    normalized = entity.get("normalized_state")
    normalized_state = normalized if isinstance(normalized, Mapping) else {}
    labels = {
        str(value).strip().casefold()
        for value in _labels(raw)
        if str(value).strip()
    }
    state = str(raw.get("state") or normalized_state.get("state") or "unknown")
    state_reason = str(
        raw.get("state_reason")
        or normalized_state.get("state_reason")
        or ""
    )
    comments = _positive_int(
        raw.get("comments")
        or raw.get("comments_count")
        or normalized_state.get("comments")
    )
    explicit = str(normalized_state.get("proposal_state") or "")
    if labels & _LABEL_ACCEPTED:
        return "accepted"
    if labels & _LABEL_REJECTED:
        return "rejected"
    if labels & _LABEL_SUPERSEDED:
        return "superseded"
    if explicit in PROPOSAL_PRIORITY and explicit != "unknown":
        if state == "closed":
            gaps.add("closed_without_acceptance_evidence")
        return explicit
    if state_reason == "duplicate":
        gaps.add("duplicate_is_not_implementation")
        return "superseded"
    if state_reason == "not_planned":
        gaps.add("not_planned_is_not_implementation")
        return "rejected"
    if state == "closed":
        gaps.add("closed_without_acceptance_evidence")
        return "proposed"
    if state == "open":
        return "discussing" if comments else "proposed"
    roles = {
        str(value) for value in entity.get("roles") or [] if isinstance(value, str)
    }
    hints = {
        str(value)
        for value in normalized_state.get("role_hints") or []
        if isinstance(value, str)
    }
    if roles & {"rfc", "design"} or hints & {"rfc", "design"}:
        return "proposed"
    return "unknown"


def _derive_implementation(
    capability: Mapping[str, Any],
    supporting: Mapping[str, list[str]],
    entities: Mapping[str, Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    pr_documents: Mapping[int, Mapping[str, Any]],
    gaps: set[str],
) -> tuple[str, str | None, str | None]:
    prs = [
        entities[identifier]
        for identifier in supporting.get("pull_request", [])
        if identifier in entities
    ]
    merged_prs: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = []
    open_prs = 0
    closed_unmerged = 0
    coverage_insufficient = False
    for entity in prs:
        number = entity.get("number")
        document = (
            pr_documents.get(number)
            if isinstance(number, int) and not isinstance(number, bool)
            else None
        )
        merged = _pr_merged(entity, document, gaps)
        if merged:
            merged_prs.append((entity, document))
            if _files_insufficient(document, gaps):
                coverage_insufficient = True
        elif _pr_state(entity, document) == "open":
            open_prs += 1
        else:
            closed_unmerged += 1
    reverted = _reverted(
        supporting,
        entities,
        relations,
        merged_prs,
    )
    target_branch = _target_branch(capability, entities, supporting, pr_documents)
    observed_revision = _observed_revision(capability, merged_prs)
    if reverted:
        return "reverted", target_branch, observed_revision
    if merged_prs:
        requirements = capability.get("requirements")
        remaining = (
            requirements.get("remaining")
            if isinstance(requirements, Mapping)
            else None
        )
        if isinstance(remaining, list) and remaining:
            return "partially_merged", target_branch, observed_revision
        if coverage_insufficient:
            return "partially_merged", target_branch, observed_revision
        return "merged", target_branch, observed_revision
    if open_prs:
        return "in_progress", target_branch, observed_revision
    if closed_unmerged:
        gaps.add("closed_unmerged_prs")
        state = "not_started" if supporting.get("proposal") else "unknown"
        return state, target_branch, observed_revision
    if supporting.get("proposal"):
        return "not_started", target_branch, observed_revision
    gaps.add("no_implementation_entity")
    return "unknown", target_branch, observed_revision


def _pr_merged(
    entity: Mapping[str, Any],
    document: Mapping[str, Any] | None,
    gaps: set[str],
) -> bool:
    if document is not None:
        merged = document.get("merged") is True
        merged_at = document.get("merged_at")
        if merged and merged_at:
            return True
        if merged and not merged_at:
            gaps.add("merged_without_merged_at")
        return False
    raw = _raw_state(entity)
    merged = bool(raw.get("merged") or _normalized_state(entity).get("merged"))
    merged_at = raw.get("merged_at") or _normalized_state(entity).get("merged_at")
    if merged and merged_at:
        return True
    if merged:
        gaps.add("merged_without_merged_at")
    return False


def _pr_state(
    entity: Mapping[str, Any],
    document: Mapping[str, Any] | None,
) -> str:
    if isinstance(document, Mapping):
        state = document.get("state")
        if isinstance(state, str) and state:
            return state
    return str(entity.get("state") or _raw_state(entity).get("state") or "unknown")


def _files_insufficient(
    document: Mapping[str, Any] | None,
    gaps: set[str],
) -> bool:
    if document is None:
        gaps.add("pr_document_missing")
        return True
    files = document.get("files")
    if not isinstance(files, Mapping):
        gaps.add("pr_files_unknown")
        return True
    if files.get("status") != "complete" or files.get("truncated"):
        gaps.add("pr_files_incomplete")
        return True
    diff = document.get("diff")
    if isinstance(diff, Mapping):
        status = diff.get("status")
        if status in ("unavailable", "budget_capped") or diff.get("truncated"):
            gaps.add("pr_diff_incomplete")
            return True
        if status == "not_requested":
            gaps.add("diff_not_requested")
    return False


def _reverted(
    supporting: Mapping[str, list[str]],
    entities: Mapping[str, Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    merged_prs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any] | None]],
) -> bool:
    implementation_ids = set(supporting.get("pull_request", [])) | set(
        supporting.get("commit", [])
    )
    merged_commits: set[str] = set()
    for _entity, document in merged_prs:
        commits = document.get("commits") if isinstance(document, Mapping) else None
        items = commits.get("items") if isinstance(commits, Mapping) else None
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping) and isinstance(item.get("sha"), str):
                    merged_commits.add(str(item["sha"]))
    merged_pr_ids = {
        str(entity["id"]) for entity, _document in merged_prs if "id" in entity
    }
    for relation in relations:
        if relation.get("type") != "reverts" or not _trusted_relation(relation):
            continue
        from_id = str(relation.get("from_id") or "")
        to_id = str(relation.get("to_id") or "")
        if from_id not in implementation_ids or to_id not in implementation_ids:
            continue
        reverting = entities.get(from_id)
        if from_id in merged_pr_ids:
            return True
        if reverting is not None and reverting.get("kind") == "commit":
            if from_id.rsplit("/", 1)[-1] in merged_commits:
                return True
    return False


def _target_branch(
    capability: Mapping[str, Any],
    entities: Mapping[str, Mapping[str, Any]],
    supporting: Mapping[str, list[str]],
    pr_documents: Mapping[int, Mapping[str, Any]],
) -> str | None:
    declared = capability.get("target_branch")
    if isinstance(declared, str) and declared:
        return declared
    for identifier in supporting.get("pull_request", []):
        entity = entities.get(identifier)
        if entity is None:
            continue
        raw = _raw_state(entity)
        base = raw.get("base")
        if isinstance(base, Mapping) and isinstance(base.get("ref"), str):
            return str(base["ref"])
        document = pr_documents.get(entity.get("number"))
        if isinstance(document, Mapping):
            base = document.get("base")
            if isinstance(base, Mapping) and isinstance(base.get("ref"), str):
                return str(base["ref"])
    return None


def _observed_revision(
    capability: Mapping[str, Any],
    merged_prs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any] | None]],
) -> str | None:
    declared = capability.get("observed_revision")
    if isinstance(declared, str) and declared:
        return declared
    for entity, document in merged_prs:
        if isinstance(document, Mapping):
            for key in ("merge_commit_sha", "head_sha"):
                value = document.get(key)
                if isinstance(value, str) and value:
                    return value
        raw = _raw_state(entity)
        head = raw.get("head")
        if isinstance(head, Mapping) and isinstance(head.get("sha"), str):
            return str(head["sha"])
    return None


def _delivery_target_merged(
    identifier: str,
    entities: Mapping[str, Mapping[str, Any]],
    pr_documents: Mapping[int, Mapping[str, Any]],
    gaps: set[str],
) -> bool:
    entity = entities.get(identifier)
    if entity is None:
        return False
    if entity.get("kind") == "commit":
        sha = identifier.rsplit("/", 1)[-1]
        return sha in _merged_commit_shas(entities, pr_documents)
    if entity.get("kind") != "pull_request":
        return False
    number = entity.get("number")
    document = pr_documents.get(number) if isinstance(number, int) else None
    return _pr_merged(entity, document, gaps)


def _document_commit_shas(document: Mapping[str, Any]) -> set[str]:
    shas: set[str] = set()
    commits = document.get("commits")
    items = commits.get("items") if isinstance(commits, Mapping) else None
    if isinstance(items, list):
        for item in items:
            if isinstance(item, Mapping) and isinstance(item.get("sha"), str):
                shas.add(str(item["sha"]))
    merge_sha = document.get("merge_commit_sha")
    if isinstance(merge_sha, str) and merge_sha:
        shas.add(merge_sha)
    return shas


def _merged_commit_shas(
    entities: Mapping[str, Mapping[str, Any]],
    pr_documents: Mapping[int, Mapping[str, Any]],
) -> set[str]:
    shas: set[str] = set()
    for entity in entities.values():
        if entity.get("kind") != "pull_request":
            continue
        number = entity.get("number")
        document = pr_documents.get(number) if isinstance(number, int) else None
        if not isinstance(document, Mapping):
            continue
        if document.get("merged") is not True or not document.get("merged_at"):
            continue
        shas.update(_document_commit_shas(document))
    return shas


def _derive_delivery(
    supporting: Mapping[str, list[str]],
    entities: Mapping[str, Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    pr_documents: Mapping[int, Mapping[str, Any]],
    release_documents: Mapping[str, Mapping[str, Any]],
    gaps: set[str],
) -> tuple[str, str, list[str]]:
    release_ids = set(supporting.get("release", []))
    implementation_ids = set(supporting.get("pull_request", [])) | set(
        supporting.get("commit", [])
    )
    verified_targets: dict[str, set[str]] = {}
    mentioned_releases: set[str] = set()
    for relation in relations:
        if not _trusted_relation(relation):
            continue
        relation_type = relation.get("type")
        from_id = str(relation.get("from_id") or "")
        to_id = str(relation.get("to_id") or "")
        if relation_type == "verified_in_release":
            if from_id in release_ids or to_id in release_ids:
                other = to_id if from_id in release_ids else from_id
                if other in implementation_ids or other in release_ids:
                    release_id = from_id if from_id in release_ids else to_id
                    targets = verified_targets.setdefault(release_id, set())
                    if other in implementation_ids:
                        if other not in entities:
                            gaps.add(f"verified_target_missing:{other}")
                        elif _delivery_target_merged(
                            other,
                            entities,
                            pr_documents,
                            gaps,
                        ):
                            targets.add(other)
                        else:
                            gaps.add(f"verified_target_not_merged:{other}")
        elif relation_type == "release_note_mentions":
            if from_id in release_ids or to_id in release_ids:
                other = to_id if from_id in release_ids else from_id
                if other in implementation_ids:
                    mentioned_releases.add(
                        from_id if from_id in release_ids else to_id
                    )
    tags: list[str] = []
    verified_tags: list[tuple[str, bool]] = []
    for identifier in sorted(verified_targets):
        entity = entities.get(identifier)
        tag = _release_tag(entity) if entity is not None else ""
        if not tag:
            gaps.add("verified_release_tag_missing")
            continue
        if not verified_targets[identifier]:
            gaps.add(f"verified_release_without_target:{tag}")
            continue
        document = release_documents.get(tag)
        published = _parse_iso(document.get("published_at")) if document else None
        unresolved = [
            target
            for target in sorted(verified_targets[identifier])
            if _merged_at(target, entities, pr_documents) is None
        ]
        if unresolved:
            for target in unresolved:
                gaps.add(f"release_timeline_unresolved:{tag}:{target}")
            continue
        rejected = [
            target
            for target in sorted(verified_targets[identifier])
            if _merged_before_release(target, published, entities, pr_documents)
        ]
        if rejected:
            for target in rejected:
                gaps.add(f"release_before_implementation:{tag}:{target}")
            continue
        if document is None:
            gaps.add(f"release_document_missing:{tag}")
            continue
        if document.get("draft") is True:
            gaps.add(f"release_draft:{tag}")
            continue
        if published is None:
            gaps.add(f"release_published_at_missing:{tag}")
            continue
        tags.append(tag)
        verified_tags.append((tag, document.get("prerelease") is True))
    if tags:
        selected = (
            "released"
            if any(not prerelease for _tag, prerelease in verified_tags)
            else "prerelease"
        )
        return selected, "verified", tags
    if mentioned_releases:
        gaps.add("release_note_only")
        return "unknown", "note_only", []
    gaps.add("no_release_evidence")
    return "unknown", "unknown", []


def _release_tag(entity: Mapping[str, Any] | None) -> str:
    if entity is None:
        return ""
    raw = _raw_state(entity)
    tag = raw.get("tag_name")
    if isinstance(tag, str) and tag:
        return tag
    normalized = _normalized_state(entity)
    tag = normalized.get("tag_name")
    if isinstance(tag, str) and tag:
        return tag
    identifier = str(entity.get("id") or "")
    prefix = "/release/"
    if prefix in identifier:
        return identifier.split(prefix, 1)[1]
    return ""


def _merged_before_release(
    implementation_id: str,
    published: datetime | None,
    entities: Mapping[str, Mapping[str, Any]],
    pr_documents: Mapping[int, Mapping[str, Any]],
) -> bool:
    """True when the implementation merged after the release was published."""
    if published is None:
        return False
    merged = _merged_at(implementation_id, entities, pr_documents)
    return merged is not None and published < merged


def _merged_at(
    implementation_id: str,
    entities: Mapping[str, Mapping[str, Any]],
    pr_documents: Mapping[int, Mapping[str, Any]],
) -> datetime | None:
    entity = entities.get(implementation_id)
    if entity is not None and str(entity.get("kind")) == "commit":
        return _commit_merged_at(implementation_id, pr_documents)
    number = _entity_number(implementation_id)
    document = pr_documents.get(number) if number is not None else None
    merged_value = document.get("merged_at") if isinstance(document, Mapping) else None
    if not merged_value:
        entity = entities.get(implementation_id)
        if entity is not None:
            merged_value = _raw_state(entity).get(
                "merged_at"
            ) or _normalized_state(entity).get("merged_at")
    return _parse_iso(merged_value)


def _commit_merged_at(
    implementation_id: str,
    pr_documents: Mapping[int, Mapping[str, Any]],
) -> datetime | None:
    sha = implementation_id.rsplit("/", 1)[-1]
    merged_at: list[datetime] = []
    for document in pr_documents.values():
        if not isinstance(document, Mapping):
            continue
        if document.get("merged") is not True or not document.get("merged_at"):
            continue
        if sha not in _document_commit_shas(document):
            continue
        parsed = _parse_iso(document.get("merged_at"))
        if parsed is not None:
            merged_at.append(parsed)
    return min(merged_at) if merged_at else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _entity_number(identifier: str) -> int | None:
    value = str(identifier).rsplit("/", 1)[-1]
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _derive_availability(
    capability: Mapping[str, Any],
    supporting: Mapping[str, list[str]],
    entities: Mapping[str, Mapping[str, Any]],
    evidence_map: Mapping[str, Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    gaps: set[str],
) -> str:
    declared = str(capability.get("availability") or "unknown")
    if declared == "unknown":
        return "unknown"
    if not _has_document_evidence(
        capability,
        supporting,
        entities,
        evidence_map,
        relations,
    ):
        gaps.add("availability_without_document_evidence")
        return "unknown"
    return declared


def _has_document_evidence(
    capability: Mapping[str, Any],
    supporting: Mapping[str, list[str]],
    entities: Mapping[str, Mapping[str, Any]],
    evidence_map: Mapping[str, Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
) -> bool:
    for evidence_id in capability.get("evidence_ids") or []:
        record = evidence_map.get(str(evidence_id))
        if (
            record is not None
            and record.get("verification") == "verified"
            and record.get("source_kind") == "doc_content"
        ):
            return True
        if record is not None and record.get("verification") == "verified":
            entity = entities.get(str(record.get("entity_id") or ""))
            if entity is not None and entity.get("kind") in ("doc", "source_file"):
                return True
    if supporting.get("document"):
        return True
    supporting_ids = {
        identifier
        for values in supporting.values()
        for identifier in values
    }
    for relation in relations:
        if relation.get("type") != "documents" or not _trusted_relation(relation):
            continue
        if (
            relation.get("from_id") in supporting_ids
            or relation.get("to_id") in supporting_ids
        ):
            return True
    return False


def _record_conflicts(
    capability: Mapping[str, Any],
    *,
    proposal_state: str,
    implementation_state: str,
    delivery_state: str,
    availability: str,
    conflicts: list[dict[str, Any]],
) -> None:
    for field, declared, derived in (
        ("proposal_state", capability.get("proposal_state"), proposal_state),
        ("implementation_state", capability.get("implementation_state"), implementation_state),
        ("delivery_state", capability.get("delivery_state"), delivery_state),
        ("availability", capability.get("availability"), availability),
    ):
        if declared in (None, "", "unknown"):
            continue
        if str(declared) != str(derived):
            conflicts.append(
                {
                    "field": field,
                    "declared": str(declared),
                    "derived": str(derived),
                    "message": f"capability.{field} 与 lifecycle 推导不一致",
                }
            )


def _state_time_basis(claims: Sequence[Mapping[str, Any]]) -> str:
    values = {str(claim.get("state_time_basis") or "") for claim in claims}
    if values and values <= {"historical_reconstructed"}:
        return "historical_reconstructed"
    return "observed_current"


def _raw_state(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    value = entity.get("raw_state")
    return value if isinstance(value, Mapping) else {}


def _normalized_state(entity: Mapping[str, Any]) -> Mapping[str, Any]:
    value = entity.get("normalized_state")
    return value if isinstance(value, Mapping) else {}


def _labels(raw: Mapping[str, Any]) -> list[str]:
    labels = raw.get("labels")
    if not isinstance(labels, list):
        return []
    values: list[str] = []
    for item in labels:
        if isinstance(item, str):
            values.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            values.append(str(item["name"]))
    return values


def _positive_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    return 0


__all__ = ["check_state_assertions", "verify_capability"]
