from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from rer.errors import ValidationFailure
from rer.models import (
    entity_id,
    format_checker_status,
    is_entity_id,
    make_entity,
    make_evidence,
    make_locator,
    validate_document,
    verify_evidence_quote,
)

FIXTURES = Path(__file__).parent / "fixtures" / "example"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("schema_name", "fixture"),
    [
        ("config", "config.sample.json"),
        ("manifest", "manifest.sample.json"),
        ("entity", "entity.sample.json"),
        ("evidence", "evidence.sample.json"),
        ("relation", "relation.sample.json"),
        ("selection", "selection.sample.json"),
        ("research", "research.sample.json"),
        ("source_profile", "source_profile.sample.json"),
        ("activity_overview", "activity_overview.sample.json"),
        ("pr_selection", "pr_selection.sample.json"),
        ("pr_detail", "pr_detail.sample.json"),
        ("release_detail", "release_detail.sample.json"),
        ("doc_snapshot", "doc_snapshot.sample.json"),
        ("issue_detail", "issue_detail.sample.json"),
        ("discussion_detail", "discussion_detail.sample.json"),
        ("proposal_selection", "proposal_selection.sample.json"),
    ],
)
def test_valid_fixtures(schema_name: str, fixture: str) -> None:
    validate_document(load(fixture), schema_name)


def test_old_config_without_new_fields_still_validates() -> None:
    config = copy.deepcopy(load("config.sample.json"))
    config["budget"].pop("max_deep_prs")
    validate_document(config, "config")

    manifest = copy.deepcopy(load("manifest.sample.json"))
    manifest["budget"].pop("max_deep_prs")
    validate_document(manifest, "manifest")


def test_all_fixture_entities_validate() -> None:
    for entity in load("entities.sample.json"):
        validate_document(entity, "entity")


def test_fixture_unknowns_are_only_an_index() -> None:
    research = load("research.sample.json")
    claims = {claim["id"]: claim for claim in research["claims"]}
    for unknown in research["unknowns"]:
        assert claims[unknown["claim_id"]]["kind"] == "unknown"


def test_fixture_capability_state_is_backed_by_entity() -> None:
    issue = next(
        entity for entity in load("entities.sample.json") if entity["kind"] == "issue"
    )
    capability = load("research.sample.json")["capabilities"][0]
    assert capability["proposal_state"] == "proposed"
    assert issue["normalized_state"]["proposal_state"] == "proposed"


@pytest.mark.parametrize(
    ("schema_name", "fixture", "mutate"),
    [
        ("entity", "entity.sample.json", lambda d: d.pop("title")),
        ("entity", "entity.sample.json", lambda d: d.pop("node_id")),
        ("entity", "entity.sample.json", lambda d: d.update(kind="pr")),
        ("evidence", "evidence.sample.json", lambda d: d.update(blob_sha256="abc")),
        ("evidence", "evidence.sample.json", lambda d: d.update(completeness="done")),
        ("evidence", "evidence.sample.json", lambda d: d.update(verification="maybe")),
        ("relation", "relation.sample.json", lambda d: d.update(type="fixes")),
        ("relation", "relation.sample.json", lambda d: d.update(basis="guess")),
        ("selection", "selection.sample.json", lambda d: d["entities"][0].update(depth="medium")),
        ("research", "research.sample.json", lambda d: d["claims"][0].update(kind="fact")),
        (
            "research",
            "research.sample.json",
            lambda d: d["capabilities"][0].update(implementation_state="done"),
        ),
        (
            "research",
            "research.sample.json",
            lambda d: d["topics"][0]["claim_ids"].pop("outlook"),
        ),
        ("config", "config.sample.json", lambda d: d.update(mode="fast")),
        ("manifest", "manifest.sample.json", lambda d: d.update(state="done")),
        ("manifest", "manifest.sample.json", lambda d: d.pop("collection_started_at")),
        (
            "research",
            "research.sample.json",
            lambda d: d["claims"][0].pop("state_time_basis"),
        ),
        (
            "research",
            "research.sample.json",
            lambda d: d["claims"][0].update(state_time_basis="now"),
        ),
        (
            "research",
            "research.sample.json",
            lambda d: d["unknowns"][0].pop("claim_id"),
        ),
        ("source_profile", "source_profile.sample.json", lambda d: d.pop("discovery")),
        (
            "source_profile",
            "source_profile.sample.json",
            lambda d: d["discovery"]["files"][0].update(status="unknown"),
        ),
        (
            "activity_overview",
            "activity_overview.sample.json",
            lambda d: d["views"][0].update(status="done"),
        ),
        (
            "activity_overview",
            "activity_overview.sample.json",
            lambda d: d["views"][0].pop("candidate_ids"),
        ),
        (
            "activity_overview",
            "activity_overview.sample.json",
            lambda d: d["views"][0].pop("shards_detail"),
        ),
        (
            "activity_overview",
            "activity_overview.sample.json",
            lambda d: d["counts"].update(pr_created=-1),
        ),
        (
            "pr_selection",
            "pr_selection.sample.json",
            lambda d: d["candidates"][0].update(status="done"),
        ),
        (
            "pr_selection",
            "pr_selection.sample.json",
            lambda d: d["quota"].pop("max_deep_entities"),
        ),
        (
            "pr_detail",
            "pr_detail.sample.json",
            lambda d: d.pop("files"),
        ),
        (
            "pr_detail",
            "pr_detail.sample.json",
            lambda d: d["files"]["items"][0].update(patch_status="unknown"),
        ),
        (
            "pr_detail",
            "pr_detail.sample.json",
            lambda d: d["body"].update(sha256="not-a-hash"),
        ),
        (
            "pr_detail",
            "pr_detail.sample.json",
            lambda d: d["coverage"].update(detail="done"),
        ),
        (
            "release_detail",
            "release_detail.sample.json",
            lambda d: d["coverage"].update(detail="done"),
        ),
        (
            "release_detail",
            "release_detail.sample.json",
            lambda d: d["tag_ref"].update(status="done"),
        ),
        (
            "release_detail",
            "release_detail.sample.json",
            lambda d: d.update(release_id=0),
        ),
        (
            "release_detail",
            "release_detail.sample.json",
            lambda d: d.pop("tag_moved"),
        ),
        (
            "doc_snapshot",
            "doc_snapshot.sample.json",
            lambda d: d.update(status="done"),
        ),
        (
            "doc_snapshot",
            "doc_snapshot.sample.json",
            lambda d: d.update(revision="abc"),
        ),
        (
            "doc_snapshot",
            "doc_snapshot.sample.json",
            lambda d: d.update(text=None),
        ),
        (
            "doc_snapshot",
            "doc_snapshot.sample.json",
            lambda d: d.update(blob_sha256=None),
        ),
        (
            "doc_snapshot",
            "doc_snapshot.sample.json",
            lambda d: d["text"].update(sha256="not-a-hash"),
        ),
        (
            "issue_detail",
            "issue_detail.sample.json",
            lambda d: d.update(state_reason="done"),
        ),
        (
            "issue_detail",
            "issue_detail.sample.json",
            lambda d: d["coverage"].update(detail="done"),
        ),
        (
            "issue_detail",
            "issue_detail.sample.json",
            lambda d: d.pop("timeline"),
        ),
        (
            "discussion_detail",
            "discussion_detail.sample.json",
            lambda d: d["comments"]["items"][0].update(replies_status="done"),
        ),
        (
            "discussion_detail",
            "discussion_detail.sample.json",
            lambda d: d["coverage"].update(body="done"),
        ),
        (
            "proposal_selection",
            "proposal_selection.sample.json",
            lambda d: d["candidates"][0].update(status="done"),
        ),
        (
            "proposal_selection",
            "proposal_selection.sample.json",
            lambda d: d["quota"].pop("issue_quota"),
        ),
    ],
)
def test_invalid_documents_are_rejected(schema_name: str, fixture: str, mutate) -> None:
    document = copy.deepcopy(load(fixture))
    mutate(document)
    with pytest.raises(ValidationFailure):
        validate_document(document, schema_name)


SHA_A = "a" * 40
SHA_B = "b" * 40


def test_entity_id_forms_are_frozen() -> None:
    assert entity_id("github.com", "o", "r", "pull_request", "101") == "github.com/o/r/pull/101"
    assert entity_id("github.com", "o", "r", "issue", "40") == "github.com/o/r/issue/40"
    assert entity_id("github.com", "o", "r", "discussion", "7") == "github.com/o/r/discussion/7"
    assert (
        entity_id("github.com", "o", "r", "release", "v1.0/rc1")
        == "github.com/o/r/release/v1.0/rc1"
    )
    assert entity_id("github.com", "o", "r", "commit", SHA_A) == f"github.com/o/r/{SHA_A}"
    assert (
        entity_id("github.com", "o", "r", "doc", "docs/design.md", revision=SHA_A)
        == f"github.com/o/r/{SHA_A}/docs/design.md"
    )
    assert (
        entity_id("github.com", "o", "r", "source_file", "src/core.py", revision=SHA_A)
        == f"github.com/o/r/{SHA_A}/src/core.py"
    )
    assert (
        entity_id("github.com", "o", "r", "comment", "123", subtype="issue")
        == "github.com/o/r/comment/issue/123"
    )
    assert (
        entity_id("github.com", "o", "r", "comment", "456", subtype="review")
        == "github.com/o/r/comment/review/456"
    )
    assert entity_id("github.com", "o", "r", "review", "456") == "github.com/o/r/review/456"

    encoded_doc = entity_id(
        "github.com", "o", "r", "doc", "docs/设计 文档+(v1).md", revision=SHA_A
    )
    assert encoded_doc.startswith(f"github.com/o/r/{SHA_A}/docs/%E8%AE%BE%E8%AE%A1%20")
    assert encoded_doc.endswith("%2B%28v1%29.md")
    assert is_entity_id(encoded_doc)
    encoded_tag = entity_id("github.com", "o", "r", "release", "v1.0+build(1)")
    assert encoded_tag == "github.com/o/r/release/v1.0%2Bbuild%281%29"

    for kind, value in (("doc", encoded_doc), ("release", encoded_tag)):
        entity = copy.deepcopy(load("entity.sample.json"))
        entity["kind"] = kind
        entity["id"] = value
        validate_document(entity, "entity")

    assert entity_id("github.com", "o", "r", "doc", "x.md", revision=SHA_A) != entity_id(
        "github.com", "o", "r", "doc", "x.md", revision=SHA_B
    )


def test_entity_ids_reject_trailing_newline() -> None:
    entity = copy.deepcopy(load("entity.sample.json"))
    entity["id"] = entity["id"] + "\n"
    with pytest.raises(ValidationFailure):
        validate_document(entity, "entity")
    assert not is_entity_id("github.com/o/r/issue/40\n")
    assert not is_entity_id("github.com/o/r\n")


def test_schema_and_tool_versions_are_bumped() -> None:
    from rer import SCHEMA_VERSION, TOOL_VERSION

    assert SCHEMA_VERSION == "1.3"
    assert TOOL_VERSION == "0.2.8"
    assert load("config.sample.json")["schema_version"] == SCHEMA_VERSION
    assert load("research.sample.json")["schema_version"] == SCHEMA_VERSION


def test_schema_accepts_legacy_1_2_snapshot() -> None:
    research = copy.deepcopy(load("research.sample.json"))
    research["schema_version"] = "1.2"
    validate_document(research, "research")


def test_entity_id_rejects_unsafe_or_unfrozen_forms() -> None:
    assert is_entity_id("github.com/o/r/issue/40")
    assert is_entity_id(f"github.com/o/r/{SHA_A}")
    assert is_entity_id(f"github.com/o/r/{SHA_A}/docs/design.md")
    assert is_entity_id("github.com/o/r")
    assert not is_entity_id("github.com/o")
    assert not is_entity_id("github.com/o/r/doc/../..")
    assert not is_entity_id(f"github.com/o/r/{SHA_A}/../secret")
    assert not is_entity_id(f"github.com/o/r/commit/{SHA_A}")
    assert not is_entity_id("github.com/o/r/comment/123")
    with pytest.raises(ValueError):
        entity_id("github.com", "o", "r", "pull_request", "../1")
    with pytest.raises(ValueError):
        entity_id("github.com", "o", "r", "doc", "x.md", revision=SHA_A + "/../x")
    with pytest.raises(ValueError):
        entity_id("github.com", "o", "r", "comment", "abc")
    with pytest.raises(ValueError):
        entity_id("github.com", "o", "r", "comment", "123")
    with pytest.raises(ValueError):
        entity_id("github.com", "o", "r", "commit", "not-a-sha")


def test_entity_schema_accepts_frozen_id_forms() -> None:
    base = load("entity.sample.json")
    forms = [
        ("commit", f"github.com/o/r/{SHA_A}"),
        ("doc", f"github.com/o/r/{SHA_A}/docs/design.md"),
        ("source_file", f"github.com/o/r/{SHA_A}/src/core.py"),
        ("comment", "github.com/o/r/comment/issue/123"),
        ("comment", "github.com/o/r/comment/review/456"),
        ("review", "github.com/o/r/review/456"),
        ("release", "github.com/o/r/release/v1.0/rc1"),
    ]
    for kind, value in forms:
        entity = copy.deepcopy(base)
        entity["id"] = value
        entity["kind"] = kind
        validate_document(entity, "entity")


def test_entity_schema_rejects_traversal_id() -> None:
    entity = copy.deepcopy(load("entity.sample.json"))
    entity["id"] = "github.com/o/r/doc/../.."
    with pytest.raises(ValidationFailure):
        validate_document(entity, "entity")


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("commit", f"github.com/o/r/{SHA_A}/docs/design.md"),
        ("doc", f"github.com/o/r/{SHA_A}"),
        ("comment", "github.com/o/r/comment/123"),
        ("review", "github.com/o/r/comment/review/456"),
    ],
)
def test_entity_schema_rejects_kind_id_mismatch(kind: str, value: str) -> None:
    entity = copy.deepcopy(load("entity.sample.json"))
    entity["kind"] = kind
    entity["id"] = value
    with pytest.raises(ValidationFailure):
        validate_document(entity, "entity")


def test_evidence_and_manifest_patterns_reject_newline() -> None:
    evidence = copy.deepcopy(load("evidence.sample.json"))
    evidence["blob_sha256"] += "\n"
    with pytest.raises(ValidationFailure):
        validate_document(evidence, "evidence")

    manifest = copy.deepcopy(load("manifest.sample.json"))
    manifest["run_id"] += "\n"
    with pytest.raises(ValidationFailure):
        validate_document(manifest, "manifest")


def test_invalid_format_values_are_rejected() -> None:
    for field, value in (
        ("observed_at", "not-a-time"),
        ("url", "not a uri"),
        ("created_at", "2026-13-40T99:99:99Z"),
    ):
        entity = copy.deepcopy(load("entity.sample.json"))
        entity[field] = value
        with pytest.raises(ValidationFailure):
            validate_document(entity, "entity")


def test_format_checkers_are_registered() -> None:
    assert format_checker_status() == {"date-time": True, "uri": True}


def test_make_entity_rejects_kind_id_mismatch() -> None:
    with pytest.raises(ValueError):
        make_entity(
            entity_id=f"github.com/o/r/{SHA_A}/docs/design.md",
            kind="commit",
            title="Wrong kind",
            url="https://github.com/o/r",
            observed_at="2026-11-01T08:00:00Z",
        )


def test_make_entity_validates() -> None:
    entity = make_entity(
        entity_id="github.com/o/r/issue/1",
        kind="issue",
        title="Example",
        url="https://github.com/o/r/issues/1",
        observed_at="2026-11-01T08:00:00Z",
        roles=["rfc"],
    )
    validate_document(entity, "entity")


def test_make_evidence_validates() -> None:
    evidence = make_evidence(
        evidence_id="ev-1",
        entity_id="github.com/o/r/issue/1",
        source_url="https://github.com/o/r/issues/1",
        source_kind="issue_body",
        observed_at="2026-11-01T08:00:00Z",
        text="hello",
        locator=make_locator("char_range", char_start=0, char_end=5),
        blob_sha256="a" * 64,
    )
    validate_document(evidence, "evidence")
    assert evidence["verification"] == "unverifiable"


def test_evidence_quote_matches_json_pointer() -> None:
    evidence = load("evidence.sample.json")
    payload = load("payloads.json")["ev-001"].encode("utf-8")
    matched, reason = verify_evidence_quote(evidence, payload)
    assert matched, reason

    mutated = copy.deepcopy(evidence)
    mutated["text"] = "Different quote"
    matched, reason = verify_evidence_quote(mutated, payload)
    assert not matched
    assert reason == "quote_mismatch"


def test_evidence_quote_pointer_null_is_a_clean_miss() -> None:
    evidence = copy.deepcopy(load("evidence.sample.json"))
    evidence["locator"]["pointer"] = None
    matched, reason = verify_evidence_quote(evidence, b'{"body": "x"}')
    assert not matched
    assert reason == "pointer_missing"

    evidence["locator"]["pointer"] = 42
    matched, reason = verify_evidence_quote(evidence, b'{"body": "x"}')
    assert not matched
    assert reason == "pointer_invalid"

    assert verify_evidence_quote({"locator": None, "text": "x"}, b"") == (
        False,
        "invalid_locator",
    )


def test_evidence_quote_detects_bad_pointer() -> None:
    evidence = copy.deepcopy(load("evidence.sample.json"))
    evidence["locator"]["pointer"] = "/missing"
    matched, reason = verify_evidence_quote(evidence, b'{"body": "x"}')
    assert not matched
    assert reason == "pointer_not_found"


def test_evidence_quote_supports_char_and_line_ranges() -> None:
    locator = make_locator("char_range", char_start=0, char_end=5)
    evidence = {
        "locator": locator,
        "text": "hello",
    }
    assert verify_evidence_quote(evidence, b"hello world") == (True, "quote_matches")

    line_locator = make_locator("line_range", line_start=2, line_end=2)
    line_evidence = {"locator": line_locator, "text": "second\n"}
    assert verify_evidence_quote(line_evidence, b"first\nsecond\nthird\n") == (
        True,
        "quote_matches",
    )


def test_evidence_quote_declares_url_fragment_unverifiable() -> None:
    evidence = {
        "locator": make_locator("url_fragment", url_fragment="section"),
        "text": "anything",
    }
    assert verify_evidence_quote(evidence, b"") == (False, "unverifiable_locator")
