from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from rer.models import validate_document, verify_evidence_quote
from rer.store import SnapshotStore
from rer.validation import ledger_digest

ROOT = Path(__file__).resolve().parents[1]


def _load_evaluate() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "eval_evaluate",
        ROOT / "evals/evaluate.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evaluate_module = _load_evaluate()

REAL_CASE = ROOT / "evals/cases/vllm-20261104-kv-correctness"


def test_real_case_sources_and_annotation_are_auditable() -> None:
    annotation = evaluate_module.load_annotation(REAL_CASE)
    sources = json.loads((REAL_CASE / "sources.json").read_text())
    baseline = json.loads((REAL_CASE / "baseline-research.json").read_text())
    validate_document(baseline, "research")
    assert sources["snapshot"] == annotation["source"]["snapshot"] == baseline["run_id"]
    assert annotation["source"]["annotated_at"] < baseline["created_at"]
    for source in sources["raw_documents"]:
        assert hashlib.sha256(source["body"].encode()).hexdigest() == source["body_sha256"]
    for evidence in sources["evidence"]:
        payload = sources["blobs"][evidence["blob_sha256"]].encode()
        assert hashlib.sha256(payload).hexdigest() == evidence["blob_sha256"]
        assert verify_evidence_quote(evidence, payload)[0]
    evidence_ids = {e["id"] for e in sources["evidence"]}
    for item in annotation["topics"] + annotation["capabilities"]:
        assert set(item["key_evidence_ids"]) <= evidence_ids
    states = {d["metadata"]["number"]: d["metadata"] for d in sources["raw_documents"]}
    assert states[52149]["state"] == "closed" and states[52149]["merged"] is False
    assert states[52789]["merged"] is True
    assert states[53073]["state"] == states[53298]["state"] == "open"


def test_real_case_detects_omissions_and_closed_pr_promotions() -> None:
    annotation = evaluate_module.load_annotation(REAL_CASE)
    baseline = json.loads((REAL_CASE / "baseline-research.json").read_text())
    topics = evaluate_module._match_topics(annotation, baseline)
    result = evaluate_module._match_capabilities(annotation, baseline, topics["matches"])
    assert result["metrics"]["state_correct"] == 16
    baseline["capabilities"][0]["implementation_state"] = "merged"
    result = evaluate_module._match_capabilities(annotation, baseline, topics["matches"])
    assert result["metrics"]["state_correct"] == 15
    assert result["mismatches"][0]["expected"] == "unknown"
    baseline["topics"].pop()
    topics = evaluate_module._match_topics(annotation, baseline)
    assert topics["metrics"]["matched"] == 2
    assert len(topics["missing"]) == 1


def test_evaluation_preserves_annotation_scope_in_json_and_markdown(tmp_path) -> None:
    annotation = _annotation()
    annotation["notes"] = ["Limited source review; not a whole-repository recall score."]
    run_dir, case_dir = _prepare(tmp_path, annotation)
    result = evaluate_module.evaluate(run_dir, case_dir)
    assert annotation["notes"][0] in result["notes"]
    assert annotation["notes"][0] in evaluate_module.render_markdown(result)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


def _seed_run(run_dir: Path) -> None:
    run_id = "20261101T080000Z-abcdef12"
    _write_json(
        run_dir / "manifest.json",
        {
            "schema_version": "1.3",
            "run_id": run_id,
            "tool_version": "0.2.7",
            "config_hash": "a" * 64,
            "repository": {
                "slug": "acme/demo",
                "canonical_url": "https://github.com/acme/demo",
            },
            "activity_window": {
                "start": "2026-08-01T00:00:00Z",
                "end": "2026-11-01T00:00:00Z",
                "timezone": "UTC",
            },
            "default_branch_sha": "b" * 40,
            "budget": {
                "max_requests": 100,
                "max_total_bytes": 1000,
                "max_deep_entities": 10,
                "max_runtime_minutes": 10,
            },
            "budget_consumed": {
                "requests": 50,
                "total_bytes": 500,
                "deep_entities": 5,
                "runtime_seconds": 300,
            },
            "coverage": [
                {
                    "channel": "activity_prs",
                    "coverage_status": "complete",
                    "discovery_status": "complete",
                    "detail_status": "not_requested",
                },
                {
                    "channel": "pr_detail",
                    "coverage_status": "partial",
                    "discovery_status": "partial",
                    "detail_status": "partial",
                },
            ],
        },
    )
    _write_json(
        run_dir / "analysis/research.json",
        {
            "schema_version": "1.3",
            "run_id": run_id,
            "created_at": "2026-11-01T09:00:00Z",
            "topics": [
                {
                    "id": "topic-001",
                    "title": "Cache lifecycle",
                    "research_question": "How does cleanup work?",
                    "summary": "Cleanup was discussed and partially implemented.",
                    "claim_ids": {"problem": ["claim-001"]},
                    "capability_ids": ["cap-001"],
                    "reading_item_ids": [],
                    "gaps": [],
                }
            ],
            "claims": [
                {
                    "id": "claim-001",
                    "text": "The RFC proposes cleanup.",
                    "kind": "observed",
                    "evidence_ids": ["ev-001"],
                    "contradicting_evidence_ids": [],
                    "confidence": "high",
                    "confidence_reason": "Direct quote.",
                    "scope": {
                        "entity_ids": ["issue-1"],
                        "capability_id": "cap-001",
                        "revision": None,
                        "version": None,
                    },
                    "reason": None,
                    "missing_evidence": [],
                    "state_time_basis": "observed_current",
                },
                {
                    "id": "claim-002",
                    "text": "The implementation likely covers eviction.",
                    "kind": "inferred",
                    "evidence_ids": ["ev-missing"],
                    "contradicting_evidence_ids": [],
                    "confidence": "low",
                    "confidence_reason": "Indirect.",
                    "scope": {
                        "entity_ids": ["pr-1"],
                        "capability_id": "cap-001",
                        "revision": None,
                        "version": None,
                    },
                    "reason": None,
                    "missing_evidence": [],
                    "state_time_basis": "observed_current",
                },
                {
                    "id": "claim-u",
                    "text": "Release coverage is unknown.",
                    "kind": "unknown",
                    "evidence_ids": [],
                    "contradicting_evidence_ids": [],
                    "confidence": "low",
                    "confidence_reason": "No release evidence.",
                    "scope": {
                        "entity_ids": [],
                        "capability_id": "cap-001",
                        "revision": None,
                        "version": None,
                    },
                    "reason": "No release document was collected.",
                    "missing_evidence": ["release document"],
                    "state_time_basis": "observed_current",
                },
            ],
            "capabilities": [
                {
                    "id": "cap-001",
                    "topic_id": "topic-001",
                    "title": "Cleanup API",
                    "scope": "Cleanup",
                    "proposal_state": "proposed",
                    "implementation_state": "merged",
                    "delivery_state": "unknown",
                    "availability": "unknown",
                    "target_branch": "main",
                    "observed_revision": "b" * 40,
                    "verified_versions": [],
                    "requirements": {"done": [], "remaining": []},
                    "evidence_ids": ["ev-001"],
                }
            ],
            "unknowns": [{"id": "unknown-001", "topic_id": "topic-001", "claim_id": "claim-u"}],
            "reading_list": [],
            "limitations": ["budget: only part of the window was collected"],
        },
    )
    _write_jsonl(run_dir / "normalized/evidence.jsonl", [{"id": "ev-001"}])
    _write_jsonl(
        run_dir / "normalized/relations.jsonl",
        [
            {"id": "rel-1", "from_id": "pr-1", "to_id": "issue-1", "type": "implements_part_of"},
            {"id": "rel-2", "from_id": "pr-1", "to_id": "issue-1", "type": "mentions"},
        ],
    )
    research_sha = hashlib.sha256(
        (run_dir / "analysis/research.json").read_bytes()
    ).hexdigest()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    ledger_sha = ledger_digest(SnapshotStore(run_dir), manifest)
    _write_json(
        run_dir / "analysis/validation.json",
        {
            "schema_version": "1.3",
            "run_id": run_id,
            "ok": True,
            "research_sha256": research_sha,
            "ledger_sha256": ledger_sha,
        },
    )


def _annotation() -> dict:
    return {
        "schema_version": "1.0",
        "case_id": "acme-demo",
        "repository": "acme/demo",
        "window": {
            "start": "2026-08-01T00:00:00Z",
            "end": "2026-11-01T00:00:00Z",
            "timezone": "UTC",
        },
        "source": {
            "default_branch_sha": "b" * 40,
            "snapshot": "fixture",
            "annotation_version": "1.0.0",
            "annotated_by": "reviewer",
            "annotated_at": "2026-11-02T00:00:00Z",
        },
        "topics": [
            {
                "id": "t1",
                "title": "Cache lifecycle",
                "research_question": "How does cleanup work?",
                "why_it_matters": "It affects cache correctness.",
                "key_evidence_ids": ["ev-001"],
            },
            {
                "id": "t2",
                "title": "Scheduling",
                "research_question": "How does scheduling work?",
                "why_it_matters": "It affects throughput.",
                "key_evidence_ids": [],
            },
        ],
        "capabilities": [
            {
                "id": "c1",
                "topic_title": "Cache lifecycle",
                "title": "Cleanup API",
                "proposal_state": "proposed",
                "implementation_state": "merged",
                "delivery_state": "released",
                "availability": "unknown",
            },
            {
                "id": "c2",
                "topic_title": "Scheduling",
                "title": "Preemption",
                "proposal_state": "unknown",
                "implementation_state": "unknown",
                "delivery_state": "unknown",
                "availability": "unknown",
            },
        ],
        "unknowns": [
            {
                "topic_title": "Cache lifecycle",
                "reason": "No release evidence.",
                "missing_evidence": ["release document"],
            }
        ],
        "relations": [
            {"from_id": "pr-1", "to_id": "issue-1", "type": "implements_part_of"},
            {"from_id": "pr-1", "to_id": "issue-1", "type": "verified_in_release"},
        ],
        "notes": [],
    }


def test_metrics_arithmetic_and_outputs(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    case_dir = tmp_path / "case"
    out_dir = tmp_path / "out"
    _seed_run(run_dir)
    _write_json(case_dir / "annotation.json", _annotation())

    document = evaluate_module.evaluate(run_dir, case_dir)

    assert document["metrics"]["topics"] == {
        "annotated": 2,
        "reported": 1,
        "matched": 1,
        "recall": 0.5,
        "precision": 1.0,
        "matched_by": {"title": 1, "alias": 0, "evidence": 0},
    }
    capabilities = document["metrics"]["capabilities"]
    assert capabilities["matched"] == 1
    assert capabilities["state_correct"] == 3
    assert capabilities["state_accuracy"] == 0.75
    assert document["metrics"]["evidence"]["evidence_ratio"] == 0.5
    relations = document["metrics"]["relations"]
    assert relations["reported"] == 1
    assert relations["reported_all"] == 2
    assert relations["recall"] == 0.5
    assert relations["precision"] == 1.0
    assert document["gate"] == {
        "validation_present": True,
        "ok": True,
        "research_sha_matches": True,
        "ledger_sha_matches": True,
        "run_id_matches": True,
    }
    assert document["metrics"]["coverage"]["blocking_channels"] == []
    assert document["metrics"]["budget"]["requests"]["ratio"] == 0.5

    json_path, markdown_path = evaluate_module.write_outputs(document, out_dir)
    assert json_path.is_file()
    assert markdown_path.is_file()
    assert "Evaluation: acme-demo" in markdown_path.read_text(encoding="utf-8")


def test_missing_annotation_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(evaluate_module.EvaluationError):
        evaluate_module.load_annotation(tmp_path / "missing")


def test_provenance_fields_are_recorded(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    case_dir = tmp_path / "case"
    _seed_run(run_dir)
    _write_json(case_dir / "annotation.json", _annotation())

    provenance = evaluate_module.evaluate(run_dir, case_dir)["provenance"]

    expected_sha = hashlib.sha256(
        (run_dir / "analysis/research.json").read_bytes()
    ).hexdigest()
    assert provenance["config_hash"] == "a" * 64
    assert provenance["research_sha256"] == expected_sha
    assert provenance["ledger_sha256"]
    assert provenance["tool_version"] == "0.2.7"
    assert provenance["model"] == "unknown"
    assert provenance["skill_git_revision"]


def test_semantic_similarity_field_is_rejected(tmp_path: Path) -> None:
    annotation = _annotation()
    annotation["semantic_similarity"] = 0.92
    path = tmp_path / "annotation.json"
    _write_json(path, annotation)

    with pytest.raises(evaluate_module.EvaluationError):
        evaluate_module.load_annotation(path)


def _prepare(tmp_path: Path, annotation: dict) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    case_dir = tmp_path / "case"
    _seed_run(run_dir)
    _write_json(case_dir / "annotation.json", annotation)
    return run_dir, case_dir


def test_topic_alias_matches_without_same_title(tmp_path: Path) -> None:
    annotation = _annotation()
    annotation["topics"] = [
        {
            "id": "t1",
            "title": "缓存生命周期",
            "title_aliases": ["Cache lifecycle"],
            "research_question": "How does cleanup work?",
            "why_it_matters": "It affects cache correctness.",
            "key_evidence_ids": [],
        }
    ]
    annotation["capabilities"] = [
        {
            "id": "c1",
            "topic_title": "缓存生命周期",
            "title": "清理接口",
            "title_aliases": ["Cleanup API"],
            "proposal_state": "proposed",
            "implementation_state": "merged",
            "delivery_state": "unknown",
            "availability": "unknown",
        }
    ]
    annotation["unknowns"] = [
        {
            "topic_title": "缓存生命周期",
            "reason": "No release evidence.",
            "missing_evidence": ["release document"],
        }
    ]
    annotation["relations"] = []
    run_dir, case_dir = _prepare(tmp_path, annotation)

    document = evaluate_module.evaluate(run_dir, case_dir)

    assert document["metrics"]["topics"]["matched"] == 1
    assert document["metrics"]["topics"]["matched_by"] == {
        "title": 0,
        "alias": 1,
        "evidence": 0,
    }
    assert document["metrics"]["capabilities"]["matched"] == 1
    assert document["metrics"]["capabilities"]["state_accuracy"] == 1.0
    assert document["metrics"]["unknowns"]["matched"] == 1
    assert document["mismatches"]["unknowns_missing"] == []


def test_topic_evidence_overlap_matches_independent_title(tmp_path: Path) -> None:
    annotation = _annotation()
    annotation["topics"] = [
        {
            "id": "t1",
            "title": "缓存清理与淘汰",
            "research_question": "How does cleanup work?",
            "why_it_matters": "It affects cache correctness.",
            "key_evidence_ids": ["ev-001"],
        }
    ]
    annotation["capabilities"] = [
        {
            "id": "c1",
            "topic_title": "缓存清理与淘汰",
            "title": "Cleanup API",
            "proposal_state": "proposed",
            "implementation_state": "merged",
            "delivery_state": "unknown",
            "availability": "unknown",
        }
    ]
    annotation["unknowns"] = []
    annotation["relations"] = []
    run_dir, case_dir = _prepare(tmp_path, annotation)

    document = evaluate_module.evaluate(run_dir, case_dir)

    assert document["metrics"]["topics"]["matched_by"]["evidence"] == 1
    assert document["metrics"]["topics"]["recall"] == 1.0


def test_relations_outside_annotated_types_do_not_hurt_precision(
    tmp_path: Path,
) -> None:
    annotation = _annotation()
    annotation["relations"] = [
        {"from_id": "pr-1", "to_id": "issue-1", "type": "implements_part_of"}
    ]
    run_dir, case_dir = _prepare(tmp_path, annotation)

    metrics = evaluate_module.evaluate(run_dir, case_dir)["metrics"]["relations"]

    assert metrics["annotated"] == 1
    assert metrics["reported"] == 1
    assert metrics["reported_all"] == 2
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0


def test_empty_annotation_relations_report_no_score(tmp_path: Path) -> None:
    annotation = _annotation()
    annotation["relations"] = []
    run_dir, case_dir = _prepare(tmp_path, annotation)

    metrics = evaluate_module.evaluate(run_dir, case_dir)["metrics"]["relations"]

    assert metrics["recall"] is None
    assert metrics["precision"] is None


def test_stale_research_is_rejected(tmp_path: Path) -> None:
    run_dir, case_dir = _prepare(tmp_path, _annotation())
    research_path = run_dir / "analysis/research.json"
    research = json.loads(research_path.read_text(encoding="utf-8"))
    research["limitations"] = ["changed after validation"]
    research_path.write_text(json.dumps(research, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(evaluate_module.EvaluationError) as excinfo:
        evaluate_module.evaluate(run_dir, case_dir)

    assert "research.json 在 validate 之后被修改" in str(excinfo.value)


def test_missing_validation_is_rejected(tmp_path: Path) -> None:
    run_dir, case_dir = _prepare(tmp_path, _annotation())
    (run_dir / "analysis/validation.json").unlink()

    with pytest.raises(evaluate_module.EvaluationError) as excinfo:
        evaluate_module.evaluate(run_dir, case_dir)

    assert "缺少 analysis/validation.json" in str(excinfo.value)


def test_failed_validation_is_rejected(tmp_path: Path) -> None:
    run_dir, case_dir = _prepare(tmp_path, _annotation())
    path = run_dir / "analysis/validation.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["ok"] = False
    _write_json(path, document)

    with pytest.raises(evaluate_module.EvaluationError) as excinfo:
        evaluate_module.evaluate(run_dir, case_dir)

    assert "validation.ok=false" in str(excinfo.value)


def test_undefined_metrics_are_null_not_perfect() -> None:
    annotation = _annotation()
    research = {"topics": [], "capabilities": [], "claims": [], "unknowns": []}

    topics = evaluate_module._match_topics(annotation, research)["metrics"]
    capabilities = evaluate_module._match_capabilities(annotation, research, {})["metrics"]
    unknowns, _missing = evaluate_module._unknown_metrics(annotation, research, {})
    evidence = evaluate_module._evidence_metrics(research, [])

    assert topics["precision"] is None
    assert capabilities["precision"] is None
    assert capabilities["state_accuracy"] is None
    assert unknowns["completeness"] is None
    assert evidence["evidence_ratio"] is None

    empty_unknowns, _missing = evaluate_module._unknown_metrics(
        {"unknowns": []},
        research,
        {},
    )
    assert empty_unknowns["recall"] is None


def test_gate_rejects_ledger_changes_after_validation(tmp_path: Path) -> None:
    run_dir, case_dir = _prepare(tmp_path, _annotation())
    evidence_path = run_dir / "normalized/evidence.jsonl"
    evidence_path.write_text(
        evidence_path.read_text(encoding="utf-8")
        + json.dumps({"id": "ev-changed"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(evaluate_module.EvaluationError) as excinfo:
        evaluate_module.evaluate(run_dir, case_dir)

    assert "派生输入" in str(excinfo.value)


def test_repository_must_match_exactly(tmp_path: Path) -> None:
    annotation = _annotation()
    annotation["repository"] = "https://evil.example/acme/demo"

    run_dir, case_dir = _prepare(tmp_path, annotation)

    with pytest.raises(evaluate_module.EvaluationError) as excinfo:
        evaluate_module.evaluate(run_dir, case_dir)

    assert "annotation.repository" in str(excinfo.value)


def test_markdown_renders_undefined_metrics_as_na() -> None:
    document = {
        "case_id": "case",
        "run_id": "run",
        "gate": {},
        "metrics": {
            "structure": {},
            "evidence": {"evidence_ratio": None},
            "topics": {"precision": None},
            "capabilities": {"state_accuracy": None},
            "unknowns": {"completeness": None},
            "relations": {},
            "budget": {},
            "coverage": {},
        },
        "mismatches": {},
        "annotation": {},
        "provenance": {},
    }

    markdown = evaluate_module.render_markdown(document)

    assert "n/a" in markdown
    assert "None" not in markdown
