from __future__ import annotations

import json
from pathlib import Path

import test_normalize as normalize_helpers

from rer.imported_source import (
    audit_imported_sources,
    imported_artifact_path,
    imported_source_key,
    legacy_imported_artifact_path,
)
from rer.models import (
    entity_id,
    make_entity,
    make_evidence,
    make_locator,
    make_relation,
)
from rer.normalize import EVIDENCE_PATH, RELATIONS_PATH
from rer.store import SnapshotStore

NOW = "2026-11-01T08:00:00Z"
HOST = "github.com"
OWNER = "acme"
NAME = "demo"


def repository_entities() -> list[dict]:
    return [
        make_entity(
            entity_id=entity_id(HOST, OWNER, NAME, "repository", ""),
            kind="repository",
            title="demo",
            url="https://github.com/acme/demo",
            observed_at=NOW,
        ),
        make_entity(
            entity_id=entity_id(HOST, OWNER, NAME, "issue", "1"),
            kind="issue",
            title="#1",
            url="https://github.com/acme/demo/issues/1",
            observed_at=NOW,
            number=1,
        ),
    ]


def relation(
    identifier: str,
    *,
    from_id: str,
    to_id: str,
    evidence_ids: list[str],
    verification: str = "verified",
    relation_type: str = "mentions",
    scope: dict | None = None,
) -> dict:
    return make_relation(
        relation_id=identifier,
        from_id=from_id,
        to_id=to_id,
        relation_type=relation_type,
        basis="text_explicit",
        verification=verification,
        evidence_ids=evidence_ids,
        scope=scope,
    )


def write_relations(store: SnapshotStore, records: list[dict]) -> None:
    payload = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    store.secure_path(RELATIONS_PATH).write_text(payload, encoding="utf-8")


def test_verify_integrity_reports_missing_relation_evidence(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-missing",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=["ev-missing"],
            )
        ],
    )

    report = store.verify_integrity()

    assert report["ok"] is False
    assert any(
        item["problem"] == "missing_relation_evidence"
        and item["evidence_id"] == "ev-missing"
        for item in report["problems"]
    )


def test_verify_integrity_reports_duplicate_relation_id_and_self_loop(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-duplicate",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
            ),
            relation(
                "rel-duplicate",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
            ),
            relation(
                "rel-self",
                from_id=entities[1]["id"],
                to_id=entities[1]["id"],
                evidence_ids=[],
            ),
        ],
    )

    report = store.verify_integrity()
    problems = {item["problem"] for item in report["problems"]}

    assert "duplicate_relation_id" in problems
    assert "self_relation" in problems
    assert report["relations_checked"] == 3


def test_verify_integrity_reports_duplicate_relation_identity(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-a",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
            ),
            relation(
                "rel-b",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
            ),
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "duplicate_relation_identity"
        and item["relation_id"] == "rel-b"
        and item["details"]["other_relation_id"] == "rel-a"
        for item in report["problems"]
    )


def test_verify_integrity_reports_verified_relation_with_unverified_evidence(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    store.write_jsonl(
        EVIDENCE_PATH,
        [evidence_for(entities[1]["id"], digest)],
        key="id",
        schema_name="evidence",
    )
    write_relations(
        store,
        [
            relation(
                "rel-verified",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=["ev-1"],
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "verified_relation_unverified_evidence"
        and item["evidence_id"] == "ev-1"
        for item in report["problems"]
    )


def test_verify_integrity_requires_evidence_for_verified_mentions(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-empty",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "verified_relation_without_evidence"
        and item["relation_id"] == "rel-empty"
        for item in report["problems"]
    )


def test_verify_integrity_allows_contains_commit_without_evidence(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-commit",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                relation_type="contains_commit",
            )
        ],
    )

    report = store.verify_integrity()

    assert not any(
        item["problem"] == "verified_relation_without_evidence"
        for item in report["problems"]
    )


def imported_source_document(entity_id_value: str, digest: str) -> dict:
    return {
        "schema_version": "1.3",
        "source_key": imported_source_key(
            "api_response",
            entity_id_value,
            "https://example.com/api",
            None,
        ),
        "source_url": "https://example.com/api",
        "provider": "agent-web",
        "retrieved_at": NOW,
        "content_type": "text/plain",
        "entity_id": entity_id_value,
        "source_kind": "api_response",
        "source_revision": None,
        "completeness": "complete",
        "content_sha256": digest,
        "content_bytes": 1,
    }


def test_verify_integrity_reports_missing_imported_source(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    store.write_jsonl(
        EVIDENCE_PATH,
        [evidence_for(entities[1]["id"], digest)],
        key="id",
        schema_name="evidence",
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "missing_imported_source"
        and item["evidence_id"] == "ev-1"
        for item in report["problems"]
    )


def test_verify_integrity_reports_imported_source_mismatch(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    store.write_jsonl(
        EVIDENCE_PATH,
        [evidence_for(entities[1]["id"], digest)],
        key="id",
        schema_name="evidence",
    )
    artifact = imported_artifact_path(
        "api_response",
        entities[1]["id"],
        "https://example.com/api",
        None,
        digest,
    )
    document = imported_source_document(entities[1]["id"], digest)
    document["entity_id"] = "github.com/acme/demo/issue/999"
    store.write_json(artifact, document)

    report = store.verify_integrity()

    assert any(
        item["problem"] == "imported_source_mismatch"
        and item["artifact"] == artifact
        for item in report["problems"]
    )


def test_verify_integrity_reports_legacy_imported_source(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    store.write_jsonl(
        EVIDENCE_PATH,
        [evidence_for(entities[1]["id"], digest)],
        key="id",
        schema_name="evidence",
    )
    legacy = legacy_imported_artifact_path(digest)
    store.write_json(
        legacy,
        imported_source_document(entities[1]["id"], digest),
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "legacy_imported_source"
        and item["artifact"] == legacy
        for item in report["problems"]
    )


def test_verify_integrity_reports_missing_imported_relation_artifact(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-import",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
                scope={
                    "generator": "import-source",
                    "artifact": "imported/missing/artifact.json",
                },
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "missing_imported_relation_artifact"
        and item["artifact"] == "imported/missing/artifact.json"
        for item in report["problems"]
    )


def test_verify_integrity_reports_imported_observation_evidence_mismatch(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-import",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
                scope={
                    "generator": "import-source",
                    "observations": [
                        {
                            "artifact": None,
                            "evidence_ids": ["ev-other"],
                        }
                    ],
                },
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "imported_observation_evidence_mismatch"
        and item["evidence_ids"] == ["ev-other"]
        for item in report["problems"]
    )


def test_verify_integrity_reports_imported_observation_mismatch(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    artifact = imported_artifact_path(
        "api_response",
        entities[1]["id"],
        "https://example.com/api",
        None,
        digest,
    )
    store.write_json(
        artifact,
        imported_source_document(entities[1]["id"], digest),
    )
    write_relations(
        store,
        [
            relation(
                "rel-import",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
                scope={
                    "generator": "import-source",
                    "observations": [
                        {
                            "artifact": artifact,
                            "content_sha256": "f" * 64,
                            "evidence_ids": [],
                        }
                    ],
                },
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "imported_observation_mismatch"
        and item["artifact"] == artifact
        for item in report["problems"]
    )


def evidence_for(entity_id_value: str, digest: str) -> dict:
    return make_evidence(
        evidence_id="ev-1",
        entity_id=entity_id_value,
        source_url="https://example.com/api",
        source_kind="api_response",
        observed_at=NOW,
        text="x",
        locator=make_locator(
            "char_range",
            char_start=0,
            char_end=1,
            line_start=1,
            line_end=1,
        ),
        blob_sha256=digest,
        verification="unverifiable",
    )


def test_verify_integrity_reports_duplicate_evidence_id(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    record = evidence_for(entities[1]["id"], digest)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    store.secure_path(EVIDENCE_PATH).write_text(line + line, encoding="utf-8")

    report = store.verify_integrity()

    assert any(
        item["problem"] == "duplicate_evidence_id" for item in report["problems"]
    )


def test_verify_integrity_reports_evidence_entity_mismatch(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    issue_two = make_entity(
        entity_id=entity_id(HOST, OWNER, NAME, "issue", "2"),
        kind="issue",
        title="#2",
        url="https://github.com/acme/demo/issues/2",
        observed_at=NOW,
        number=2,
    )
    entities.append(issue_two)
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    evidence = evidence_for(entities[1]["id"], digest)
    store.write_evidence(evidence)
    store.write_jsonl(
        RELATIONS_PATH,
        [
            relation(
                "rel-mismatch",
                from_id=issue_two["id"],
                to_id=entities[0]["id"],
                evidence_ids=["ev-1"],
            )
        ],
        key="id",
        schema_name="relation",
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "evidence_entity_mismatch" for item in report["problems"]
    )


def test_verify_integrity_reports_missing_relation_scope(tmp_path: Path) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-import",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
                scope={"generator": "import-source"},
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "missing_imported_relation_artifact"
        and item["relation_id"] == "rel-import"
        for item in report["problems"]
    )
    assert any(
        item["problem"] == "imported_observation_mismatch"
        and item["relation_id"] == "rel-import"
        for item in report["problems"]
    )


def test_verify_integrity_reports_missing_observation_artifact(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    write_relations(
        store,
        [
            relation(
                "rel-import",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
                scope={
                    "generator": "import-source",
                    "artifact": "imported/missing/artifact.json",
                    "observations": [{"evidence_ids": []}],
                },
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "imported_observation_mismatch"
        and item["relation_id"] == "rel-import"
        and item.get("details", {}).get("field") == "observations[].artifact"
        for item in report["problems"]
    )


def test_verify_integrity_reports_unreadable_observation_artifact(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    artifact = "imported/broken/artifact.json"
    store.write_text(artifact, "not json")
    write_relations(
        store,
        [
            relation(
                "rel-import",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
                scope={
                    "generator": "import-source",
                    "artifact": artifact,
                    "observations": [
                        {
                            "artifact": artifact,
                            "content_sha256": "a" * 64,
                            "evidence_ids": [],
                        }
                    ],
                },
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "imported_observation_mismatch"
        and item["relation_id"] == "rel-import"
        and item["artifact"] == artifact
        for item in report["problems"]
    )


def test_verify_integrity_reports_imported_source_content_mismatch(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    store.write_jsonl(
        EVIDENCE_PATH,
        [evidence_for(entities[1]["id"], digest)],
        key="id",
        schema_name="evidence",
    )
    artifact = imported_artifact_path(
        "api_response",
        entities[1]["id"],
        "https://example.com/api",
        None,
        digest,
    )
    document = imported_source_document(entities[1]["id"], digest)
    document["content_sha256"] = "f" * 64
    store.write_json(artifact, document)

    report = store.verify_integrity()

    assert any(
        item["problem"] == "imported_source_content_mismatch"
        and item["artifact"] == artifact
        for item in report["problems"]
    )


def test_verify_integrity_handles_malformed_observation_evidence_ids(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")
    digest = store.save_blob(b"x")
    artifact = imported_artifact_path(
        "api_response",
        entities[1]["id"],
        "https://example.com/api",
        None,
        digest,
    )
    store.write_json(
        artifact,
        imported_source_document(entities[1]["id"], digest),
    )
    write_relations(
        store,
        [
            relation(
                "rel-import",
                from_id=entities[1]["id"],
                to_id=entities[0]["id"],
                evidence_ids=[],
                verification="candidate",
                scope={
                    "generator": "import-source",
                    "artifact": artifact,
                    "observations": [
                        {
                            "artifact": artifact,
                            "content_sha256": digest,
                            "evidence_ids": 5,
                        }
                    ],
                },
            )
        ],
    )

    report = store.verify_integrity()

    assert any(
        item["problem"] == "imported_observation_evidence_mismatch"
        and item["relation_id"] == "rel-import"
        and "不是数组" in item["message"]
        for item in report["problems"]
    )


def test_audit_imported_sources_rejects_non_string_source_revision(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    entities = repository_entities()
    digest = store.save_blob(b"x")
    weak = evidence_for(entities[1]["id"], digest)
    weak["source_revision"] = 123

    problems = audit_imported_sources(store, evidence=[weak], relations=[])

    assert any(
        item["problem"] == "imported_source_mismatch"
        and item.get("details", {}).get("field") == "source_revision"
        for item in problems
    )
