"""Deterministic Markdown reading packs grouped by source kind and module."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from . import SCHEMA_VERSION
from .collectors.pr_store import load_pr_documents
from .collectors.proposal_store import load_discussion_documents, load_issue_documents
from .diversity import diverse_order
from .models import validate_document
from .validation import substantive_text

PACK_INDEX_PATH = "analysis/pack-index.json"
PACK_DIR = "analysis/packs"
RULE_VERSION = "packs-v1"
PREVIEW_CAPS = (800, 400, 200, 100)
RESEARCH_CAPS = (1600, 800, 400)
DISCOVERY_CAP = 240
MAX_RENDERED_EVIDENCE = 8
MAX_RESEARCH_EVIDENCE = 5
_KIND_ORDER = {
    "pull_request": 0,
    "issue": 1,
    "discussion": 2,
    "release": 3,
    "doc": 4,
}
_BODY_SOURCE_PRIORITY = {
    "pull_request_body": 0,
    "issue_body": 0,
    "discussion_body": 0,
    "release_body": 0,
    "doc_content": 0,
    "pull_request_review": 1,
    "pull_request_review_comment": 1,
    "pull_request_comment": 1,
    "issue_comment": 1,
    "discussion_comment": 1,
    "commit_message": 2,
}


def pack_id(source_kind: str, module: str) -> str:
    material = f"{RULE_VERSION}|{source_kind}|{module}".encode()
    return f"pack-{hashlib.sha256(material).hexdigest()[:16]}"


def build_pack_index(
    store: Any,
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
    candidates: Mapping[str, Any],
    *,
    research_handoff: bool | None = None,
) -> dict[str, Any]:
    max_chars = int(config.get("budget", {}).get("max_pack_chars", 0))
    if research_handoff is None:
        research_handoff = any(
            item.get("agent_selected") is True
            for item in candidates.get("candidates") or []
            if isinstance(item, Mapping)
        )
    groups = _groups(candidates)
    previews = _previews(store, candidates, research_handoff=research_handoff)
    for group in groups:
        for item in group["items"]:
            values = previews.get(str(item["entity_id"]), [])
            item["preview_text"] = str(values[0].get("text") or "") if values else ""
            item["evidence_previews"] = values if _is_research_item(
                item, research_handoff=research_handoff
            ) else []
            item["original_chars"] = (
                int(values[0].get("original_chars") or 0)
                if values
                else 0
            )
            item["tier"] = (
                "research"
                if _is_research_item(item, research_handoff=research_handoff)
                else "discovery"
            )
        group["items"].sort(key=_item_order)

    # Reserve a few discovery representatives even after research material grows.
    # Agent-selected evidence still has first priority under a hard budget.
    if max_chars >= 8000:
        discovery = sorted(
            [item for group in groups for item in group["items"]
             if item["tier"] == "discovery"], key=_item_order,
        )
        for item in diverse_order(
            discovery, key=lambda value: str(value.get("module") or "unknown"), reserve=4,
        )[:4]:
            item["exploration_reserved"] = True

    included, omitted, preview_cap, research_cap = _fit_budget(
        groups,
        max_chars=max_chars,
        manifest=manifest,
        candidates=candidates,
        research_handoff=research_handoff,
    )
    for group in included:
        for item in group["items"]:
            item["preview_cap"] = preview_cap
            item["research_cap"] = research_cap
    packs: list[dict[str, Any]] = []
    pack_texts: dict[tuple[str, str], str] = {}
    for index, group in enumerate(sorted(included, key=_group_order), start=1):
        items = sorted(
            group["items"],
            key=lambda value: _item_order(value, research_handoff),
        )
        if not items:
            continue
        identifier = pack_id(str(group["source_kind"]), str(group["module"]))
        path = f"{PACK_DIR}/part-{index:03d}.md"
        text = _render_pack(
            identifier,
            str(group["source_kind"]),
            str(group["module"]),
            items,
            manifest=manifest,
            candidates=candidates,
            research_cap=research_cap,
            discovery_cap=preview_cap,
        )
        omitted_ids = sorted(group.get("omitted_candidate_ids", set()))
        truncated = any(
            _item_truncated(item) for item in items
        ) or bool(omitted_ids)
        record = {
            "pack_id": identifier,
            "path": path,
            "source_kind": str(group["source_kind"]),
            "module": str(group["module"]),
            "candidate_ids": [str(item["candidate_id"]) for item in items],
            "chars": len(text),
            "items": [_item_index(item) for item in items],
            "truncated": truncated,
            "truncation_reasons": sorted(
                {
                    *(
                        ["preview_truncated"]
                        if any(_item_truncated(item) for item in items)
                        else []
                    ),
                    *(["pack_char_budget"] if omitted_ids else []),
                }
            ),
            "omitted_candidate_ids": omitted_ids,
        }
        packs.append(record)
        pack_texts[(str(group["source_kind"]), str(group["module"]))] = text
    used_chars = sum(record["chars"] for record in packs)
    observed_at = str(
        manifest.get("collection_finished_at")
        or manifest.get("observed", {}).get("finished_at")
        or manifest.get("created_at")
        or ""
    )
    counts = {
        "candidates": len(candidates.get("candidates") or []),
        "packs": len(packs),
        "items_included": sum(len(pack["items"]) for pack in packs),
        "items_omitted": len(omitted),
        "preview_slots": sum(
            len(item["evidence_previews"])
            if item.get("tier") == "research"
            else 1
            for pack in packs
            for item in pack["items"]
        ),
        "previews_total": sum(
            1
            for pack in packs
            for item in pack["items"]
            for preview in (
                item["evidence_previews"]
                if item.get("tier") == "research"
                else [item["preview"]]
            )
            if int(preview.get("chars") or 0) > 0
        ),
        "previews_truncated": sum(
            1
            for pack in packs
            for item in pack["items"]
            for preview in (
                item["evidence_previews"]
                if item.get("tier") == "research"
                else [item["preview"]]
            )
            if preview["truncated"]
        ),
        "items_with_truncated_preview": sum(
            1
            for pack in packs
            for item in pack["items"]
            if any(
                preview["truncated"]
                for preview in (
                    item["evidence_previews"]
                    if item.get("tier") == "research"
                    else [item["preview"]]
                )
            )
        ),
        "research_items": sum(
            1
            for pack in packs
            for item in pack["items"]
            if item.get("tier") == "research"
        ),
        "discovery_items": sum(
            1
            for pack in packs
            for item in pack["items"]
            if item.get("tier") == "discovery"
        ),
        "chars": used_chars,
        "max_chars": max_chars,
        "preview_cap": preview_cap,
    }
    limitations = {"observed_current_state"}
    if omitted:
        limitations.add("pack_char_budget_exceeded")
    if any(pack["truncated"] for pack in packs):
        limitations.add("pack_truncated")
    if any(pack["module"] == "unknown" for pack in packs):
        limitations.add("unknown_module")
    if research_handoff and counts["research_items"]:
        limitations.add("research_material_prioritized")
    if not packs:
        limitations.add("no_candidates")
    document = {
        "schema_version": SCHEMA_VERSION,
        "rule_version": RULE_VERSION,
        "observed_at": observed_at,
        "repository": candidates.get("repository")
        or {
            "host": config["repository"]["host"],
            "owner": config["repository"]["owner"],
            "name": config["repository"]["name"],
        },
        "budget": {
            "max_pack_chars": max_chars,
            "used_chars": used_chars,
            "remaining_chars": max(0, max_chars - used_chars),
        },
        "packs": packs,
        "omitted_candidates": omitted,
        "counts": counts,
        "limitations": sorted(limitations),
    }
    validate_document(document, "pack_index")
    return {"document": document, "texts": pack_texts}


def write_pack_index(store: Any, payload: Mapping[str, Any]) -> str:
    document = payload["document"]
    texts = payload["texts"]
    expected = {str(pack["path"]) for pack in document["packs"]}
    directory = store.secure_path(PACK_DIR)
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            relpath = f"{PACK_DIR}/{path.name}"
            if relpath not in expected:
                path.unlink()
    for pack in document["packs"]:
        text = texts[(str(pack["source_kind"]), str(pack["module"]))]
        store.write_text(str(pack["path"]), text)
    store.write_json(PACK_INDEX_PATH, document, schema_name="pack_index")
    return PACK_INDEX_PATH


def audit_pack_index(
    store: Any,
    *,
    candidates: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    path = store.secure_path(PACK_INDEX_PATH)
    if not path.is_file():
        return []
    problems: list[dict[str, Any]] = []
    try:
        document = store.read_json(PACK_INDEX_PATH)
        validate_document(document, "pack_index")
    except Exception as exc:
        return [
            {
                "artifact": PACK_INDEX_PATH,
                "problem": "invalid_pack_index",
                "message": str(getattr(exc, "message", exc)),
            }
        ]
    candidate_ids = {
        str(item.get("candidate_id"))
        for item in (candidates or {}).get("candidates") or []
        if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
    }
    seen_candidates: set[str] = set()
    expected_paths: set[str] = set()
    for pack in document.get("packs") or []:
        identifier = str(pack.get("pack_id"))
        expected_id = pack_id(str(pack.get("source_kind")), str(pack.get("module")))
        if identifier != expected_id:
            problems.append(
                {
                    "artifact": PACK_INDEX_PATH,
                    "pack_id": identifier,
                    "problem": "pack_id_mismatch",
                    "message": f"pack_id 应为 {expected_id}",
                }
            )
        relpath = str(pack.get("path"))
        expected_paths.add(relpath)
        pack_path = store.secure_path(relpath)
        if not pack_path.is_file():
            problems.append(
                {
                    "artifact": PACK_INDEX_PATH,
                    "pack_id": identifier,
                    "path": relpath,
                    "problem": "missing_pack_file",
                    "message": "pack-index 引用的 Markdown 文件不存在",
                }
            )
        else:
            chars = len(pack_path.read_text(encoding="utf-8"))
            if chars != pack.get("chars"):
                problems.append(
                    {
                        "artifact": PACK_INDEX_PATH,
                        "pack_id": identifier,
                        "path": relpath,
                        "problem": "pack_chars_mismatch",
                        "message": f"pack.chars={pack.get('chars')}，实际 {chars}",
                    }
                )
        for candidate_identifier in pack.get("candidate_ids") or []:
            if candidate_identifier in seen_candidates:
                problems.append(
                    {
                        "artifact": PACK_INDEX_PATH,
                        "pack_id": identifier,
                        "candidate_id": candidate_identifier,
                        "problem": "duplicate_pack_candidate",
                        "message": "同一候选出现在多个 pack 中",
                    }
                )
            seen_candidates.add(candidate_identifier)
            if candidates is not None and candidate_identifier not in candidate_ids:
                problems.append(
                    {
                        "artifact": PACK_INDEX_PATH,
                        "pack_id": identifier,
                        "candidate_id": candidate_identifier,
                        "problem": "missing_pack_candidate",
                        "message": "pack.candidate_ids 不在 candidates.json 中",
                    }
                )
    directory = store.secure_path(PACK_DIR)
    if directory.is_dir():
        for pack_path in sorted(directory.glob("*.md")):
            relpath = f"{PACK_DIR}/{pack_path.name}"
            if relpath not in expected_paths:
                problems.append(
                    {
                        "artifact": relpath,
                        "problem": "orphan_pack_file",
                        "message": "analysis/packs 中存在未被 pack-index 引用的文件",
                    }
                )
    return problems


def _groups(candidates: Mapping[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        kind = str(candidate.get("kind") or "unknown")
        module = str(candidate.get("module") or "unknown")
        grouped[(kind, module)].append(dict(candidate))
    groups = []
    for (kind, module), items in grouped.items():
        groups.append(
            {
                "source_kind": kind,
                "module": module,
                "items": items,
                "omitted_candidate_ids": set(),
            }
        )
    return groups


def _is_research_item(item: Mapping[str, Any], *, research_handoff: bool) -> bool:
    if item.get("agent_selected") is True:
        return True
    return bool(research_handoff and item.get("deep_read") is True)


def _preview_record(record: Mapping[str, Any], *, limit: int) -> dict[str, Any]:
    text = str(record.get("text") or "")
    shown = text[:limit]
    locator = record.get("locator")
    return {
        "evidence_id": str(record.get("id") or ""),
        "source_kind": str(record.get("source_kind") or ""),
        "source_url": str(record.get("source_url") or ""),
        "locator": dict(locator) if isinstance(locator, Mapping) else {},
        "text": shown,
        "chars": len(shown),
        "original_chars": len(text),
        "truncated": len(shown) < len(text),
    }


def _preview_truncated(preview: Mapping[str, Any], cap: int) -> bool:
    text = str(preview.get("text") or "")
    original = preview.get("original_chars")
    if not isinstance(original, int):
        original = len(text)
    return original > cap


def _excerpt_metadata(store: Any) -> dict[str, int]:
    """Read saved author associations; content heuristics never confer trust."""
    roles: dict[str, int] = {}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, Mapping):
            body = value.get("body")
            user = value.get("user") or value.get("author") or ""
            if isinstance(user, Mapping):
                user = user.get("login") or ""
            if isinstance(body, Mapping) and body.get("sha256"):
                association = str(value.get("author_association") or "").upper()
                role = (-1 if str(user).endswith("[bot]") else
                        2 if association in {"OWNER", "MEMBER", "COLLABORATOR"} else 0)
                roles[str(body["sha256"])] = role
            for item in value.values():
                if isinstance(item, (list, Mapping)):
                    visit(item)

    for document in [*load_pr_documents(store), *load_issue_documents(store),
                     *load_discussion_documents(store)]:
        visit(document)
    return roles


def _decision_score(text: str) -> int:
    """Prioritize revisions, limits and rationale, without interpreting them as facts."""
    lowered = text.casefold()
    return sum(weight for pattern, weight in (
        (r"~~|\bswitched\b|\bno longer\b|\binstead\b", 5),
        (r"\blimitation|not implemented|not yet|disabled by default|out of scope", 4),
        (r"\bbecause\b|\bhowever\b|\btrade.?off|\brequired\b", 2),
        (r"\bdecided\b|doesn.t.*sense|increases.*size|\bfollow.up\b", 3),
    ) if re.search(pattern, lowered))


def _useful_excerpt(value: Mapping[str, Any], metadata: Mapping[str, int]) -> bool:
    text = str(value.get("text") or "").strip()
    if not substantive_text(text) or metadata.get(str(value.get("blob_sha256")), 0) < 0:
        return False
    lowered = text.casefold()
    return not (
        lowered.startswith("<!--") or lowered.endswith("-->")
        or lowered in {"<details>", "</details>", "<br>", "<br/>"}
        or "generated-comment" in lowered
        or "please read and follow our [ai policy]" in lowered
        or "ai **should not** be used to communicate" in lowered
        or "does this pull request include" in lowered
    )


def _previews(
    store: Any,
    candidates: Mapping[str, Any],
    *,
    research_handoff: bool,
) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in store.read_jsonl("normalized/evidence.jsonl"):
        if not isinstance(item, Mapping):
            continue
        entity_identifier = item.get("entity_id")
        text = item.get("text")
        identifier = item.get("id")
        if not isinstance(entity_identifier, str) or not isinstance(text, str):
            continue
        if not isinstance(identifier, str):
            continue
        evidence[entity_identifier].append(item)

    metadata = _excerpt_metadata(store)
    previews: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        entity_identifier = candidate.get("entity_id")
        if not isinstance(entity_identifier, str):
            continue
        values = [value for value in evidence.get(entity_identifier, [])
                  if _useful_excerpt(value, metadata)]
        if not values:
            continue
        research = _is_research_item(candidate, research_handoff=research_handoff)
        if not research:
            first = sorted(
                values,
                key=lambda value: (
                    _BODY_SOURCE_PRIORITY.get(str(value.get("source_kind")), 3),
                    -_decision_score(str(value.get("text") or "")),
                    (value.get("locator") or {}).get("char_start", 0),
                    str(value.get("id") or ""),
                ),
            )[0]
            previews[entity_identifier] = [_preview_record(first, limit=DISCOVERY_CAP)]
            continue
        body = [
            value
            for value in values
            if _BODY_SOURCE_PRIORITY.get(str(value.get("source_kind")), 3) == 0
        ]
        comments = [
            value
            for value in values
            if _BODY_SOURCE_PRIORITY.get(str(value.get("source_kind")), 3) == 1
        ]
        body.sort(key=lambda value: (
            (value.get("locator") or {}).get("char_start", 0), str(value.get("id") or ""),
        ))
        decisions = sorted(body, key=lambda value: (
            -_decision_score(str(value.get("text") or "")),
            (value.get("locator") or {}).get("char_start", 0), str(value.get("id") or ""),
        ))
        comments.sort(key=lambda value: (
            -metadata.get(str(value.get("blob_sha256")), 0),
            -_decision_score(str(value.get("text") or "")),
            # ISO timestamps sort chronologically; the newest revision wins ties.
            tuple(-ord(c) for c in str(value.get("source_event_at") or "")),
            str(value.get("id") or ""),
        ))
        selected: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for value in [*body[:1], *decisions[:1], *comments[:2], *decisions[1:], *body[1:]]:
            identifier = str(value.get("id") or "")
            if not identifier or identifier in seen:
                continue
            selected.append(value)
            seen.add(identifier)
            if len(selected) >= MAX_RESEARCH_EVIDENCE:
                break
        previews[entity_identifier] = [
            _preview_record(value, limit=1600) for value in selected
        ]
    return previews

def _fit_budget(
    groups: list[dict[str, Any]],
    *,
    max_chars: int,
    manifest: Mapping[str, Any] | None,
    candidates: Mapping[str, Any] | None,
    research_handoff: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    omitted: list[dict[str, Any]] = []
    if max_chars <= 0:
        max_chars = 0
    for research_cap in RESEARCH_CAPS:
        discovery_cap = min(DISCOVERY_CAP, max(160, research_cap // 4))
        texts = _render_groups(
            groups,
            research_cap=research_cap,
            discovery_cap=discovery_cap,
            manifest=manifest,
            candidates=candidates,
        )
        total = sum(len(text) for text in texts.values())
        if total <= max_chars:
            return groups, omitted, discovery_cap, research_cap
    research_cap = RESEARCH_CAPS[-1]
    discovery_cap = 160
    texts = _render_groups(
        groups,
        research_cap=research_cap,
        discovery_cap=discovery_cap,
        manifest=manifest,
        candidates=candidates,
    )
    total = sum(len(text) for text in texts.values())
    for group, item in _drop_order(groups, research_handoff=research_handoff):
        if total <= max_chars:
            break
        if item not in group["items"]:
            continue
        key = (str(group["source_kind"]), str(group["module"]))
        group["items"].remove(item)
        group["omitted_candidate_ids"].add(str(item["candidate_id"]))
        omitted.append(
            {
                "candidate_id": str(item["candidate_id"]),
                "entity_id": str(item["entity_id"]),
                "reason": (
                    "selected_hard_budget"
                    if item.get("agent_selected") is True
                    else "pack_char_budget"
                ),
            }
        )
        previous = len(texts.get(key, ""))
        if group["items"]:
            texts[key] = _render_pack(
                pack_id(*key),
                key[0],
                key[1],
                sorted(group["items"], key=lambda value: _item_order(value, research_handoff)),
                manifest=manifest,
                candidates=candidates,
                research_cap=research_cap,
                discovery_cap=discovery_cap,
            )
            total += len(texts[key]) - previous
        else:
            texts.pop(key, None)
            total -= previous
    return groups, omitted, discovery_cap, research_cap


def _drop_order(
    groups: Iterable[dict[str, Any]],
    *,
    research_handoff: bool,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs = [
        (group, item)
        for group in groups
        for item in group["items"]
    ]
    pairs.sort(
        key=lambda pair: _drop_priority(
            pair[1],
            research_handoff=research_handoff,
        )
    )
    return pairs


def _tier_rank(item: Mapping[str, Any], *, research_handoff: bool) -> int:
    if item.get("agent_selected") is True:
        return 0
    if research_handoff and item.get("deep_read") is True:
        return 1
    if item.get("recommended") is True:
        return 2
    components = item.get("score_components") or {}
    if components.get("design_role") or item.get("selection_lane") == "exploration":
        return 3
    if item.get("deep_read") is True:
        return 4
    return 5


def _drop_priority(
    item: Mapping[str, Any],
    *,
    research_handoff: bool,
) -> tuple[int, int, int, int, str]:
    """Order candidates so the least valuable are dropped first.

    In a research handoff, selected and deep-read materials outrank discovery
    candidates. During the first prepare, discovery still wins so the Agent can
    choose what to read next.
    """
    components = item.get("score_components") or {}
    design = bool(components.get("design_role")) or item.get("selection_lane") == "exploration"
    if item.get("agent_selected") is True:
        tier = 5
    elif item.get("exploration_reserved") is True:
        tier = 4
    elif research_handoff and item.get("deep_read") is True:
        tier = 3
    elif item.get("recommended") is True:
        tier = 3 if not research_handoff else 2
    elif design:
        tier = 2 if not research_handoff else 1
    elif item.get("deep_read") is True:
        tier = 0 if not research_handoff else 1
    else:
        tier = 1 if not research_handoff else 0
    return (
        tier,
        int(item.get("score") or 0),
        _KIND_ORDER.get(str(item.get("kind")), 99),
        1 if item.get("recommended") is True else 0,
        str(item.get("candidate_id")),
    )


def _item_order(
    item: Mapping[str, Any],
    research_handoff: bool = False,
) -> tuple[int, int, int, str]:
    return (
        _tier_rank(item, research_handoff=research_handoff),
        -int(item.get("score") or 0),
        _KIND_ORDER.get(str(item.get("kind")), 99),
        str(item.get("candidate_id")),
    )

def _group_order(group: Mapping[str, Any]) -> tuple[int, str]:
    return (
        _KIND_ORDER.get(str(group.get("source_kind")), 99),
        str(group.get("module")),
    )


def _render_groups(
    groups: Iterable[Mapping[str, Any]],
    *,
    research_cap: int,
    discovery_cap: int,
    manifest: Mapping[str, Any] | None,
    candidates: Mapping[str, Any] | None,
) -> dict[tuple[str, str], str]:
    texts: dict[tuple[str, str], str] = {}
    for group in groups:
        if not group["items"]:
            continue
        key = (str(group["source_kind"]), str(group["module"]))
        items = sorted(group["items"], key=_item_order)
        texts[key] = _render_pack(
            pack_id(*key),
            key[0],
            key[1],
            items,
            manifest=manifest,
            candidates=candidates,
            research_cap=research_cap,
            discovery_cap=discovery_cap,
        )
    return texts


def _render_pack(
    identifier: str,
    source_kind: str,
    module: str,
    items: list[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any] | None,
    candidates: Mapping[str, Any] | None,
    research_cap: int = RESEARCH_CAPS[0],
    discovery_cap: int = DISCOVERY_CAP,
) -> str:
    lines = [
        f"# Reading pack {identifier}",
        "",
        f"- source_kind: {source_kind}",
        f"- module: {module}",
    ]
    if manifest is not None:
        observed_at = (
            manifest.get("collection_finished_at")
            or manifest.get("observed", {}).get("finished_at")
            or manifest.get("created_at")
            or ""
        )
        lines.append(f"- observed_at: {observed_at}")
    if candidates is not None:
        lines.append(f"- rule_version: {candidates.get('rule_version')}")
    lines.append("")
    research = [item for item in items if item.get("tier") == "research"]
    discovery = [item for item in items if item.get("tier") != "research"]
    if research:
        lines.extend(["## Research materials", ""])
        for item in research:
            lines.extend(_render_item(item, research_cap, research=True))
    if discovery:
        lines.extend(["## Candidate index", ""])
        for item in discovery:
            lines.extend(_render_item(item, discovery_cap, research=False))
    return "\n".join(lines).rstrip() + "\n"


def _locator_label(locator: Any) -> str:
    if not isinstance(locator, Mapping):
        return "-"
    parts: list[str] = []
    line_start = locator.get("line_start")
    line_end = locator.get("line_end")
    if isinstance(line_start, int):
        if isinstance(line_end, int) and line_end != line_start:
            parts.append(f"L{line_start}-{line_end}")
        else:
            parts.append(f"L{line_start}")
    char_start = locator.get("char_start")
    char_end = locator.get("char_end")
    if isinstance(char_start, int) and isinstance(char_end, int):
        parts.append(f"chars {char_start}-{char_end}")
    pointer = locator.get("pointer")
    if isinstance(pointer, str) and pointer:
        parts.append(f"pointer={pointer}")
    return ", ".join(parts) or "-"


def _render_item(
    item: Mapping[str, Any],
    cap: int,
    *,
    research: bool,
) -> list[str]:
    components = item.get("score_components") or {}
    component_text = ", ".join(
        f"{key}={components[key]}" for key in sorted(components)
    )
    selection = item.get("selection_reason") or []
    missing = item.get("missing_features") or []
    raw_refs = [
        _ref_text(ref) for ref in item.get("raw_refs") or [] if isinstance(ref, Mapping)
    ]
    evidence = _evidence_summary(item.get("evidence_ids") or [])
    text = str(item.get("preview_text") or "")
    shown = text[:cap]
    heading = "###"
    lines = [
        f"{heading} [{item.get('candidate_id')}] {item.get('title') or ''}",
        "",
        f"- entity_id: `{item.get('entity_id')}`",
        f"- kind: {item.get('kind')} | bucket: {item.get('bucket')} | "
        f"module: {item.get('module')} ({item.get('module_source')})",
        f"- tier: {item.get('tier')} | deep_read: "
        f"{str(bool(item.get('deep_read'))).lower()} | "
        f"agent_selected: {str(bool(item.get('agent_selected'))).lower()} | "
        f"recommended: {str(bool(item.get('recommended'))).lower()} | "
        f"budget_fit: {item.get('budget_fit')} | "
        f"selection_status: {item.get('selection_status') or '-'}",
        f"- score: {item.get('score')} ({component_text})",
        f"- selection_reason: {', '.join(str(value) for value in selection) or '-'}",
        f"- raw: {', '.join(raw_refs) or '-'}",
        f"- evidence: {evidence or '-'}",
        f"- missing: {', '.join(str(value) for value in missing) or '-'}",
        "",
    ]
    if research:
        previews = item.get("evidence_previews") or []
        lines.extend(["#### Evidence excerpts", ""])
        if not previews:
            lines.append("- no evidence previews available")
        for preview in previews:
            if not isinstance(preview, Mapping):
                continue
            locator = _locator_label(preview.get("locator"))
            lines.extend(
                [
                    f"- `{preview.get('evidence_id')}` | "
                    f"{preview.get('source_kind')} | {locator}",
                    f"  - source: {preview.get('source_url') or '-'}",
                ]
            )
            value = str(preview.get("text") or "")[:cap]
            original = preview.get("original_chars")
            if not isinstance(original, int):
                original = len(value)
            truncated = original > cap
            fence = _fence(value)
            lines.extend(
                [
                    f"  - excerpt: {len(value)}/{original} chars"
                    + (" (truncated)" if truncated else ""),
                    f"  {fence}text",
                    *[f"  {line}" for line in value.splitlines()],
                    f"  {fence}",
                ]
            )
        lines.extend(["", f"- full content: {', '.join(raw_refs) or '-'}", ""])
        return lines
    lines.extend(["### Preview", ""])
    if shown:
        fence = _fence(shown)
        lines.extend([fence + "text", shown.rstrip("\n"), fence])
    else:
        lines.append("- preview unavailable; use the raw refs and evidence above.")
    original = item.get("original_chars")
    if not isinstance(original, int):
        original = len(text)
    lines.extend(
        [
            f"- preview: {len(shown)}/{original} chars"
            + (" (truncated)" if original > cap else ""),
            f"- full content: {', '.join(raw_refs) or '-'}",
            "",
        ]
    )
    return lines


def _fence(text: str) -> str:
    longest = 0
    current = 0
    for character in text:
        if character == "~":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return "~" * max(3, longest + 1)


def _evidence_summary(identifiers: Iterable[Any]) -> str:
    """Keep pack items readable: list a few IDs and count the rest."""
    values = [str(value) for value in identifiers]
    shown = values[:MAX_RENDERED_EVIDENCE]
    if len(values) > len(shown):
        return ", ".join(shown) + f", …(+{len(values) - len(shown)})"
    return ", ".join(shown)


def _ref_text(ref: Mapping[str, Any]) -> str:
    artifact = str(ref.get("artifact") or "")
    pointer = ref.get("json_pointer")
    return f"`{artifact}`" + (f"#{pointer}" if isinstance(pointer, str) else "")


def _item_truncated(item: Mapping[str, Any]) -> bool:
    if item.get("tier") == "research":
        cap = item.get("research_cap")
        if not isinstance(cap, int):
            cap = RESEARCH_CAPS[0]
        previews = item.get("evidence_previews") or []
        return any(
            _preview_truncated(preview, cap)
            for preview in previews
            if isinstance(preview, Mapping)
        )
    text = str(item.get("preview_text") or "")
    cap = item.get("preview_cap")
    if not isinstance(cap, int):
        cap = PREVIEW_CAPS[0]
    original = item.get("original_chars")
    if not isinstance(original, int):
        original = len(text)
    return original > cap


def _item_index(item: Mapping[str, Any]) -> dict[str, Any]:
    text = str(item.get("preview_text") or "")
    research = item.get("tier") == "research"
    cap = item.get("research_cap") if research else item.get("preview_cap")
    if not isinstance(cap, int):
        cap = RESEARCH_CAPS[0] if research else PREVIEW_CAPS[0]
    previews = []
    for preview in item.get("evidence_previews") or []:
        if not isinstance(preview, Mapping):
            continue
        preview_text = str(preview.get("text") or "")
        original = preview.get("original_chars")
        if not isinstance(original, int):
            original = len(preview_text)
        previews.append(
            {
                "evidence_id": str(preview.get("evidence_id") or ""),
                "source_kind": str(preview.get("source_kind") or ""),
                "source_url": str(preview.get("source_url") or ""),
                "locator": dict(preview.get("locator") or {}),
                "text": preview_text[:cap],
                "chars": min(len(preview_text), cap),
                "original_chars": original,
                "truncated": original > cap,
            }
        )
    original = item.get("original_chars")
    if not isinstance(original, int):
        original = len(text)
    return {
        "candidate_id": str(item.get("candidate_id")),
        "entity_id": str(item.get("entity_id")),
        "kind": str(item.get("kind")),
        "title": str(item.get("title") or ""),
        "bucket": str(item.get("bucket") or ""),
        "module": str(item.get("module") or "unknown"),
        "tier": str(item.get("tier") or "discovery"),
        "deep_read": bool(item.get("deep_read")),
        "score": int(item.get("score") or 0),
        "score_components": dict(item.get("score_components") or {}),
        "selection_reason": list(item.get("selection_reason") or []),
        "missing_features": list(item.get("missing_features") or []),
        "raw_refs": [
            {
                "artifact": str(ref.get("artifact") or ""),
                "json_pointer": ref.get("json_pointer"),
            }
            for ref in item.get("raw_refs") or []
            if isinstance(ref, Mapping)
        ],
        "evidence_ids": list(item.get("evidence_ids") or []),
        "agent_selected": bool(item.get("agent_selected")),
        "recommended": bool(item.get("recommended")),
        "budget_fit": str(item.get("budget_fit") or "recorded"),
        "preview": {
            "chars": min(len(text), cap),
            "original_chars": original,
            "truncated": original > cap,
        },
        "evidence_previews": previews,
        "full_content_refs": [
            {
                "artifact": str(ref.get("artifact") or ""),
                "json_pointer": ref.get("json_pointer"),
            }
            for ref in item.get("raw_refs") or []
            if isinstance(ref, Mapping)
        ],
    }


__all__ = [
    "PACK_DIR",
    "PACK_INDEX_PATH",
    "RULE_VERSION",
    "audit_pack_index",
    "build_pack_index",
    "pack_id",
    "write_pack_index",
]
