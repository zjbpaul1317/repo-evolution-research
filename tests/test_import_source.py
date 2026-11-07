from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import test_normalize as normalize_helpers
import test_validation_render as render_helpers

from rer import cli
from rer.collectors.progress import coverage_entry
from rer.collectors.releases import project_release, write_release_document
from rer.config import build_config, compute_config_hash
from rer.errors import SchemaError, StoreError, UsageError
from rer.import_source import (
    commit_import,
    import_source,
    load_envelope,
    plan_import,
)
from rer.imported_source import imported_source_key
from rer.lifecycle import check_state_assertions, verify_capability
from rer.models import (
    build_manifest,
    entity_id,
    make_entity,
    make_evidence,
    make_locator,
)
from rer.store import SnapshotStore, create_run, new_run_id
from rer.validation import ledger_digest

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
NOW_ISO = "2026-11-01T08:00:00Z"
REPOSITORY = {"host": "github.com", "owner": "acme", "name": "demo"}
RELEASE_ID = "github.com/acme/demo/release/v1.0.0"
PR_ID = "github.com/acme/demo/pull/101"


def seed_entities(store: SnapshotStore) -> None:
    store.write_jsonl(
        "normalized/entities.jsonl",
        [
            make_entity(
                entity_id=RELEASE_ID,
                kind="release",
                title="v1.0.0",
                url="https://github.com/acme/demo/releases/tag/v1.0.0",
                observed_at=NOW_ISO,
                raw_state={"tag_name": "v1.0.0", "release_id": 1},
            ),
            make_entity(
                entity_id=PR_ID,
                kind="pull_request",
                title="PR 101",
                url="https://github.com/acme/demo/pull/101",
                observed_at=NOW_ISO,
                number=101,
                raw_state={
                    "state": "closed",
                    "merged": True,
                    "merged_at": "2026-10-20T10:00:00Z",
                },
                normalized_state={
                    "merged": True,
                    "merged_at": "2026-10-20T10:00:00Z",
                },
                roles=["implementation"],
            ),
        ],
        key="id",
        schema_name="entity",
    )


def write_pr_evidence(store: SnapshotStore) -> str:
    text = "PR 101 implements the cache planner described by the capability."
    payload = text.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    evidence = make_evidence(
        evidence_id="ev-pr-101",
        entity_id=PR_ID,
        source_url="https://github.com/acme/demo/pull/101",
        source_kind="pull_request_body",
        observed_at=NOW_ISO,
        text=text,
        locator=make_locator(
            "char_range",
            char_start=0,
            char_end=len(text),
            line_start=1,
            line_end=1,
        ),
        blob_sha256=digest,
    )
    store.write_evidence(evidence, payload=payload)
    return "ev-pr-101"


def envelope() -> dict:
    return {
        "schema_version": "1.3",
        "source_url": "https://github.com/acme/demo/releases/tag/v1.0.0",
        "retrieved_at": "2026-11-01T07:00:00Z",
        "provider": "agent-web",
        "content_type": "text/markdown",
        "content": "Release v1.0.0 includes the cache planner from PR #101.",
        "entity_id": RELEASE_ID,
        "source_kind": "external_page",
        "completeness": "complete",
        "relations": [
            {
                "from_id": RELEASE_ID,
                "to_id": PR_ID,
                "type": "verified_in_release",
                "basis": "text_explicit",
                "verification": "verified",
            }
        ],
    }


def test_import_source_enables_verified_release(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    pr_evidence_id = write_pr_evidence(store)

    imported = import_source(store, envelope())

    relation = next(
        item
        for item in store.read_jsonl("normalized/relations.jsonl")
        if item["type"] == "verified_in_release"
    )
    assert relation["verification"] == "verified"
    assert relation["basis"] == "text_explicit"
    assert relation["evidence_ids"] == [imported["evidence_id"]]
    assert relation["scope"]["generator"] == "import-source"

    assert store.secure_path(imported["artifact"]).is_file()
    evidence = store.read_jsonl("normalized/evidence.jsonl")
    assert any(item["id"] == imported["evidence_id"] for item in evidence)
    assert any(
        item["id"] == imported["evidence_id"]
        and item["source_kind"] == "external_page"
        for item in evidence
    )

    result = verify_capability(
        {"id": "cap-cache", "evidence_ids": [pr_evidence_id]},
        store.read_jsonl("normalized/entities.jsonl"),
        store.read_jsonl("normalized/relations.jsonl"),
        evidence,
        pr_documents=[
            {
                "number": 101,
                "merged": True,
                "merged_at": "2026-10-20T10:00:00Z",
            }
        ],
        release_documents=[
            {
                "tag_name": "v1.0.0",
                "published_at": "2026-10-25T10:00:00Z",
                "draft": False,
                "prerelease": False,
            }
        ],
    )

    assert result["delivery_state"] == "released"
    assert result["delivery_verification"] == "verified"
    assert result["verified_versions"] == ["v1.0.0"]
    issues = check_state_assertions(
        [
            {
                "id": "C-CACHE-01",
                "kind": "observed",
                "scope": {
                    "capability_id": "cap-cache",
                    "version": "v1.0.0",
                },
            }
        ],
        [{"id": "cap-cache"}],
        [result],
    )
    assert issues == []


def test_import_source_reaches_released_report(tmp_path: Path) -> None:
    store = render_helpers.seed_run(tmp_path)
    release_document = json.loads(
        (Path(__file__).parent / "fixtures/example/release_detail.sample.json").read_text(
            encoding="utf-8"
        )
    )
    body = b"Release notes"
    body_sha = store.save_blob(body)
    release_document.update(
        {
            "release_id": 42,
            "tag_name": "v1.0.0",
            "name": "v1.0.0",
            "url": "https://github.com/acme/demo/releases/tag/v1.0.0",
            "published_at": "2026-10-25T00:00:00Z",
            "body": {
                "preview": body.decode("utf-8"),
                "sha256": body_sha,
                "bytes": len(body),
                "preview_truncated": False,
            },
        }
    )
    write_release_document(store, release_document)
    project_release(
        store,
        "github.com",
        "acme",
        "demo",
        release_document,
    )
    manifest = store.read_manifest()
    manifest["coverage"].append(
        coverage_entry(
            channel="releases",
            query_scope="activity_window",
            query="fixture",
            expected=1,
            fetched=1,
            pages=1,
            coverage_status="complete",
            observed_start=NOW_ISO,
            observed_end=NOW_ISO,
        )
    )
    store.write_manifest(manifest)

    import_source(store, envelope())

    research = render_helpers.research_document()
    research["capabilities"][0]["delivery_state"] = "released"
    store.write_json("analysis/research.json", research)

    result, _lines = render_helpers.validate(store)
    assert result["result"]["ok"] is True
    validation = store.read_json("analysis/validation.json")
    lifecycle = validation["lifecycle"][0]
    assert lifecycle["derived"]["delivery_state"] == "released"
    assert lifecycle["delivery_verification"] == "verified"
    assert lifecycle["verified_versions"] == ["v1.0.0"]

    render_helpers.render(store)
    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert "已发布" in report
    assert "verified" in report


def test_disputed_import_reaches_unknown_report(tmp_path: Path) -> None:
    store = render_helpers.seed_run(tmp_path)
    release_document = json.loads(
        (Path(__file__).parent / "fixtures/example/release_detail.sample.json").read_text(
            encoding="utf-8"
        )
    )
    body = b"Release notes"
    body_sha = store.save_blob(body)
    release_document.update(
        {
            "release_id": 42,
            "tag_name": "v1.0.0",
            "name": "v1.0.0",
            "url": "https://github.com/acme/demo/releases/tag/v1.0.0",
            "published_at": "2026-10-25T00:00:00Z",
            "body": {
                "preview": body.decode("utf-8"),
                "sha256": body_sha,
                "bytes": len(body),
                "preview_truncated": False,
            },
        }
    )
    write_release_document(store, release_document)
    project_release(
        store,
        "github.com",
        "acme",
        "demo",
        release_document,
    )
    manifest = store.read_manifest()
    manifest["coverage"].append(
        coverage_entry(
            channel="releases",
            query_scope="activity_window",
            query="fixture",
            expected=1,
            fetched=1,
            pages=1,
            coverage_status="complete",
            observed_start=NOW_ISO,
            observed_end=NOW_ISO,
        )
    )
    store.write_manifest(manifest)

    import_source(store, envelope())
    disputed = envelope()
    disputed["source_url"] = "https://example.com/dispute"
    disputed["content"] = "A correction disputes the release claim."
    disputed["relations"][0]["verification"] = "disputed"
    import_source(store, disputed)

    research = render_helpers.research_document()
    research["capabilities"][0]["delivery_state"] = "unknown"
    store.write_json("analysis/research.json", research)

    result, _lines = render_helpers.validate(store)
    assert result["result"]["ok"] is True
    validation = store.read_json("analysis/validation.json")
    lifecycle = validation["lifecycle"][0]
    assert lifecycle["derived"]["delivery_state"] == "unknown"
    assert lifecycle["delivery_verification"] == "unknown"

    render_helpers.render(store)
    report = store.secure_path("report.md").read_text(encoding="utf-8")
    assert "unknown" in report


def test_import_source_is_idempotent(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    first = import_source(store, envelope())
    second = import_source(store, envelope())

    assert first["evidence_id"] == second["evidence_id"]
    assert first["relation_ids"] == second["relation_ids"]
    assert len(store.read_jsonl("normalized/evidence.jsonl")) == 1
    relations = store.read_jsonl("normalized/relations.jsonl")
    assert len(relations) == 1
    assert len(relations[0]["scope"]["observations"]) == 1


def test_plan_import_is_read_only_until_commit(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    plan = plan_import(store, envelope())

    assert store.read_jsonl("normalized/evidence.jsonl") == []
    assert store.read_jsonl("normalized/relations.jsonl") == []
    assert list(store.secure_path("imported").rglob("*")) == []

    imported = commit_import(store, plan)

    assert imported["evidence_id"] == plan.evidence["id"]
    assert store.secure_path(imported["artifact"]).is_file()
    assert any(
        item["id"] == imported["evidence_id"]
        for item in store.read_jsonl("normalized/evidence.jsonl")
    )
    assert any(
        item["id"] == imported["relation_ids"][0]
        for item in store.read_jsonl("normalized/relations.jsonl")
    )


def test_imported_artifact_has_source_identity(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    imported = import_source(store, envelope())

    artifact = store.read_json(imported["artifact"])
    assert artifact["source_key"] == imported_source_key(
        "external_page",
        RELEASE_ID,
        "https://github.com/acme/demo/releases/tag/v1.0.0",
        None,
    )
    assert artifact["content_sha256"] == hashlib.sha256(
        envelope()["content"].encode("utf-8")
    ).hexdigest()
    assert artifact["observations"] == [
        {
            "retrieved_at": "2026-11-01T07:00:00Z",
            "provider": "agent-web",
            "content_type": "text/markdown",
            "completeness": "complete",
        }
    ]


def test_imported_artifact_is_part_of_ledger_digest(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    imported = import_source(store, envelope())
    manifest: dict = {}

    before = ledger_digest(store, manifest)
    artifact = store.read_json(imported["artifact"])
    artifact["provider"] = "changed"
    store.write_json(imported["artifact"], artifact)
    after = ledger_digest(store, manifest)

    assert before != after


def test_same_content_different_source_keeps_distinct_artifacts(
    tmp_path: Path,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    first_envelope = envelope()
    first_envelope["relations"] = []
    second_envelope = envelope()
    second_envelope["relations"] = []
    second_envelope["source_url"] = "https://example.com/other"
    second_envelope["provider"] = "agent-web-2"

    first = import_source(store, first_envelope)
    second = import_source(store, second_envelope)

    assert first["artifact"] != second["artifact"]
    assert first["evidence_id"] != second["evidence_id"]
    first_artifact = store.read_json(first["artifact"])
    second_artifact = store.read_json(second["artifact"])
    assert first_artifact["source_url"] == first_envelope["source_url"]
    assert second_artifact["source_url"] == second_envelope["source_url"]
    assert second_artifact["provider"] == "agent-web-2"


def test_same_source_new_revision_creates_new_evidence(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    first_envelope = envelope()
    first_envelope["relations"] = []
    first_envelope["source_revision"] = "a" * 40
    second_envelope = envelope()
    second_envelope["relations"] = []
    second_envelope["source_revision"] = "b" * 40

    first = import_source(store, first_envelope)
    second = import_source(store, second_envelope)

    assert first["artifact"] != second["artifact"]
    assert first["evidence_id"] != second["evidence_id"]
    evidence = {
        item["id"]: item for item in store.read_jsonl("normalized/evidence.jsonl")
    }
    assert evidence[first["evidence_id"]]["source_revision"] == "a" * 40
    assert evidence[second["evidence_id"]]["source_revision"] == "b" * 40
    assert store.read_json(first["artifact"])["source_revision"] == "a" * 40
    assert store.read_json(second["artifact"])["source_revision"] == "b" * 40


def test_same_source_reimport_appends_observation(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    first = import_source(store, envelope())
    second_envelope = envelope()
    second_envelope["retrieved_at"] = "2026-11-01T07:10:00Z"
    second_envelope["provider"] = "agent-web-2"
    second = import_source(store, second_envelope)

    assert first["evidence_id"] == second["evidence_id"]
    assert first["artifact"] == second["artifact"]
    assert any(
        "merged relation observations" in warning
        for warning in second["warnings"]
    )
    artifact = store.read_json(first["artifact"])
    assert artifact["retrieved_at"] == "2026-11-01T07:00:00Z"
    assert artifact["provider"] == "agent-web"
    assert artifact["observations"] == [
        {
            "retrieved_at": "2026-11-01T07:00:00Z",
            "provider": "agent-web",
            "content_type": "text/markdown",
            "completeness": "complete",
        },
        {
            "retrieved_at": "2026-11-01T07:10:00Z",
            "provider": "agent-web-2",
            "content_type": "text/markdown",
            "completeness": "complete",
        },
    ]


def test_existing_evidence_source_mismatch_is_rejected(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    imported = import_source(store, envelope())

    records = store.read_jsonl("normalized/evidence.jsonl")
    for record in records:
        if record["id"] == imported["evidence_id"]:
            record["source_revision"] = "mismatch"
    store.write_jsonl(
        "normalized/evidence.jsonl",
        records,
        key="id",
        on_conflict="replace",
        schema_name="evidence",
    )

    with pytest.raises(StoreError, match="来源不一致"):
        import_source(store, envelope())


def test_reimport_does_not_clear_disputed_relation(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    pr_evidence_id = write_pr_evidence(store)

    first = import_source(store, envelope())

    disputed = envelope()
    disputed["source_url"] = "https://example.com/release-dispute"
    disputed["content"] = "A later correction disputes the release claim."
    disputed["relations"][0]["verification"] = "disputed"
    second = import_source(store, disputed)

    confirmation = envelope()
    confirmation["source_url"] = "https://example.com/release-confirmation"
    confirmation["content"] = "A second source repeats the release claim."
    third = import_source(store, confirmation)

    relation = next(
        item
        for item in store.read_jsonl("normalized/relations.jsonl")
        if item["type"] == "verified_in_release"
    )
    assert relation["verification"] == "disputed"
    assert set(relation["evidence_ids"]) == {
        first["evidence_id"],
        second["evidence_id"],
        third["evidence_id"],
    }
    observations = relation["scope"]["observations"]
    assert len(observations) == 3
    assert {item["verification"] for item in observations} == {
        "verified",
        "disputed",
    }

    result = verify_capability(
        {"id": "cap-cache", "evidence_ids": [pr_evidence_id]},
        store.read_jsonl("normalized/entities.jsonl"),
        store.read_jsonl("normalized/relations.jsonl"),
        store.read_jsonl("normalized/evidence.jsonl"),
        pr_documents=[
            {
                "number": 101,
                "merged": True,
                "merged_at": "2026-10-20T10:00:00Z",
            }
        ],
        release_documents=[
            {
                "tag_name": "v1.0.0",
                "published_at": "2026-10-25T10:00:00Z",
                "draft": False,
                "prerelease": False,
            }
        ],
    )
    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "unknown"


def test_disputed_duplicate_identity_blocks_verified(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    write_pr_evidence(store)

    first = import_source(store, envelope())

    disputed = envelope()
    disputed["source_url"] = "https://example.com/release-dispute"
    disputed["content"] = "A later correction disputes the release claim."
    disputed["relations"][0]["id"] = "rel-dispute"
    disputed["relations"][0]["verification"] = "disputed"
    second = import_source(store, disputed)

    confirmation = envelope()
    confirmation["source_url"] = "https://example.com/release-confirmation"
    confirmation["content"] = "A second source repeats the release claim."
    confirmation["relations"][0]["id"] = "rel-confirmation"
    third = import_source(store, confirmation)

    relations = [
        item
        for item in store.read_jsonl("normalized/relations.jsonl")
        if item["type"] == "verified_in_release"
    ]
    assert len(relations) == 1
    relation = relations[0]
    assert relation["id"] == first["relation_ids"][0]
    assert relation["verification"] == "disputed"
    assert set(relation["evidence_ids"]) == {
        first["evidence_id"],
        second["evidence_id"],
        third["evidence_id"],
    }
    assert relation["scope"]["aliases"] == ["rel-confirmation", "rel-dispute"]
    assert second["relation_ids"] == [relation["id"]]
    assert third["relation_ids"] == [relation["id"]]
    assert second["relation_aliases"] == {"rel-dispute": relation["id"]}
    assert third["relation_aliases"] == {"rel-confirmation": relation["id"]}


def test_verified_relation_rejects_unverified_evidence(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    text = "Weak external claim."
    payload = text.encode("utf-8")
    digest = store.save_blob(payload)
    weak = make_evidence(
        evidence_id="ev-weak",
        entity_id=RELEASE_ID,
        source_url="https://example.com/weak",
        source_kind="external_page",
        observed_at=NOW_ISO,
        text=text,
        locator=make_locator(
            "char_range",
            char_start=0,
            char_end=len(text),
            line_start=1,
            line_end=1,
        ),
        blob_sha256=digest,
        verification="unverifiable",
    )
    store.write_jsonl(
        "normalized/evidence.jsonl",
        [weak],
        key="id",
        schema_name="evidence",
    )

    document = envelope()
    document["relations"] = [
        {
            "from_id": RELEASE_ID,
            "to_id": PR_ID,
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "verified",
            "evidence_ids": ["ev-weak"],
        }
    ]

    with pytest.raises(UsageError, match="verified evidence"):
        import_source(store, document)


def test_import_source_rejects_unknown_entity(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    document = envelope()
    document["entity_id"] = "github.com/acme/demo/release/v9.9.9"

    with pytest.raises(UsageError, match="entity_id"):
        import_source(store, document)


def test_import_source_allows_verified_contains_commit_without_evidence(
    tmp_path: Path,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    commit_sha = "a" * 40
    commit_id = entity_id("github.com", "acme", "demo", "commit", commit_sha)
    entities = store.read_jsonl("normalized/entities.jsonl")
    entities.append(
        make_entity(
            entity_id=commit_id,
            kind="commit",
            title=commit_sha[:7],
            url=f"https://github.com/acme/demo/commit/{commit_sha}",
            observed_at=NOW_ISO,
            raw_state={"sha": commit_sha},
        )
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    document = envelope()
    document["relations"] = [
        {
            "from_id": PR_ID,
            "to_id": commit_id,
            "type": "contains_commit",
            "basis": "api_explicit",
            "verification": "verified",
            "evidence_ids": [],
        }
    ]

    import_source(store, document)

    relation = next(
        item
        for item in store.read_jsonl("normalized/relations.jsonl")
        if item["type"] == "contains_commit"
    )
    assert relation["verification"] == "verified"
    assert relation["evidence_ids"] == []
    assert relation["scope"]["observations"][0]["evidence_ids"] == []


def test_import_source_rejects_non_current_doc_revision(tmp_path: Path) -> None:
    config = build_config(repository="acme/demo", timezone_name="UTC", now=None)
    store = create_run(tmp_path, config["repository"], new_run_id(NOW))
    store.write_manifest(build_manifest(config, store.run_dir.name, NOW_ISO))
    seed_entities(store)
    old_revision = "a" * 40
    current_revision = "b" * 40
    doc_identifier = entity_id(
        "github.com",
        "acme",
        "demo",
        "doc",
        "docs/old.md",
        revision=old_revision,
    )
    entities = store.read_jsonl("normalized/entities.jsonl")
    entities.append(
        make_entity(
            entity_id=doc_identifier,
            kind="doc",
            title="docs/old.md",
            url=f"https://github.com/acme/demo/blob/{old_revision}/docs/old.md",
            observed_at=NOW_ISO,
            raw_state={
                "revision": old_revision,
                "path": "docs/old.md",
                "status": "read",
            },
            normalized_state={
                "revision": old_revision,
                "path": "docs/old.md",
                "status": "read",
            },
        )
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    manifest = store.read_manifest()
    manifest["default_branch_sha"] = current_revision
    store.write_manifest(manifest)
    before_manifest = store.read_manifest()
    evidence_path = store.secure_path("normalized/evidence.jsonl")
    relations_path = store.secure_path("normalized/relations.jsonl")
    evidence_before = (
        evidence_path.read_bytes() if evidence_path.is_file() else None
    )
    relations_before = (
        relations_path.read_bytes() if relations_path.is_file() else None
    )
    imported_before = sorted(
        path.relative_to(store.secure_path("imported")).as_posix()
        for path in store.secure_path("imported").rglob("*")
        if path.is_file()
    )
    ledger_before = ledger_digest(store, before_manifest)
    document = envelope()
    document["entity_id"] = doc_identifier
    document["relations"] = []

    with pytest.raises(UsageError, match="revision"):
        import_source(store, document)

    assert store.read_manifest() == before_manifest
    assert (
        evidence_path.read_bytes() if evidence_path.is_file() else None
    ) == evidence_before
    assert (
        relations_path.read_bytes() if relations_path.is_file() else None
    ) == relations_before
    assert sorted(
        path.relative_to(store.secure_path("imported")).as_posix()
        for path in store.secure_path("imported").rglob("*")
        if path.is_file()
    ) == imported_before
    assert ledger_digest(store, store.read_manifest()) == ledger_before


def test_verified_in_release_requires_imported_evidence(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    text = "Release notes mention the change."
    payload = text.encode("utf-8")
    release_evidence = make_evidence(
        evidence_id="ev-release-note",
        entity_id=RELEASE_ID,
        source_url="https://github.com/acme/demo/releases/tag/v1.0.0",
        source_kind="release_body",
        observed_at=NOW_ISO,
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
    store.write_jsonl(
        "normalized/evidence.jsonl",
        [release_evidence],
        key="id",
        schema_name="evidence",
    )
    document = envelope()
    document["relations"][0]["evidence_ids"] = [release_evidence["id"]]

    with pytest.raises(UsageError, match="本次导入"):
        import_source(store, document)

    assert len(store.read_jsonl("normalized/evidence.jsonl")) == 1


def test_verified_relation_requires_explicit_basis(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    document = envelope()
    document["relations"][0]["basis"] = "agent_inferred"

    with pytest.raises(UsageError, match="basis"):
        import_source(store, document)


def test_relation_scope_cannot_override_provenance(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    document = envelope()
    document["relations"][0]["scope"] = {
        "source_url": "https://example.com/forged",
        "extra": "allowed",
    }

    with pytest.raises(UsageError, match="核心溯源字段"):
        import_source(store, document)


def test_import_source_invalid_relation_does_not_write_evidence(
    tmp_path: Path,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    document = envelope()
    document["relations"][0]["to_id"] = "github.com/acme/demo/pull/999"

    with pytest.raises(UsageError, match="to_id"):
        import_source(store, document)

    assert store.read_jsonl("normalized/evidence.jsonl") == []
    assert store.read_jsonl("normalized/relations.jsonl") == []
    assert list(store.secure_path("imported").rglob("*")) == []


def test_rejected_self_relation_has_no_side_effects(tmp_path: Path) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    document = envelope()
    document["relations"][0]["to_id"] = RELEASE_ID

    with pytest.raises(UsageError, match="自引用"):
        import_source(store, document)

    assert store.read_jsonl("normalized/evidence.jsonl") == []
    assert store.read_jsonl("normalized/relations.jsonl") == []
    assert list(store.secure_path("imported").rglob("*")) == []


def test_rejected_relation_evidence_entity_mismatch_has_no_side_effects(
    tmp_path: Path,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    unrelated = make_entity(
        entity_id=entity_id(
            "github.com",
            "acme",
            "demo",
            "issue",
            "999",
        ),
        kind="issue",
        title="#999",
        url="https://github.com/acme/demo/issues/999",
        observed_at=NOW_ISO,
        number=999,
    )
    entities = store.read_jsonl("normalized/entities.jsonl")
    entities.append(unrelated)
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    document = envelope()
    document["entity_id"] = unrelated["id"]

    with pytest.raises(UsageError, match="entity_id"):
        import_source(store, document)

    assert store.read_jsonl("normalized/evidence.jsonl") == []
    assert store.read_jsonl("normalized/relations.jsonl") == []
    assert list(store.secure_path("imported").rglob("*")) == []


def test_rejected_import_keeps_manifest_and_ledger_unchanged(
    tmp_path: Path,
) -> None:
    store = normalize_helpers.make_empty_run(tmp_path)
    seed_entities(store)
    manifest = store.read_manifest()
    before = ledger_digest(store, manifest)
    document = envelope()
    document["relations"][0]["to_id"] = "github.com/acme/demo/pull/999"

    with pytest.raises(UsageError, match="to_id"):
        import_source(store, document)

    assert store.read_manifest() == manifest
    assert ledger_digest(store, store.read_manifest()) == before
    assert list(store.secure_path("imported").rglob("*")) == []


def test_import_commit_failure_rolls_back_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    write_pr_evidence(store)
    evidence_path = store.secure_path("normalized/evidence.jsonl")
    before = evidence_path.read_bytes()

    original = SnapshotStore.write_json

    def fail_artifact(self, relpath, document, **kwargs):
        if str(relpath).startswith("imported/"):
            raise OSError("disk full")
        return original(self, relpath, document, **kwargs)

    monkeypatch.setattr(SnapshotStore, "write_json", fail_artifact)

    with pytest.raises(OSError, match="disk full"):
        import_source(store, envelope())

    assert evidence_path.read_bytes() == before
    assert store.read_jsonl("normalized/relations.jsonl") == []
    assert list(store.secure_path("imported").rglob("*")) == []


def test_import_commit_failure_rolls_back_relations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    original = SnapshotStore.write_jsonl

    def fail_relations(self, relpath, records, **kwargs):
        if str(relpath) == "normalized/relations.jsonl":
            raise OSError("relations disk full")
        return original(self, relpath, records, **kwargs)

    monkeypatch.setattr(SnapshotStore, "write_jsonl", fail_relations)

    with pytest.raises(OSError, match="relations disk full"):
        import_source(store, envelope())

    assert store.read_jsonl("normalized/evidence.jsonl") == []
    assert store.read_jsonl("normalized/relations.jsonl") == []
    assert list(store.secure_path("imported").rglob("*")) == []


def test_import_rollback_failure_reports_partial_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    def fail_artifact(self, relpath, document, **kwargs):
        if str(relpath).startswith("imported/"):
            raise OSError("disk full")
        raise AssertionError(f"unexpected write: {relpath}")

    def fail_restore(self, relpath, payload):
        raise OSError("rollback disk full")

    monkeypatch.setattr(SnapshotStore, "write_json", fail_artifact)
    monkeypatch.setattr(SnapshotStore, "write_bytes", fail_restore)
    monkeypatch.setattr(SnapshotStore, "remove_file", fail_restore)

    with pytest.raises(StoreError, match="回滚不完整") as excinfo:
        import_source(store, envelope())

    assert excinfo.value.source == "import_source.rollback"
    assert excinfo.value.details["rollback_problems"]


def test_load_envelope_rejects_invalid_document(tmp_path: Path) -> None:
    path = tmp_path / "envelope.json"
    path.write_text('{"schema_version": "1.3"}', encoding="utf-8")

    with pytest.raises(SchemaError):
        load_envelope(path)


def test_cli_rejected_import_keeps_manifest_and_report_valid(
    tmp_path: Path,
) -> None:
    store = render_helpers.seed_run(tmp_path)
    render_helpers.validate(store)
    first, _ = render_helpers.render(store)
    manifest_before = store.read_manifest()
    report_before = store.secure_path("report.md").read_bytes()
    invalid = {
        "schema_version": "1.3",
        "source_url": "https://example.com/rejected",
        "retrieved_at": NOW_ISO,
        "provider": "test",
        "content": "This envelope must be rejected.",
        "entity_id": render_helpers.ISSUE_ID,
        "relations": [
            {
                "from_id": render_helpers.ISSUE_ID,
                "to_id": "github.com/acme/demo/pull/999",
                "type": "mentions",
                "basis": "text_explicit",
                "verification": "candidate",
            }
        ],
    }
    envelope_path = tmp_path / "rejected.json"
    envelope_path.write_text(
        json.dumps(invalid, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(UsageError, match="to_id"):
        cli.command_import_source(
            cli.build_parser().parse_args(
                [
                    "import-source",
                    "--run",
                    str(store.run_dir),
                    "--input",
                    str(envelope_path),
                ]
            )
        )

    assert store.read_manifest() == manifest_before
    assert store.secure_path("report.md").read_bytes() == report_before
    second, _ = render_helpers.render(store)
    assert second["result"]["sha256"] == first["result"]["sha256"]


def test_cli_import_source_invalidates_downstream(tmp_path: Path) -> None:
    init_args = cli.build_parser().parse_args(
        [
            "init",
            "--repo",
            "acme/demo",
            "--timezone",
            "UTC",
            "--output-root",
            str(tmp_path),
        ]
    )
    init_result, _ = cli.command_init(init_args)
    store = SnapshotStore(init_result["result"]["run"])
    seed_entities(store)
    manifest = store.read_manifest()
    manifest["stages"]["prepare"] = "complete"
    manifest["state"] = "prepared"
    store.write_manifest(manifest)
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(
        json.dumps(envelope(), ensure_ascii=False),
        encoding="utf-8",
    )

    result, _ = cli.command_import_source(
        cli.build_parser().parse_args(
            [
                "import-source",
                "--run",
                str(store.run_dir),
                "--input",
                str(envelope_path),
            ]
        )
    )

    assert result["result"]["evidence_id"].startswith("ev-")
    assert result["result"]["relation_ids"]
    updated = store.read_manifest()
    assert updated["stages"]["prepare"] == "not_started"
    assert updated["state"] == "collecting"
    assert any(
        note.startswith("import-source changed normalized inputs")
        for note in updated["notes"]
    )

    second = envelope()
    second["content"] = "Release v1.0.0 additionally records the cache planner."
    second["relations"].append(
        {
            "from_id": RELEASE_ID,
            "to_id": PR_ID,
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "candidate",
        }
    )
    second_path = tmp_path / "envelope-2.json"
    second_path.write_text(
        json.dumps(second, ensure_ascii=False),
        encoding="utf-8",
    )
    second_result, _ = cli.command_import_source(
        cli.build_parser().parse_args(
            [
                "import-source",
                "--run",
                str(store.run_dir),
                "--input",
                str(second_path),
            ]
        )
    )
    assert len(second_result["result"]["relation_ids"]) == 2


def test_cli_import_source_rejects_legacy_schema_version(tmp_path: Path) -> None:
    init_args = cli.build_parser().parse_args(
        [
            "init",
            "--repo",
            "acme/demo",
            "--timezone",
            "UTC",
            "--output-root",
            str(tmp_path),
        ]
    )
    init_result, _ = cli.command_init(init_args)
    store = SnapshotStore(init_result["result"]["run"])
    config = json.loads(
        store.secure_path("config.resolved.json").read_text(encoding="utf-8")
    )
    config["schema_version"] = "1.2"
    config["config_hash"] = compute_config_hash(config)
    store.write_json("config.resolved.json", config, schema_name="config")
    manifest = store.read_manifest()
    manifest["schema_version"] = "1.2"
    manifest["config_hash"] = config["config_hash"]
    store.write_manifest(manifest)
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(
        json.dumps(envelope(), ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(UsageError, match="schema_version"):
        cli.command_import_source(
            cli.build_parser().parse_args(
                [
                    "import-source",
                    "--run",
                    str(store.run_dir),
                    "--input",
                    str(envelope_path),
                ]
            )
        )


def test_cli_import_source_requires_prepared_entities(tmp_path: Path) -> None:
    init_args = cli.build_parser().parse_args(
        [
            "init",
            "--repo",
            "acme/demo",
            "--timezone",
            "UTC",
            "--output-root",
            str(tmp_path),
        ]
    )
    init_result, _ = cli.command_init(init_args)
    run_dir = init_result["result"]["run"]
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text(
        json.dumps(envelope(), ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(UsageError, match="entities.jsonl"):
        cli.command_import_source(
            cli.build_parser().parse_args(
                [
                    "import-source",
                    "--run",
                    run_dir,
                    "--input",
                    str(envelope_path),
                ]
            )
        )


def test_import_rejects_truncated_entities_stream_without_repair(
    tmp_path: Path,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    stream = store.secure_path("normalized/entities.jsonl")
    stream.write_text('{"id": "github.com/acme/demo/pull/999"', encoding="utf-8")
    before = stream.read_bytes()

    with pytest.raises(UsageError, match="导入前快照不可读"):
        import_source(store, envelope())

    assert stream.read_bytes() == before
    assert list(store.secure_path("imported").rglob("*")) == []


def test_import_rejects_truncated_evidence_stream_without_repair(
    tmp_path: Path,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    stream = store.secure_path("normalized/evidence.jsonl")
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"id": "ev-partial"', encoding="utf-8")
    before = stream.read_bytes()

    with pytest.raises(UsageError, match="导入前快照不可读"):
        import_source(store, envelope())

    assert stream.read_bytes() == before
    assert list(store.secure_path("imported").rglob("*")) == []


def test_import_rejects_truncated_relations_stream_without_repair(
    tmp_path: Path,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)
    stream = store.secure_path("normalized/relations.jsonl")
    stream.parent.mkdir(parents=True, exist_ok=True)
    stream.write_text('{"id": "rel-partial"', encoding="utf-8")
    before = stream.read_bytes()

    with pytest.raises(UsageError, match="导入前快照不可读"):
        import_source(store, envelope())

    assert stream.read_bytes() == before
    assert store.read_jsonl("normalized/evidence.jsonl") == []
    assert list(store.secure_path("imported").rglob("*")) == []


def test_import_retry_after_artifact_failure_writes_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    seed_entities(store)

    original = SnapshotStore.write_json
    failed = {"once": False}

    def fail_artifact_once(self, relpath, document, **kwargs):
        if str(relpath).startswith("imported/") and not failed["once"]:
            failed["once"] = True
            raise OSError("transient artifact failure")
        return original(self, relpath, document, **kwargs)

    monkeypatch.setattr(SnapshotStore, "write_json", fail_artifact_once)

    with pytest.raises(OSError, match="transient artifact failure"):
        import_source(store, envelope())

    assert store.read_jsonl("normalized/evidence.jsonl") == []
    assert list(store.secure_path("imported").rglob("*")) == []

    imported = import_source(store, envelope())

    evidence = store.read_jsonl("normalized/evidence.jsonl")
    assert any(item["id"] == imported["evidence_id"] for item in evidence)
    assert store.secure_path(imported["artifact"]).is_file()
    assert any(
        item["type"] == "verified_in_release"
        for item in store.read_jsonl("normalized/relations.jsonl")
    )
