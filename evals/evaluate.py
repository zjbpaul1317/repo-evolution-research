#!/usr/bin/env python3
"""Score one research run against a human annotation case.

Usage:
    python evals/evaluate.py --run <RUN> --case <CASE_DIR> [--out evals/results]

The evaluator only reads run artifacts. It writes evaluation.json and
evaluation.md outside the run directory and never calls the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPTS = ROOT / "skills/repo-evolution-research/scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from rer.collectors.progress import CHANNEL_POLICIES  # noqa: E402
from rer.store import SnapshotStore  # noqa: E402
from rer.validation import ledger_digest  # noqa: E402

EVAL_ROOT = Path(__file__).resolve().parent
ANNOTATION_SCHEMA = EVAL_ROOT / "cases/schema/annotation.schema.json"
DEFAULT_OUT = EVAL_ROOT / "results"
EVALUATION_SCHEMA_VERSION = "1.0"
CAPABILITY_FIELDS = (
    "proposal_state",
    "implementation_state",
    "delivery_state",
    "availability",
)
DEGRADED = frozenset({"partial", "sampled", "unavailable"})


class EvaluationError(Exception):
    """Invalid case, run, or annotation."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvaluationError(f"无法读取 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"{path} 不是合法 JSON：{exc}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationError(f"{path}:{number} 不是合法 JSON：{exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else round(numerator / denominator, 4)


def _ratio_or_none(numerator: int, denominator: int) -> float | None:
    """Undefined when there is nothing to check, instead of a perfect score."""
    return None if denominator == 0 else round(numerator / denominator, 4)


def _unique(values: Sequence[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise EvaluationError(f"annotation.{label} 存在重复值")


def _check_annotation(annotation: Mapping[str, Any]) -> None:
    topics = annotation.get("topics") or []
    capabilities = annotation.get("capabilities") or []
    unknowns = annotation.get("unknowns") or []
    topic_titles = {_norm(item.get("title")) for item in topics}
    if len(topic_titles) != len(topics):
        raise EvaluationError("annotation.topics 标题必须唯一")
    _unique([str(item.get("id")) for item in topics], "topics[].id")
    _unique([str(item.get("id")) for item in capabilities], "capabilities[].id")
    for item in capabilities:
        if _norm(item.get("topic_title")) not in topic_titles:
            raise EvaluationError(
                f"capability {item.get('id')} 的 topic_title 不存在：{item.get('topic_title')}"
            )
    for item in unknowns:
        if _norm(item.get("topic_title")) not in topic_titles:
            raise EvaluationError(
                f"unknown 的 topic_title 不存在：{item.get('topic_title')}"
            )


def load_annotation(case_path: Path) -> dict[str, Any]:
    path = case_path / "annotation.json" if case_path.is_dir() else case_path
    if not path.is_file():
        raise EvaluationError(f"annotation 文件不存在：{path}")
    annotation = _read_json(path)
    schema = _read_json(ANNOTATION_SCHEMA)
    try:
        jsonschema.validate(
            annotation,
            schema,
            format_checker=jsonschema.FormatChecker(),
        )
    except jsonschema.ValidationError as exc:
        raise EvaluationError(f"annotation schema 校验失败：{exc.message}") from exc
    if not isinstance(annotation, dict):
        raise EvaluationError("annotation 必须是 JSON object")
    _check_annotation(annotation)
    return annotation


def _instant(value: Any, label: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationError(f"{label} 不是合法时间：{value!r}") from exc


def _check_case_matches_run(
    annotation: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> None:
    repository = str(annotation.get("repository") or "").rstrip("/").casefold()
    manifest_repo = manifest.get("repository") or {}
    slug = str(manifest_repo.get("slug") or "").casefold()
    canonical = str(manifest_repo.get("canonical_url") or "").rstrip("/").casefold()
    if repository not in {slug, canonical}:
        raise EvaluationError(
            f"annotation.repository 与 run 不一致：{annotation.get('repository')}"
        )

    window = annotation.get("window") or {}
    run_window = manifest.get("activity_window") or {}
    for key in ("start", "end"):
        annotated = _instant(window.get(key), f"annotation.window.{key}")
        actual = _instant(run_window.get(key), f"manifest.activity_window.{key}")
        if annotated != actual:
            raise EvaluationError(f"annotation.window.{key} 与 run 窗口不一致")

    annotated_sha = (annotation.get("source") or {}).get("default_branch_sha")
    run_sha = manifest.get("default_branch_sha")
    if annotated_sha and run_sha and annotated_sha != run_sha:
        raise EvaluationError("annotation.source.default_branch_sha 与 run 不一致")


def _load_run(run_dir: Path) -> dict[str, Any]:
    if not run_dir.is_dir():
        raise EvaluationError(f"run 目录不存在：{run_dir}")
    manifest = _read_json(run_dir / "manifest.json")
    research_path = run_dir / "analysis/research.json"
    research = _read_json(research_path)
    evidence = _read_jsonl(run_dir / "normalized/evidence.jsonl")
    relations = _read_jsonl(run_dir / "normalized/relations.jsonl")
    validation_path = run_dir / "analysis/validation.json"
    validation = _read_json(validation_path) if validation_path.is_file() else None
    store = SnapshotStore(run_dir)
    return {
        "manifest": manifest,
        "research": research,
        "research_sha256": hashlib.sha256(research_path.read_bytes()).hexdigest(),
        "ledger_sha256": ledger_digest(store, manifest),
        "evidence": evidence,
        "relations": relations,
        "validation": validation,
    }


def _gate(run: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse to score a run whose validation is missing, failed or stale."""
    manifest = run["manifest"]
    validation = run["validation"]
    present = isinstance(validation, Mapping)
    ok = present and validation.get("ok") is True
    research_matches = (
        present and validation.get("research_sha256") == run["research_sha256"]
    )
    ledger_matches = (
        present and validation.get("ledger_sha256") == run["ledger_sha256"]
    )
    run_matches = present and validation.get("run_id") == manifest.get("run_id")
    gate = {
        "validation_present": present,
        "ok": ok,
        "research_sha_matches": research_matches,
        "ledger_sha_matches": ledger_matches,
        "run_id_matches": run_matches,
    }
    problems: list[str] = []
    if not present:
        problems.append("缺少 analysis/validation.json")
    else:
        if not ok:
            problems.append("validation.ok=false")
        if not research_matches:
            problems.append("research.json 在 validate 之后被修改")
        if not ledger_matches:
            problems.append("派生输入在 validate 之后发生变化")
        if not run_matches:
            problems.append("validation.run_id 与 manifest 不一致")
    if problems:
        raise EvaluationError("run 未通过门禁，拒绝评分：" + "；".join(problems))
    return gate


def _claim_evidence(research: Mapping[str, Any]) -> dict[str, set[str]]:
    return {
        str(item.get("id")): {str(value) for value in item.get("evidence_ids") or []}
        for item in research.get("claims") or []
        if isinstance(item, Mapping)
    }


def _topic_evidence(
    research: Mapping[str, Any],
    claims: Mapping[str, set[str]],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for topic in research.get("topics") or []:
        evidence_ids: set[str] = set()
        stage_map = topic.get("claim_ids")
        if isinstance(stage_map, Mapping):
            for identifiers in stage_map.values():
                for claim_id in identifiers or []:
                    evidence_ids.update(claims.get(str(claim_id), set()))
        result[str(topic.get("id"))] = evidence_ids
    return result


def _names(item: Mapping[str, Any]) -> set[str]:
    names = {_norm(item.get("title"))}
    for alias in item.get("title_aliases") or []:
        names.add(_norm(alias))
    names.discard("")
    return names


def _match_topics(
    annotation: Mapping[str, Any],
    research: Mapping[str, Any],
) -> dict[str, Any]:
    """Match annotated topics by title/alias, then by key-evidence overlap."""
    claims = _claim_evidence(research)
    evidence_by_topic = _topic_evidence(research, claims)
    reported = [
        {
            "id": str(topic.get("id")),
            "title": _norm(topic.get("title")),
            "evidence": evidence_by_topic.get(str(topic.get("id")), set()),
        }
        for topic in research.get("topics") or []
    ]
    annotated = list(annotation.get("topics") or [])
    matched: dict[str, str] = {}
    matched_by = {"title": 0, "alias": 0, "evidence": 0}
    used: set[str] = set()
    missing: list[str] = []
    for item in annotated:
        key = _norm(item.get("title"))
        names = _names(item)
        hit = next(
            (
                entry
                for entry in reported
                if entry["id"] not in used and entry["title"] in names
            ),
            None,
        )
        if hit is not None:
            matched[key] = hit["id"]
            used.add(hit["id"])
            matched_by["title" if hit["title"] == key else "alias"] += 1
            continue
        wanted = {str(value) for value in item.get("key_evidence_ids") or []}
        if wanted:
            candidates = sorted(
                (
                    (len(wanted & entry["evidence"]), entry["id"])
                    for entry in reported
                    if entry["id"] not in used
                ),
                key=lambda pair: (-pair[0], pair[1]),
            )
            if candidates and candidates[0][0] > 0:
                matched[key] = candidates[0][1]
                used.add(candidates[0][1])
                matched_by["evidence"] += 1
                continue
        missing.append(key)
    extra = sorted(entry["title"] for entry in reported if entry["id"] not in used)
    metrics = {
        "annotated": len(annotated),
        "reported": len(reported),
        "matched": len(matched),
        "recall": _ratio_or_none(len(matched), len(annotated)),
        "precision": _ratio_or_none(len(matched), len(reported)),
        "matched_by": matched_by,
    }
    return {
        "metrics": metrics,
        "matches": matched,
        "missing": sorted(missing),
        "extra": extra,
    }


def _match_capabilities(
    annotation: Mapping[str, Any],
    research: Mapping[str, Any],
    topic_matches: Mapping[str, str],
) -> dict[str, Any]:
    """Match annotated capabilities inside the matched topic by title/alias."""
    reported_by_topic: dict[str, list[dict[str, Any]]] = {}
    for item in research.get("capabilities") or []:
        reported_by_topic.setdefault(str(item.get("topic_id")), []).append(
            {"id": str(item.get("id")), "names": _names(item), "record": item}
        )
    annotated = list(annotation.get("capabilities") or [])
    used: set[str] = set()
    pairs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    missing: list[str] = []
    for item in annotated:
        topic_id = topic_matches.get(_norm(item.get("topic_title")))
        names = _names(item)
        hit = None
        if topic_id is not None:
            hit = next(
                (
                    entry
                    for entry in reported_by_topic.get(topic_id, [])
                    if entry["id"] not in used and entry["names"] & names
                ),
                None,
            )
        if hit is None:
            missing.append(_norm(item.get("title")))
            continue
        used.add(hit["id"])
        pairs.append((item, hit["record"]))
    mismatches: list[dict[str, Any]] = []
    correct = 0
    for expected, actual in pairs:
        for field in CAPABILITY_FIELDS:
            if expected.get(field) == actual.get(field):
                correct += 1
            else:
                mismatches.append(
                    {
                        "topic_title": _norm(expected.get("topic_title")),
                        "title": _norm(expected.get("title")),
                        "field": field,
                        "expected": expected.get(field),
                        "actual": actual.get(field),
                    }
                )
    reported_total = sum(len(entries) for entries in reported_by_topic.values())
    dimensions = len(pairs) * len(CAPABILITY_FIELDS)
    metrics = {
        "annotated": len(annotated),
        "reported": reported_total,
        "matched": len(pairs),
        "recall": _ratio_or_none(len(pairs), len(annotated)),
        "precision": _ratio_or_none(len(pairs), reported_total),
        "state_correct": correct,
        "state_dimensions": dimensions,
        "state_accuracy": _ratio_or_none(correct, dimensions),
    }
    return {
        "metrics": metrics,
        "mismatches": mismatches,
        "missing": sorted(missing),
    }


def _unknown_metrics(
    annotation: Mapping[str, Any],
    research: Mapping[str, Any],
    topic_matches: Mapping[str, str],
) -> tuple[dict[str, Any], list[str]]:
    claims = {
        str(item.get("id")): item for item in research.get("claims") or []
    }
    annotated_titles = [
        _norm(item.get("topic_title")) for item in annotation.get("unknowns") or []
    ]
    annotated_ids = {
        topic_matches[title] for title in annotated_titles if title in topic_matches
    }
    reported_ids: set[str] = {
        str(item.get("topic_id")) for item in research.get("unknowns") or []
    }
    complete = 0
    for item in research.get("unknowns") or []:
        claim = claims.get(str(item.get("claim_id"))) or {}
        if (
            claim.get("kind") == "unknown"
            and str(claim.get("reason") or "").strip()
            and claim.get("missing_evidence")
        ):
            complete += 1
    total = len(research.get("unknowns") or [])
    matched = annotated_ids & reported_ids
    metrics = {
        "annotated": len(annotated_titles),
        "reported": total,
        "matched": len(matched),
        "recall": _ratio_or_none(len(matched), len(annotated_titles)),
        "complete": complete,
        "completeness": _ratio_or_none(complete, total),
    }
    missing = [
        title
        for title in annotated_titles
        if topic_matches.get(title) not in reported_ids
    ]
    return metrics, sorted(set(missing))


def _relation_metrics(
    annotation: Mapping[str, Any],
    relations: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[list[str]], list[list[str]]]:
    """Score only the relation types the annotation cares about."""
    annotated = {
        (
            str(item.get("from_id")),
            str(item.get("to_id")),
            str(item.get("type")),
        )
        for item in annotation.get("relations") or []
    }
    scope_types = sorted({item[2] for item in annotated})
    reported_all = {
        (str(item.get("from_id")), str(item.get("to_id")), str(item.get("type")))
        for item in relations
    }
    reported = {item for item in reported_all if item[2] in scope_types}
    matched = annotated & reported
    metrics = {
        "annotated": len(annotated),
        "reported": len(reported),
        "reported_all": len(reported_all),
        "scope_types": scope_types,
        "matched": len(matched),
        "recall": _ratio_or_none(len(matched), len(annotated)),
        "precision": _ratio_or_none(len(matched), len(reported)),
    }
    missing = [list(item) for item in sorted(annotated - reported)]
    extra = [list(item) for item in sorted(reported - annotated)]
    return metrics, missing, extra


def _budget_metrics(manifest: Mapping[str, Any]) -> dict[str, Any]:
    budget = manifest.get("budget") or {}
    used = manifest.get("budget_consumed") or {}
    pairs = (
        ("requests", "requests", "max_requests"),
        ("total_bytes", "total_bytes", "max_total_bytes"),
        ("deep_entities", "deep_entities", "max_deep_entities"),
    )
    metrics: dict[str, Any] = {}
    for name, used_key, limit_key in pairs:
        current = int(used.get(used_key) or 0)
        limit = int(budget.get(limit_key) or 0)
        metrics[name] = {
            "used": current,
            "limit": limit,
            "ratio": _ratio(current, limit) if limit else None,
        }
    runtime_minutes = int(used.get("runtime_seconds") or 0) / 60
    runtime_limit = int(budget.get("max_runtime_minutes") or 0)
    metrics["runtime_minutes"] = {
        "used": round(runtime_minutes, 4),
        "limit": runtime_limit,
        "ratio": round(runtime_minutes / runtime_limit, 4) if runtime_limit else None,
    }
    return metrics


def _coverage_metrics(manifest: Mapping[str, Any]) -> dict[str, Any]:
    channels: list[dict[str, Any]] = []
    degraded = 0
    blocking: list[str] = []
    for entry in manifest.get("coverage") or []:
        channel = str(entry.get("channel") or "")
        statuses = {
            str(entry.get(key) or "")
            for key in ("coverage_status", "discovery_status", "detail_status")
        }
        degraded_statuses = sorted(statuses & DEGRADED)
        if degraded_statuses:
            degraded += 1
        policy = CHANNEL_POLICIES.get(channel)
        if policy is not None and statuses & policy.blocking_statuses:
            blocking.append(channel)
        channels.append(
            {
                "channel": channel,
                "coverage_status": entry.get("coverage_status"),
                "discovery_status": entry.get("discovery_status"),
                "detail_status": entry.get("detail_status"),
                "degraded": degraded_statuses,
                "blocking": channel in blocking,
            }
        )
    return {
        "channels": channels,
        "degraded_channels": degraded,
        "blocking_channels": sorted(set(blocking)),
    }


def _evidence_metrics(
    research: Mapping[str, Any],
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    evidence_ids = {str(item.get("id")) for item in evidence}
    claims = research.get("claims") or []
    factual = [
        item
        for item in claims
        if str(item.get("kind")) in {"observed", "inferred"}
    ]
    supported = 0
    for item in factual:
        identifiers = [str(value) for value in item.get("evidence_ids") or []]
        if identifiers and all(value in evidence_ids for value in identifiers):
            supported += 1
    return {
        "claims_total": len(claims),
        "factual_claims": len(factual),
        "claims_with_evidence": supported,
        "evidence_ratio": _ratio_or_none(supported, len(factual)),
        "evidence_records": len(evidence),
    }


def _structure_metrics(research: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "topics": len(research.get("topics") or []),
        "claims": len(research.get("claims") or []),
        "capabilities": len(research.get("capabilities") or []),
        "unknowns": len(research.get("unknowns") or []),
        "reading_items": len(research.get("reading_list") or []),
        "limitations": len(research.get("limitations") or []),
    }


def _git_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _provenance(
    run: Mapping[str, Any],
) -> dict[str, Any]:
    manifest = run["manifest"]
    validation = run["validation"]
    return {
        "skill_git_revision": _git_revision(),
        "run_id": manifest.get("run_id"),
        "config_hash": manifest.get("config_hash"),
        "research_sha256": run["research_sha256"],
        "ledger_sha256": (validation or {}).get("ledger_sha256"),
        "tool_version": manifest.get("tool_version"),
        "schema_version": manifest.get("schema_version"),
        "model": "unknown",
        "evaluated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def evaluate(run_dir: Path, case_path: Path) -> dict[str, Any]:
    annotation = load_annotation(case_path)
    run = _load_run(run_dir)
    manifest = run["manifest"]
    research = run["research"]
    gate = _gate(run)
    _check_case_matches_run(annotation, manifest)

    topics = _match_topics(annotation, research)
    capabilities = _match_capabilities(annotation, research, topics["matches"])
    unknowns, unknowns_missing = _unknown_metrics(
        annotation,
        research,
        topics["matches"],
    )
    relations, relations_missing, relations_extra = _relation_metrics(
        annotation,
        run["relations"],
    )
    document = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "case_id": annotation.get("case_id"),
        "run_id": manifest.get("run_id"),
        "gate": gate,
        "metrics": {
            "structure": _structure_metrics(research),
            "evidence": _evidence_metrics(research, run["evidence"]),
            "topics": topics["metrics"],
            "capabilities": capabilities["metrics"],
            "unknowns": unknowns,
            "relations": relations,
            "budget": _budget_metrics(manifest),
            "coverage": _coverage_metrics(manifest),
        },
        "mismatches": {
            "topics_missing": topics["missing"],
            "topics_extra": topics["extra"],
            "capabilities": capabilities["mismatches"],
            "capabilities_missing": capabilities["missing"],
            "unknowns_missing": unknowns_missing,
            "relations_missing": relations_missing,
            "relations_extra": relations_extra,
        },
        "annotation": {
            "annotation_version": (annotation.get("source") or {}).get(
                "annotation_version"
            ),
            "annotated_by": (annotation.get("source") or {}).get("annotated_by"),
            "annotated_at": (annotation.get("source") or {}).get("annotated_at"),
        },
        "provenance": _provenance(run),
        "notes": [
            "自动指标不包含关键词、embedding 或 LLM 语义相似度。",
            "主题匹配顺序：标题/别名精确匹配，其次 key_evidence_ids 重叠。",
            "关系 precision 的分母只统计标注涉及的 relation type。",
            "人工评分维度见 evals/rubric.md。",
            *annotation.get("notes", []),
        ],
    }
    return document


def _display(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _metric_line(label: str, values: Mapping[str, Any], keys: Sequence[str]) -> str:
    cells = " | ".join(_display(values.get(key)) for key in keys)
    return f"| {label} | {cells} |"


def render_markdown(document: Mapping[str, Any]) -> str:
    metrics = document.get("metrics") or {}
    structure = metrics.get("structure") or {}
    evidence = metrics.get("evidence") or {}
    topics = metrics.get("topics") or {}
    capabilities = metrics.get("capabilities") or {}
    unknowns = metrics.get("unknowns") or {}
    relations = metrics.get("relations") or {}
    provenance = document.get("provenance") or {}
    lines = [
        f"# Evaluation: {document.get('case_id')}",
        "",
        f"- run: `{document.get('run_id')}`",
        f"- annotation: {document.get('annotation')}",
        f"- gate: {document.get('gate')}",
        "",
        "## 评测范围与限制",
        "",
        *(f"- {note}" for note in document.get("notes", [])),
        "",
        "## 结构",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| topics / claims / capabilities | {_display(structure.get('topics'))} / "
        f"{_display(structure.get('claims'))} / {_display(structure.get('capabilities'))} |",
        f"| unknowns / reading items | {_display(structure.get('unknowns'))} / "
        f"{_display(structure.get('reading_items'))} |",
        f"| factual claims with evidence | {_display(evidence.get('claims_with_evidence'))} / "
        f"{_display(evidence.get('factual_claims'))} "
        f"({_display(evidence.get('evidence_ratio'))}) |",
        "",
        "## 标注匹配",
        "",
        "| 维度 | annotated | reported | matched | recall | precision |",
        "| --- | --- | --- | --- | --- | --- |",
        _metric_line(
            "topics",
            topics,
            ("annotated", "reported", "matched", "recall", "precision"),
        ),
        _metric_line(
            "capabilities",
            capabilities,
            ("annotated", "reported", "matched", "recall", "precision"),
        ),
        _metric_line(
            "relations",
            relations,
            ("annotated", "reported", "matched", "recall", "precision"),
        ),
        f"| capability state accuracy | {_display(capabilities.get('state_correct'))} / "
        f"{_display(capabilities.get('state_dimensions'))} "
        f"({_display(capabilities.get('state_accuracy'))}) | | | | |",
        f"| unknown completeness | {_display(unknowns.get('complete'))} / "
        f"{_display(unknowns.get('reported'))} "
        f"({_display(unknowns.get('completeness'))}) | | | | |",
        f"| topic matches by title/alias/evidence | "
        f"{_display(topics.get('matched_by'))} | | | | |",
        f"| relation scope | {_display(relations.get('scope_types'))} "
        f"(reported {_display(relations.get('reported'))}/"
        f"{_display(relations.get('reported_all'))}) "
        f"| | | | |",
        "",
        "## 预算与覆盖",
        "",
        f"- budget: {metrics.get('budget')}",
        f"- coverage: {metrics.get('coverage')}",
        "",
        "## Mismatches",
        "",
    ]
    mismatches = document.get("mismatches") or {}
    for key, values in mismatches.items():
        lines.append(f"- {key}: {values}")
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            f"```json\n{json.dumps(provenance, ensure_ascii=False, indent=2)}\n```",
            "",
        ]
    )
    return "\n".join(lines)


def _safe_name(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "case")).strip("-")
    return cleaned or "case"


def write_outputs(document: Mapping[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"{_safe_name(document.get('case_id'))}-{_safe_name(document.get('run_id'))}"
    json_path = out_dir / f"{base}.json"
    markdown_path = out_dir / f"{base}.md"
    json_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(document), encoding="utf-8")
    return json_path, markdown_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="run 目录")
    parser.add_argument(
        "--case",
        required=True,
        help="含 annotation.json 的 case 目录或文件",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="结果输出目录")
    args = parser.parse_args(argv)
    try:
        document = evaluate(Path(args.run), Path(args.case))
        json_path, markdown_path = write_outputs(document, Path(args.out))
    except EvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"evaluation: {json_path}")
    print(f"summary: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
