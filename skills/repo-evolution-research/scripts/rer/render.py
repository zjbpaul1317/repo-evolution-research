"""Deterministic Markdown renderer for validated research documents.

Every substantive paragraph comes from structured Claims, Topics, capabilities
and the reading list. The renderer refuses stale validation, records the exact
input hashes and never embeds the current time in ``report.md``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from . import SCHEMA_VERSION, SKILL_ROOT
from .errors import (
    InternalError,
    RERError,
    SchemaError,
    UsageError,
    ValidationFailure,
)
from .models import validate_document
from .store import SHA256_RE, SnapshotStore
from .validation import (
    RESEARCH_PATH,
    VALIDATION_PATH,
    ledger_digest,
    load_research,
    quote_span,
    research_digest,
    sha256_hex,
    substantive_text,
)

REPORT_PATH = "report.md"
RENDER_REPORT_PATH = "analysis/render-report.json"
INCOMPLETE_PATH = "incomplete.md"
TEMPLATE_PATH = SKILL_ROOT / "assets" / "report-template.md"
TEMPLATE_VERSION = "2"

_SOURCE_KIND_LABELS = {
    "pull_request_body": "PR 正文",
    "pull_request_comment": "PR 评论",
    "pull_request_review": "PR Review",
    "pull_request_review_comment": "PR Review 评论",
    "issue_body": "Issue 正文",
    "issue_comment": "Issue 评论",
    "discussion_body": "Discussion 正文",
    "discussion_comment": "Discussion 评论",
    "release_body": "发布说明",
    "doc_content": "文档",
    "commit_message": "Commit 信息",
    "external_page": "外部页面",
    "api_response": "API 响应",
}
_SCOPE_WARNING_CODES = {
    "claim_missing_revision",
    "claim_missing_version",
    "claim_scope_version_mismatch",
}
_COVERAGE_WARNING_CODES = {
    "coverage_degraded",
    "coverage_not_requested",
}
_EVIDENCE_WARNING_CODES = {
    "evidence_unverified",
    "unknown_evidence_heading_only",
}
_REPAIR_HINTS = {
    "claim_missing_revision": "补齐 claim.scope.revision（证据固定 SHA）",
    "claim_missing_version": "补齐 claim.scope.version 或 scope.versions",
    "claim_scope_version_mismatch": "让 scope.version(s) 与 release 实体一致",
    "lifecycle_gap": "按 capability 解释缺口；diff_not_requested 为 V0.1 统一限制",
    "coverage_degraded": "在 limitations 说明未读范围，必要时补采",
    "coverage_not_requested": "说明主动未请求或未执行",
    "evidence_unverified": "改用 verified evidence，或把 claim 降级为 unknown",
    "unknown_evidence_heading_only": "为 unknown 补实质证据或改写 missing_evidence",
}

_PROPOSAL_LABELS = {
    "unknown": "未知",
    "proposed": "已提案",
    "discussing": "讨论中",
    "accepted": "已接受",
    "rejected": "已拒绝",
    "superseded": "已取代",
}
_IMPLEMENTATION_LABELS = {
    "unknown": "未知",
    "not_started": "未开始",
    "in_progress": "进行中",
    "partially_merged": "部分合并",
    "merged": "已合并",
    "reverted": "已回退",
}
_DELIVERY_LABELS = {
    "unknown": "未知",
    "not_found": "未发现",
    "prerelease": "预发布",
    "released": "已发布",
}
_VERIFICATION_LABELS = {
    "verified": "已核验",
    "note_only": "仅发布说明",
    "unknown": "未核验",
    "absent_at_verified_revision": "核验版本中不存在",
}
_AVAILABILITY_LABELS = {
    "unknown": "未知",
    "experimental": "实验性",
    "opt_in": "需显式启用",
    "default_on": "默认开启",
    "deprecated": "已弃用",
    "removed": "已移除",
}
_KIND_LABELS = {"observed": "事实", "inferred": "推断", "unknown": "未知"}
_CONFIDENCE_LABELS = {"high": "高", "medium": "中", "low": "低"}
_READING_KIND_LABELS = {
    "rfc": "RFC",
    "issue": "Issue",
    "pr": "PR",
    "discussion": "Discussion",
    "release": "Release",
    "doc": "文档",
    "source": "源码",
    "external": "外部材料",
}
_EVOLUTION_STAGES = (
    ("problem", "问题与动机"),
    ("design", "设计与方案"),
    ("implementation", "实现进展"),
    ("tradeoff", "取舍与限制"),
)



class _Footnotes:
    def __init__(self) -> None:
        self._numbers: dict[str, int] = {}
        self._claims: dict[str, Mapping[str, Any]] = {}

    def ref(self, claim: Mapping[str, Any]) -> str:
        identifier = str(claim.get("id"))
        if identifier not in self._numbers:
            self._numbers[identifier] = len(self._numbers) + 1
            self._claims[identifier] = claim
        return f"[^C{self._numbers[identifier]:03d}]"

    def render(self, evidence: Mapping[str, Mapping[str, Any]]) -> str:
        if not self._claims:
            return "## 注释\n\n本次报告没有可脚注引用的 claim。"
        lines = ["## 注释", ""]
        for identifier, number in sorted(
            self._numbers.items(), key=lambda item: item[1]
        ):
            claim = self._claims[identifier]
            lines.append(f"[^C{number:03d}]: {self._definition(claim, evidence)}")
        return "\n".join(lines)

    def _definition(
        self,
        claim: Mapping[str, Any],
        evidence: Mapping[str, Mapping[str, Any]],
    ) -> str:
        kind = _KIND_LABELS.get(str(claim.get("kind")), str(claim.get("kind")))
        confidence = _CONFIDENCE_LABELS.get(
            str(claim.get("confidence")), str(claim.get("confidence"))
        )
        reason = _sentence(claim.get("confidence_reason") or "").rstrip("。；; ")
        detail = f"{kind}，置信度：{confidence}"
        if reason:
            detail += f"（{reason}）"
        claim_id = str(claim.get("id") or "")
        detail = f"`{claim_id}` {detail}"
        support = _evidence_details(
            claim.get("evidence_ids") or [],
            evidence,
            claim.get("evidence_quotes"),
        )
        if support:
            detail += f"。证据：{support}"
        contradict = _evidence_details(
            claim.get("contradicting_evidence_ids") or [], evidence
        )
        if contradict:
            detail += f"。反证：{contradict}"
        return detail if detail.endswith(("。", "”", "！", "？")) else detail + "。"


def _dependency_evidence_ids(
    research: Mapping[str, Any],
    relations: Sequence[Mapping[str, Any]],
) -> set[str]:
    identifiers: set[str] = set()
    for claim in _records(research, "claims"):
        for field in ("evidence_ids", "contradicting_evidence_ids"):
            identifiers.update(str(value) for value in claim.get(field) or [])
        quotes = claim.get("evidence_quotes")
        if isinstance(quotes, Mapping):
            identifiers.update(str(key) for key in quotes)
    for capability in _records(research, "capabilities"):
        identifiers.update(
            str(value) for value in capability.get("evidence_ids") or []
        )
    for relation in relations:
        identifiers.update(
            str(value) for value in relation.get("evidence_ids") or []
        )
    return identifiers


def _evidence_dependency_problems(
    store: SnapshotStore,
    research: Mapping[str, Any],
) -> list[dict[str, Any]]:
    try:
        evidence_records = store.read_jsonl("normalized/evidence.jsonl")
        relation_records = store.read_jsonl("normalized/relations.jsonl")
    except RERError as exc:
        return [
            {
                "problem": "unreadable_evidence_ledger",
                "message": exc.message,
            }
        ]
    evidence = {
        str(item.get("id")): item
        for item in evidence_records
        if isinstance(item, Mapping)
    }
    relations = [item for item in relation_records if isinstance(item, Mapping)]
    problems: list[dict[str, Any]] = []
    for evidence_id in sorted(_dependency_evidence_ids(research, relations)):
        record = evidence.get(evidence_id)
        if record is None:
            problems.append(
                {
                    "evidence_id": evidence_id,
                    "problem": "missing_evidence_record",
                    "message": "报告依赖的 evidence 不在 evidence 流中",
                }
            )
            continue
        digest = str(record.get("blob_sha256") or "")
        if not SHA256_RE.fullmatch(digest):
            problems.append(
                {
                    "evidence_id": evidence_id,
                    "blob_sha256": digest,
                    "problem": "corrupt_evidence_blob",
                    "message": f"非法 blob hash：{digest}",
                }
            )
            continue
        if not store.secure_path(f"blobs/{digest}").is_file():
            problems.append(
                {
                    "evidence_id": evidence_id,
                    "blob_sha256": digest,
                    "problem": "missing_evidence_blob",
                    "message": f"blob 不存在：{digest}",
                }
            )
            continue
        try:
            store.read_blob(digest)
        except RERError as exc:
            problems.append(
                {
                    "evidence_id": evidence_id,
                    "blob_sha256": digest,
                    "problem": "corrupt_evidence_blob",
                    "message": exc.message,
                }
            )
    return problems


def render_run(
    store: SnapshotStore,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    research = load_research(store)
    validation_document, validation_raw = _read_validation(store)
    research_sha = research_digest(store)
    ledger_sha = ledger_digest(store, manifest)
    reasons = _stale_reasons(
        validation_document,
        manifest,
        research_sha,
        ledger_sha,
    )
    if reasons:
        write_incomplete(store, manifest, reasons, validation_document)
        raise ValidationFailure(
            "validation 已过期或未通过，不能渲染正式报告",
            source="render.gate",
            recovery="修改 analysis/research.json 后重新运行 validate，再运行 render",
            details={
                "reasons": reasons,
                "validation": str(store.secure_path(VALIDATION_PATH)),
                "incomplete": str(store.secure_path(INCOMPLETE_PATH)),
            },
        )

    dependency_problems = _evidence_dependency_problems(store, research)
    if dependency_problems:
        reasons = [
            f"证据依赖损坏：{item.get('evidence_id') or '-'}"
            f"（{item.get('problem')}）"
            for item in dependency_problems
        ]
        write_incomplete(store, manifest, reasons, validation_document)
        raise ValidationFailure(
            "报告依赖的 evidence 不完整，不能生成正式报告",
            source="render.evidence_integrity",
            recovery="重新 collect/prepare 恢复 evidence blob，再重新 validate/render",
            details={
                "problems": dependency_problems,
                "incomplete": str(store.secure_path(INCOMPLETE_PATH)),
            },
        )

    text = build_report(store, manifest, research, validation_document)
    store.write_text(REPORT_PATH, text)
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(manifest["run_id"]),
        "template_version": TEMPLATE_VERSION,
        "research_sha256": str(research_sha),
        "ledger_sha256": ledger_sha,
        "validation_sha256": sha256_hex(validation_raw),
        "report_path": REPORT_PATH,
        "report_sha256": sha256_hex(text.encode("utf-8")),
        "report_chars": len(text),
        "sections": [
            "overview",
            "evolution",
            "capabilities",
            "unresolved",
            "reading_list",
            "method",
            "footnotes",
        ],
    }
    store.write_json(RENDER_REPORT_PATH, record, schema_name="render_report")
    discard_incomplete(store)
    return {
        "report": str(store.secure_path(REPORT_PATH)),
        "render_report": str(store.secure_path(RENDER_REPORT_PATH)),
        "report_sha256": record["report_sha256"],
        "chars": record["report_chars"],
        "template_version": TEMPLATE_VERSION,
    }


def build_report(
    store: SnapshotStore,
    manifest: Mapping[str, Any],
    research: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> str:
    evidence = _evidence_map(store)
    footnotes = _Footnotes()
    claims = {
        str(item.get("id")): item
        for item in _records(research, "claims")
    }
    topics = _records(research, "topics")
    capabilities = _records(research, "capabilities")
    reading = _records(research, "reading_list")

    sections = {
        "title": _title(manifest),
        "meta": _meta(manifest, research, validation),
        "overview": _overview(manifest, research, footnotes, claims),
        "evolution": _topic_evolution(topics, footnotes, claims),
        "capabilities": _capabilities(
            capabilities,
            manifest,
            {
                str(item.get("capability_id")): item
                for item in validation.get("lifecycle") or []
                if isinstance(item, Mapping)
            },
        ),
        "unresolved": _unresolved(topics, claims, footnotes),
        "reading_list": _reading_list(reading),
        "method": _method(manifest, research, validation),
        "footnotes": footnotes.render(evidence),
    }
    text = _fill_template(_load_template(), sections)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.rstrip() + "\n"


def audit_report_artifacts(
    store: SnapshotStore,
    *,
    manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    research_path = store.secure_path(RESEARCH_PATH)
    validation_path = store.secure_path(VALIDATION_PATH)
    report_path = store.secure_path(REPORT_PATH)
    render_path = store.secure_path(RENDER_REPORT_PATH)

    if validation_path.is_file():
        try:
            document = store.read_json(VALIDATION_PATH)
            validate_document(document, "validation")
        except RERError as exc:
            problems.append(
                {
                    "artifact": VALIDATION_PATH,
                    "problem": "invalid_validation",
                    "message": exc.message,
                }
            )
        else:
            if document.get("run_id") != manifest.get("run_id"):
                problems.append(
                    {
                        "artifact": VALIDATION_PATH,
                        "problem": "validation_run_mismatch",
                        "message": "validation.run_id 与 manifest.run_id 不一致",
                    }
                )
            digest = research_digest(store)
            if digest is None or document.get("research_sha256") != digest:
                problems.append(
                    {
                        "artifact": VALIDATION_PATH,
                        "problem": "stale_validation",
                        "message": "analysis/research.json 已变化，旧的 validation 不再有效",
                    }
                )
            if document.get("ledger_sha256") != ledger_digest(store, manifest):
                problems.append(
                    {
                        "artifact": VALIDATION_PATH,
                        "problem": "stale_validation_inputs",
                        "message": "派生输入已变化，旧的 validation 不再有效",
                    }
                )
    elif report_path.is_file():
        problems.append(
            {
                "artifact": REPORT_PATH,
                "problem": "report_without_validation",
                "message": "存在 report.md，但缺少 analysis/validation.json",
            }
        )

    if render_path.is_file():
        try:
            record = store.read_json(RENDER_REPORT_PATH)
            validate_document(record, "render_report")
        except RERError as exc:
            problems.append(
                {
                    "artifact": RENDER_REPORT_PATH,
                    "problem": "invalid_render_report",
                    "message": exc.message,
                }
            )
        else:
            if not report_path.is_file():
                problems.append(
                    {
                        "artifact": REPORT_PATH,
                        "problem": "missing_report",
                        "message": "render-report 存在，但 report.md 缺失",
                    }
                )
            elif sha256_hex(report_path.read_bytes()) != record.get("report_sha256"):
                problems.append(
                    {
                        "artifact": REPORT_PATH,
                        "problem": "stale_report",
                        "message": "report.md 内容与 render-report.report_sha256 不一致",
                    }
                )
            elif (
                not research_path.is_file()
                or record.get("research_sha256") != research_digest(store)
            ):
                problems.append(
                    {
                        "artifact": REPORT_PATH,
                        "problem": "stale_report",
                        "message": "research.json 已变化，report.md 需要重新渲染",
                    }
                )
            elif record.get("template_version") != TEMPLATE_VERSION:
                problems.append(
                    {
                        "artifact": REPORT_PATH,
                        "problem": "stale_report",
                        "message": (
                            "render-report.template_version 与当前模板 "
                            f"{TEMPLATE_VERSION} 不一致"
                        ),
                    }
                )
            elif (
                validation_path.is_file()
                and record.get("validation_sha256")
                != sha256_hex(validation_path.read_bytes())
            ):
                problems.append(
                    {
                        "artifact": REPORT_PATH,
                        "problem": "stale_report",
                        "message": "validation.json 已变化，report.md 需要重新渲染",
                    }
                )
    elif report_path.is_file():
        problems.append(
            {
                "artifact": REPORT_PATH,
                "problem": "report_without_manifest",
                "message": "存在 report.md，但缺少 analysis/render-report.json",
            }
        )
    return problems


def discard_incomplete(store: SnapshotStore) -> None:
    path = store.secure_path(INCOMPLETE_PATH)
    if path.is_file():
        path.unlink()


def _read_validation(store: SnapshotStore) -> tuple[dict[str, Any], bytes]:
    path = store.secure_path(VALIDATION_PATH)
    if not path.is_file():
        raise UsageError(
            f"缺少 {VALIDATION_PATH}",
            source="render.validation",
            recovery="先运行 validate，再运行 render",
        )
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchemaError(
            f"无法读取 {VALIDATION_PATH}：{exc}",
            source="render.validation",
        ) from exc
    validate_document(document, "validation")
    return document, raw


def _stale_reasons(
    validation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    research_sha: str | None,
    ledger_sha: str,
) -> list[str]:
    reasons: list[str] = []
    if validation.get("run_id") != manifest.get("run_id"):
        reasons.append("validation.run_id 与 manifest.run_id 不一致")
    if research_sha is None:
        reasons.append("analysis/research.json 缺失")
    elif validation.get("research_sha256") != research_sha:
        reasons.append("analysis/research.json 在 validate 之后被修改")
    if validation.get("ledger_sha256") != ledger_sha:
        reasons.append("派生输入在 validate 之后发生变化")
    if not validation.get("ok"):
        reasons.append("validation.ok=false，存在未处理的阻断错误")
    return reasons


def write_incomplete(
    store: SnapshotStore,
    manifest: Mapping[str, Any],
    reasons: Sequence[str],
    validation: Mapping[str, Any],
) -> None:
    lines = [
        "# 未生成正式报告",
        "",
        f"run `{manifest['run_id']}` 未通过报告门禁，不能作为正式报告发布。",
        "",
        "## 原因",
        "",
    ]
    lines.extend(f"- {reason}" for reason in reasons)
    errors = [
        item
        for item in validation.get("errors") or []
        if isinstance(item, Mapping)
    ]
    if errors:
        lines.extend(["", "## 校验错误", ""])
        for item in errors:
            location = f" ({item['path']})" if item.get("path") else ""
            lines.append(f"- `{item.get('code')}`{location}: {item.get('message')}")
    lines.extend(
        [
            "",
            "修改 `analysis/research.json` 后重新运行 `validate`，再运行 `render`。",
            "",
        ]
    )
    store.write_text(INCOMPLETE_PATH, "\n".join(lines))
    discard_report(store)


def discard_report(store: SnapshotStore) -> None:
    for relpath in (REPORT_PATH, RENDER_REPORT_PATH):
        path = store.secure_path(relpath)
        if path.is_file():
            path.unlink()


_PLACEHOLDER_RE = re.compile(r"\{\{([a-z_]+)\}\}")


def _fill_template(template: str, sections: Mapping[str, str]) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in sections:
            missing.append(key)
            return match.group(0)
        return sections[key]

    text = _PLACEHOLDER_RE.sub(replace, template)
    if missing:
        raise InternalError(
            "报告模板包含未知占位符",
            source="render.template",
            details={
                "placeholders": sorted(set(missing)),
                "template": str(TEMPLATE_PATH),
            },
        )
    return text


def _load_template() -> str:
    if not TEMPLATE_PATH.is_file():
        raise InternalError(
            "缺少报告模板",
            source="render.template",
            details={"template": str(TEMPLATE_PATH)},
        )
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    first_line = text.splitlines()[0].strip() if text else ""
    expected = f"<!-- template_version: {TEMPLATE_VERSION} -->"
    if first_line != expected:
        raise InternalError(
            "报告模板版本与 render.py 不一致",
            source="render.template",
            details={"expected": expected, "actual": first_line},
        )
    return text


def _evidence_map(store: SnapshotStore) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("id")): item
        for item in store.read_jsonl("normalized/evidence.jsonl")
        if isinstance(item, Mapping)
    }


def _records(document: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _sentence(value: Any) -> str:
    text = " ".join(str(value).split())
    for char, replacement in (
        ("&", "&amp;"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("[", "\\["),
        ("]", "\\]"),
    ):
        text = text.replace(char, replacement)
    return text


def _table_cell(value: Any) -> str:
    return _sentence(value).replace("|", "\\|")


def _safe_url(value: Any) -> str | None:
    text = str(value or "").strip()
    if not re.match(r"(?i)^https?://", text):
        return None
    for char, replacement in (
        (" ", "%20"),
        ("\n", ""),
        ("\r", ""),
        ("\t", ""),
        ("(", "%28"),
        (")", "%29"),
        ("<", "%3C"),
        (">", "%3E"),
        ('"', "%22"),
        ("\\", "%5C"),
    ):
        text = text.replace(char, replacement)
    return text


def _link(label: Any, url: Any) -> str:
    text = _sentence(label)
    target = _safe_url(url)
    if target is None:
        return text
    return f"[{text}]({target})"


def _locator_label(record: Mapping[str, Any]) -> str:
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


def _entity_label(identifier: Any) -> str:
    value = str(identifier or "")
    parts = value.split("/")
    if len(parts) >= 2 and parts[-2] in (
        "pull",
        "issue",
        "issues",
        "discussion",
        "discussions",
    ):
        prefix = {
            "pull": "PR",
            "issue": "Issue",
            "issues": "Issue",
            "discussion": "Discussion",
            "discussions": "Discussion",
        }
        return f"{prefix[parts[-2]]} #{parts[-1]}"
    if len(parts) >= 2 and parts[-2] == "release":
        return f"Release {parts[-1]}"
    if len(parts) >= 2:
        return parts[-1]
    return value or "-"


def _quote_label(record: Mapping[str, Any], quote: Any = None) -> str:
    source_text = quote if quote is not None else record.get("text")
    text = " ".join(str(source_text or "").split())
    if not text:
        return "“（空文本）”"
    shown = text[:180]
    suffix = "…" if len(text) > 180 else ""
    label = f"“{_sentence(shown)}{suffix}”"
    if not substantive_text(source_text):
        label += "（仅标题/空壳，不能单独支撑事实）"
    return label


def _quote_locator(record: Mapping[str, Any], quote: Any) -> str:
    span = quote_span(record, quote)
    if span is None:
        return "anchor=missing"
    start, end = span
    locator = record.get("locator")
    base = locator.get("char_start") if isinstance(locator, Mapping) else None
    if isinstance(base, int):
        return f"引文 chars {base + start}-{base + end}"
    return f"引文 chars {start}-{end}"


def _evidence_details(
    identifiers: Sequence[Any],
    evidence: Mapping[str, Mapping[str, Any]],
    quotes: Mapping[str, Any] | None = None,
) -> str:
    quote_map = quotes if isinstance(quotes, Mapping) else {}
    parts = []
    for identifier in identifiers:
        record = evidence.get(str(identifier))
        if record is None:
            parts.append(f"`{identifier}`（缺失，verification=missing）")
            continue
        source = _SOURCE_KIND_LABELS.get(
            str(record.get("source_kind")),
            str(record.get("source_kind") or "未知来源"),
        )
        quote = quote_map.get(str(identifier))
        locator = _locator_label(record)
        if quote is not None:
            locator = f"{locator}；{_quote_locator(record, quote)}"
        parts.append(
            f"{_link(str(identifier), record.get('source_url'))}"
            f"（{source}，{_entity_label(record.get('entity_id'))}，"
            f"{locator}，verification={record.get('verification')}）："
            f"{_quote_label(record, quote)}"
        )
    return "；".join(parts)

def _title(manifest: Mapping[str, Any]) -> str:
    repository = manifest["repository"]
    window = manifest["activity_window"]
    return (
        f"{repository['owner']}/{repository['name']} 技术演进研究"
        f"（{window['start'][:10]} 至 {window['end'][:10]}）"
    )


def _meta(
    manifest: Mapping[str, Any],
    research: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> str:
    window = manifest["activity_window"]
    observed = manifest.get("observed") or {}
    basis = manifest.get("state_time_basis", "observed_current")
    return "\n".join(
        [
            f"> run_id：`{manifest['run_id']}` ｜ research：`{validation['research_sha256']}`"
            f" ｜ inputs：`{validation['ledger_sha256']}` ｜ template：`{TEMPLATE_VERSION}`",
            f"> 研究窗口：{window['start']} 至 {window['end']}（{window['timezone']}）",
            f"> 观察时间：{observed.get('started_at') or '未知'} 至 "
            f"{observed.get('finished_at') or '未知'} ｜ 状态口径：`{basis}`",
            f"> research.created_at：{research.get('created_at')}",
        ]
    )


def _overview(
    manifest: Mapping[str, Any],
    research: Mapping[str, Any],
    footnotes: _Footnotes,
    claims: Mapping[str, Mapping[str, Any]],
) -> str:
    repository = manifest["repository"]
    branch = manifest.get("default_branch") or "未记录"
    revision = manifest.get("default_branch_sha") or "未记录"
    lines = [
        f"- 仓库：`{repository['slug']}`（默认分支 `{branch}` @ `{revision}`）",
        f"- 覆盖：{_coverage_summary(manifest)}",
        f"- 预算：{_budget_summary(manifest)}",
        "- 结论速览：",
    ]
    topics = _records(research, "topics")
    for topic in topics:
        summary = _sentence(topic.get("summary") or "")
        entry = f"  - **{_sentence(topic.get('title'))}**"
        if summary:
            entry += f"：{summary}"
        lines.append(entry)
    return "\n".join(lines)


def _coverage_summary(manifest: Mapping[str, Any]) -> str:
    rows = [
        item for item in manifest.get("coverage") or [] if isinstance(item, Mapping)
    ]
    if not rows:
        return "未记录 coverage 通道"
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("coverage_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    detail = "、".join(f"{status} {count}" for status, count in sorted(counts.items()))
    return f"{len(rows)} 个通道（{detail}）"


def _budget_summary(manifest: Mapping[str, Any]) -> str:
    budget = manifest.get("budget") or {}
    used = manifest.get("budget_consumed") or {}
    return (
        f"requests {used.get('requests', 0)}/{budget.get('max_requests', '?')}、"
        f"deep_entities {used.get('deep_entities', 0)}/"
        f"{budget.get('max_deep_entities', '?')}、"
        f"response_bytes {used.get('response_bytes', 0)}"
    )


def _topic_evolution(
    topics: Sequence[Mapping[str, Any]],
    footnotes: _Footnotes,
    claims: Mapping[str, Mapping[str, Any]],
) -> str:
    """Keep the argument for each topic together; render a claim's text once."""
    blocks: list[str] = []
    seen: set[str] = set()
    stages = (("background", "背景"), *_EVOLUTION_STAGES, ("outlook", "后续判断"))
    for topic in topics:
        blocks.extend([f"### {_sentence(topic.get('title'))}", "",
                       f"**研究问题**：{_sentence(topic.get('research_question'))}", ""])
        for stage, label in stages:
            entries = []
            for identifier in (topic.get("claim_ids") or {}).get(stage) or []:
                claim = claims.get(str(identifier))
                if claim is None or claim.get("kind") == "unknown":
                    continue
                if str(identifier) in seen:
                    entries.append(f"- 参见已述结论 {footnotes.ref(claim)}。")
                else:
                    seen.add(str(identifier))
                    entries.append(_claim_line(claim, footnotes))
            if entries:
                blocks.extend([f"**{label}**", "", *entries, ""])
        gaps = [str(value) for value in topic.get("gaps") or []]
        if gaps:
            blocks.extend(["**本主题缺口**", "", *[f"- {_sentence(v)}" for v in gaps], ""])
    return "\n".join(blocks).rstrip() or "本次没有可展示的技术演进主题。"


def _claim_line(claim: Mapping[str, Any], footnotes: _Footnotes) -> str:
    kind = _KIND_LABELS.get(str(claim.get("kind")), str(claim.get("kind")))
    confidence = _CONFIDENCE_LABELS.get(
        str(claim.get("confidence")), str(claim.get("confidence"))
    )
    return (
        f"- {_sentence(claim.get('text'))}{footnotes.ref(claim)}"
        f"（{kind}，置信度：{confidence}）"
    )


def _capabilities(
    capabilities: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    lifecycle: Mapping[str, Mapping[str, Any]],
) -> str:
    if not capabilities:
        return "本次结构化结果没有 capability 状态表。"
    default_branch = manifest.get("default_branch") or ""
    lines = [
        "| 子能力 | 提案 | 实现 | 发布（核验） | 可用性 | 分支@提交 | 适用版本 | 证据 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for capability in capabilities:
        derived = lifecycle.get(str(capability.get("id"))) or {}
        branch = capability.get("target_branch")
        revision = capability.get("observed_revision") or derived.get(
            "observed_revision"
        )
        if branch and revision:
            branch_cell = (
                f"`{_table_cell(branch)}` @ `{_table_cell(str(revision)[:12])}`"
            )
        elif branch:
            branch_cell = f"`{_table_cell(branch)}`"
        elif default_branch:
            branch_cell = f"`{_table_cell(default_branch)}`"
        else:
            branch_cell = "—"
        versions = (
            derived.get("verified_versions")
            or capability.get("verified_versions")
            or []
        )
        evidence_ids = [
            f"`{_table_cell(item)}`" for item in capability.get("evidence_ids") or []
        ]
        title = _table_cell(capability.get("title"))
        scope = _table_cell(capability.get("scope") or "")
        if scope:
            title = f"{title}（{scope}）"
        delivery = _state_label(
            derived.get("delivery_state") or capability.get("delivery_state"),
            _DELIVERY_LABELS,
        )
        verification = str(derived.get("delivery_verification") or "unknown")
        delivery_cell = (
            f"{delivery} ｜ `{verification}`"
            f"（{_VERIFICATION_LABELS.get(verification, verification)}）"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    title,
                    _state_label(
                        derived.get("proposal_state")
                        or capability.get("proposal_state"),
                        _PROPOSAL_LABELS,
                    ),
                    _state_label(
                        derived.get("implementation_state")
                        or capability.get("implementation_state"),
                        _IMPLEMENTATION_LABELS,
                    ),
                    delivery_cell,
                    _state_label(
                        derived.get("availability")
                        or capability.get("availability"),
                        _AVAILABILITY_LABELS,
                    ),
                    branch_cell,
                    "、".join(_table_cell(item) for item in versions)
                    if versions
                    else "—",
                    "、".join(evidence_ids) if evidence_ids else "—",
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def _state_label(value: Any, labels: Mapping[str, str]) -> str:
    key = str(value)
    label = labels.get(key, key)
    return f"{label}（`{key}`）"


def _unresolved(
    topics: Sequence[Mapping[str, Any]],
    claims: Mapping[str, Mapping[str, Any]],
    footnotes: _Footnotes,
) -> str:
    blocks: list[str] = []
    for topic in topics:
        identifiers = (topic.get("claim_ids") or {}).get("unresolved") or []
        entries = []
        for claim_id in identifiers:
            claim = claims.get(str(claim_id))
            if claim is None or claim.get("kind") == "unknown":
                continue
            entries.append(_claim_line(claim, footnotes))
        if entries:
            blocks.append(f"### {_sentence(topic.get('title'))}")
            blocks.append("")
            blocks.extend(entries)
            blocks.append("")
    unknown_claims = [
        claim for claim in claims.values() if claim.get("kind") == "unknown"
    ]
    if unknown_claims:
        blocks.append("### 未知项")
        blocks.append("")
        for claim in sorted(unknown_claims, key=lambda item: str(item.get("id"))):
            reason = _sentence(claim.get("reason") or "未说明原因")
            missing = "、".join(
                _sentence(item) for item in claim.get("missing_evidence") or []
            )
            line = f"- {_sentence(claim.get('text'))}{footnotes.ref(claim)}：{reason}"
            if missing:
                line += f"（缺少：{missing}）"
            blocks.append(line)
        blocks.append("")
    if not blocks:
        return "本次结构化结果没有列出未解决问题。"
    return "\n".join(blocks).rstrip()


def _reading_list(reading: Sequence[Mapping[str, Any]]) -> str:
    if not reading:
        return "本次结构化结果没有推荐阅读。"
    lines = []
    for index, item in enumerate(
        sorted(reading, key=lambda entry: (entry.get("order", 0), str(entry.get("id")))),
        start=1,
    ):
        kind = _READING_KIND_LABELS.get(str(item.get("kind")), str(item.get("kind")))
        lines.append(
            f"{index}. {item.get('priority')} "
            f"{_link(item.get('title'), item.get('url'))}（{kind}）— "
            f"{_sentence(item.get('why'))}"
        )
    return "\n".join(lines)


def _method(
    manifest: Mapping[str, Any],
    research: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> str:
    lines = [
        "### 方法",
        "",
        f"- 研究窗口：{manifest['activity_window']['start']} 至 "
        f"{manifest['activity_window']['end']}"
        f"（{manifest['activity_window']['timezone']}），"
        f"状态口径 `{manifest.get('state_time_basis')}`。",
        f"- 覆盖：{_coverage_summary(manifest)}。",
        f"- 预算消耗：{_budget_summary(manifest)}。",
        f"- 自动化校验：validation ok={str(validation.get('ok')).lower()}，"
        f"errors {validation.get('counts', {}).get('errors', 0)}，"
        f"warnings {validation.get('counts', {}).get('warnings', 0)}。"
        " 自动校验只能证明引用与状态规则成立，不能证明语义解释正确。",
        "",
        "### 局限",
        "",
    ]
    limitations = [str(item) for item in research.get("limitations") or []]
    if limitations:
        lines.extend(f"- {_sentence(item)}" for item in limitations)
    else:
        lines.append("- 未声明局限。")
    warnings = [
        item for item in validation.get("warnings") or [] if isinstance(item, Mapping)
    ]
    if warnings:
        lines.extend(_warning_section(warnings, research))
    return "\n".join(lines)


def _warning_section(
    warnings: Sequence[Mapping[str, Any]],
    research: Mapping[str, Any],
) -> list[str]:
    scope: dict[str, list[Mapping[str, Any]]] = {}
    lifecycle: dict[str, list[str]] = {}
    coverage: dict[str, list[Mapping[str, Any]]] = {}
    evidence: dict[str, list[Mapping[str, Any]]] = {}
    other: dict[str, list[Mapping[str, Any]]] = {}
    capability_titles = {
        str(item.get("id")): str(item.get("title") or item.get("id"))
        for item in _records(research, "capabilities")
    }
    for item in warnings:
        code = str(item.get("code") or "")
        if code in _SCOPE_WARNING_CODES:
            scope.setdefault(str(item.get("claim_id") or "-"), []).append(item)
        elif code in ("lifecycle_gap", "lifecycle_state_differs"):
            capability_id = str(item.get("capability_id") or "-")
            message = str(item.get("message") or code)
            values = lifecycle.setdefault(capability_id, [])
            if message not in values:
                values.append(message)
        elif code in _COVERAGE_WARNING_CODES:
            coverage.setdefault(str(item.get("path") or "coverage"), []).append(item)
        elif code in _EVIDENCE_WARNING_CODES:
            evidence.setdefault(code, []).append(item)
        else:
            other.setdefault(code, []).append(item)

    lines = ["", f"### 校验警告（{len(warnings)} 条）", ""]
    if scope:
        count = sum(len(items) for items in scope.values())
        lines.append(f"- **scope 元数据**（{count} 条）：可从证据来源补齐")
        for claim_id, items in sorted(scope.items()):
            codes = sorted({str(item.get("code")) for item in items})
            paths = sorted(
                {
                    str(item.get("path"))
                    for item in items
                    if isinstance(item.get("path"), str)
                }
            )
            hint = "；".join(
                _REPAIR_HINTS.get(code, "检查 claim.scope") for code in codes
            )
            location = f"；位置：{', '.join(paths)}" if paths else ""
            lines.append(
                f"  - `{claim_id}`：{', '.join(codes)}；修复：{hint}{location}"
            )
    if lifecycle:
        count = sum(len(items) for items in lifecycle.values())
        lines.append(f"- **lifecycle 缺口**（{count} 条）：按 capability 汇总")
        diff_present = False
        for capability_id, messages in sorted(lifecycle.items()):
            gaps = []
            for message in messages:
                gap = message.split("：", 1)[-1]
                gap = gap.split("（", 1)[0]
                if gap:
                    gaps.append(gap)
                if "diff_not_requested" in message:
                    diff_present = True
            title = capability_titles.get(capability_id, capability_id)
            lines.append(
                f"  - `{capability_id}`（{_sentence(title)}）："
                f"{'、'.join(sorted(dict.fromkeys(gaps))) or '-'}"
            )
        if diff_present:
            lines.append(
                "  - 说明：`diff_not_requested` 为 V0.1 统一限制，"
                "不单独降低生命周期状态。"
            )
    if coverage:
        count = sum(len(items) for items in coverage.values())
        lines.append(f"- **coverage**（{count} 条）：区分降级、截断与未执行")
        for path, items in sorted(coverage.items()):
            messages = sorted(
                {str(item.get("message") or "") for item in items if item.get("message")}
            )
            lines.append(
                f"  - `{path}`：{'；'.join(messages)}；"
                f"修复：{_REPAIR_HINTS.get(str(items[0].get('code')), '在 limitations 说明')}"
            )
    if evidence:
        count = sum(len(items) for items in evidence.values())
        lines.append(f"- **evidence**（{count} 条）：引用未 verified 或证据过弱")
        for code, items in sorted(evidence.items()):
            identifiers = sorted(
                {
                    str(item.get("claim_id"))
                    for item in items
                    if item.get("claim_id")
                }
            )
            lines.append(
                f"  - `{code}`：{len(items)} 条；对象："
                f"{', '.join(identifiers) or '-'}；修复："
                f"{_REPAIR_HINTS.get(code, '检查 evidence')}"
            )
    if other:
        count = sum(len(items) for items in other.values())
        lines.append(f"- **其他**（{count} 条）")
        for code, items in sorted(other.items()):
            identifiers = sorted(
                {
                    str(item.get(key))
                    for item in items
                    for key in ("claim_id", "capability_id", "evidence_id")
                    if item.get(key)
                }
            )
            lines.append(
                f"  - `{code}`：{len(items)} 条；对象："
                f"{', '.join(identifiers) or '-'}"
            )
    return lines


__all__ = [
    "INCOMPLETE_PATH",
    "REPORT_PATH",
    "RENDER_REPORT_PATH",
    "TEMPLATE_PATH",
    "TEMPLATE_VERSION",
    "audit_report_artifacts",
    "build_report",
    "discard_incomplete",
    "discard_report",
    "render_run",
    "write_incomplete",
]
