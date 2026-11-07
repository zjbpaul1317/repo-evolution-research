"""Offline validation gates for Agent-authored research documents.

The validator never performs network I/O. It turns a prepared snapshot plus
``analysis/research.json`` into a deterministic ``analysis/validation.json``
that render can trust: schema, identifier uniqueness, reference targets,
evidence integrity, lifecycle state rules and coverage limitations.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import SCHEMA_VERSION
from .collectors.progress import CHANNEL_POLICIES
from .errors import RERError, SchemaError, UsageError
from .lifecycle import check_state_assertions, verify_capability
from .models import normalize_newlines, validate_document
from .store import SnapshotStore

RESEARCH_PATH = "analysis/research.json"
VALIDATION_PATH = "analysis/validation.json"
EVIDENCE_REVIEW_PATH = "analysis/evidence-review.md"

_LEDGER_FILES = (
    "normalized/entities.jsonl",
    "normalized/evidence.jsonl",
    "normalized/relations.jsonl",
    "raw/prs.jsonl",
    "raw/issues.jsonl",
    "raw/discussions.jsonl",
    "analysis/pr-selection.json",
    "analysis/proposal-selection.json",
    "analysis/candidates.json",
    "analysis/pack-index.json",
)
_LEDGER_DIRS = (
    "raw/pr-details",
    "raw/releases",
    "raw/issues",
    "raw/discussions",
    "raw/docs",
    "imported",
)
_DEGRADED_STATUSES = frozenset({"partial", "sampled", "unavailable"})
_CHANNEL_TOKENS: dict[str, tuple[str, ...]] = {
    "activity_prs": ("activity_prs", "pull request"),
    "activity_issues": ("activity_issues", "issue"),
    "activity_discussions": ("activity_discussions", "discussion"),
    "pr_detail": ("pr_detail", "pr 深读", "pull request", "pr deep"),
    "issue_detail": ("issue_detail", "issue 深读", "issue deep"),
    "discussion_detail": ("discussion_detail", "discussion 深读", "discussion deep"),
    "releases": ("releases", "release", "发布"),
    "docs": ("docs", "doc", "文档"),
    "repository": ("repository", "仓库"),
    "default_branch": ("default_branch", "默认分支"),
    "tree": ("tree", "目录树"),
    "discovery_paths": ("discovery_paths", "发现路径"),
    "labels": ("labels", "标签"),
    "discussions_capability": ("discussions_capability", "discussion"),
    "discussions_items": ("discussions_items", "discussion"),
    "files": ("files", "文件"),
}
_BUDGET_TOKENS = ("budget", "预算", "截断", "partial", "部分采集", "采样")
_SCOPE_REVISION_KINDS = frozenset({"doc", "source_file"})
_SCOPE_VERSION_KINDS = frozenset({"release"})


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def research_digest(store: SnapshotStore) -> str | None:
    path = store.secure_path(RESEARCH_PATH)
    if not path.is_file():
        return None
    return sha256_hex(path.read_bytes())


def ledger_digest(store: SnapshotStore, manifest: Mapping[str, Any]) -> str:
    """Hash the inputs that make a previous validation stale."""
    digest = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    for relpath in _LEDGER_FILES:
        path = store.secure_path(relpath)
        if path.is_file():
            entries.append((relpath, sha256_hex(path.read_bytes())))
    for directory in _LEDGER_DIRS:
        root = store.secure_path(directory)
        if not root.is_dir():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relpath = path.relative_to(store.run_dir).as_posix()
            entries.append((relpath, sha256_hex(path.read_bytes())))
    entries.sort()
    for relpath, value in entries:
        digest.update(f"{relpath}\0{value}\n".encode())
    coverage = json.dumps(
        manifest.get("coverage"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest.update(f"coverage\0{coverage}\n".encode())
    digest.update(
        f"default_branch_sha\0{manifest.get('default_branch_sha')}\n".encode()
    )
    return digest.hexdigest()


def load_research(store: SnapshotStore) -> dict[str, Any]:
    path = store.secure_path(RESEARCH_PATH)
    if not path.is_file():
        raise UsageError(
            f"缺少 {RESEARCH_PATH}",
            source="validation.research",
            recovery="先由 Agent 写入 analysis/research.json，再运行 validate",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UsageError(
            f"无法读取 {RESEARCH_PATH}：{exc}",
            source="validation.research",
        ) from exc
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(
            f"{RESEARCH_PATH} 不是合法 UTF-8 JSON：{exc}",
            source="validation.research",
        ) from exc
    validate_document(document, "research")
    return document


def validate_run(
    store: SnapshotStore,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Run every gate and return the validation document (not yet written)."""
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    research: dict[str, Any] | None = None
    research_sha = research_digest(store)
    ledger_sha = ledger_digest(store, manifest)

    try:
        research = load_research(store)
    except UsageError:
        raise
    except RERError as exc:
        errors.append(_problem("research_schema", exc.message, path=RESEARCH_PATH))
        checks.append(_check("research_schema", "failed", exc.message))
    else:
        checks.append(
            _check("research_schema", "passed", "research.json 通过 research schema")
        )

    entities, evidence, relations = _load_ledger(store, errors)
    if not evidence:
        errors.append(
            _problem(
                "no_research_evidence",
                "快照没有研究证据，不能生成正式报告；补读或导入来源后重跑 prepare",
                path="normalized/evidence.jsonl",
            )
        )
    integrity = _integrity_problems(store)
    errors.extend(integrity)
    checks.append(
        _check(
            "evidence_integrity",
            "failed" if integrity else "passed",
            f"{len(integrity)} 个存储/引文问题" if integrity else "证据流、blob 与引文一致",
        )
    )

    lifecycle: list[dict[str, Any]] = []
    if research is not None:
        structural, structural_warnings = _structure_problems(
            research, manifest, entities
        )
        errors.extend(structural)
        warnings.extend(structural_warnings)
        checks.append(
            _check(
                "reference_targets",
                "failed" if structural else "passed",
                f"{len(structural)} 个结构/引用问题" if structural else "ID 与引用目标完整",
            )
        )

        reference, reference_warnings = _evidence_problems(
            research, entities, evidence
        )
        errors.extend(reference)
        warnings.extend(reference_warnings)
        checks.append(
            _check(
                "claim_evidence",
                "failed" if reference else "passed",
                f"{len(reference)} 个证据引用问题" if reference else "claim 证据引用有效",
            )
        )
        _write_evidence_review(store, research, evidence)

        lifecycle, lifecycle_errors, lifecycle_warnings = _lifecycle_problems(
            store, research, entities, evidence, relations
        )
        errors.extend(lifecycle_errors)
        warnings.extend(lifecycle_warnings)
        checks.append(
            _check(
                "lifecycle",
                "failed" if lifecycle_errors else "passed",
                (
                    f"{len(lifecycle_errors)} 个状态断言问题"
                    if lifecycle_errors
                    else f"{len(lifecycle)} 个 capability 状态推导一致"
                ),
            )
        )

        coverage, coverage_warnings = _coverage_problems(research, manifest)
        errors.extend(coverage)
        warnings.extend(coverage_warnings)
        checks.append(
            _check(
                "coverage_limitations",
                "failed" if coverage else "passed",
                f"{len(coverage)} 个覆盖缺口未说明" if coverage else "覆盖缺口已进入局限",
            )
        )

    _sort_problems(errors)
    _sort_problems(warnings)
    counts = {
        "topics": _count(research, "topics"),
        "claims": _count(research, "claims"),
        "capabilities": _count(research, "capabilities"),
        "unknowns": _count(research, "unknowns"),
        "reading_items": _count(research, "reading_list"),
        "lifecycle": len(lifecycle),
        "errors": len(errors),
        "warnings": len(warnings),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(manifest["run_id"]),
        "ok": not errors,
        "research_path": RESEARCH_PATH,
        "research_sha256": research_sha,
        "ledger_sha256": ledger_sha,
        "counts": counts,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "lifecycle": lifecycle,
    }


def write_validation(store: SnapshotStore, document: Mapping[str, Any]) -> Path:
    return store.write_json(VALIDATION_PATH, dict(document), schema_name="validation")


def _count(research: Mapping[str, Any] | None, key: str) -> int:
    if research is None:
        return 0
    value = research.get(key)
    return len(value) if isinstance(value, list) else 0


def _problem(
    code: str,
    message: str,
    *,
    path: str | None = None,
    topic_id: str | None = None,
    claim_id: str | None = None,
    capability_id: str | None = None,
    evidence_id: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "path": path,
        "topic_id": topic_id,
        "claim_id": claim_id,
        "capability_id": capability_id,
        "evidence_id": evidence_id,
        "entity_id": entity_id,
    }


def _check(identifier: str, status: str, detail: str) -> dict[str, Any]:
    return {"id": identifier, "status": status, "detail": detail}


def _sort_problems(problems: list[dict[str, Any]]) -> None:
    problems.sort(
        key=lambda item: (
            item["code"],
            item["path"] or "",
            item["message"],
        )
    )


def _load_ledger(
    store: SnapshotStore,
    errors: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        entities = store.read_jsonl("normalized/entities.jsonl")
        evidence = store.read_jsonl("normalized/evidence.jsonl")
        relations = store.read_jsonl("normalized/relations.jsonl")
    except RERError as exc:
        errors.append(_problem("ledger_unreadable", exc.message, path="normalized/"))
        return [], [], []
    return entities, evidence, relations


def _integrity_problems(store: SnapshotStore) -> list[dict[str, Any]]:
    try:
        report = store.verify_integrity(include_research=False)
    except RERError as exc:
        return [_problem("integrity_error", exc.message, path="manifest.json")]
    problems = []
    for item in report.get("problems") or []:
        if not isinstance(item, Mapping):
            continue
        raw_code = str(item.get("problem") or "unknown")
        problems.append(
            _problem(
                f"integrity:{raw_code}",
                str(item.get("message") or raw_code),
                path=item.get("artifact") or item.get("stream"),
                claim_id=item.get("claim_id"),
                capability_id=item.get("capability_id"),
                evidence_id=item.get("evidence_id"),
                entity_id=item.get("entity_id"),
            )
        )
    return problems


def _structure_problems(
    research: Mapping[str, Any],
    manifest: Mapping[str, Any],
    entities: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    topics = _records(research, "topics")
    claims = _records(research, "claims")
    capabilities = _records(research, "capabilities")
    unknowns = _records(research, "unknowns")
    reading = _records(research, "reading_list")

    if research.get("run_id") != manifest.get("run_id"):
        problems.append(
            _problem(
                "research_run_mismatch",
                f"research.run_id={research.get('run_id')!r} 与 manifest.run_id 不一致",
                path="run_id",
            )
        )
    if not topics:
        problems.append(
            _problem("empty_research", "research.topics 为空，不能生成正式报告", path="topics")
        )

    for label, records in (
        ("topic", topics),
        ("claim", claims),
        ("capability", capabilities),
        ("unknown", unknowns),
        ("reading_item", reading),
    ):
        seen: set[str] = set()
        for index, record in enumerate(records):
            identifier = record.get("id")
            if not isinstance(identifier, str) or not identifier:
                continue
            if identifier in seen:
                problems.append(
                    _problem(
                        "duplicate_id",
                        f"{label} id 重复：{identifier}",
                        path=f"{_collection_name(label)}[{index}].id",
                    )
                )
            seen.add(identifier)

    claim_ids = {str(item.get("id")) for item in claims}
    capability_ids = {str(item.get("id")) for item in capabilities}
    reading_ids = {str(item.get("id")) for item in reading}
    topic_ids = {str(item.get("id")) for item in topics}
    entity_ids = {
        str(item.get("id"))
        for item in entities
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    referenced_claims: set[str] = set()
    referenced_capabilities: set[str] = set()
    referenced_reading: set[str] = set()

    for topic_index, topic in enumerate(topics):
        topic_id = str(topic.get("id"))
        for stage, identifiers in _claim_stages(topic):
            for position, claim_id in enumerate(identifiers):
                referenced_claims.add(str(claim_id))
                if claim_id not in claim_ids:
                    problems.append(
                        _problem(
                            "missing_claim_reference",
                            f"topic.claim_ids.{stage} 引用不存在的 claim：{claim_id}",
                            path=f"topics[{topic_index}].claim_ids.{stage}[{position}]",
                            topic_id=topic_id,
                            claim_id=str(claim_id),
                        )
                    )
        for position, capability_id in enumerate(topic.get("capability_ids") or []):
            referenced_capabilities.add(str(capability_id))
            if capability_id not in capability_ids:
                problems.append(
                    _problem(
                        "missing_capability_reference",
                        f"topic.capability_ids 引用不存在的 capability：{capability_id}",
                        path=f"topics[{topic_index}].capability_ids[{position}]",
                        topic_id=topic_id,
                        capability_id=str(capability_id),
                    )
                )
        for position, item_id in enumerate(topic.get("reading_item_ids") or []):
            referenced_reading.add(str(item_id))
            if item_id not in reading_ids:
                problems.append(
                    _problem(
                        "missing_reading_item_reference",
                        f"topic.reading_item_ids 引用不存在的阅读项：{item_id}",
                        path=f"topics[{topic_index}].reading_item_ids[{position}]",
                        topic_id=topic_id,
                    )
                )
        if not any(identifiers for _stage, identifiers in _claim_stages(topic)):
            problems.append(
                _problem(
                    "empty_topic",
                    "topic 没有关联任何 claim",
                    path=f"topics[{topic_index}]",
                    topic_id=topic_id,
                )
            )

    for index, capability in enumerate(capabilities):
        capability_id = str(capability.get("id"))
        topic_id = capability.get("topic_id")
        if topic_id not in topic_ids:
            problems.append(
                _problem(
                    "missing_topic_reference",
                    f"capability.topic_id 引用不存在的 topic：{topic_id}",
                    path=f"capabilities[{index}].topic_id",
                    capability_id=capability_id,
                )
            )
        if capability_id not in referenced_capabilities:
            warnings.append(
                _problem(
                    "unreferenced_capability",
                    f"capability 未被任何 topic 引用：{capability_id}",
                    path=f"capabilities[{index}]",
                    capability_id=capability_id,
                )
            )

    for index, unknown in enumerate(unknowns):
        claim_id = unknown.get("claim_id")
        if claim_id not in claim_ids:
            problems.append(
                _problem(
                    "missing_unknown_claim",
                    f"unknown.claim_id 引用不存在的 claim：{claim_id}",
                    path=f"unknowns[{index}].claim_id",
                    claim_id=str(claim_id),
                )
            )
        else:
            claim = next(item for item in claims if item.get("id") == claim_id)
            if claim.get("kind") != "unknown":
                problems.append(
                    _problem(
                        "unknown_claim_not_unknown",
                        f"unknowns 指向的 claim kind={claim.get('kind')!r}，应为 unknown",
                        path=f"unknowns[{index}].claim_id",
                        claim_id=str(claim_id),
                    )
                )
        topic_id = unknown.get("topic_id")
        if topic_id is not None and topic_id not in topic_ids:
            problems.append(
                _problem(
                    "missing_topic_reference",
                    f"unknown.topic_id 引用不存在的 topic：{topic_id}",
                    path=f"unknowns[{index}].topic_id",
                    topic_id=str(topic_id),
                )
            )

    repository_host = str(manifest["repository"]["host"]).lower()
    for index, item in enumerate(reading):
        entity_id = item.get("entity_id")
        if entity_id is not None and entity_id not in entity_ids:
            problems.append(
                _problem(
                    "missing_reading_entity",
                    f"reading_list.entity_id 不在 entities 中：{entity_id}",
                    path=f"reading_list[{index}].entity_id",
                    entity_id=str(entity_id),
                )
            )
        parts = urlsplit(str(item.get("url") or ""))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            problems.append(
                _problem(
                    "reading_url_invalid",
                    f"reading_list.url 不是稳定的 HTTP 来源：{item.get('url')!r}",
                    path=f"reading_list[{index}].url",
                )
            )
        elif (
            str(item.get("kind")) != "external"
            and parts.netloc.lower() != repository_host
        ):
            problems.append(
                _problem(
                    "reading_url_host_mismatch",
                    f"reading_list.url 指向 {parts.netloc}，不是仓库 host {repository_host}",
                    path=f"reading_list[{index}].url",
                )
            )

    orders: dict[int, int] = {}
    for item in reading:
        order = item.get("order")
        if isinstance(order, int):
            orders[order] = orders.get(order, 0) + 1
    for order, count in sorted(orders.items()):
        if count > 1:
            warnings.append(
                _problem(
                    "duplicate_reading_order",
                    f"reading_list.order={order} 出现 {count} 次",
                    path="reading_list",
                )
            )

    for index, claim in enumerate(claims):
        claim_id = str(claim.get("id"))
        if claim_id not in referenced_claims:
            warnings.append(
                _problem(
                    "unreferenced_claim",
                    f"claim 未被任何 topic 引用：{claim_id}",
                    path=f"claims[{index}]",
                    claim_id=claim_id,
                )
            )
    return problems, warnings


def _collection_name(label: str) -> str:
    return {
        "topic": "topics",
        "claim": "claims",
        "capability": "capabilities",
        "unknown": "unknowns",
        "reading_item": "reading_list",
    }[label]


def _claim_stages(topic: Mapping[str, Any]) -> list[tuple[str, list[Any]]]:
    mapping = topic.get("claim_ids")
    if not isinstance(mapping, Mapping):
        return []
    stages = (
        "background",
        "problem",
        "design",
        "implementation",
        "tradeoff",
        "unresolved",
        "outlook",
    )
    return [(stage, list(mapping.get(stage) or [])) for stage in stages]


def _records(research: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = research.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _strip_quote_prefix(line: str) -> str:
    value = line.strip()
    while value.startswith(">"):
        value = value[1:].lstrip()
    return value


def substantive_text(text: Any) -> bool:
    """Return False when a block contains only headings or empty scaffolding."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    in_fence = False
    for index, raw_line in enumerate(lines):
        line = _strip_quote_prefix(raw_line)
        if not line:
            continue
        if line.startswith("```") or line.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            return True
        next_line = (
            _strip_quote_prefix(lines[index + 1])
            if index + 1 < len(lines)
            else ""
        )
        if next_line and re.fullmatch(r"(=+|-+)", next_line):
            continue
        if re.fullmatch(r"#{1,6}\s*.*", line):
            continue
        if re.fullmatch(r"[-*_]{3,}", line):
            continue
        if re.fullmatch(r"!\[[^\]]*\]\([^)]*\)", line):
            continue
        if re.fullmatch(r"=+\s*", line):
            continue
        if re.fullmatch(r"-+\s*", line):
            continue
        if line.startswith("<!--") and line.endswith("-->"):
            continue
        return True
    return False


QUOTE_REQUIRED_CHARS = 400
MIN_QUOTE_CHARS = 24


def quote_span(record: Mapping[str, Any], quote: Any) -> tuple[int, int] | None:
    """Return the exact character span of a claim quote inside an evidence block."""
    text = normalize_newlines(str(record.get("text") or ""))
    value = normalize_newlines(str(quote or ""))
    if not value:
        return None
    start = text.find(value)
    if start < 0:
        return None
    return start, start + len(value)


def _quote_locator_text(record: Mapping[str, Any], quote: Any) -> str:
    span = quote_span(record, quote)
    if span is None:
        return "anchor=missing"
    start, end = span
    locator = record.get("locator")
    base = locator.get("char_start") if isinstance(locator, Mapping) else None
    if isinstance(base, int):
        return f"anchor chars {base + start}-{base + end}"
    return f"anchor chars {start}-{end}"


def _heading_only_records(
    evidence_ids: Sequence[Any],
    evidence_map: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    records = []
    for identifier in evidence_ids:
        record = evidence_map.get(str(identifier))
        if record is None:
            return []
        records.append(record)
    if not records:
        return []
    if any(substantive_text(record.get("text")) for record in records):
        return []
    return records


def _locator_text(record: Mapping[str, Any]) -> str:
    locator = record.get("locator")
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
    fragment = locator.get("url_fragment")
    if isinstance(fragment, str) and fragment:
        parts.append(f"fragment={fragment}")
    return ", ".join(parts) or "-"


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _review_quote(text: Any, limit: int = 400) -> list[str]:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return ["> （空文本）"]
    clipped = value[:limit]
    suffix = "…" if len(value) > limit else ""
    lines = [
        f"> {html.escape(line, quote=False)}" if line else ">"
        for line in clipped.splitlines()
    ]
    if suffix:
        lines[-1] += suffix
    return lines


def _review_evidence_lines(
    identifiers: Sequence[Any],
    evidence_map: Mapping[str, Mapping[str, Any]],
    quotes: Mapping[str, Any] | None = None,
) -> list[str]:
    lines: list[str] = []
    quote_map = quotes if isinstance(quotes, Mapping) else {}
    for identifier in identifiers:
        record = evidence_map.get(str(identifier))
        if record is None:
            lines.append(f"- `{identifier}`: missing from normalized/evidence.jsonl")
            continue
        substantive = substantive_text(record.get("text"))
        status = "substantive=yes" if substantive else "substantive=no（仅标题/空壳）"
        quote = quote_map.get(str(identifier))
        anchor = (
            f"anchor=claim, {_quote_locator_text(record, quote)}"
            if quote
            else "anchor=block"
        )
        lines.append(
            f"- `{identifier}` | {record.get('source_kind')} | "
            f"{record.get('entity_id')} | {_locator_text(record)} | "
            f"verification={record.get('verification')} | {status} | {anchor}"
        )
        if record.get("source_url"):
            lines.append(
                "  - source: "
                + html.escape(str(record.get("source_url")), quote=False)
            )
        lines.extend(
            f"  {line}"
            for line in _review_quote(quote if quote is not None else record.get("text"))
        )
    return lines


def _review_issues(
    *,
    kind: str,
    evidence_ids: Sequence[Any],
    evidence_map: Mapping[str, Mapping[str, Any]],
    quotes: Mapping[str, Any] | None,
    require_anchor: bool,
) -> list[str]:
    issues: list[str] = []
    if not evidence_ids and (require_anchor or kind == "capability"):
        issues.append("no_evidence")
    quote_map = quotes if isinstance(quotes, Mapping) else {}
    evidence_id_set = {str(identifier) for identifier in evidence_ids}
    for identifier in quote_map:
        if str(identifier) not in evidence_id_set:
            issues.append("anchor_evidence_mismatch")
    for identifier in evidence_ids:
        record = evidence_map.get(str(identifier))
        if record is None:
            issues.append("missing_evidence")
            continue
        if record.get("verification") != "verified":
            issues.append("unverified_evidence")
        if not substantive_text(record.get("text")):
            issues.append("heading_only")
        quote = quote_map.get(str(identifier))
        if quote is not None:
            value = normalize_newlines(str(quote)).strip()
            if quote_span(record, quote) is None:
                issues.append("anchor_not_found")
            elif not substantive_text(quote):
                issues.append("anchor_heading_only")
            elif len(value) < MIN_QUOTE_CHARS:
                issues.append("anchor_too_short")
            elif normalize_newlines(str(record.get("text") or "")).count(value) > 1:
                issues.append("anchor_ambiguous")
        if (
            require_anchor
            and len(normalize_newlines(str(record.get("text") or "")))
            > QUOTE_REQUIRED_CHARS
            and str(identifier) not in quote_map
        ):
            issues.append("anchor_missing")
    return sorted(set(issues))


def build_evidence_review(
    research: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    """Render a claim/capability to evidence-text view for manual review."""
    evidence_map = {
        str(item.get("id")): item
        for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    claims = _records(research, "claims")
    capabilities = _records(research, "capabilities")
    review_rows: list[tuple[str, str, list[str]]] = []
    for claim in claims:
        kind = str(claim.get("kind") or "")
        evidence_ids = list(claim.get("evidence_ids") or [])
        issues = _review_issues(
            kind=kind,
            evidence_ids=evidence_ids,
            evidence_map=evidence_map,
            quotes=claim.get("evidence_quotes"),
            require_anchor=kind in ("observed", "inferred"),
        )
        review_rows.append((str(claim.get("id")), kind, issues))
    for capability in capabilities:
        evidence_ids = list(capability.get("evidence_ids") or [])
        issues = _review_issues(
            kind="capability",
            evidence_ids=evidence_ids,
            evidence_map=evidence_map,
            quotes=None,
            require_anchor=False,
        )
        review_rows.append((str(capability.get("id")), "capability", issues))
    issue_counts: dict[str, int] = {}
    for _identifier, _kind, issues in review_rows:
        for issue in issues:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    needs_attention = sum(1 for _id, _kind, issues in review_rows if issues)
    lines = [
        "# Evidence Review",
        "",
        f"- run_id: `{research.get('run_id')}`",
        f"- claims: {len(claims)}",
        f"- capabilities: {len(capabilities)}",
        f"- needs_attention: {needs_attention}",
        "- issue_counts: "
        + (", ".join(f"{key}={value}" for key, value in sorted(issue_counts.items())) or "-"),
        "- 说明：`substantive=no` 表示该块只有标题/空壳内容，不能单独支撑事实 claim。",
        "- 说明：`anchor=claim` 表示 claim 级引文；`anchor=block` 表示尚未提供 claim 级引文。",
        "",
        "## Claims",
        "",
    ]
    for claim in claims:
        claim_id = str(claim.get("id"))
        evidence_ids = list(claim.get("evidence_ids") or [])
        kind = str(claim.get("kind") or "")
        issues = _review_issues(
            kind=kind,
            evidence_ids=evidence_ids,
            evidence_map=evidence_map,
            quotes=claim.get("evidence_quotes"),
            require_anchor=kind in ("observed", "inferred"),
        )
        status = "needs_attention" if issues else "ok"
        scope = claim.get("scope")
        scope_map = scope if isinstance(scope, Mapping) else {}
        scope_entities = ", ".join(
            str(value) for value in scope_map.get("entity_ids") or []
        ) or "-"
        scope_versions = ", ".join(
            str(value) for value in scope_map.get("versions") or []
        ) or "-"
        lines.extend(
            [
                f"### `{claim_id}`",
                "",
                f"- kind: {claim.get('kind')} | confidence: {claim.get('confidence')} | "
                f"review: {status} | issues: {', '.join(issues) or '-'}",
                f"- text: {_one_line(claim.get('text'))}",
                f"- scope: capability={scope_map.get('capability_id')} | "
                f"entities={scope_entities} | revision={scope_map.get('revision')} | "
                f"version={scope_map.get('version')} | versions={scope_versions}",
                "",
            ]
        )
        if evidence_ids:
            lines.extend(
                _review_evidence_lines(
                    evidence_ids,
                    evidence_map,
                    claim.get("evidence_quotes"),
                )
            )
        else:
            lines.append("- （无 evidence_ids）")
        lines.append("")
    lines.extend(["## Capabilities", ""])
    for capability in capabilities:
        capability_id = str(capability.get("id"))
        evidence_ids = list(capability.get("evidence_ids") or [])
        issues = _review_issues(
            kind="capability",
            evidence_ids=evidence_ids,
            evidence_map=evidence_map,
            quotes=None,
            require_anchor=False,
        )
        status = "needs_attention" if issues else "ok"
        lines.extend(
            [
                f"### `{capability_id}`",
                "",
                f"- title: {capability.get('title')} | review: {status} | "
                f"issues: {', '.join(issues) or '-'}",
                f"- states: proposal={capability.get('proposal_state')} | "
                f"implementation={capability.get('implementation_state')} | "
                f"delivery={capability.get('delivery_state')} | "
                f"availability={capability.get('availability')}",
                "",
            ]
        )
        if evidence_ids:
            lines.extend(_review_evidence_lines(evidence_ids, evidence_map))
        else:
            lines.append("- （无 evidence_ids）")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_evidence_review(
    store: SnapshotStore,
    research: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> None:
    store.write_text(EVIDENCE_REVIEW_PATH, build_evidence_review(research, evidence))


def _entity_release_tag(entity: Mapping[str, Any]) -> str:
    raw_state = entity.get("raw_state")
    if isinstance(raw_state, Mapping):
        tag = raw_state.get("tag_name")
        if isinstance(tag, str) and tag:
            return tag
    return str(entity.get("id") or "").rsplit("/", 1)[-1]


def _evidence_problems(
    research: Mapping[str, Any],
    entities: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    entity_map = {
        str(item.get("id")): item
        for item in entities
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    evidence_map = {
        str(item.get("id")): item
        for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    claims = _records(research, "claims")
    capabilities = _records(research, "capabilities")

    for index, claim in enumerate(claims):
        claim_id = str(claim.get("id"))
        kind = str(claim.get("kind"))
        evidence_ids = list(claim.get("evidence_ids") or [])
        if kind in ("observed", "inferred") and not evidence_ids:
            problems.append(
                _problem(
                    "claim_without_evidence",
                    f"{kind} claim 没有 evidence_ids",
                    path=f"claims[{index}].evidence_ids",
                    claim_id=claim_id,
                )
            )
        for position, evidence_id in enumerate(evidence_ids):
            if evidence_id not in evidence_map:
                problems.append(
                    _problem(
                        "missing_evidence",
                        f"claim.evidence_ids 引用不存在的 evidence：{evidence_id}",
                        path=f"claims[{index}].evidence_ids[{position}]",
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                    )
                )
        quotes = claim.get("evidence_quotes")
        quote_map = quotes if isinstance(quotes, Mapping) else {}
        for evidence_id, quote in quote_map.items():
            if evidence_id not in evidence_ids:
                problems.append(
                    _problem(
                        "claim_quote_evidence_mismatch",
                        f"claim.evidence_quotes 引用了不在 evidence_ids 中的证据：{evidence_id}",
                        path=f"claims[{index}].evidence_quotes.{evidence_id}",
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                    )
                )
                continue
            record = evidence_map.get(str(evidence_id))
            if record is None:
                continue
            value = normalize_newlines(str(quote)).strip()
            if quote_span(record, quote) is None:
                problems.append(
                    _problem(
                        "claim_quote_not_found",
                        f"claim.evidence_quotes 的引文不是证据原文的精确片段：{evidence_id}",
                        path=f"claims[{index}].evidence_quotes.{evidence_id}",
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                    )
                )
            elif not substantive_text(quote):
                problems.append(
                    _problem(
                        "claim_quote_heading_only",
                        f"claim.evidence_quotes 只包含标题/空壳内容：{evidence_id}",
                        path=f"claims[{index}].evidence_quotes.{evidence_id}",
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                    )
                )
            elif len(value) < MIN_QUOTE_CHARS:
                problems.append(
                    _problem(
                        "claim_quote_too_short",
                        f"claim.evidence_quotes 的引文过短：{evidence_id}",
                        path=f"claims[{index}].evidence_quotes.{evidence_id}",
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                    )
                )
            elif normalize_newlines(str(record.get("text") or "")).count(value) > 1:
                problems.append(
                    _problem(
                        "claim_quote_ambiguous",
                        f"claim.evidence_quotes 在证据中出现多次，无法唯一定位：{evidence_id}",
                        path=f"claims[{index}].evidence_quotes.{evidence_id}",
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                    )
                )
        if kind in ("observed", "inferred"):
            for evidence_id in evidence_ids:
                record = evidence_map.get(str(evidence_id))
                if (
                    record is not None
                    and len(normalize_newlines(str(record.get("text") or "")))
                    > QUOTE_REQUIRED_CHARS
                    and evidence_id not in quote_map
                ):
                    problems.append(
                        _problem(
                            "claim_quote_missing",
                            f"长证据缺少 claim 级引文：{evidence_id}",
                            path=f"claims[{index}].evidence_quotes",
                            claim_id=claim_id,
                            evidence_id=str(evidence_id),
                        )
                    )
        for position, evidence_id in enumerate(
            claim.get("contradicting_evidence_ids") or []
        ):
            if evidence_id not in evidence_map:
                problems.append(
                    _problem(
                        "missing_contradicting_evidence",
                        f"claim.contradicting_evidence_ids 引用不存在的 evidence：{evidence_id}",
                        path=f"claims[{index}].contradicting_evidence_ids[{position}]",
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                    )
                )
        scope = claim.get("scope")
        scope_map = scope if isinstance(scope, Mapping) else {}
        release_tags: list[str] = []
        revision_kinds: list[str] = []
        for position, entity_id in enumerate(scope_map.get("entity_ids") or []):
            if entity_id not in entity_map:
                problems.append(
                    _problem(
                        "missing_claim_entity",
                        f"claim.scope.entity_ids 引用不存在的实体：{entity_id}",
                        path=f"claims[{index}].scope.entity_ids[{position}]",
                        claim_id=claim_id,
                        entity_id=str(entity_id),
                    )
                )
                continue
            entity = entity_map[entity_id]
            entity_kind = str(entity.get("kind") or "")
            if entity_kind in _SCOPE_REVISION_KINDS:
                revision_kinds.append(entity_kind)
            if entity_kind in _SCOPE_VERSION_KINDS:
                release_tags.append(_entity_release_tag(entity))
        versions = [
            str(value)
            for value in scope_map.get("versions") or []
            if isinstance(value, str) and value
        ]
        if revision_kinds and not scope_map.get("revision"):
            warnings.append(
                _problem(
                    "claim_missing_revision",
                    f"{revision_kinds[0]} 证据缺少 scope.revision 标签",
                    path=f"claims[{index}].scope.revision",
                    claim_id=claim_id,
                )
            )
        if release_tags and not scope_map.get("version") and not versions:
            warnings.append(
                _problem(
                    "claim_missing_version",
                    "release 证据缺少 scope.version/scope.versions 标签",
                    path=f"claims[{index}].scope.version",
                    claim_id=claim_id,
                )
            )
        if versions:
            missing_versions = sorted(set(release_tags) - set(versions))
            extra_versions = sorted(set(versions) - set(release_tags))
            if missing_versions or extra_versions:
                warnings.append(
                    _problem(
                        "claim_scope_version_mismatch",
                        "scope.versions 与 release 实体不一致；缺少 "
                        f"{missing_versions or '-'}，多出 {extra_versions or '-'}",
                        path=f"claims[{index}].scope.versions",
                        claim_id=claim_id,
                    )
                )
        elif scope_map.get("version") and release_tags:
            if str(scope_map["version"]) not in release_tags:
                warnings.append(
                    _problem(
                        "claim_scope_version_mismatch",
                        f"scope.version={scope_map['version']!r} 不在 release scope 中",
                        path=f"claims[{index}].scope.version",
                        claim_id=claim_id,
                    )
                )
        heading_records = _heading_only_records(evidence_ids, evidence_map)
        if heading_records:
            if kind == "unknown":
                warnings.append(
                    _problem(
                        "unknown_evidence_heading_only",
                        "unknown claim 的全部 evidence 都是标题/空壳块；不能证明缺口",
                        path=f"claims[{index}].evidence_ids",
                        claim_id=claim_id,
                    )
                )
            else:
                problems.append(
                    _problem(
                        "claim_evidence_heading_only",
                        "observed/inferred claim 的全部 evidence 都是标题/空壳块",
                        path=f"claims[{index}].evidence_ids",
                        claim_id=claim_id,
                    )
                )
        for position, evidence_id in enumerate(evidence_ids):
            if evidence_id not in evidence_map:
                continue
            record = evidence_map[evidence_id]
            if record.get("verification") != "verified":
                warnings.append(
                    _problem(
                        "evidence_unverified",
                        f"claim 引用的 evidence verification={record.get('verification')!r}",
                        path=f"claims[{index}].evidence_ids[{position}]",
                        claim_id=claim_id,
                        evidence_id=str(evidence_id),
                    )
                )

    for index, capability in enumerate(capabilities):
        capability_id = str(capability.get("id"))
        capability_evidence = list(capability.get("evidence_ids") or [])
        for position, evidence_id in enumerate(capability_evidence):
            if evidence_id not in evidence_map:
                problems.append(
                    _problem(
                        "missing_evidence",
                        f"capability.evidence_ids 引用不存在的 evidence：{evidence_id}",
                        path=f"capabilities[{index}].evidence_ids[{position}]",
                        capability_id=capability_id,
                        evidence_id=str(evidence_id),
                    )
                )
        if _heading_only_records(capability_evidence, evidence_map):
            problems.append(
                _problem(
                    "capability_evidence_heading_only",
                    "capability 的全部 evidence 都是标题/空壳块",
                    path=f"capabilities[{index}].evidence_ids",
                    capability_id=capability_id,
                )
            )
    return problems, warnings


def _lifecycle_problems(
    store: SnapshotStore,
    research: Mapping[str, Any],
    entities: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    capabilities = _records(research, "capabilities")
    claims = _records(research, "claims")
    if not capabilities:
        errors = []
        for issue in check_state_assertions(claims, {}, []):
            errors.append(
                _problem(
                    str(issue.get("code") or "state_assertion_failed"),
                    str(issue.get("message") or "状态断言不成立"),
                    capability_id=(
                        str(issue["capability_id"])
                        if issue.get("capability_id")
                        else None
                    ),
                    claim_id=str(issue["claim_id"]) if issue.get("claim_id") else None,
                )
            )
        return [], errors, []
    pr_documents, pr_problems = _load_documents(store, "raw/pr-details", "pr_detail")
    release_documents, release_problems = _load_documents(
        store, "raw/releases", "release_detail"
    )
    results = []
    for capability in capabilities:
        result = verify_capability(
            capability,
            entities,
            relations,
            evidence,
            pr_documents=pr_documents,
            release_documents=release_documents,
            claims=claims,
        )
        results.append(result)

    records = []
    warnings: list[dict[str, Any]] = []
    for result in results:
        capability = next(
            item
            for item in capabilities
            if str(item.get("id")) == result["capability_id"]
        )
        records.append(
            {
                "capability_id": result["capability_id"],
                "declared": {
                    "proposal_state": str(capability.get("proposal_state")),
                    "implementation_state": str(capability.get("implementation_state")),
                    "delivery_state": str(capability.get("delivery_state")),
                    "availability": str(capability.get("availability")),
                },
                "derived": {
                    "proposal_state": str(result.get("proposal_state")),
                    "implementation_state": str(result.get("implementation_state")),
                    "delivery_state": str(result.get("delivery_state")),
                    "availability": str(result.get("availability")),
                },
                "delivery_verification": str(result.get("delivery_verification")),
                "state_time_basis": str(result.get("state_time_basis")),
                "observed_revision": result.get("observed_revision"),
                "verified_versions": list(result.get("verified_versions") or []),
                "supporting_entities": dict(result.get("supporting_entities") or {}),
                "gaps": list(result.get("gaps") or []),
                "conflicts": list(result.get("conflicts") or []),
            }
        )
        for gap in result.get("gaps") or []:
            message = f"lifecycle 缺口：{gap}"
            if gap == "diff_not_requested":
                message += "（V0.1 未请求 diff；不单独降低状态）"
            warnings.append(
                _problem(
                    "lifecycle_gap",
                    message,
                    capability_id=result["capability_id"],
                )
            )
        for conflict in result.get("conflicts") or []:
            warnings.append(
                _problem(
                    "lifecycle_state_differs",
                    str(conflict.get("message") or "lifecycle 推导与声明不一致"),
                    capability_id=result["capability_id"],
                )
            )
    warnings.extend(pr_problems)
    warnings.extend(release_problems)
    records.sort(key=lambda item: item["capability_id"])
    warnings.sort(key=lambda item: (item["code"], item["capability_id"] or "", item["message"]))
    errors = []
    for issue in check_state_assertions(claims, capabilities, results):
        errors.append(
            _problem(
                str(issue.get("code") or "state_assertion_failed"),
                str(issue.get("message") or "状态断言不成立"),
                capability_id=(
                    str(issue["capability_id"]) if issue.get("capability_id") else None
                ),
                claim_id=str(issue["claim_id"]) if issue.get("claim_id") else None,
            )
        )
    return records, errors, warnings


def _load_documents(
    store: SnapshotStore,
    directory: str,
    schema_name: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = store.secure_path(directory)
    if not root.is_dir():
        return [], []
    documents = []
    problems: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*.json") if item.is_file()):
        relpath = path.relative_to(store.run_dir).as_posix()
        try:
            document = store.read_json(relpath)
            validate_document(document, schema_name)
        except RERError as exc:
            problems.append(
                _problem(
                    "lifecycle_document_unreadable",
                    f"{relpath} 无法用于 lifecycle 推导：{exc.message}",
                    path=relpath,
                )
            )
            continue
        documents.append(document)
    return documents, problems


def _coverage_problems(
    research: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    limitations = " \n ".join(str(item) for item in research.get("limitations") or [])
    lowered = limitations.lower()

    if not limitations.strip():
        problems.append(
            _problem("missing_limitations", "research.limitations 为空", path="limitations")
        )

    reported: set[str] = set()
    for entry in manifest.get("coverage") or []:
        if not isinstance(entry, Mapping):
            continue
        channel = str(entry.get("channel") or "")
        if not channel:
            continue
        statuses = {
            str(entry.get(key) or "")
            for key in ("coverage_status", "discovery_status", "detail_status")
        }
        if statuses & _DEGRADED_STATUSES:
            degraded = sorted(statuses & _DEGRADED_STATUSES)
            policy = CHANNEL_POLICIES.get(channel)
            blocking = policy is not None and bool(
                statuses & policy.blocking_statuses
            )
            if not blocking:
                reason = entry.get("truncation_reason")
                message = f"channel {channel} 状态为 {degraded}，不影响阻断判定"
                if isinstance(reason, str) and reason:
                    message += f"（truncation_reason={reason}）"
                warnings.append(
                    _problem(
                        "coverage_degraded",
                        message,
                        path=f"coverage:{channel}",
                    )
                )
            elif not _mentions_channel(lowered, channel):
                if channel in reported:
                    continue
                reported.add(channel)
                problems.append(
                    _problem(
                        "coverage_undocumented",
                        f"coverage channel {channel} 状态为 {degraded}，"
                        "但 limitations 未说明",
                        path="limitations",
                    )
                )
        elif str(entry.get("coverage_status") or "") == "not_requested":
            warnings.append(
                _problem(
                    "coverage_not_requested",
                    f"coverage channel {channel} 未执行（coverage_status=not_requested）",
                    path=f"coverage:{channel}",
                )
            )

    partial = manifest.get("stages", {}).get("collect") == "partial"
    budget_hit = _budget_exhausted(manifest)
    if partial or budget_hit:
        if not any(token in lowered for token in _BUDGET_TOKENS):
            problems.append(
                _problem(
                    "budget_undocumented",
                    "collect 为 partial 或预算耗尽，但 limitations 未说明",
                    path="limitations",
                )
            )
    return problems, warnings


def _mentions_channel(lowered_limitations: str, channel: str) -> bool:
    tokens = _CHANNEL_TOKENS.get(channel, (channel,))
    return any(token.lower() in lowered_limitations for token in tokens)


def _budget_exhausted(manifest: Mapping[str, Any]) -> bool:
    budget = manifest.get("budget")
    used = manifest.get("budget_consumed")
    if not isinstance(budget, Mapping) or not isinstance(used, Mapping):
        return False
    for used_key, budget_key in (
        ("requests", "max_requests"),
        ("total_bytes", "max_total_bytes"),
        ("deep_entities", "max_deep_entities"),
    ):
        limit = budget.get(budget_key)
        current = used.get(used_key)
        if isinstance(limit, int) and isinstance(current, int) and current >= limit:
            return True
    runtime_seconds = used.get("runtime_seconds")
    minutes = budget.get("max_runtime_minutes")
    if isinstance(runtime_seconds, (int, float)) and isinstance(minutes, int):
        if float(runtime_seconds) >= minutes * 60:
            return True
    return False


__all__ = [
    "RESEARCH_PATH",
    "VALIDATION_PATH",
    "ledger_digest",
    "load_research",
    "research_digest",
    "sha256_hex",
    "validate_run",
    "write_validation",
]
