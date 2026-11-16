from __future__ import annotations

import copy
import json
from pathlib import Path

import test_normalize as normalize_helpers

from rer import ranking
from rer.config import build_config
from rer.models import (
    build_manifest,
    entity_id,
    make_entity,
    make_evidence,
    make_locator,
    make_relation,
    validate_document,
)

NOW = "2026-11-01T08:00:00Z"
SHA = "a" * 40
HOST = "github.com"
OWNER = "acme"
NAME = "demo"


def entity(kind: str, suffix: str, **kwargs) -> dict:
    return make_entity(
        entity_id=entity_id(HOST, OWNER, NAME, kind, suffix),
        kind=kind,
        title=kwargs.pop("title", f"{kind} {suffix}"),
        url=f"https://github.com/acme/demo/{suffix}",
        observed_at=NOW,
        source_event_at=kwargs.pop("source_event_at", NOW),
        **kwargs,
    )


def seed_first_round(tmp_path: Path, *, budget: dict | None = None) -> tuple:
    store = normalize_helpers.make_empty_run(tmp_path)
    revision = "b" * 40
    entities = [
        entity(
            "pull_request",
            "101",
            number=101,
            title="Cache planner",
            roles=["implementation"],
            raw_state={"merged_at": "2026-10-20T10:00:00Z", "comments": 2},
        ),
        entity(
            "pull_request",
            "102",
            number=102,
            title="Draft cache follow-up",
            raw_state={"draft": True},
        ),
        entity(
            "issue",
            "40",
            number=40,
            title="RFC: cache lifecycle",
            roles=["rfc", "design"],
            raw_state={"labels": ["area: cache"], "comments": 4},
            normalized_state={"scope": "in_window", "role_hints": ["rfc", "design"]},
        ),
        entity(
            "discussion",
            "7",
            number=7,
            title="Design: cache ownership",
            roles=["design"],
            raw_state={"labels": ["design"], "upvote_count": 5},
            normalized_state={"scope": "in_window", "role_hints": ["design"]},
        ),
        entity(
            "issue",
            "55",
            number=55,
            title="Related cache question",
            raw_state={"labels": []},
            normalized_state={"scope": "context"},
        ),
        entity(
            "release",
            "v1.0.0",
            title="v1.0.0",
            roles=["release_notes"],
            raw_state={"release_id": 1, "tag_name": "v1.0.0"},
        ),
        make_entity(
            entity_id=entity_id(
                HOST,
                OWNER,
                NAME,
                "doc",
                "docs/design/cache.md",
                revision=revision,
            ),
            kind="doc",
            title="Cache design",
            url=f"https://github.com/acme/demo/blob/{revision}/docs/design/cache.md",
            observed_at=NOW,
            roles=["design"],
            raw_state={"revision": revision, "path": "docs/design/cache.md"},
            normalized_state={"revision": revision, "path": "docs/design/cache.md"},
        ),
    ]
    store.write_jsonl("normalized/entities.jsonl", entities, key="id", schema_name="entity")
    store.write_jsonl(
        "raw/prs.jsonl",
        [
            {
                "number": 101,
                "title": "Cache planner",
                "merged_at": "2026-10-20T10:00:00Z",
                "comments": 2,
                "labels": [{"name": "area: cache"}],
            },
            {"number": 102, "title": "Draft cache follow-up", "draft": True},
        ],
        key="number",
    )
    store.write_jsonl(
        "raw/issues.jsonl",
        [
            {"number": 40, "title": "RFC: cache lifecycle", "labels": ["area: cache"]},
            {"number": 55, "title": "Related cache question", "labels": []},
        ],
        key="number",
    )
    store.write_jsonl(
        "raw/discussions.jsonl",
        [{"number": 7, "title": "Design: cache ownership", "upvote_count": 5}],
        key="number",
    )
    store.write_json(
        "analysis/pr-selection.json",
        {
            "candidates": [
                {
                    "entity_id": "github.com/acme/demo/pull/101",
                    "selection_reason": ["merged_in_window"],
                },
                {
                    "entity_id": "github.com/acme/demo/pull/102",
                    "selection_reason": ["focus_match:cache"],
                },
            ]
        },
    )
    store.write_json(
        "analysis/proposal-selection.json",
        {
            "candidates": [
                {
                    "carrier": "issue",
                    "entity_id": "github.com/acme/demo/issue/40",
                    "selection_reason": ["role_hint:rfc"],
                },
                {
                    "carrier": "discussion",
                    "entity_id": "github.com/acme/demo/discussion/7",
                    "selection_reason": ["role_hint:design"],
                },
            ]
        },
    )
    store.write_json(
        "analysis/activity-overview.json",
        {
            "views": [
                {
                    "kind": "pull_request",
                    "candidate_ids": ["github.com/acme/demo/pull/101"],
                },
                {
                    "kind": "issue",
                    "candidate_ids": [
                        "github.com/acme/demo/issue/40",
                        "github.com/acme/demo/issue/999",
                    ],
                },
            ]
        },
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        [
            make_relation(
                relation_id="rel-release",
                from_id="github.com/acme/demo/release/v1.0.0",
                to_id="github.com/acme/demo/pull/101",
                relation_type="release_note_mentions",
                basis="text_explicit",
                verification="verified",
                evidence_ids=["ev-release"],
            ),
            make_relation(
                relation_id="rel-neighbor",
                from_id="github.com/acme/demo/pull/101",
                to_id="github.com/acme/demo/issue/55",
                relation_type="mentions",
                basis="text_explicit",
                verification="verified",
                evidence_ids=["ev-101"],
            ),
        ],
        key="id",
        schema_name="relation",
    )
    evidence = []
    for identifier in (
        "github.com/acme/demo/pull/101",
        "github.com/acme/demo/issue/40",
        "github.com/acme/demo/release/v1.0.0",
    ):
        evidence.append(
            make_evidence(
                evidence_id="ev-" + identifier.rsplit("/", 1)[-1],
                entity_id=identifier,
                source_url=f"https://example.com/{identifier}",
                source_kind="pull_request_body",
                observed_at=NOW,
                text="cache planner",
                locator=make_locator(
                    "char_range",
                    char_start=0,
                    char_end=13,
                    line_start=1,
                    line_end=1,
                ),
                blob_sha256="0" * 64,
            )
        )
    store.write_jsonl(
        "normalized/evidence.jsonl",
        evidence,
        key="id",
        schema_name="evidence",
    )
    config = build_config(
        repository="acme/demo",
        mode="standard",
        timezone_name="UTC",
        focus=["cache"],
        now=None,
        budget_overrides={"max_deep_entities": 8, **(budget or {})},
    )
    manifest = build_manifest(config, "20261101T080000Z-abcdef12", NOW)
    manifest["collection_finished_at"] = NOW
    manifest["budget_consumed"]["deep_entities"] = 0
    return store, config, manifest


def test_candidates_cover_all_sources_and_are_deterministic(tmp_path: Path) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    first = ranking.build_candidates(store, config, manifest)
    second = ranking.build_candidates(store, config, manifest)

    assert json.dumps(first, ensure_ascii=False) == json.dumps(second, ensure_ascii=False)
    validate_document(first, "candidates")
    by_entity = {item["entity_id"]: item for item in first["candidates"]}
    assert by_entity["github.com/acme/demo/pull/101"]["bucket"] == "merged_pr"
    assert by_entity["github.com/acme/demo/pull/101"]["module"] == "cache"
    assert by_entity["github.com/acme/demo/issue/40"]["bucket"] == "design"
    assert by_entity["github.com/acme/demo/discussion/7"]["bucket"] == "design"
    assert by_entity["github.com/acme/demo/release/v1.0.0"]["bucket"] == "release_doc"
    assert (
        by_entity[
            "github.com/acme/demo/" + "b" * 40 + "/docs/design/cache.md"
        ][
            "bucket"
        ]
        == "design"
    )
    # A plain issue only joins through the bounded one-hop relation expansion.
    neighbor_reasons = by_entity["github.com/acme/demo/issue/55"]["selection_reason"]
    assert "from_relation_neighbor" in neighbor_reasons
    assert "relation_neighbor" in neighbor_reasons
    assert by_entity["github.com/acme/demo/issue/55"]["score_components"][
        "explicit_relation"
    ] == 8
    assert by_entity["github.com/acme/demo/pull/101"]["score_components"][
        "release_mention"
    ] == 20
    assert "from_activity_overview" in by_entity["github.com/acme/demo/pull/101"][
        "selection_reason"
    ]
    assert first["counts"]["missing_activity_entities"] == 1
    assert first["quota"]["exploration_reserved"] >= 1
    assert first["counts"]["recommended_exploration"] >= 1
    assert first["counts"]["recommended_focus"] <= first["quota"]["focus_cap"]


def test_activity_is_counted_once_across_entity_and_raw_state(tmp_path: Path) -> None:
    store, config, manifest = seed_first_round(tmp_path)

    document = ranking.build_candidates(store, config, manifest)

    candidate = next(
        item
        for item in document["candidates"]
        if item["entity_id"] == "github.com/acme/demo/pull/101"
    )
    assert candidate["score_components"]["activity"] == 6


def test_focus_match_requires_token_boundaries() -> None:
    record = {
        "entity": {
            "id": "github.com/acme/demo/issue/1",
            "title": "Main scheduler",
            "roles": [],
            "normalized_state": {},
        },
        "module": "scheduler",
        "merged": False,
        "draft": False,
        "labels": [],
        "updated_at": NOW,
    }

    _score, components = ranking._score(
        record,
        relation_counts={},
        relation_types={},
        covered_modules=set(),
        focus=["ai"],
        window_end=None,
    )

    assert components["focus_match"] == 0


def test_doc_deep_read_requires_read_status() -> None:
    revision = "b" * 40
    entity = make_entity(
        entity_id=entity_id(
            HOST,
            OWNER,
            NAME,
            "doc",
            "docs/design/cache.md",
            revision=revision,
        ),
        kind="doc",
        title="Cache design",
        url=f"https://github.com/acme/demo/blob/{revision}/docs/design/cache.md",
        observed_at=NOW,
        raw_state={"revision": revision, "path": "docs/design/cache.md"},
        normalized_state={
            "revision": revision,
            "path": "docs/design/cache.md",
            "status": "unavailable",
        },
    )

    assert ranking._deep_read(entity) is False


def test_explicit_selection_survives_candidate_limit(tmp_path: Path, monkeypatch) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    monkeypatch.setattr(ranking, "MIN_CANDIDATE_LIMIT", 2)
    monkeypatch.setattr(ranking, "CANDIDATE_MULTIPLIER", 0)
    selection = {
        "entities": [
            {
                "entity_id": "github.com/acme/demo/issue/55",
                "reason": "selected by theme",
                "depth": "deep",
                "read": {
                    "body": True,
                    "comments": True,
                    "reviews": False,
                    "files": False,
                    "diff": False,
                },
            }
        ]
    }

    document = ranking.build_candidates(
        store,
        config,
        manifest,
        agent_selection=selection,
    )

    selected = next(
        item
        for item in document["candidates"]
        if item["entity_id"] == "github.com/acme/demo/issue/55"
    )
    assert selected["agent_selected"] is True
    assert document["counts"]["agent_selected"] == 1
    assert document["counts"]["agent_selected_omitted"] == 0


def test_deep_read_survives_candidate_limit(tmp_path: Path, monkeypatch) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    monkeypatch.setattr(ranking, "MIN_CANDIDATE_LIMIT", 2)
    monkeypatch.setattr(ranking, "CANDIDATE_MULTIPLIER", 0)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for item in entities:
        if item["id"] == "github.com/acme/demo/pull/102":
            item["raw_state"]["deep_read"] = {"detail_path": "raw/pr-details/102.json"}
    store.write_jsonl("normalized/entities.jsonl", entities, key="id", schema_name="entity")

    document = ranking.build_candidates(store, config, manifest)

    kept = next(
        item
        for item in document["candidates"]
        if item["entity_id"] == "github.com/acme/demo/pull/102"
    )
    assert kept["deep_read"] is True
    assert kept["selection_status"] == "already_read"
    assert document["counts"]["deep_read_omitted"] == 0


def test_recommendation_respects_exhausted_pr_budget(tmp_path: Path, monkeypatch) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    monkeypatch.setattr(
        ranking,
        "load_pr_documents",
        lambda _store: [{"number": number} for number in range(1, 23)],
    )

    document = ranking.build_candidates(store, config, manifest)

    assert document["budget"]["remaining_deep_prs"] == 0
    assert document["counts"]["recommended_merged_pr"] == 0
    assert all(
        item["budget_fit"] == "blocked_pr_budget"
        for item in document["candidates"]
        if item["kind"] == "pull_request"
    )
    assert not any(
        item["recommended"] and item["kind"] == "pull_request"
        for item in document["candidates"]
    )
    assert "deep_pr_budget_exhausted" in document["limitations"]


def test_pr_budget_fit_checks_entity_budget_first() -> None:
    assert (
        ranking._budget_fit(
            {"entity": {"kind": "pull_request"}},
            remaining_entities=0,
            remaining_prs=5,
            deep_read=False,
        )
        == "blocked_entity_budget"
    )
    assert (
        ranking._budget_fit(
            {"entity": {"kind": "pull_request"}},
            remaining_entities=1,
            remaining_prs=0,
            deep_read=False,
        )
        == "blocked_pr_budget"
    )


def test_recommendation_uses_remaining_pr_slots(tmp_path: Path, monkeypatch) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    monkeypatch.setattr(
        ranking,
        "load_pr_documents",
        lambda _store: [{"number": number} for number in range(1, 22)],
    )

    document = ranking.build_candidates(store, config, manifest)

    assert document["budget"]["remaining_deep_prs"] == 1
    recommended_prs = [
        item
        for item in document["candidates"]
        if item["recommended"] and item["kind"] == "pull_request"
    ]
    assert len(recommended_prs) == 1


def test_candidate_budget_truncation_is_visible(tmp_path: Path, monkeypatch) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    monkeypatch.setattr(ranking, "MIN_CANDIDATE_LIMIT", 3)
    monkeypatch.setattr(ranking, "CANDIDATE_MULTIPLIER", 0)

    document = ranking.build_candidates(store, config, manifest)

    assert document["counts"]["candidates_omitted"] > 0
    assert document["counts"]["candidates_emitted"] == 3
    assert "candidate_budget_truncated" in document["limitations"]


def test_focus_cap_limits_recommended_focus(tmp_path: Path) -> None:
    store, config, manifest = seed_first_round(
        tmp_path,
        budget={"max_deep_entities": 2},
    )

    document = ranking.build_candidates(store, config, manifest)

    focus = [
        item
        for item in document["candidates"]
        if item["recommended"]
        and item["score_components"]["focus_match"] > 0
    ]
    assert len(focus) <= document["quota"]["focus_cap"] == 1


def test_deep_budget_exhaustion_is_visible(tmp_path: Path) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    manifest["budget_consumed"]["deep_entities"] = 8

    document = ranking.build_candidates(store, config, manifest)

    assert document["quota"]["total"] == 0
    assert document["counts"]["recommended"] == 0
    assert "deep_budget_exhausted" in document["limitations"]


def test_candidate_schema_rejects_probability_fields(tmp_path: Path) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    document = ranking.build_candidates(store, config, manifest)
    tampered = copy.deepcopy(document)
    tampered["candidates"][0]["confidence"] = 0.97

    try:
        validate_document(tampered, "candidates")
    except Exception as exc:
        assert getattr(exc, "code", None) in ("schema_error", "validation_failed")
    else:  # pragma: no cover - schema must reject unknown fields
        raise AssertionError("candidate schema accepted an undocumented confidence field")


def test_prepare_writes_candidates_and_packs_idempotently(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import test_pull_requests as pr_helpers

    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    normalize_helpers.prepare(store)
    candidates_bytes = store.secure_path("analysis/candidates.json").read_bytes()
    index_bytes = store.secure_path("analysis/pack-index.json").read_bytes()
    pack_bytes = {
        path.name: path.read_bytes()
        for path in store.secure_path("analysis/packs").glob("*.md")
    }
    assert pack_bytes
    stale = store.secure_path("analysis/packs/part-999.md")
    stale.write_text("stale", encoding="utf-8")

    normalize_helpers.prepare(store)

    assert store.secure_path("analysis/candidates.json").read_bytes() == candidates_bytes
    assert store.secure_path("analysis/pack-index.json").read_bytes() == index_bytes
    assert {
        path.name: path.read_bytes()
        for path in store.secure_path("analysis/packs").glob("*.md")
    } == pack_bytes
    assert not stale.exists()
    validate_document(
        store.read_json("analysis/candidates.json"),
        "candidates",
    )
    validate_document(
        store.read_json("analysis/pack-index.json"),
        "pack_index",
    )
    assert store.verify_integrity()["ok"] is True


def test_disputed_duplicate_relation_identity_is_not_expanded(
    tmp_path: Path,
) -> None:
    store, config, manifest = seed_first_round(tmp_path)
    issue_id = entity_id(HOST, OWNER, NAME, "issue", "55")
    baseline = ranking.build_candidates(store, config, manifest)
    assert issue_id in {item["entity_id"] for item in baseline["candidates"]}

    relations = store.read_jsonl("normalized/relations.jsonl")
    relations.append(
        make_relation(
            relation_id="rel-neighbor-disputed",
            from_id="github.com/acme/demo/pull/101",
            to_id=issue_id,
            relation_type="mentions",
            basis="text_explicit",
            verification="disputed",
            evidence_ids=["ev-101"],
        )
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        relations,
        key="id",
        schema_name="relation",
    )

    after = ranking.build_candidates(store, config, manifest)

    assert issue_id not in {item["entity_id"] for item in after["candidates"]}


def test_verify_integrity_reports_candidate_reference_breakage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import test_pull_requests as pr_helpers

    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    normalize_helpers.prepare(store)
    document = store.read_json("analysis/candidates.json")
    document["candidates"][0]["entity_id"] = "github.com/acme/demo/pull/9999"
    store.write_json("analysis/candidates.json", document)

    report = store.verify_integrity()

    problems = {item["problem"] for item in report["problems"]}
    assert "missing_candidate_entity" in problems
    assert "candidate_id_mismatch" in problems


def test_closed_unmerged_pr_is_not_an_open_proposal(tmp_path):
    store, config, manifest = seed_first_round(tmp_path)
    records = store.read_jsonl("raw/prs.jsonl")
    for record in records:
        if record["number"] == 102:
            record["state"] = "closed"
    store.write_jsonl("raw/prs.jsonl", records, key="number")
    result = ranking.build_candidates(store, config, manifest)
    validate_document(result, "candidates")
    candidate = next(c for c in result["candidates"] if c["entity_id"].endswith("/pull/102"))
    assert candidate["bucket"] == "closed_unmerged"
    assert candidate["selection_lane"] != "open_proposal"


def test_closed_pr_does_not_consume_open_selection_quota():
    from rer.collectors.selection import Candidate, choose

    def item(number, bucket, score):
        return Candidate(number, f"pr-{number}", {}, bucket, score, {}, [])

    closed, opened, merged = item(1, "closed", 1000), item(2, "open", 10), item(3, "merged", 20)
    choose([closed, merged, opened], 2, 1, 1)
    assert opened.selected and merged.selected
    assert not closed.selected


def test_default_pr_selection_reserves_other_title_areas():
    from rer.collectors.selection import Candidate, choose

    candidates = [
        Candidate(i, f"pr-{i}", {"title": f"[ty] solver {i}"}, "merged", 100-i, {}, [])
        for i in range(12)
    ]
    other = Candidate(30, "pr-30", {"title": "Optimize lexer"}, "merged", 5, {}, [])
    candidates.append(other)
    choose(candidates, 6, 6, 0)
    assert other.selected
    assert sum(c.selected for c in candidates) == 6


def test_title_area_is_a_hint_not_a_source_path(tmp_path):
    store, config, manifest = seed_first_round(tmp_path)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for e in entities:
        if e.get("number") == 102:
            e["title"] = "[ty] Refine type inference"
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    raw = store.read_jsonl("raw/prs.jsonl")
    for e in raw:
        if e.get("number") == 102:
            e["title"] = "[ty] Refine type inference"
    store.write_jsonl("raw/prs.jsonl", raw, key="number")
    result = ranking.build_candidates(store, config, manifest)
    value = next(c for c in result["candidates"] if c["entity_id"].endswith("/pull/102"))
    assert value["module"] == "title:ty"
    assert value["module_source"] == "title"


def test_external_reference_never_inherits_local_same_number_metadata(tmp_path):
    from rer.models import make_relation

    store, config, manifest = seed_first_round(tmp_path)
    external = make_entity(
        entity_id="github.com/other/lib/pull/101", kind="pull_request", title="#101",
        url="https://github.com/other/lib/pull/101", observed_at=NOW,
        number=101, external=True,
    )
    entities = store.read_jsonl("normalized/entities.jsonl") + [external]
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    relations = [make_relation(
        relation_id="rel-external", from_id="github.com/acme/demo/pull/101",
        to_id=external["id"], relation_type="mentions", basis="text_explicit",
        verification="candidate", evidence_ids=[],
    )]
    result = ranking.build_candidates(store, config, manifest, relations=relations)
    candidate = next(c for c in result["candidates"] if c["entity_id"] == external["id"])
    assert candidate["bucket"] != "merged_pr"
    assert candidate["module"] == "external"
    assert not candidate["recommended"]
    assert "external_reference_only" in candidate["selection_reason"]


def test_monorepo_default_reads_preserve_untagged_parser_formatter_and_wasm():
    from rer.collectors.selection import Candidate, choose

    titles = [f"[ty] constraint solver {i}" for i in range(20)] + [
        "Speed up identifier lexing", "Reduce formatter work", "Shrink WebAssembly output",
    ]
    candidates = [Candidate(i, f"pr-{i}", {"title": title}, "merged", 100-i, {}, [])
                  for i, title in enumerate(titles)]
    choose(candidates, 8, 8, 0)
    assert all(c.selected for c in candidates[-3:])
    assert sum(c.selected for c in candidates) == 8
