from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import test_normalize as normalize_helpers
import test_ranking as ranking_helpers

from rer import packs, ranking
from rer.config import build_config
from rer.models import build_manifest, make_evidence, make_locator, validate_document


def candidates_for(tmp_path: Path, **budget) -> tuple:
    store, config, manifest = ranking_helpers.seed_first_round(tmp_path, budget=budget)
    return store, config, manifest, ranking.build_candidates(store, config, manifest)


def test_pack_index_is_deterministic_and_fits_budget(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(
        tmp_path,
        max_pack_chars=2200,
    )
    first = packs.build_pack_index(store, config, manifest, candidates)
    second = packs.build_pack_index(store, config, manifest, candidates)

    assert first["document"] == second["document"]
    assert first["texts"] == second["texts"]
    validate_document(first["document"], "pack_index")
    assert first["document"]["budget"]["used_chars"] <= 2200
    assert first["document"]["counts"]["items_omitted"] > 0
    assert first["document"]["omitted_candidates"]
    assert "pack_char_budget_exceeded" in first["document"]["limitations"]
    for text in first["texts"].values():
        assert len(text) <= 2200


def test_pack_budget_protects_agent_selected_items(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(
        tmp_path,
        max_pack_chars=1200,
    )
    protected = candidates["candidates"][-1]
    protected["agent_selected"] = True
    protected["selection_reason"] = sorted(
        [*protected["selection_reason"], "agent_selected"]
    )

    payload = packs.build_pack_index(store, config, manifest, candidates)

    included = {
        item["candidate_id"]
        for pack in payload["document"]["packs"]
        for item in pack["items"]
    }
    omitted = {
        item["candidate_id"]
        for item in payload["document"]["omitted_candidates"]
    }
    assert protected["candidate_id"] in included
    assert protected["candidate_id"] not in omitted


def test_selected_hard_budget_reason_is_schema_valid(tmp_path: Path) -> None:
    store, config, manifest = ranking_helpers.seed_first_round(
        tmp_path, budget={"max_pack_chars": 1}
    )
    candidates = ranking.build_candidates(store, config, manifest)
    selected = candidates["candidates"][0]
    selected["agent_selected"] = True

    payload = packs.build_pack_index(store, config, manifest, candidates)

    validate_document(payload["document"], "pack_index")
    reasons = {
        item["reason"] for item in payload["document"]["omitted_candidates"]
    }
    assert "selected_hard_budget" in reasons


def test_research_handoff_keeps_selected_and_deep_read_material(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(
        tmp_path,
        max_pack_chars=2400,
    )
    selected_issue = next(
        item
        for item in candidates["candidates"]
        if item["entity_id"] == "github.com/acme/demo/issue/40"
    )
    selected_issue["agent_selected"] = True
    deep_pr = next(
        item
        for item in candidates["candidates"]
        if item["entity_id"] == "github.com/acme/demo/pull/101"
    )
    deep_pr["deep_read"] = True
    existing = [
        json.loads(line)
        for line in store.secure_path("normalized/evidence.jsonl").read_text().splitlines()
        if line.strip()
    ]
    existing.append(
        make_evidence(
            evidence_id="ev-pr-comment",
            entity_id=deep_pr["entity_id"],
            source_url="https://github.com/acme/demo/pull/101#issuecomment-1",
            source_kind="pull_request_comment",
            observed_at=ranking_helpers.NOW,
            text="Reviewer asks for a cache-boundary test.",
            locator=make_locator(
                "char_range",
                char_start=0,
                char_end=39,
                line_start=4,
                line_end=4,
            ),
            blob_sha256="1" * 64,
            verification="verified",
        )
    )
    store.write_jsonl(
        "normalized/evidence.jsonl",
        existing,
        key="id",
        schema_name="evidence",
    )

    payload = packs.build_pack_index(
        store,
        config,
        manifest,
        candidates,
        research_handoff=True,
    )

    included = {
        item["candidate_id"]
        for pack in payload["document"]["packs"]
        for item in pack["items"]
    }
    assert selected_issue["candidate_id"] in included
    assert deep_pr["candidate_id"] in included
    text = "\n".join(payload["texts"].values())
    assert "## Research materials" in text
    assert "#### Evidence excerpts" in text
    assert "ev-pr-comment" in text
    assert "rewritten" not in text


def test_pack_budget_drops_low_scores_before_high_scores(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(
        tmp_path,
        max_pack_chars=900,
    )
    for item in candidates["candidates"]:
        item["agent_selected"] = False
        item["recommended"] = False
        item["selection_lane"] = ""
        item["score_components"] = {}
        item["score"] = 1
    keeper = next(
        item
        for item in candidates["candidates"]
        if item["kind"] == "issue" and item["module"] == "cache"
    )
    keeper["score"] = 999

    payload = packs.build_pack_index(store, config, manifest, candidates)

    included = {
        item["candidate_id"]
        for pack in payload["document"]["packs"]
        for item in pack["items"]
    }
    omitted = {
        item["candidate_id"]
        for item in payload["document"]["omitted_candidates"]
    }
    assert keeper["candidate_id"] in included
    assert omitted
    assert keeper["candidate_id"] not in omitted


def test_pack_write_cleans_stale_files_and_audits(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(tmp_path)
    payload = packs.build_pack_index(store, config, manifest, candidates)
    packs.write_pack_index(store, payload)
    stale = store.secure_path("analysis/packs/part-999.md")
    stale.write_text("stale", encoding="utf-8")

    packs.write_pack_index(store, payload)

    assert not stale.exists()
    assert packs.audit_pack_index(store, candidates=candidates) == []
    index = store.read_json("analysis/pack-index.json")
    for pack in index["packs"]:
        path = store.secure_path(pack["path"])
        assert path.is_file()
        assert len(path.read_text(encoding="utf-8")) == pack["chars"]


def test_preview_truncation_is_visible(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entity = ranking_helpers.entity(
        "issue",
        "40",
        number=40,
        title="Long body",
        roles=["rfc"],
        raw_state={"labels": ["area: cache"]},
        normalized_state={"scope": "in_window", "role_hints": ["rfc"]},
    )
    store.write_jsonl("normalized/entities.jsonl", [entity], key="id", schema_name="entity")
    text = "x" * 2000
    payload_bytes = text.encode("utf-8")
    store.write_evidence(
        make_evidence(
            evidence_id="ev-long",
            entity_id=entity["id"],
            source_url="https://example.com/long",
            source_kind="issue_body",
            observed_at=ranking_helpers.NOW,
            text=text,
            locator=make_locator(
                "char_range",
                char_start=0,
                char_end=len(text),
                line_start=1,
                line_end=1,
            ),
            blob_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        ),
        payload=payload_bytes,
    )
    config = build_config(
        repository="acme/demo",
        mode="standard",
        timezone_name="UTC",
        now=None,
        budget_overrides={"max_pack_chars": 5000},
    )
    manifest = build_manifest(config, "20261101T080000Z-abcdef12", ranking_helpers.NOW)
    manifest["collection_finished_at"] = ranking_helpers.NOW
    candidates = ranking.build_candidates(store, config, manifest)

    payload = packs.build_pack_index(store, config, manifest, candidates)

    item = payload["document"]["packs"][0]["items"][0]
    assert item["preview"]["original_chars"] == 2000
    assert item["preview"]["chars"] == 240
    assert item["preview"]["truncated"] is True
    assert payload["document"]["counts"]["preview_slots"] == 1
    assert payload["document"]["counts"]["previews_total"] == 1
    assert payload["document"]["counts"]["previews_truncated"] == 1
    assert payload["document"]["counts"]["items_with_truncated_preview"] == 1
    assert payload["document"]["packs"][0]["truncated"] is True


def test_discovery_preview_prefers_longer_evidence(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entity = ranking_helpers.entity(
        "issue",
        "41",
        number=41,
        title="Long body",
        roles=["rfc"],
        raw_state={"labels": ["area: cache"]},
        normalized_state={"scope": "in_window", "role_hints": ["rfc"]},
    )
    store.write_jsonl(
        "normalized/entities.jsonl", [entity], key="id", schema_name="entity"
    )
    for evidence_id, text in (
        ("ev-short-heading", "## Purpose"),
        ("ev-long-body", "Substantive body. " * 30),
    ):
        payload = text.encode("utf-8")
        store.write_evidence(
            make_evidence(
                evidence_id=evidence_id,
                entity_id=entity["id"],
                source_url="https://example.com/issue/41",
                source_kind="issue_body",
                observed_at=ranking_helpers.NOW,
                text=text,
                locator=make_locator(
                    "char_range",
                    char_start=0,
                    char_end=len(text),
                    line_start=1,
                    line_end=1,
                ),
                blob_sha256=hashlib.sha256(payload).hexdigest(),
            ),
            payload=payload,
        )
    config = build_config(
        repository="acme/demo",
        mode="standard",
        timezone_name="UTC",
        now=None,
    )
    manifest = build_manifest(config, "20261101T080000Z-abcdef12", ranking_helpers.NOW)
    candidates = ranking.build_candidates(store, config, manifest)

    previews = packs._previews(store, candidates, research_handoff=False)
    assert previews[entity["id"]][0]["evidence_id"] == "ev-long-body"
    assert "Substantive body" in previews[entity["id"]][0]["text"]


def test_research_preview_skips_heading_only_blocks(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entity = ranking_helpers.entity(
        "issue",
        "42",
        number=42,
        title="Heading and body",
        roles=["rfc"],
        raw_state={"labels": ["area: cache"]},
        normalized_state={"scope": "in_window", "role_hints": ["rfc"]},
    )
    store.write_jsonl(
        "normalized/entities.jsonl", [entity], key="id", schema_name="entity"
    )
    for evidence_id, text in (
        ("ev-heading", "## Purpose"),
        ("ev-body", "Substantive body. " * 20),
    ):
        payload = text.encode("utf-8")
        store.write_evidence(
            make_evidence(
                evidence_id=evidence_id,
                entity_id=entity["id"],
                source_url="https://example.com/issue/42",
                source_kind="issue_body",
                observed_at=ranking_helpers.NOW,
                text=text,
                locator=make_locator(
                    "char_range",
                    char_start=0,
                    char_end=len(text),
                    line_start=1,
                    line_end=1,
                ),
                blob_sha256=hashlib.sha256(payload).hexdigest(),
            ),
            payload=payload,
        )
    config = build_config(
        repository="acme/demo",
        mode="standard",
        timezone_name="UTC",
        now=None,
    )
    manifest = build_manifest(config, "20261101T080000Z-abcdef12", ranking_helpers.NOW)
    candidates = ranking.build_candidates(store, config, manifest)
    candidates["candidates"][0]["agent_selected"] = True

    previews = packs._previews(store, candidates, research_handoff=True)

    assert [item["evidence_id"] for item in previews[entity["id"]]] == ["ev-body"]


def test_research_preview_omits_heading_only_material(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entity = ranking_helpers.entity(
        "issue",
        "44",
        number=44,
        title="Heading-only issue",
        roles=["rfc"],
        raw_state={"labels": ["area: cache"]},
        normalized_state={"scope": "in_window", "role_hints": ["rfc"]},
    )
    store.write_jsonl(
        "normalized/entities.jsonl", [entity], key="id", schema_name="entity"
    )
    for evidence_id, source_kind, text in (
        ("ev-heading-body", "issue_body", "## Purpose"),
        ("ev-heading-comment", "issue_comment", "### Discussion"),
    ):
        payload = text.encode("utf-8")
        store.write_evidence(
            make_evidence(
                evidence_id=evidence_id,
                entity_id=entity["id"],
                source_url="https://example.com/issue/44",
                source_kind=source_kind,
                observed_at=ranking_helpers.NOW,
                text=text,
                locator=make_locator(
                    "char_range",
                    char_start=0,
                    char_end=len(text),
                    line_start=1,
                    line_end=1,
                ),
                blob_sha256=hashlib.sha256(payload).hexdigest(),
            ),
            payload=payload,
        )
    config = build_config(
        repository="acme/demo",
        mode="standard",
        timezone_name="UTC",
        now=None,
    )
    manifest = build_manifest(config, "20261101T080000Z-abcdef12", ranking_helpers.NOW)
    candidates = ranking.build_candidates(store, config, manifest)
    candidate = next(
        item for item in candidates["candidates"] if item["entity_id"] == entity["id"]
    )
    candidate["agent_selected"] = True

    previews = packs._previews(store, candidates, research_handoff=True)

    assert entity["id"] not in previews


def test_research_preview_prefers_substantive_comments(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entity = ranking_helpers.entity(
        "issue",
        "45",
        number=45,
        title="Comments",
        roles=["rfc"],
        raw_state={"labels": ["area: cache"]},
        normalized_state={"scope": "in_window", "role_hints": ["rfc"]},
    )
    store.write_jsonl(
        "normalized/entities.jsonl", [entity], key="id", schema_name="entity"
    )
    records = (
        ("ev-body", "issue_body", "Substantive body. " * 20),
        ("ev-heading-comment", "issue_comment", "### Comment header"),
        ("ev-substantive-comment", "issue_comment", "Useful review context. " * 20),
    )
    for evidence_id, source_kind, text in records:
        payload = text.encode("utf-8")
        store.write_evidence(
            make_evidence(
                evidence_id=evidence_id,
                entity_id=entity["id"],
                source_url="https://example.com/issue/45",
                source_kind=source_kind,
                observed_at=ranking_helpers.NOW,
                text=text,
                locator=make_locator(
                    "char_range",
                    char_start=0,
                    char_end=len(text),
                    line_start=1,
                    line_end=1,
                ),
                blob_sha256=hashlib.sha256(payload).hexdigest(),
            ),
            payload=payload,
        )
    config = build_config(
        repository="acme/demo",
        mode="standard",
        timezone_name="UTC",
        now=None,
    )
    manifest = build_manifest(config, "20261101T080000Z-abcdef12", ranking_helpers.NOW)
    candidates = ranking.build_candidates(store, config, manifest)
    candidate = next(
        item for item in candidates["candidates"] if item["entity_id"] == entity["id"]
    )
    candidate["deep_read"] = True

    previews = packs._previews(store, candidates, research_handoff=True)

    assert [item["evidence_id"] for item in previews[entity["id"]]] == [
        "ev-body",
        "ev-substantive-comment",
    ]


def test_legacy_1_2_candidates_and_pack_index_are_rejected(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(tmp_path)
    legacy_candidates = copy.deepcopy(candidates)
    legacy_candidates["schema_version"] = "1.2"
    store.write_json(ranking.CANDIDATES_PATH, legacy_candidates)

    problems = ranking.audit_candidates(store, entities=[], evidence=[])

    assert [item["problem"] for item in problems] == ["invalid_candidates"]

    payload = packs.build_pack_index(store, config, manifest, candidates)
    legacy_pack_index = copy.deepcopy(payload["document"])
    legacy_pack_index["schema_version"] = "1.2"
    store.write_json(packs.PACK_INDEX_PATH, legacy_pack_index)

    problems = packs.audit_pack_index(store, candidates=candidates)

    assert [item["problem"] for item in problems] == ["invalid_pack_index"]


def test_truncation_uses_original_chars_at_final_cap() -> None:
    research_item = {
        "candidate_id": "cand-research",
        "entity_id": "github.com/acme/demo/pull/101",
        "kind": "pull_request",
        "title": "Long research material",
        "bucket": "merged_pr",
        "module": "cache",
        "tier": "research",
        "deep_read": True,
        "score": 10,
        "score_components": {},
        "selection_reason": [],
        "missing_features": [],
        "raw_refs": [],
        "evidence_ids": ["ev-long"],
        "agent_selected": True,
        "recommended": False,
        "budget_fit": "deep_read",
        "preview_cap": 80,
        "research_cap": 400,
        "preview_text": "x" * 120,
        "original_chars": 600,
        "evidence_previews": [
            {
                "evidence_id": "ev-long",
                "source_kind": "pull_request_body",
                "source_url": "https://example.com/long",
                "locator": {},
                "text": "x" * 1600,
                "original_chars": 2000,
                "truncated": False,
            }
        ],
    }
    discovery_item = {
        **research_item,
        "candidate_id": "cand-discovery",
        "tier": "discovery",
        "agent_selected": False,
        "evidence_previews": [],
        "preview_cap": 80,
        "preview_text": "y" * 120,
        "original_chars": 600,
    }

    assert packs._item_truncated(research_item) is True
    assert packs._item_truncated(discovery_item) is True
    rendered = "\n".join(packs._render_item(research_item, 400, research=True))
    assert "400/2000 chars (truncated)" in rendered
    assert packs._item_index(research_item)["evidence_previews"][0]["truncated"] is True
    assert packs._item_index(discovery_item)["preview"]["truncated"] is True


def test_pack_index_rejects_unknown_fields(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(tmp_path)
    payload = packs.build_pack_index(store, config, manifest, candidates)
    document = payload["document"]
    document["confidence"] = 0.9

    try:
        validate_document(document, "pack_index")
    except Exception as exc:
        assert getattr(exc, "code", None) in ("schema_error", "validation_failed")
    else:  # pragma: no cover
        raise AssertionError("pack_index schema accepted an undocumented field")


def test_pack_markdown_has_stable_shape(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entity = ranking_helpers.entity(
        "issue",
        "40",
        number=40,
        title="RFC: cache",
        roles=["rfc"],
        raw_state={"labels": ["area: cache"]},
        normalized_state={"scope": "in_window", "role_hints": ["rfc"]},
    )
    store.write_jsonl("normalized/entities.jsonl", [entity], key="id", schema_name="entity")
    config = build_config(
        repository="acme/demo",
        mode="standard",
        timezone_name="UTC",
        now=None,
    )
    manifest = build_manifest(config, "20261101T080000Z-abcdef12", ranking_helpers.NOW)
    manifest["collection_finished_at"] = ranking_helpers.NOW
    candidates = ranking.build_candidates(store, config, manifest)

    payload = packs.build_pack_index(store, config, manifest, candidates)

    text = next(iter(payload["texts"].values()))
    expected = (
        f"# Reading pack {packs.pack_id('issue', 'cache')}\n"
        "\n"
        "- source_kind: issue\n"
        "- module: cache\n"
        "- observed_at: 2026-11-01T08:00:00Z\n"
        "- rule_version: candidates-v1\n"
        "\n"
        "## Candidate index\n"
        "\n"
        f"### [{candidates['candidates'][0]['candidate_id']}] RFC: cache\n"
        "\n"
        "- entity_id: `github.com/acme/demo/issue/40`\n"
        "- kind: issue | bucket: design | module: cache (labels)\n"
        "- tier: discovery | deep_read: false | agent_selected: false | "
        "recommended: true | budget_fit: fetchable | selection_status: recommended\n"
        "- score: 52 (activity=0, design_role=30, draft=0, "
        "explicit_relation=0, focus_match=0, merged=0, recency=10, "
        "release_mention=0, uncovered_module=12)\n"
        "- selection_reason: design_role, from_entity_index, recent, "
        "uncovered_module\n"
        "- raw: -\n"
        "- evidence: -\n"
        "- missing: no_activity_metadata, no_evidence_yet, "
        "no_explicit_relation, release_mention_absent\n"
        "\n"
        "### Preview\n"
        "\n"
        "- preview unavailable; use the raw refs and evidence above.\n"
        "- preview: 0/0 chars\n"
        "- full content: -\n"
    )
    assert text == expected


def test_pack_index_candidate_reference_audit(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(tmp_path)
    payload = packs.build_pack_index(store, config, manifest, candidates)
    packs.write_pack_index(store, payload)
    tampered = json.loads(json.dumps(candidates))
    tampered["candidates"] = []

    problems = packs.audit_pack_index(store, candidates=tampered)

    assert any(item["problem"] == "missing_pack_candidate" for item in problems)


def test_pack_items_summarize_evidence_and_keep_previews(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(tmp_path)
    for index, item in enumerate(candidates["candidates"]):
        item["evidence_ids"] = [f"ev-{index:04d}{n:04d}" for n in range(60)]

    payload = packs.build_pack_index(store, config, manifest, candidates)

    assert payload["document"]["counts"]["preview_cap"] >= 100
    text = next(iter(payload["texts"].values()))
    assert "…(+52)" in text
    assert len(text) < 6000


def test_drop_priority_prefers_unread_candidates_within_a_tier() -> None:
    read = {
        "candidate_id": "cand-read",
        "deep_read": True,
        "score": 999,
        "score_components": {},
        "kind": "issue",
    }
    unread = {
        "candidate_id": "cand-unread",
        "deep_read": False,
        "score": 1,
        "score_components": {},
        "kind": "issue",
    }

    assert packs._drop_priority(read, research_handoff=False) < packs._drop_priority(
        unread, research_handoff=False
    )


def test_deep_read_design_doc_survives_before_unread_low_score_pr(tmp_path: Path) -> None:
    store, config, manifest, candidates = candidates_for(
        tmp_path,
        max_pack_chars=900,
    )
    for item in candidates["candidates"]:
        item["agent_selected"] = False
        item["recommended"] = False
        item["selection_lane"] = ""
        item["score"] = 1
        item["deep_read"] = False
        item["score_components"] = {}
    doc = next(item for item in candidates["candidates"] if item["kind"] == "doc")
    doc["deep_read"] = True
    doc["score"] = 1
    doc["score_components"] = {"design_role": 30}
    pr = next(
        item for item in candidates["candidates"] if item["kind"] == "pull_request"
    )
    pr["deep_read"] = False
    pr["score"] = 999
    pr["score_components"] = {}

    payload = packs.build_pack_index(store, config, manifest, candidates)

    included = {
        item["candidate_id"]
        for pack in payload["document"]["packs"]
        for item in pack["items"]
    }
    assert doc["candidate_id"] in included
    assert pr["candidate_id"] not in included


def test_handoff_keeps_discovery_when_research_fills_budget(tmp_path):
    store, config, manifest, candidates = candidates_for(tmp_path, max_pack_chars=10000)
    seed = candidates["candidates"][0]
    values = []
    for i in range(30):
        item = copy.deepcopy(seed)
        item.update(candidate_id=f"cand-{i}", entity_id=f"github.com/acme/demo/pull/{i+1000}",
                    agent_selected=i == 0, deep_read=True, module="solver", score=100)
        values.append(item)
    discovery = copy.deepcopy(seed)
    discovery.update(candidate_id="cand-discovery", entity_id="github.com/acme/demo/pull/9999",
                     agent_selected=False, deep_read=False, module="parser", score=1)
    values.append(discovery)
    candidates["candidates"] = values
    payload = packs.build_pack_index(store, config, manifest, candidates, research_handoff=True)
    included = {i["candidate_id"] for p in payload["document"]["packs"] for i in p["items"]}
    assert "cand-discovery" in included
    assert "cand-0" in included
    assert payload["document"]["budget"]["used_chars"] <= 10000


def test_excerpts_prioritize_revisions_and_maintainer_limits_over_template_and_bot(
    tmp_path, monkeypatch,
):
    store, config, manifest, candidates = candidates_for(tmp_path)
    candidate = candidates["candidates"][0]
    candidate["agent_selected"] = True
    entity_id = candidate["entity_id"]
    rows = [
        ("template", "pull_request_body", "<!-- Thank you for contributing -->", "2026-10-01"),
        ("summary", "pull_request_body", "We add a diagnostic baseline.", "2026-10-01"),
        ("old", "pull_request_body", "Old performance table: " + "x"*800, "2026-10-01"),
        ("revision", "pull_request_body", "~~Old algorithm~~ I switched to range matching instead.",
         "2026-10-01"),
        ("bot", "pull_request_comment", "Benchmark table " + "x"*2000, "2026-10-30"),
        ("early", "pull_request_comment", "Looks good to me!", "2026-10-01"),
        ("maintainer", "pull_request_comment",
         "However, this increases binary size because both backends remain.",
         "2026-10-29"),
        ("late", "pull_request_comment",
         "Not yet implemented: async functions remain out of scope.",
         "2026-10-30"),
    ]
    metadata = {}
    evidence = []
    for i, (identifier, kind, text, date) in enumerate(rows):
        digest = hashlib.sha256(text.encode()).hexdigest()
        if identifier == "bot":
            metadata[digest] = -1
        if identifier == "maintainer":
            metadata[digest] = 2
        evidence.append(make_evidence(
            evidence_id=identifier, entity_id=entity_id,
            source_url="https://github.com/acme/demo/pull/1", source_kind=kind,
            observed_at=ranking_helpers.NOW, source_event_at=date+"T00:00:00Z",
            text=text, blob_sha256=digest,
            locator=make_locator("char_range", char_start=i*3000,
                                 char_end=i*3000+len(text), line_start=i+1, line_end=i+1),
        ))
    store.write_jsonl("normalized/evidence.jsonl", evidence, key="id")
    monkeypatch.setattr(packs, "_excerpt_metadata", lambda _: metadata)
    previews = packs._previews(store, candidates, research_handoff=True)[entity_id]
    ids = [p["evidence_id"] for p in previews]
    assert ids[:4] == ["summary", "revision", "maintainer", "late"]
    assert "template" not in ids and "bot" not in ids
    # Extracts are still literal substrings with unchanged evidence locators.
    assert all(p["text"] in next(e["text"] for e in evidence if e["id"] == p["evidence_id"])
               for p in previews)
