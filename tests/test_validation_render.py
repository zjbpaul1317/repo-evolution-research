from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rer import SCHEMA_VERSION, cli, validation
from rer import render as render_module
from rer.collectors.pr_store import reconcile_entity_projection, write_pr_document
from rer.collectors.status import derive_limitations
from rer.config import build_config
from rer.errors import SchemaError, UsageError, ValidationFailure
from rer.models import (
    build_manifest,
    make_capability,
    make_claim,
    make_entity,
    make_evidence,
    make_locator,
    make_topic,
    validate_document,
)
from rer.store import SnapshotStore, create_run

NOW = "2026-11-01T08:00:00Z"
NOW_DT = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
REVISION = "b" * 40
RUN_ID = "20261101T080000Z-abcdef12"
ISSUE_ID = "github.com/acme/demo/issue/40"
PR_ID = "github.com/acme/demo/pull/101"


def _pr_detail_coverage() -> dict:
    return {
        "channel": "pr_detail",
        "query_scope": "deep_read_selection",
        "query": "window",
        "expected_count": 1,
        "fetched_count": 1,
        "pages": 1,
        "exhausted": True,
        "truncation_reason": None,
        "remaining_cursor": None,
        "observed_start": NOW,
        "observed_end": NOW,
        "coverage_status": "complete",
        "discovery_status": "complete",
        "detail_status": "complete",
    }


def _write_pr_selection(store: SnapshotStore, manifest: dict) -> None:
    window = manifest["activity_window"]
    store.write_json(
        "analysis/pr-selection.json",
        {
            "schema_version": SCHEMA_VERSION,
            "observed_at": NOW,
            "repository": {"host": "github.com", "owner": "acme", "name": "demo"},
            "activity_window": {
                "start": window["start"],
                "end": window["end"],
                "timezone": window["timezone"],
            },
            "quota": {
                "pr_quota": 1,
                "merged_quota": 1,
                "open_quota": 0,
                "max_deep_entities": 8,
                "deep_entities_used": 0,
            },
            "candidates": [
                {
                    "number": 101,
                    "entity_id": PR_ID,
                    "bucket": "merged",
                    "score": 0,
                    "score_components": {},
                    "selection_reason": ["merged_in_window"],
                    "selected": True,
                    "skipped_reason": None,
                    "status": "selected",
                }
            ],
            "counts": {
                "candidates": 1,
                "selected": 1,
                "merged_selected": 1,
                "open_selected": 0,
                "skipped_quota": 0,
                "skipped_unchanged": 0,
                "failed": 0,
                "not_attempted": 0,
            },
            "limitations": [],
        },
        schema_name="pr_selection",
    )


def _write_pr_detail(store: SnapshotStore) -> None:
    section = {
        "status": "complete",
        "fetched": 0,
        "total_known": 0,
        "truncated": False,
        "truncation_reason": None,
        "items": [],
    }
    document = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": NOW,
        "repository": {"host": "github.com", "owner": "acme", "name": "demo"},
        "number": 101,
        "url": "https://github.com/acme/demo/pull/101",
        "title": "Cache planner",
        "author": "alice",
        "state": "closed",
        "draft": False,
        "merged": True,
        "merged_at": "2026-10-20T10:00:00Z",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": NOW,
        "closed_at": "2026-10-20T10:00:00Z",
        "base_sha": "e" * 40,
        "head_sha": "c" * 40,
        "head_sha_end": "c" * 40,
        "head_changed": False,
        "merge_commit_sha": None,
        "comparator": "github_pr_files",
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
        "commits_count": 0,
        "comments_count": 0,
        "review_comments_count": 0,
        "selection_reason": [],
        "body": {
            "preview": "",
            "sha256": None,
            "bytes": 0,
            "preview_truncated": False,
        },
        "files": dict(section),
        "commits": dict(section),
        "issue_comments": dict(section),
        "reviews": dict(section),
        "review_comments": dict(section),
        "diff": {
            "status": "not_requested",
            "sha256": None,
            "bytes": 0,
            "truncated": False,
        },
        "coverage": {
            "detail": "complete",
            "files": "complete",
            "commits": "complete",
            "issue_comments": "complete",
            "reviews": "complete",
            "review_comments": "complete",
        },
        "limitations": [],
    }
    document["limitations"] = derive_limitations(document)
    write_pr_document(store, document)


def seed_run(
    tmp_path: Path,
    *,
    collect_status: str = "partial",
    coverage: list[dict] | None = None,
) -> SnapshotStore:
    config = build_config(
        repository="acme/demo",
        mode="standard",
        timezone_name="UTC",
        now=NOW_DT,
        budget_overrides={"max_deep_entities": 8},
    )
    store = create_run(tmp_path, config["repository"], RUN_ID)
    manifest = build_manifest(config, RUN_ID, NOW)
    manifest["collection_started_at"] = NOW
    manifest["collection_finished_at"] = NOW
    manifest["observed"] = {"started_at": NOW, "finished_at": NOW}
    manifest["default_branch"] = "main"
    manifest["default_branch_sha"] = REVISION
    manifest["stages"]["collect"] = collect_status
    manifest["stages"]["prepare"] = "complete"
    manifest["state"] = "prepared"
    manifest["coverage"] = list(coverage or []) + [_pr_detail_coverage()]
    store.write_manifest(manifest)
    store.write_json("config.resolved.json", config, schema_name="config")

    issues = _evidence(
        store,
        "ev-issue-40",
        ISSUE_ID,
        "RFC: add an explicit cache cleanup API.",
        "issue_body",
        "https://github.com/acme/demo/issues/40",
    )
    pull = _evidence(
        store,
        "ev-pr-101",
        PR_ID,
        "PR #101 implements the cleanup API skeleton.",
        "pull_request_body",
        "https://github.com/acme/demo/pull/101",
    )
    store.write_jsonl(
        "normalized/evidence.jsonl",
        [issues, pull],
        key="id",
        schema_name="evidence",
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        [
            make_entity(
                entity_id=ISSUE_ID,
                kind="issue",
                title="RFC: cache lifecycle",
                url="https://github.com/acme/demo/issues/40",
                observed_at=NOW,
                number=40,
                roles=["rfc", "design"],
                raw_state={
                    "state": "open",
                    "state_reason": None,
                    "labels": ["rfc"],
                    "comments": 0,
                },
                normalized_state={"role_hints": ["rfc", "design"]},
            ),
            make_entity(
                entity_id=PR_ID,
                kind="pull_request",
                title="Cache planner",
                url="https://github.com/acme/demo/pull/101",
                observed_at=NOW,
                number=101,
                roles=["implementation"],
                raw_state={
                    "state": "closed",
                    "merged": True,
                    "merged_at": "2026-10-20T10:00:00Z",
                    "base": {"ref": "main"},
                    "head": {"sha": "c" * 40},
                },
                normalized_state={"merged": True, "merged_at": "2026-10-20T10:00:00Z"},
            ),
        ],
        key="id",
        schema_name="entity",
    )
    _write_pr_detail(store)
    _write_pr_selection(store, manifest)
    reconcile_entity_projection(store)
    store.write_jsonl("normalized/relations.jsonl", [], key="id", schema_name="relation")
    store.write_json("analysis/research.json", research_document())
    return store


def test_validate_refuses_empty_evidence_even_for_unknown_only_research(tmp_path) -> None:
    store = seed_run(tmp_path)
    research = research_document()
    research.update(topics=[], claims=[], capabilities=[], unknowns=[], reading_list=[])
    store.write_json("analysis/research.json", research)
    store.write_jsonl("normalized/evidence.jsonl", [], key="id", schema_name="evidence")

    assert cli.main(["validate", "--run", str(store.run_dir)]) == 5
    result = store.read_json("analysis/validation.json")
    assert any(item["code"] == "no_research_evidence" for item in result["errors"])
    assert not store.secure_path("report.md").exists()


def _evidence(
    store: SnapshotStore,
    evidence_id: str,
    entity_id: str,
    text: str,
    source_kind: str,
    source_url: str,
) -> dict:
    payload = text.encode("utf-8")
    return make_evidence(
        evidence_id=evidence_id,
        entity_id=entity_id,
        source_url=source_url,
        source_kind=source_kind,
        observed_at=NOW,
        text=text,
        locator=make_locator(
            "char_range",
            char_start=0,
            char_end=len(text),
            line_start=1,
            line_end=1,
        ),
        blob_sha256=store.save_blob(payload),
        verification="verified",
    )


def claim(identifier: str, text: str, evidence: str, entity: str, capability: str) -> dict:
    return make_claim(
        claim_id=identifier,
        text=text,
        kind="observed",
        evidence_ids=[evidence],
        confidence="high",
        confidence_reason="直接来自对象正文。",
        scope={
            "entity_ids": [entity],
            "capability_id": capability,
            "revision": None,
            "version": None,
        },
    )


def research_document(**overrides) -> dict:
    claims = [
        claim("claim-001", "RFC 提出增加显式资源清理接口。", "ev-issue-40", ISSUE_ID, "cap-001"),
        claim("claim-002", "PR #101 合并了清理接口的第一部分。", "ev-pr-101", PR_ID, "cap-001"),
    ]
    capability = make_capability(
        capability_id="cap-001",
        topic_id="topic-001",
        title="显式缓存清理接口",
        scope="清理 API 骨架",
        proposal_state="proposed",
        implementation_state="merged",
        delivery_state="unknown",
        availability="unknown",
        target_branch="main",
        observed_revision=REVISION,
        requirements_done=["清理 API 骨架"],
        evidence_ids=["ev-issue-40", "ev-pr-101"],
    )
    topic = make_topic(
        topic_id="topic-001",
        title="缓存生命周期管理",
        research_question="项目为何以及如何引入显式缓存清理接口？",
        summary="提案已经出现，第一个实现 PR 已合并，但尚未核验发布。",
        problem_claim_ids=["claim-001"],
        implementation_claim_ids=["claim-002"],
        capability_ids=["cap-001"],
        reading_item_ids=["read-001", "read-002"],
    )
    reading = [
        {
            "id": "read-001",
            "priority": "P0",
            "kind": "rfc",
            "title": "RFC: explicit cache lifecycle",
            "url": "https://github.com/acme/demo/issues/40",
            "entity_id": ISSUE_ID,
            "why": "理解为什么需要显式清理接口。",
            "order": 0,
        },
        {
            "id": "read-002",
            "priority": "P0",
            "kind": "pr",
            "title": "Add cleanup API skeleton",
            "url": "https://github.com/acme/demo/pull/101",
            "entity_id": PR_ID,
            "why": "确认第一个实现覆盖的范围。",
            "order": 1,
        },
    ]
    document = {
        "schema_version": "1.3",
        "run_id": RUN_ID,
        "created_at": "2026-11-01T09:00:00Z",
        "topics": [topic],
        "claims": claims,
        "capabilities": [capability],
        "unknowns": [],
        "reading_list": reading,
        "limitations": ["预算：collect 只完成了部分通道，覆盖不完整。"],
    }
    document.update(overrides)
    return document


def validate(store: SnapshotStore) -> tuple[dict, list[str]]:
    args = cli.build_parser().parse_args(["validate", "--run", str(store.run_dir)])
    return cli.command_validate(args)


def render(store: SnapshotStore) -> tuple[dict, list[str]]:
    args = cli.build_parser().parse_args(["render", "--run", str(store.run_dir)])
    return cli.command_render(args)


def read_json(store: SnapshotStore, relpath: str) -> dict:
    return json.loads(store.secure_path(relpath).read_text(encoding="utf-8"))


def test_validate_and_render_happy_path(tmp_path: Path) -> None:
    store = seed_run(tmp_path)

    result, _lines = validate(store)

    assert result["result"]["ok"] is True
    validation = read_json(store, "analysis/validation.json")
    validate_document(validation, "validation")
    assert validation["ok"] is True
    assert validation["counts"]["capabilities"] == 1
    assert validation["lifecycle"][0]["derived"]["implementation_state"] == "merged"
    assert read_json(store, "manifest.json")["stages"]["validate"] == "complete"
    assert read_json(store, "manifest.json")["state"] == "validated"

    result, _lines = render(store)

    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert result["result"]["sha256"]
    assert "缓存生命周期管理" in report
    assert "功能进展表" in report
    assert "已合并（`merged`）" in report
    assert "## 注释" in report
    assert "[^C001]" in report
    assert "### 校验警告（" in report
    assert "lifecycle 缺口" in report
    assert "diff_not_requested" in report
    assert "（Issue 正文，Issue #40，L1" in report
    assert "“RFC: add an explicit cache cleanup API.”" in report
    manifest = read_json(store, "manifest.json")
    assert manifest["stages"]["render"] == "complete"
    assert manifest["state"] == "rendered"
    record = read_json(store, "analysis/render-report.json")
    validate_document(record, "render_report")
    assert record["template_version"] == "2"
    assert record["report_sha256"] == result["result"]["sha256"]

    validate(store)
    render(store)
    assert store.secure_path("report.md").read_text(encoding="utf-8") == report


def _append_evidence(store: SnapshotStore, record: dict) -> None:
    existing = [
        json.loads(line)
        for line in store.secure_path("normalized/evidence.jsonl").read_text().splitlines()
        if line.strip()
    ]
    store.write_jsonl(
        "normalized/evidence.jsonl",
        [*existing, record],
        key="id",
        schema_name="evidence",
    )


def test_review_flags_unverified_evidence(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    unverified = _evidence(
        store,
        "ev-unverified",
        ISSUE_ID,
        "Substantive claim text.",
        "issue_body",
        "https://github.com/acme/demo/issues/40",
    )
    unverified["verification"] = "unverifiable"
    _append_evidence(store, unverified)
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-unverified"]
    store.write_json("analysis/research.json", document)

    validate(store)

    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    assert "review: needs_attention" in review
    assert "unverified_evidence" in review


def test_review_flags_capability_without_evidence(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["capabilities"][0]["evidence_ids"] = []
    store.write_json("analysis/research.json", document)

    validate(store)

    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    capability_section = review.split("### `cap-001`", 1)[1]
    assert "review: needs_attention" in capability_section
    assert "no_evidence" in capability_section


def test_long_evidence_requires_claim_quote(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    long_text = "supporting sentence " * 30
    _append_evidence(
        store,
        _evidence(
            store,
            "ev-long-anchor",
            ISSUE_ID,
            long_text,
            "issue_body",
            "https://github.com/acme/demo/issues/40",
        ),
    )
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-long-anchor"]
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    assert "claim_quote_missing" in {item["code"] for item in validation["errors"]}
    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    assert "anchor_missing" in review


def test_claim_quote_must_match_evidence_text(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["evidence_quotes"] = {
        "ev-issue-40": "not in the evidence"
    }
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    assert "claim_quote_not_found" in {item["code"] for item in validation["errors"]}
    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    assert "review: needs_attention" in review
    assert "anchor_not_found" in review


def test_evidence_review_escapes_html(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    _append_evidence(
        store,
        _evidence(
            store,
            "ev-html",
            ISSUE_ID,
            "<script>alert(1)</script> substantive body",
            "issue_body",
            "https://github.com/acme/demo/issues/40",
        ),
    )
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-html"]
    store.write_json("analysis/research.json", document)

    validate(store)

    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in review
    assert "<script>alert(1)</script>" not in review


def test_claim_quote_must_not_be_heading_only(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    _append_evidence(
        store,
        _evidence(
            store,
            "ev-heading-anchor",
            ISSUE_ID,
            "## Overview\nSubstantive body.\n",
            "issue_body",
            "https://github.com/acme/demo/issues/40",
        ),
    )
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-heading-anchor"]
    document["claims"][0]["evidence_quotes"] = {
        "ev-heading-anchor": "## Overview"
    }
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    assert "claim_quote_heading_only" in {
        item["code"] for item in validation["errors"]
    }
    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    assert "anchor_heading_only" in review


def test_claim_quote_must_belong_to_claim_evidence(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["evidence_quotes"] = {
        "ev-pr-101": "PR #101 implements the cleanup API skeleton"
    }
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    assert "claim_quote_evidence_mismatch" in {
        item["code"] for item in validation["errors"]
    }
    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    assert "anchor_evidence_mismatch" in review


def test_claim_quote_is_used_in_report(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["evidence_quotes"] = {
        "ev-issue-40": "explicit cache cleanup API"
    }
    store.write_json("analysis/research.json", document)

    validate(store)
    render(store)

    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert "引文 chars" in report
    assert "verification=verified" in report
    assert "explicit cache cleanup API" in report


def test_validate_blocks_heading_only_evidence(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    heading = _evidence(
        store,
        "ev-heading-only",
        ISSUE_ID,
        "## Overview\n",
        "pull_request_body",
        "https://github.com/acme/demo/pull/101",
    )
    _append_evidence(store, heading)
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-heading-only"]
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation_document = read_json(store, "analysis/validation.json")
    codes = {item["code"] for item in validation_document["errors"]}
    assert "claim_evidence_heading_only" in codes
    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    assert "claim-001" in review
    assert "substantive=no" in review
    assert "## Overview" in review


def test_substantive_text_rejects_setext_and_quoted_headings() -> None:
    assert not validation.substantive_text("Overview\n========\n")
    assert not validation.substantive_text("> ## Overview\n")
    assert not validation.substantive_text("> Title\n> -----\n")
    assert validation.substantive_text("> Body text.\n")
    assert validation.substantive_text("Paragraph.\n\n---\n")


def test_claim_quote_must_not_be_too_short(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    _append_evidence(
        store,
        _evidence(
            store,
            "ev-short-anchor",
            ISSUE_ID,
            "Substantive body with a short quote.\n",
            "issue_body",
            "https://github.com/acme/demo/issues/40",
        ),
    )
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-short-anchor"]
    document["claims"][0]["evidence_quotes"] = {"ev-short-anchor": "short quote"}
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation_document = read_json(store, "analysis/validation.json")
    assert "claim_quote_too_short" in {
        item["code"] for item in validation_document["errors"]
    }


def test_claim_quote_must_be_unique(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    repeated = "Cache planner improves throughput."
    _append_evidence(
        store,
        _evidence(
            store,
            "ev-ambiguous-anchor",
            ISSUE_ID,
            f"{repeated} {repeated}\n",
            "issue_body",
            "https://github.com/acme/demo/issues/40",
        ),
    )
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-ambiguous-anchor"]
    document["claims"][0]["evidence_quotes"] = {
        "ev-ambiguous-anchor": repeated
    }
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation_document = read_json(store, "analysis/validation.json")
    assert "claim_quote_ambiguous" in {
        item["code"] for item in validation_document["errors"]
    }


def test_empty_code_fence_is_heading_only_in_report(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    empty_fence = _evidence(
        store,
        "ev-empty-fence",
        ISSUE_ID,
        "```\n```\n",
        "issue_body",
        "https://github.com/acme/demo/issues/40",
    )
    _append_evidence(store, empty_fence)
    document = research_document()
    document["claims"][0] = make_claim(
        claim_id="claim-001",
        text="尚未确认完整实现。",
        kind="unknown",
        evidence_ids=["ev-empty-fence"],
        confidence="low",
        confidence_reason="只有空代码围栏。",
        scope={
            "entity_ids": [ISSUE_ID],
            "capability_id": "cap-001",
            "revision": None,
            "version": None,
        },
        reason="材料不足",
        missing_evidence=["实现正文"],
    )
    store.write_json("analysis/research.json", document)

    validate(store)
    render(store)

    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert "仅标题/空壳，不能单独支撑事实" in report


def test_validate_warns_heading_only_unknown_evidence(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    heading = _evidence(
        store,
        "ev-heading-unknown",
        ISSUE_ID,
        "#### GSM8K results\n",
        "issue_body",
        "https://github.com/acme/demo/issues/40",
    )
    _append_evidence(store, heading)
    document = research_document()
    document["claims"][0] = make_claim(
        claim_id="claim-001",
        text="尚未确认完整性能数据。",
        kind="unknown",
        evidence_ids=["ev-heading-unknown"],
        confidence="low",
        confidence_reason="只有标题，不能确认内容。",
        scope={
            "entity_ids": [ISSUE_ID],
            "capability_id": "cap-001",
            "revision": None,
            "version": None,
        },
        reason="材料不足",
        missing_evidence=["完整结果段"],
    )
    store.write_json("analysis/research.json", document)

    validate(store)

    validation_document = read_json(store, "analysis/validation.json")
    codes = {item["code"] for item in validation_document["warnings"]}
    assert "unknown_evidence_heading_only" in codes
    render(store)
    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert "仅标题/空壳，不能单独支撑事实" in report


def test_heading_plus_substantive_evidence_is_allowed(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    heading = _evidence(
        store,
        "ev-heading-mixed",
        ISSUE_ID,
        "## Overview\n",
        "issue_body",
        "https://github.com/acme/demo/issues/40",
    )
    _append_evidence(store, heading)
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-heading-mixed", "ev-issue-40"]
    store.write_json("analysis/research.json", document)

    validate(store)

    validation_document = read_json(store, "analysis/validation.json")
    assert "claim_evidence_heading_only" not in {
        item["code"] for item in validation_document["errors"]
    }


def test_validate_blocks_heading_only_capability_evidence(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    heading = _evidence(
        store,
        "ev-cap-heading",
        PR_ID,
        "## CI status\n",
        "pull_request_body",
        "https://github.com/acme/demo/pull/101",
    )
    _append_evidence(store, heading)
    document = research_document()
    document["capabilities"][0]["evidence_ids"] = ["ev-cap-heading"]
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation_document = read_json(store, "analysis/validation.json")
    codes = {item["code"] for item in validation_document["errors"]}
    assert "capability_evidence_heading_only" in codes


def test_scope_versions_cover_multiple_releases() -> None:
    releases = [
        make_entity(
            entity_id="github.com/acme/demo/release/v1.0.0",
            kind="release",
            title="v1.0.0",
            url="https://github.com/acme/demo/releases/tag/v1.0.0",
            observed_at=NOW,
            raw_state={"tag_name": "v1.0.0"},
        ),
        make_entity(
            entity_id="github.com/acme/demo/release/v1.1.0",
            kind="release",
            title="v1.1.0",
            url="https://github.com/acme/demo/releases/tag/v1.1.0",
            observed_at=NOW,
            raw_state={"tag_name": "v1.1.0"},
        ),
    ]
    evidence_record = make_evidence(
        evidence_id="ev-release",
        entity_id=releases[0]["id"],
        source_url="https://github.com/acme/demo/releases/tag/v1.0.0",
        source_kind="release_body",
        observed_at=NOW,
        text="Release notes describe the change.",
        locator=make_locator("char_range", char_start=0, char_end=33, line_start=1, line_end=1),
        blob_sha256="a" * 64,
        verification="verified",
    )
    document = research_document(
        claims=[
            make_claim(
                claim_id="claim-multi",
                text="v1.0.0 与 v1.1.0 发布说明都记载了该变化。",
                kind="observed",
                evidence_ids=["ev-release"],
                confidence="high",
                confidence_reason="来自发布说明。",
                scope={
                    "entity_ids": [releases[0]["id"], releases[1]["id"]],
                    "capability_id": None,
                    "revision": None,
                    "version": None,
                    "versions": ["v1.0.0", "v1.1.0"],
                },
            )
        ],
        capabilities=[],
        topics=[],
        unknowns=[],
        reading_list=[],
    )

    _problems, warnings = validation._evidence_problems(
        document,
        releases,
        [evidence_record],
    )

    assert "claim_missing_version" not in {item["code"] for item in warnings}
    assert "claim_scope_version_mismatch" not in {item["code"] for item in warnings}


def test_validate_blocks_missing_evidence(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-missing"]
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure) as excinfo:
        validate(store)

    assert excinfo.value.exit_code == 5
    validation = read_json(store, "analysis/validation.json")
    assert validation["ok"] is False
    assert "missing_evidence" in {item["code"] for item in validation["errors"]}
    review = store.secure_path("analysis/evidence-review.md").read_text(encoding="utf-8")
    assert "review: needs_attention" in review
    assert "missing_evidence" in review
    assert read_json(store, "manifest.json")["stages"]["validate"] == "failed"
    assert not store.secure_path("report.md").exists()


def test_validate_blocks_merged_with_remaining_requirements(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["capabilities"][0]["requirements"]["remaining"] = ["跨 policy 清理"]
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    assert "merged_with_remaining_requirements" in {
        item["code"] for item in validation["errors"]
    }


def test_coverage_gaps_must_be_visible_in_limitations(tmp_path: Path) -> None:
    coverage = [
        {
            "channel": "activity_prs",
            "query_scope": "window",
            "query": "merged:>=2026-08-01",
            "expected_count": 3,
            "fetched_count": 1,
            "pages": 1,
            "exhausted": False,
            "truncation_reason": "budget_exceeded",
            "remaining_cursor": None,
            "observed_start": NOW,
            "observed_end": NOW,
            "coverage_status": "partial",
            "discovery_status": "complete",
            "detail_status": "partial",
        }
    ]
    store = seed_run(tmp_path, coverage=coverage)
    document = research_document()
    document["limitations"] = ["只观察到当前状态。"]
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    codes = {item["code"] for item in validation["errors"]}
    assert "coverage_undocumented" in codes

    document["limitations"] = ["activity_prs 只覆盖了 1/3 的 PR。", "预算：部分通道被截断。"]
    store.write_json("analysis/research.json", document)

    validate(store)
    assert read_json(store, "analysis/validation.json")["ok"] is True


def test_warning_section_groups_coverage_by_channel(tmp_path: Path) -> None:
    coverage = [
        {
            "channel": "docs",
            "query_scope": "default_branch",
            "query": "docs",
            "expected_count": 2,
            "fetched_count": 1,
            "pages": 1,
            "exhausted": False,
            "truncation_reason": "some_selected_files_unreadable",
            "remaining_cursor": None,
            "observed_start": NOW,
            "observed_end": NOW,
            "coverage_status": "partial",
            "discovery_status": "partial",
            "detail_status": "partial",
        },
        {
            "channel": "discussion_detail",
            "query_scope": "proposals",
            "query": "discussions",
            "expected_count": 0,
            "fetched_count": 0,
            "pages": 0,
            "exhausted": True,
            "truncation_reason": None,
            "remaining_cursor": None,
            "observed_start": NOW,
            "observed_end": NOW,
            "coverage_status": "not_requested",
            "discovery_status": "not_requested",
            "detail_status": "not_requested",
        },
    ]
    store = seed_run(tmp_path, coverage=coverage)
    document = research_document()
    document["limitations"] = [
        "docs partial：只读取了部分文件；discussion_detail 主动未请求；预算未继续扩大采集。"
    ]
    store.write_json("analysis/research.json", document)
    validate(store)
    render(store)

    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert "`coverage:docs`" in report
    assert "some_selected_files_unreadable" in report
    assert "coverage:discussion_detail" in report
    assert "not_requested" in report


def test_render_rejects_stale_validation_and_removes_report(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    assert store.secure_path("report.md").is_file()

    document = research_document()
    document["topics"][0]["summary"] = "修改后的结论摘要。"
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure) as excinfo:
        render(store)

    assert excinfo.value.exit_code == 5
    assert not store.secure_path("report.md").exists()
    incomplete = store.secure_path("incomplete.md").read_text(encoding="utf-8")
    assert "未生成正式报告" in incomplete
    assert "在 validate 之后被修改" in incomplete
    report = store.verify_integrity()
    problems = {item["problem"] for item in report["problems"]}
    assert "stale_validation" in problems


def test_render_rejects_missing_dependency_blob_after_validate(
    tmp_path: Path,
) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    assert store.secure_path("report.md").is_file()
    evidence = next(
        item
        for item in store.read_jsonl("normalized/evidence.jsonl")
        if item["id"] == "ev-pr-101"
    )
    store.secure_path(Path("blobs") / evidence["blob_sha256"]).unlink()

    with pytest.raises(ValidationFailure) as excinfo:
        render(store)

    assert excinfo.value.source == "render.evidence_integrity"
    assert not store.secure_path("report.md").exists()
    assert not store.secure_path("analysis/render-report.json").exists()
    incomplete = store.secure_path("incomplete.md").read_text(encoding="utf-8")
    assert "ev-pr-101" in incomplete
    assert "missing_evidence_blob" in incomplete


def test_render_rejects_corrupt_dependency_blob_after_validate(
    tmp_path: Path,
) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    evidence = next(
        item
        for item in store.read_jsonl("normalized/evidence.jsonl")
        if item["id"] == "ev-pr-101"
    )
    store.secure_path(Path("blobs") / evidence["blob_sha256"]).write_bytes(
        b"tampered"
    )

    with pytest.raises(ValidationFailure) as excinfo:
        render(store)

    assert excinfo.value.source == "render.evidence_integrity"
    assert not store.secure_path("report.md").exists()
    assert not store.secure_path("analysis/render-report.json").exists()
    incomplete = store.secure_path("incomplete.md").read_text(encoding="utf-8")
    assert "ev-pr-101" in incomplete
    assert "corrupt_evidence_blob" in incomplete


def test_render_ignores_unreferenced_missing_blob(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    extra = _evidence(
        store,
        "ev-extra",
        ISSUE_ID,
        "Unreferenced evidence.",
        "issue_body",
        "https://github.com/acme/demo/issues/40",
    )
    store.append_jsonl(
        "normalized/evidence.jsonl",
        extra,
        key="id",
        schema_name="evidence",
    )
    validate(store)
    first, _lines = render(store)
    first_sha = first["result"]["sha256"]

    store.secure_path(Path("blobs") / extra["blob_sha256"]).unlink()
    second, _lines = render(store)

    assert second["result"]["sha256"] == first_sha
    assert store.secure_path("report.md").is_file()


def test_render_checks_dependencies_before_build_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = seed_run(tmp_path)
    validate(store)
    evidence = next(
        item
        for item in store.read_jsonl("normalized/evidence.jsonl")
        if item["id"] == "ev-pr-101"
    )
    store.secure_path(Path("blobs") / evidence["blob_sha256"]).unlink()
    called: list[bool] = []

    def build_report(*args, **kwargs):
        called.append(True)
        raise RuntimeError("build_report must not run")

    monkeypatch.setattr(render_module, "build_report", build_report)

    with pytest.raises(ValidationFailure):
        render(store)

    assert called == []


def test_evidence_dependency_problems_reports_missing_record(
    tmp_path: Path,
) -> None:
    store = seed_run(tmp_path)
    research = research_document()
    research["claims"][0]["evidence_ids"] = ["ev-missing"]

    problems = render_module._evidence_dependency_problems(store, research)

    assert any(
        item["problem"] == "missing_evidence_record"
        and item["evidence_id"] == "ev-missing"
        for item in problems
    )


def test_render_build_failure_discards_existing_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    assert store.secure_path("report.md").is_file()

    def boom(*args, **kwargs):
        raise RuntimeError("template exploded")

    monkeypatch.setattr(render_module, "build_report", boom)

    with pytest.raises(RuntimeError, match="template exploded"):
        render(store)

    assert not store.secure_path("report.md").exists()
    assert not store.secure_path("analysis/render-report.json").exists()
    incomplete = store.secure_path("incomplete.md").read_text(encoding="utf-8")
    assert "未生成正式报告" in incomplete
    assert "render 执行失败" in incomplete
    manifest = read_json(store, "manifest.json")
    assert manifest["stages"]["render"] == "failed"
    assert manifest["state"] == "researching"


def test_render_report_write_failure_discards_existing_report(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    assert store.secure_path("report.md").is_file()

    original = SnapshotStore.write_json

    def fail_render_report(self, relpath, document, **kwargs):
        if str(relpath) == "analysis/render-report.json":
            raise OSError("disk full")
        return original(self, relpath, document, **kwargs)

    monkeypatch.setattr(SnapshotStore, "write_json", fail_render_report)

    with pytest.raises(OSError, match="disk full"):
        render(store)

    assert not store.secure_path("report.md").exists()
    assert not store.secure_path("analysis/render-report.json").exists()
    incomplete = store.secure_path("incomplete.md").read_text(encoding="utf-8")
    assert "render 执行失败" in incomplete


def test_render_requires_a_successful_validate(tmp_path: Path) -> None:
    store = seed_run(tmp_path)

    with pytest.raises(UsageError) as excinfo:
        render(store)

    assert excinfo.value.exit_code == 2
    assert read_json(store, "manifest.json")["stages"]["render"] == "failed"
    assert store.secure_path("incomplete.md").is_file()


def test_tampered_report_is_reported_by_integrity(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    store.secure_path("report.md").write_text("# tampered\n", encoding="utf-8")

    report = store.verify_integrity()

    problems = {item["problem"] for item in report["problems"]}
    assert "stale_report" in problems


def test_validate_rejects_missing_research_document(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    store.secure_path("analysis/research.json").unlink()

    with pytest.raises(UsageError) as excinfo:
        validate(store)

    assert excinfo.value.exit_code == 2


def test_validate_failure_discards_previous_report(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    assert store.secure_path("report.md").is_file()

    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-missing"]
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    assert not store.secure_path("report.md").exists()
    assert not store.secure_path("analysis/render-report.json").exists()
    assert "未生成正式报告" in store.secure_path("incomplete.md").read_text(encoding="utf-8")


def test_validate_blocks_released_without_verified_release(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["capabilities"][0]["delivery_state"] = "released"
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    assert "released_without_verified_release" in {
        item["code"] for item in validation["errors"]
    }


@pytest.mark.parametrize(
    "scope",
    [
        {
            "entity_ids": [ISSUE_ID],
            "capability_id": None,
            "revision": None,
            "version": None,
            "versions": ["v1.0.0"],
        },
        {
            "entity_ids": [ISSUE_ID],
            "capability_id": None,
            "revision": None,
            "version": "v1.0.0",
            "versions": ["v1.0.0", "v1.1.0"],
        },
    ],
)
def test_claim_versions_require_two_and_conflict_with_version(scope: dict) -> None:
    document = research_document()
    document["claims"][0]["scope"] = scope

    with pytest.raises(SchemaError):
        validate_document(document, "research")


def test_schema_errors_are_written_to_validation_json(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["kind"] = "fact"
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    assert validation["ok"] is False
    assert "research_schema" in {item["code"] for item in validation["errors"]}
    assert validation["research_sha256"]


def test_validation_and_render_are_byte_deterministic(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    first_validation = store.secure_path("analysis/validation.json").read_bytes()
    render(store)
    first_record = store.secure_path("analysis/render-report.json").read_bytes()
    first_report = store.secure_path("report.md").read_bytes()

    validate(store)
    render(store)

    assert store.secure_path("analysis/validation.json").read_bytes() == first_validation
    assert store.secure_path("analysis/render-report.json").read_bytes() == first_record
    assert store.secure_path("report.md").read_bytes() == first_report


def test_claim_text_cannot_pollute_the_template(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["text"] = (
        "模板片段 {{method}}、{{placeholder}} 和 }} 必须原样保留。"
    )
    store.write_json("analysis/research.json", document)

    validate(store)
    render(store)

    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert report.count("### 方法") == 1
    assert "{{method}}" in report
    assert "{{placeholder}}" in report
    assert "}}" in report


def test_missing_research_resets_state_and_discards_report(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    store.secure_path("analysis/research.json").unlink()

    with pytest.raises(UsageError) as excinfo:
        validate(store)

    assert excinfo.value.exit_code == 2
    manifest = read_json(store, "manifest.json")
    assert manifest["stages"]["validate"] == "failed"
    assert manifest["stages"]["render"] == "not_started"
    assert manifest["state"] == "researching"
    assert not store.secure_path("report.md").exists()
    assert not store.secure_path("analysis/render-report.json").exists()
    assert not store.secure_path("analysis/validation.json").exists()
    assert "未生成正式报告" in store.secure_path("incomplete.md").read_text(encoding="utf-8")


def test_unknown_claims_are_rendered_once(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    unknown = make_claim(
        claim_id="claim-003",
        text="是否已经支持跨 policy 清理？",
        kind="unknown",
        reason="窗口内没有找到后续实现或发布记录。",
        missing_evidence=["后续实现 PR", "包含该能力的 release"],
        scope={
            "entity_ids": [],
            "capability_id": "cap-001",
            "revision": None,
            "version": None,
        },
    )
    document["claims"].append(unknown)
    document["topics"][0]["claim_ids"]["unresolved"] = ["claim-003"]
    document["unknowns"] = [
        {"id": "unk-001", "topic_id": "topic-001", "claim_id": "claim-003"}
    ]
    store.write_json("analysis/research.json", document)

    validate(store)
    render(store)

    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert report.count("是否已经支持跨 policy 清理？") == 1
    assert "### 未知项" in report


def test_unreferenced_items_and_duplicate_order_are_warnings(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"].append(
        claim(
            "claim-009",
            "补充材料提到缓存清理的必要性。",
            "ev-issue-40",
            ISSUE_ID,
            None,
        )
    )
    document["reading_list"][1]["order"] = 0
    store.write_json("analysis/research.json", document)

    result, _lines = validate(store)

    validation = read_json(store, "analysis/validation.json")
    warning_codes = {item["code"] for item in validation["warnings"]}
    error_codes = {item["code"] for item in validation["errors"]}
    assert result["result"]["ok"] is True
    assert "unreferenced_claim" in warning_codes
    assert "duplicate_reading_order" in warning_codes
    assert "unreferenced_claim" not in error_codes


def test_lifecycle_result_is_visible_in_validation_and_report(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)

    validation = read_json(store, "analysis/validation.json")
    lifecycle = validation["lifecycle"][0]
    assert lifecycle["delivery_verification"] == "unknown"
    assert lifecycle["state_time_basis"] == "observed_current"
    assert PR_ID in lifecycle["supporting_entities"]["pull_request"]

    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert "发布（核验）" in report
    assert "`unknown`（未核验）" in report


def test_reading_urls_must_be_stable_sources(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["reading_list"][0]["url"] = "https://evil.example/rfc"
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)
    codes = {
        item["code"]
        for item in read_json(store, "analysis/validation.json")["errors"]
    }
    assert "reading_url_host_mismatch" in codes

    document["reading_list"][0]["url"] = "ftp://github.com/acme/demo/issues/40"
    store.write_json("analysis/research.json", document)
    with pytest.raises(ValidationFailure):
        validate(store)
    codes = {
        item["code"]
        for item in read_json(store, "analysis/validation.json")["errors"]
    }
    assert "reading_url_invalid" in codes


def test_render_refuses_runs_whose_validation_failed(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-missing"]
    store.write_json("analysis/research.json", document)
    with pytest.raises(ValidationFailure):
        validate(store)

    with pytest.raises(ValidationFailure) as excinfo:
        render(store)

    assert excinfo.value.exit_code == 5


def _integrity_problems(store: SnapshotStore) -> set[str]:
    return {item["problem"] for item in store.verify_integrity()["problems"]}


def test_integrity_detects_stale_validation_inputs(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    store.secure_path("raw/issues.jsonl").write_text('{"number": 1}\n', encoding="utf-8")

    assert "stale_validation_inputs" in _integrity_problems(store)


def test_integrity_detects_report_manifest_combinations(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)

    store.secure_path("analysis/validation.json").unlink()
    assert "report_without_validation" in _integrity_problems(store)

    validate(store)
    render(store)
    store.secure_path("analysis/render-report.json").unlink()
    assert "report_without_manifest" in _integrity_problems(store)

    render(store)
    store.secure_path("report.md").unlink()
    assert "missing_report" in _integrity_problems(store)


def test_integrity_detects_validation_run_mismatch(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    validation = read_json(store, "analysis/validation.json")
    validation["run_id"] = "20261101T090000Z-deadbeef"
    store.write_json("analysis/validation.json", validation, schema_name="validation")

    assert "validation_run_mismatch" in _integrity_problems(store)


def test_integrity_detects_template_and_validation_drift(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    record = read_json(store, "analysis/render-report.json")
    record["template_version"] = "0"
    store.write_json("analysis/render-report.json", record, schema_name="render_report")
    assert "stale_report" in _integrity_problems(store)

    validate(store)
    render(store)
    manifest = read_json(store, "manifest.json")
    manifest["coverage"] = [
        {
            "channel": "docs",
            "query_scope": "default_branch",
            "query": "docs/**/*.md",
            "expected_count": 0,
            "fetched_count": 0,
            "pages": 1,
            "exhausted": True,
            "truncation_reason": None,
            "remaining_cursor": None,
            "observed_start": NOW,
            "observed_end": NOW,
            "coverage_status": "complete",
            "discovery_status": "complete",
            "detail_status": "not_requested",
        },
        _pr_detail_coverage(),
    ]
    store.write_json("manifest.json", manifest, schema_name="manifest")
    validate(store)
    assert "stale_report" in _integrity_problems(store)


def test_report_matches_golden(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    golden = (Path(__file__).parent / "fixtures" / "example" / "report.golden.md").read_text(
        encoding="utf-8"
    )
    assert store.secure_path("report.md").read_text(encoding="utf-8") == golden


def _set_limitations(*values: str):
    def mutate(document: dict) -> None:
        document["limitations"] = list(values)

    return mutate


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda d: d["claims"][1].update(id="claim-001"), "duplicate_id"),
        (lambda d: d.update(topics=[]), "empty_research"),
        (
            lambda d: d["topics"][0]["claim_ids"].update(design=["claim-404"]),
            "missing_claim_reference",
        ),
        (
            lambda d: d["topics"][0].update(capability_ids=["cap-404"]),
            "missing_capability_reference",
        ),
        (
            lambda d: d["topics"][0]["reading_item_ids"].append("read-404"),
            "missing_reading_item_reference",
        ),
        (
            lambda d: d.update(
                unknowns=[{"id": "unk-001", "topic_id": "topic-001", "claim_id": "claim-001"}]
            ),
            "unknown_claim_not_unknown",
        ),
        (_set_limitations(), "missing_limitations"),
        (_set_limitations("只观察到当前状态。"), "budget_undocumented"),
        (
            lambda d: d["claims"][0].update(contradicting_evidence_ids=["ev-nope"]),
            "missing_contradicting_evidence",
        ),
        (
            lambda d: d["claims"][0]["scope"].update(
                entity_ids=["github.com/acme/demo/issue/999"]
            ),
            "missing_claim_entity",
        ),
        (
            lambda d: d["reading_list"][0].update(
                entity_id="github.com/acme/demo/pull/999"
            ),
            "missing_reading_entity",
        ),
    ],
)
def test_gate_error_matrix(tmp_path: Path, mutate, code: str) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    mutate(document)
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    codes = {
        item["code"]
        for item in read_json(store, "analysis/validation.json")["errors"]
    }
    assert code in codes


def test_gate_warning_matrix(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    research_document()
    records = [
        json.loads(line)
        for line in store.secure_path("normalized/evidence.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    for record in records:
        if record["id"] == "ev-issue-40":
            record["verification"] = "unverifiable"
    store.write_jsonl(
        "normalized/evidence.jsonl", records, key="id", schema_name="evidence"
    )
    manifest = read_json(store, "manifest.json")
    manifest["coverage"] = [
        {
            "channel": "docs",
            "query_scope": "default_branch",
            "query": "docs/**/*.md",
            "expected_count": 1,
            "fetched_count": 0,
            "pages": 1,
            "exhausted": True,
            "truncation_reason": None,
            "remaining_cursor": None,
            "observed_start": NOW,
            "observed_end": NOW,
            "coverage_status": "partial",
            "discovery_status": "complete",
            "detail_status": "not_requested",
        },
        {
            "channel": "discussions_capability",
            "query_scope": "capability",
            "query": "discussions",
            "expected_count": 0,
            "fetched_count": 0,
            "pages": 0,
            "exhausted": True,
            "truncation_reason": None,
            "remaining_cursor": None,
            "observed_start": NOW,
            "observed_end": NOW,
            "coverage_status": "not_requested",
            "discovery_status": "not_requested",
            "detail_status": "not_requested",
        },
        _pr_detail_coverage(),
    ]
    store.write_json("manifest.json", manifest, schema_name="manifest")

    validate(store)

    codes = {
        item["code"]
        for item in read_json(store, "analysis/validation.json")["warnings"]
    }
    assert "evidence_unverified" in codes
    assert "coverage_degraded" in codes
    assert "coverage_not_requested" in codes


def test_successful_validate_discards_incomplete(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["evidence_ids"] = ["ev-missing"]
    store.write_json("analysis/research.json", document)
    with pytest.raises(ValidationFailure):
        validate(store)
    assert store.secure_path("incomplete.md").is_file()

    store.write_json("analysis/research.json", research_document())
    validate(store)

    assert not store.secure_path("incomplete.md").exists()


def test_validate_after_research_change_discards_the_old_report(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    validate(store)
    render(store)
    assert store.secure_path("report.md").is_file()

    document = research_document()
    document["topics"][0]["summary"] = "新的结论摘要。"
    store.write_json("analysis/research.json", document)

    validate(store)

    manifest = read_json(store, "manifest.json")
    assert manifest["stages"]["validate"] == "complete"
    assert manifest["stages"]["render"] == "not_started"
    assert not store.secure_path("report.md").exists()
    assert not store.secure_path("analysis/render-report.json").exists()


def test_report_escapes_html_and_rejects_unsafe_links(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["claims"][0]["text"] = '<img src=x onerror="alert(1)"> [link](https://evil)'
    store.write_json("analysis/research.json", document)
    evidence = store.read_jsonl("normalized/evidence.jsonl")
    for item in evidence:
        if item["id"] == "ev-issue-40":
            item["source_url"] = "javascript:alert(1)"
    store.write_jsonl(
        "normalized/evidence.jsonl",
        evidence,
        key="id",
        schema_name="evidence",
    )

    validate(store)
    render(store)

    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert "&lt;img" in report
    assert "<img" not in report
    assert "javascript:alert" not in report
    assert "\\[link\\]" in report


def test_claim_scope_is_still_checked_without_capabilities(tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    document = research_document()
    document["capabilities"] = []
    document["topics"][0]["capability_ids"] = []
    store.write_json("analysis/research.json", document)

    with pytest.raises(ValidationFailure):
        validate(store)

    validation = read_json(store, "analysis/validation.json")
    codes = {item["code"] for item in validation["errors"]}
    assert "missing_claim_capability" in codes


def test_detail_not_requested_does_not_warn_the_whole_channel(tmp_path: Path) -> None:
    store = seed_run(
        tmp_path,
        coverage=[
            {
                "channel": "docs",
                "query_scope": "default_branch",
                "query": "docs/**/*.md",
                "expected_count": 0,
                "fetched_count": 0,
                "pages": 1,
                "exhausted": True,
                "truncation_reason": None,
                "remaining_cursor": None,
                "observed_start": NOW,
                "observed_end": NOW,
                "coverage_status": "complete",
                "discovery_status": "complete",
                "detail_status": "not_requested",
            }
        ],
    )

    validate(store)

    codes = {
        item["code"]
        for item in read_json(store, "analysis/validation.json")["warnings"]
    }
    assert "coverage_not_requested" not in codes


def test_compact_report_deduplicates_topic_summary_and_keeps_audit_appendix(tmp_path):
    store = seed_run(tmp_path)
    document = research_document()
    topic = document["topics"][0]
    topic["summary"] = "唯一主题摘要，不应在多个固定章节重复。"
    topic["claim_ids"]["outlook"] = list(topic["claim_ids"]["implementation"])
    store.write_json("analysis/research.json", document)
    validate(store)
    render(store)
    text = store.secure_path("report.md").read_text()
    assert text.count(topic["summary"]) == 1
    assert "## 近期方向" not in text
    assert "## 关键材料索引" not in text
    assert "## 后续方向" not in text
    assert "参见已述结论" in text
    assert "<details>\n<summary>展开证据附录" in text
    assert "verification=verified" in text.split("<details>", 1)[1]
    assert "### 校验警告" in text.split("<details>", 1)[0]
