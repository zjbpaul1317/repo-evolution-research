from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from rer.collectors.releases import project_release
from rer.errors import ValidationFailure
from rer.models import make_relation

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_validation_render import (  # noqa: E402
    ISSUE_ID,
    NOW,
    PR_ID,
    REVISION,
    read_json,
    research_document,
    seed_run,
    validate,
)

NEGATIVE_DIR = ROOT / "evals/cases/negative"
RELEASE_ID = "github.com/acme/demo/release/v1.0.0"
RELEASE_TAG = "v1.0.0"
RELEASE_TIME = "2026-09-21T00:00:00Z"


def _load_cases() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(NEGATIVE_DIR.glob("*.json"))
    ]


CASES = _load_cases()


def _control(store) -> None:
    result, _ = validate(store)
    assert result["result"]["ok"] is True


def _write_research(store, document: dict) -> None:
    store.write_json("analysis/research.json", document)


def _failure_codes(store) -> set[str]:
    with pytest.raises(ValidationFailure):
        validate(store)
    validation = read_json(store, "analysis/validation.json")
    return {str(item["code"]) for item in validation["errors"]}


def _release_document(store) -> dict:
    body = "Release notes"
    raw = body.encode("utf-8")
    return {
        "schema_version": "1.3",
        "observed_at": NOW,
        "repository": {"host": "github.com", "owner": "acme", "name": "demo"},
        "release_id": 42,
        "tag_name": RELEASE_TAG,
        "name": RELEASE_TAG,
        "url": f"https://github.com/acme/demo/releases/tag/{RELEASE_TAG}",
        "author": "alice",
        "draft": False,
        "prerelease": False,
        "created_at": RELEASE_TIME,
        "published_at": RELEASE_TIME,
        "target_commitish": "main",
        "tag_ref": {
            "status": "complete",
            "sha": REVISION,
            "object_type": "commit",
            "commit_sha": REVISION,
            "reason": None,
        },
        "tag_moved": False,
        "previous_commit_sha": None,
        "body": {
            "preview": body,
            "sha256": store.save_blob(raw),
            "bytes": len(raw),
            "preview_truncated": False,
        },
        "coverage": {"detail": "complete", "tag": "complete", "body": "complete"},
        "limitations": ["observed_current_state"],
    }


def _add_release_relation(store, relation_type: str) -> None:
    document = _release_document(store)
    store.write_json("raw/releases/42.json", document, schema_name="release_detail")
    project_release(store, "github.com", "acme", "demo", document)
    relation = make_relation(
        relation_id="rel-release",
        from_id=RELEASE_ID,
        to_id=PR_ID,
        relation_type=relation_type,
        basis="api_explicit" if relation_type == "verified_in_release" else "text_explicit",
        verification="verified" if relation_type == "verified_in_release" else "candidate",
        evidence_ids=[],
        scope=None,
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        [relation],
        key="id",
        schema_name="relation",
    )
    manifest = store.read_manifest()
    manifest["coverage"].append(
        {
            "channel": "releases",
            "query_scope": "window",
            "query": "releases in window",
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
    )
    store.write_manifest(manifest)


def _mutate_dangling_evidence(store) -> None:
    document = read_json(store, "analysis/research.json")
    document["claims"][0]["evidence_ids"] = ["ev-missing"]
    _write_research(store, document)


def _mutate_fabricated_quote(store) -> None:
    evidence = store.read_jsonl("normalized/evidence.jsonl")
    evidence[0]["text"] = "Fabricated quote that is not in the blob."
    store.write_jsonl(
        "normalized/evidence.jsonl",
        evidence,
        key="id",
        schema_name="evidence",
    )


def _mutate_missing_limitations(store) -> None:
    document = read_json(store, "analysis/research.json")
    document["limitations"] = []
    _write_research(store, document)


def _mutate_stale_validation(store) -> None:
    document = read_json(store, "analysis/research.json")
    document["claims"][0]["text"] = "Changed after validation."
    _write_research(store, document)


def _mutate_release_note_as_proof(store) -> None:
    _add_release_relation(store, "release_note_mentions")
    document = read_json(store, "analysis/research.json")
    document["capabilities"][0]["delivery_state"] = "released"
    _write_research(store, document)


def _mutate_release_before_merge(store) -> None:
    _add_release_relation(store, "verified_in_release")
    document = read_json(store, "analysis/research.json")
    document["capabilities"][0]["delivery_state"] = "released"
    _write_research(store, document)


def _mutate_mentions_as_implementation(store) -> None:
    relation = make_relation(
        relation_id="rel-mentions",
        from_id=ISSUE_ID,
        to_id=PR_ID,
        relation_type="mentions",
        basis="text_explicit",
        verification="candidate",
        evidence_ids=[],
        scope=None,
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        [relation],
        key="id",
        schema_name="relation",
    )
    document = research_document()
    document["claims"] = document["claims"][:1]
    document["topics"][0]["claim_ids"]["implementation"] = []
    document["capabilities"][0]["evidence_ids"] = ["ev-issue-40"]
    _write_research(store, document)


MUTATIONS: dict[str, Callable] = {
    "dangling_evidence": _mutate_dangling_evidence,
    "fabricated_quote": _mutate_fabricated_quote,
    "missing_limitations": _mutate_missing_limitations,
    "stale_validation": _mutate_stale_validation,
    "release_note_as_proof": _mutate_release_note_as_proof,
    "release_before_merge": _mutate_release_before_merge,
    "mentions_as_implementation": _mutate_mentions_as_implementation,
}


def test_case_files_are_unique() -> None:
    identifiers = [case["id"] for case in CASES]
    assert len(identifiers) == len(set(identifiers))
    assert {case["mutation"] for case in CASES} <= set(MUTATIONS)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_negative_case(case: dict, tmp_path: Path) -> None:
    store = seed_run(tmp_path)
    _control(store)
    MUTATIONS[case["mutation"]](store)

    if case["check"] == "validation_error":
        codes = _failure_codes(store)
    elif case["check"] == "integrity_problem":
        codes = {
            str(item.get("problem")) for item in store.verify_integrity()["problems"]
        }
    else:
        raise AssertionError(f"unknown check: {case['check']}")
    assert case["expected_code"] in codes
