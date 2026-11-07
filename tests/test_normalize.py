from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import test_pull_requests as pr_helpers

from rer import cli
from rer.collectors.docs import doc_path
from rer.collectors.pr_store import load_pr_documents, write_pr_document
from rer.collectors.proposal_store import (
    write_discussion_document,
    write_issue_document,
)
from rer.collectors.releases import write_release_document
from rer.collectors.status import derive_limitations
from rer.collectors.texts import text_ref
from rer.config import build_config
from rer.errors import StoreError
from rer.import_source import import_source
from rer.models import (
    build_manifest,
    entity_id,
    make_entity,
    make_evidence,
    make_locator,
    make_relation,
    validate_document,
)
from rer.normalize import (
    MAX_EVIDENCE_BLOCKS,
    TextSource,
    _write_relations,
    build_evidence_set,
    evidence_id,
    split_blocks,
    write_evidence,
)
from rer.store import SnapshotStore, create_run, new_run_id

NOW = "2026-11-01T08:00:00Z"
BLOB = "0" * 64


def make_empty_run(tmp_path: Path) -> SnapshotStore:
    config = build_config(
        repository="acme/demo",
        timezone_name="UTC",
        now=None,
    )
    store = create_run(tmp_path, config["repository"], new_run_id())
    manifest = build_manifest(config, store.run_dir.name, NOW)
    manifest["stages"]["collect"] = "complete"
    manifest["state"] = "collecting"
    store.write_manifest(manifest)
    store.write_json("config.resolved.json", config, schema_name="config")
    return store


def prepare(store: SnapshotStore) -> dict:
    args = cli.build_parser().parse_args(["prepare", "--run", str(store.run_dir)])
    result, _lines = cli.command_prepare(args)
    return result


def test_split_blocks_uses_paragraphs_and_exact_ranges() -> None:
    text = "first paragraph\n\nsecond paragraph\n\nthird"
    blocks = split_blocks(text)
    assert [block.text for block in blocks] == [
        "first paragraph\n",
        "second paragraph\n",
        "third",
    ]
    for block in blocks:
        assert text[block.start : block.end] == block.text


def test_split_blocks_caps_long_lines_and_paragraphs() -> None:
    text = ("a" * 4500) + "\n" + ("b" * 9000)
    blocks = split_blocks(text, max_chars=4000)
    assert blocks
    assert all(len(block.text) <= 4000 for block in blocks)
    for block in blocks:
        assert text[block.start : block.end] == block.text
    assert "".join(block.text for block in blocks) == text


def test_evidence_block_budget_is_per_document() -> None:
    text = "\n\n".join(f"paragraph {index}" for index in range(MAX_EVIDENCE_BLOCKS + 5))
    source = TextSource(
        artifact="raw/docs/abc/docs/large.md.json",
        document_key="raw/docs/abc/docs/large.md.json",
        json_pointer="/text",
        entity_id="github.com/acme/demo/abc/docs/large.md",
        source_kind="doc_content",
        source_url="https://github.com/acme/demo/blob/abc/docs/large.md",
        observed_at=NOW,
        source_event_at=None,
        source_revision="a" * 40,
        blob_sha256=BLOB,
        text=text,
    )
    complete, _, gaps = build_evidence_set([source])
    assert len(complete) == MAX_EVIDENCE_BLOCKS + 5
    assert "paragraph 204" in complete[-1]["text"]
    assert not gaps
    records, pairs, truncated = build_evidence_set([source], max_blocks=MAX_EVIDENCE_BLOCKS)
    assert len(records) == MAX_EVIDENCE_BLOCKS
    assert len(pairs) == MAX_EVIDENCE_BLOCKS
    assert truncated[0]["problem"] == "evidence_truncated"
    assert truncated[0]["blocks"] == MAX_EVIDENCE_BLOCKS + 5
    assert truncated[0]["kept"] == MAX_EVIDENCE_BLOCKS


def test_prepare_empty_collected_run(tmp_path: Path) -> None:
    store = make_empty_run(tmp_path)
    report = prepare(store)

    assert report["result"]["evidence_blocks"] == 0
    assert report["result"]["relations"] == 0
    assert report["result"]["has_evidence"] is False
    assert report["result"]["partial"] is True
    assert any("no_research_evidence" in item for item in report["warnings"])
    assert store.read_jsonl("normalized/evidence.jsonl") == []
    assert store.read_jsonl("normalized/relations.jsonl") == []
    manifest = store.read_manifest()
    assert manifest["state"] == "prepared"
    assert manifest["stages"]["prepare"] == "complete"


def test_prepare_allows_partial_collect(tmp_path: Path) -> None:
    store = make_empty_run(tmp_path)
    manifest = store.read_manifest()
    manifest["stages"]["collect"] = "partial"
    manifest["state"] = "partial"
    store.write_manifest(manifest)

    result = prepare(store)

    assert result["ok"] is True
    assert store.read_manifest()["state"] == "prepared"


def test_empty_prepare_returns_partial_exit_and_preserves_import_path(tmp_path, capsys) -> None:
    store = make_empty_run(tmp_path)
    assert cli.main(["prepare", "--run", str(store.run_dir)]) == 4
    result = json.loads(capsys.readouterr().out)
    assert result["result"]["has_evidence"] is False
    assert store.read_manifest()["stages"]["prepare"] == "complete"



def test_prepare_requires_collected_run(tmp_path: Path) -> None:
    config = build_config(repository="acme/demo", timezone_name="UTC", now=None)
    store = create_run(tmp_path, config["repository"], new_run_id())
    store.write_manifest(build_manifest(config, store.run_dir.name, NOW))
    store.write_json("config.resolved.json", config, schema_name="config")
    args = cli.build_parser().parse_args(["prepare", "--run", str(store.run_dir)])

    with pytest.raises(Exception) as error:
        cli.command_prepare(args)

    assert getattr(error.value, "code", None) == "usage_error"


def test_prepare_is_idempotent_and_offline(tmp_path: Path, monkeypatch) -> None:
    _code, run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    requests_before = store.secure_path("requests.jsonl").read_text(encoding="utf-8")
    first = prepare(store)
    evidence = store.secure_path("normalized/evidence.jsonl").read_bytes()
    relations = store.secure_path("normalized/relations.jsonl").read_bytes()
    second = prepare(store)

    assert first["result"]["evidence_blocks"] == second["result"]["evidence_blocks"]
    assert first["result"]["relations"] == second["result"]["relations"]
    assert first["result"]["evidence_derived"] == first["result"]["evidence_blocks"]
    assert first["result"]["evidence_total"] == first["result"]["evidence_derived"]
    assert first["result"]["relations_derived"] == first["result"]["relations"]
    assert first["result"]["relations_total"] == first["result"]["relations_derived"]
    assert store.secure_path("normalized/evidence.jsonl").read_bytes() == evidence
    assert store.secure_path("normalized/relations.jsonl").read_bytes() == relations
    assert store.secure_path("requests.jsonl").read_text(encoding="utf-8") == requests_before
    report = store.read_json("analysis/prepare-report.json")
    validate_document(report, "prepare_report")
    assert report["evidence_derived"] == len(store.read_jsonl("normalized/evidence.jsonl"))
    assert report["relations_derived"] == len(store.read_jsonl("normalized/relations.jsonl"))
    assert store.verify_integrity()["ok"] is True

    documents = load_pr_documents(store)
    assert documents
    entities = store.read_jsonl("normalized/entities.jsonl")
    assert any(entity["kind"] == "commit" for entity in entities)
    assert any(
        relation["type"] == "contains_commit"
        for relation in store.read_jsonl("normalized/relations.jsonl")
    )


def test_missing_text_blob_names_the_artifact(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    document = load_pr_documents(store)[0]
    digest = document["body"]["sha256"]
    assert isinstance(digest, str)
    store.secure_path(Path("blobs") / digest).unlink()

    args = cli.build_parser().parse_args(["prepare", "--run", str(store.run_dir)])
    with pytest.raises(StoreError) as error:
        cli.command_prepare(args)

    assert "raw/pr-details" in error.value.message
    assert error.value.details["artifact"].startswith("raw/pr-details/")
    assert error.value.details["json_pointer"] == "/body"


FIXTURES = Path(__file__).parent / "fixtures" / "example"
TEXT_REF_KEYS = {"preview", "sha256", "bytes", "preview_truncated"}


def materialize(store: SnapshotStore, value):
    if isinstance(value, dict):
        if TEXT_REF_KEYS <= set(value):
            return text_ref(store, value.get("preview", ""))
        return {key: materialize(store, item) for key, item in value.items()}
    if isinstance(value, list):
        return [materialize(store, item) for item in value]
    return value


def issue_entity(number: int = 42) -> dict:
    return make_entity(
        entity_id=entity_id("github.com", "acme", "demo", "issue", str(number)),
        kind="issue",
        title=f"#{number}",
        url=f"https://github.com/acme/demo/issues/{number}",
        observed_at=NOW,
        number=number,
    )


def discussion_entity(number: int = 7) -> dict:
    return make_entity(
        entity_id=entity_id("github.com", "acme", "demo", "discussion", str(number)),
        kind="discussion",
        title=f"#{number}",
        url=f"https://github.com/acme/demo/discussions/{number}",
        observed_at=NOW,
        number=number,
    )


def release_entity(tag: str = "v1.2.0") -> dict:
    return make_entity(
        entity_id=entity_id("github.com", "acme", "demo", "release", tag),
        kind="release",
        title=tag,
        url=f"https://github.com/acme/demo/releases/tag/{tag}",
        observed_at=NOW,
        raw_state={"tag_name": tag},
    )


def doc_entity(revision: str, path: str) -> dict:
    return make_entity(
        entity_id=entity_id(
            "github.com",
            "acme",
            "demo",
            "doc",
            path,
            revision=revision,
        ),
        kind="doc",
        title=path,
        url=f"https://github.com/acme/demo/blob/{revision}/{path}",
        observed_at=NOW,
        raw_state={"revision": revision, "path": path},
    )


def test_prepare_covers_every_text_source_kind(tmp_path: Path) -> None:
    store = make_empty_run(tmp_path)
    revision = "a" * 40
    entities = [
        issue_entity(),
        discussion_entity(),
        release_entity(),
        doc_entity(revision, "docs/design.md"),
    ]
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")

    issue = materialize(store, json.loads((FIXTURES / "issue_detail.sample.json").read_text()))
    issue["repository"] = {"host": "github.com", "owner": "acme", "name": "demo"}
    issue["url"] = "https://github.com/acme/demo/issues/42"
    write_issue_document(store, issue)

    discussion = materialize(
        store,
        json.loads((FIXTURES / "discussion_detail.sample.json").read_text()),
    )
    discussion["repository"] = {"host": "github.com", "owner": "acme", "name": "demo"}
    discussion["url"] = "https://github.com/acme/demo/discussions/7"
    write_discussion_document(store, discussion)

    release = materialize(
        store,
        json.loads((FIXTURES / "release_detail.sample.json").read_text()),
    )
    write_release_document(store, release)

    doc = materialize(store, json.loads((FIXTURES / "doc_snapshot.sample.json").read_text()))
    doc["revision"] = revision
    doc["url"] = f"https://github.com/acme/demo/blob/{revision}/docs/design.md"
    doc["path"] = "docs/design.md"
    doc["size"] = len(doc["text"]["preview"])
    doc["chars"] = len(doc["text"]["preview"])
    doc["blob_sha256"] = doc["text"]["sha256"]
    store.write_json(doc_path(revision, "docs/design.md"), doc, schema_name="doc_snapshot")

    older = copy.deepcopy(doc)
    older["revision"] = "b" * 40
    older["url"] = f"https://github.com/acme/demo/blob/{'b' * 40}/docs/design.md"
    store.write_json(
        doc_path("b" * 40, "docs/design.md"),
        older,
        schema_name="doc_snapshot",
    )

    result = prepare(store)
    evidence = store.read_jsonl("normalized/evidence.jsonl")
    kinds = {item["source_kind"] for item in evidence}

    assert result["result"]["text_sources"] == 7
    assert kinds == {
        "issue_body",
        "issue_comment",
        "discussion_body",
        "discussion_comment",
        "release_body",
        "doc_content",
    }
    assert all(item["source_revision"] != "b" * 40 for item in evidence)


def test_evidence_id_matches_frozen_formula() -> None:
    source = TextSource(
        artifact="raw/docs/abc/docs/design.md.json",
        document_key="raw/docs/abc/docs/design.md.json",
        json_pointer="/text",
        entity_id="github.com/acme/demo/abc/docs/design.md",
        source_kind="doc_content",
        source_url="https://github.com/acme/demo/blob/abc/docs/design.md",
        observed_at=NOW,
        source_event_at=None,
        source_revision="a" * 40,
        blob_sha256=BLOB,
        text="hello",
    )
    block = split_blocks("hello")[0]
    material = (
        f"doc_content|{source.entity_id}|{source.source_url}|"
        f"{source.artifact}|{BLOB}|{block.start}|{block.end}"
    )
    expected = f"ev-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"
    assert evidence_id(source, block) == expected


def text_source(
    source_url: str,
    *,
    text: str = "+1",
    source_kind: str = "pull_request_comment",
) -> TextSource:
    return TextSource(
        artifact="raw/pr-details/10.json",
        document_key="raw/pr-details/10.json",
        json_pointer="/issue_comments/items/0/body",
        entity_id=entity_id("github.com", "acme", "demo", "pull_request", "10"),
        source_kind=source_kind,
        source_url=source_url,
        observed_at=NOW,
        source_event_at=NOW,
        source_revision=None,
        blob_sha256=BLOB,
        text=text,
    )


def test_evidence_ids_include_source_url_and_reject_collisions() -> None:
    first = text_source("https://github.com/acme/demo/pull/10#issuecomment-1")
    second = text_source("https://github.com/acme/demo/pull/10#issuecomment-2")
    records, _pairs, _truncated = build_evidence_set([first, second])
    assert len(records) == 2
    assert len({record["id"] for record in records}) == 2
    assert {record["source_url"] for record in records} == {
        first.source_url,
        second.source_url,
    }

    with pytest.raises(StoreError) as error:
        build_evidence_set([first, first])
    assert error.value.code == "store_error"

    same_commit_from_other_pr = replace(first, artifact="raw/pr-details/11.json")
    records, _pairs, _truncated = build_evidence_set(
        [first, same_commit_from_other_pr]
    )
    assert len({record["id"] for record in records}) == 2


def test_prepare_replaces_stale_derived_records(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    prepare(store)
    old = store.read_jsonl("normalized/evidence.jsonl")
    assert any(item["source_kind"] == "pull_request_comment" for item in old)
    assert any(item["source_kind"] == "commit_message" for item in old)

    document = load_pr_documents(store)[0]
    document["issue_comments"]["items"] = []
    document["reviews"]["items"] = []
    document["review_comments"]["items"] = []
    document["commits"]["items"] = []
    document["limitations"] = derive_limitations(document)
    write_pr_document(store, document)

    prepare(store)
    evidence = store.read_jsonl("normalized/evidence.jsonl")
    relations = store.read_jsonl("normalized/relations.jsonl")
    entities = store.read_jsonl("normalized/entities.jsonl")
    report = store.read_json("analysis/prepare-report.json")

    assert not any(item["source_kind"] == "pull_request_comment" for item in evidence)
    assert not any(item["source_kind"] == "commit_message" for item in evidence)
    assert not any(item["type"] == "contains_commit" for item in relations)
    assert not any(entity["kind"] == "commit" for entity in entities)
    assert report["evidence_total"] == len(evidence)
    assert report["relations_total"] == len(relations)
    assert report["evidence_derived"] <= report["evidence_total"]


def test_prepare_preserves_imported_evidence(tmp_path: Path) -> None:
    store = make_empty_run(tmp_path)
    repository = make_entity(
        entity_id=entity_id("github.com", "acme", "demo", "repository", ""),
        kind="repository",
        title="demo",
        url="https://github.com/acme/demo",
        observed_at=NOW,
    )
    store.write_jsonl("normalized/entities.jsonl", [repository], key="id")
    payload = b"imported"
    digest = hashlib.sha256(payload).hexdigest()
    evidence = make_evidence(
        evidence_id="ev-imported",
        entity_id=repository["id"],
        source_url="https://example.com/api",
        source_kind="api_response",
        observed_at=NOW,
        text="imported",
        locator=make_locator(
            "char_range",
            char_start=0,
            char_end=8,
            line_start=1,
            line_end=1,
        ),
        blob_sha256=digest,
    )
    store.write_evidence(evidence, payload)

    result = prepare(store)

    records = store.read_jsonl("normalized/evidence.jsonl")
    assert [item["id"] for item in records] == ["ev-imported"]
    assert result["result"]["evidence_blocks"] == 0
    report = store.read_json("analysis/prepare-report.json")
    assert report["evidence_derived"] == 0
    assert report["evidence_total"] == 1


def test_prepare_fails_when_pr_entity_is_missing(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    entities = [
        entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if entity.get("kind") not in ("pull_request", "commit")
    ]
    store.write_jsonl("normalized/entities.jsonl", entities, key="id")

    with pytest.raises(Exception) as error:
        prepare(store)

    assert getattr(error.value, "code", None) == "validation_failed"
    manifest = store.read_manifest()
    assert manifest["stages"]["prepare"] == "failed"
    assert manifest["state"] == "failed"


def test_repeated_prepare_preserves_downstream_until_evidence_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    prepare(store)
    manifest = store.read_manifest()
    manifest["stages"]["research"] = "complete"
    manifest["stages"]["validate"] = "complete"
    manifest["stages"]["render"] = "complete"
    manifest["state"] = "rendered"
    store.write_manifest(manifest)

    prepare(store)
    manifest = store.read_manifest()
    assert manifest["stages"]["research"] == "complete"
    assert manifest["stages"]["validate"] == "complete"
    assert manifest["stages"]["render"] == "complete"

    document = load_pr_documents(store)[0]
    document["body"] = text_ref(store, "Changed body")
    write_pr_document(store, document)
    prepare(store)
    manifest = store.read_manifest()
    assert manifest["stages"]["research"] == "not_started"
    assert manifest["stages"]["validate"] == "not_started"
    assert manifest["stages"]["render"] == "not_started"
    assert any("downstream stages invalidated" in note for note in manifest["notes"])


def test_text_mode_prints_warnings(capsys) -> None:
    from rer.cli import _emit_success

    _emit_success(
        {"warnings": ["evidence_truncated: raw/docs/abc.json"]},
        ["ok"],
        "text",
    )
    captured = capsys.readouterr()
    assert "warning: evidence_truncated: raw/docs/abc.json" in captured.err


def test_write_evidence_missing_blob_reports_artifact(tmp_path: Path) -> None:
    store = make_empty_run(tmp_path)
    source = text_source("https://github.com/acme/demo/pull/10#issuecomment-1")
    records, _pairs, _truncated = build_evidence_set([source])
    contexts = {
        record["id"]: {
            "artifact": source.artifact,
            "json_pointer": source.json_pointer,
        }
        for record in records
    }

    with pytest.raises(StoreError) as error:
        write_evidence(store, records, contexts=contexts)

    assert error.value.details["artifact"] == source.artifact
    assert error.value.details["json_pointer"] == source.json_pointer


def test_prepare_merges_imported_relation_upgrade(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    pull_identifier = entity_id("github.com", "acme", "demo", "pull_request", "10")
    issue_identifier = entity_id("github.com", "acme", "demo", "issue", "42")
    entities = store.read_jsonl("normalized/entities.jsonl")
    entities.append(
        make_entity(
            entity_id=issue_identifier,
            kind="issue",
            title="#42",
            url="https://github.com/acme/demo/issues/42",
            observed_at=NOW,
            number=42,
        )
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    document = load_pr_documents(store)[0]
    document["body"] = text_ref(store, "See #42.")
    document["limitations"] = derive_limitations(document)
    write_pr_document(store, document)

    first = prepare(store)
    assert first["result"]["relations_merged"] == 0
    assert any(
        relation["from_id"] == pull_identifier
        and relation["to_id"] == issue_identifier
        and relation["type"] == "mentions"
        for relation in store.read_jsonl("normalized/relations.jsonl")
    )

    imported = import_source(
        store,
        {
            "schema_version": "1.3",
            "source_url": "https://example.com/mentions-confirmation",
            "retrieved_at": NOW,
            "provider": "agent-web",
            "content_type": "text/plain",
            "content": "Confirmed the mentions link.",
            "entity_id": issue_identifier,
            "source_kind": "external_page",
            "relations": [
                {
                    "from_id": pull_identifier,
                    "to_id": issue_identifier,
                    "type": "mentions",
                    "basis": "text_explicit",
                    "verification": "verified",
                }
            ],
        },
    )

    second = prepare(store)
    assert second["result"]["relations_merged"] == 1
    matches = [
        relation
        for relation in store.read_jsonl("normalized/relations.jsonl")
        if relation["from_id"] == pull_identifier
        and relation["to_id"] == issue_identifier
        and relation["type"] == "mentions"
    ]
    assert len(matches) == 1
    assert matches[0]["verification"] == "verified"
    assert matches[0]["scope"]["generator"] == "import-source"
    assert imported["evidence_id"] in matches[0]["evidence_ids"]


def test_prepare_preserves_imported_relation(tmp_path: Path) -> None:
    store = make_empty_run(tmp_path)
    repository = make_entity(
        entity_id=entity_id("github.com", "acme", "demo", "repository", ""),
        kind="repository",
        title="demo",
        url="https://github.com/acme/demo",
        observed_at=NOW,
    )
    issue = make_entity(
        entity_id=entity_id("github.com", "acme", "demo", "issue", "1"),
        kind="issue",
        title="#1",
        url="https://github.com/acme/demo/issues/1",
        observed_at=NOW,
        number=1,
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        [repository, issue],
        key="id",
    )
    imported = make_relation(
        relation_id="rel-imported",
        from_id=issue["id"],
        to_id=repository["id"],
        relation_type="related_candidate",
        basis="agent_inferred",
        verification="candidate",
        evidence_ids=[],
        scope={"generator": "agent"},
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        [imported],
        key="id",
        schema_name="relation",
    )

    result = prepare(store)

    relations = store.read_jsonl("normalized/relations.jsonl")
    assert [item["id"] for item in relations] == ["rel-imported"]
    assert result["result"]["relations"] == 0
    report = store.read_json("analysis/prepare-report.json")
    assert report["relations_derived"] == 0
    assert report["relations_total"] == 1


def test_write_relations_merges_derived_into_imported(tmp_path: Path) -> None:
    store = make_empty_run(tmp_path)
    imported = make_relation(
        relation_id="rel-shared",
        from_id="github.com/acme/demo/pull/1",
        to_id="github.com/acme/demo/issue/2",
        relation_type="mentions",
        basis="text_explicit",
        verification="verified",
        evidence_ids=["ev-import"],
        scope={"generator": "import-source"},
    )
    derived = make_relation(
        relation_id="rel-shared",
        from_id="github.com/acme/demo/pull/1",
        to_id="github.com/acme/demo/issue/2",
        relation_type="mentions",
        basis="text_explicit",
        verification="candidate",
        evidence_ids=["ev-derived"],
        scope={"generator": "prepare"},
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        [imported],
        key="id",
        schema_name="relation",
    )

    total, merged = _write_relations(store, [derived])

    assert (total, merged) == (1, 1)
    relations = store.read_jsonl("normalized/relations.jsonl")
    assert len(relations) == 1
    relation = relations[0]
    assert relation["id"] == "rel-shared"
    assert relation["verification"] == "verified"
    assert relation["scope"]["generator"] == "import-source"
    assert relation["evidence_ids"] == ["ev-derived", "ev-import"]


def test_write_relations_keeps_imported_dispute(tmp_path: Path) -> None:
    store = make_empty_run(tmp_path)
    imported = make_relation(
        relation_id="rel-shared",
        from_id="github.com/acme/demo/pull/1",
        to_id="github.com/acme/demo/issue/2",
        relation_type="mentions",
        basis="text_explicit",
        verification="disputed",
        evidence_ids=["ev-import"],
        scope={"generator": "import-source"},
    )
    derived = make_relation(
        relation_id="rel-shared",
        from_id="github.com/acme/demo/pull/1",
        to_id="github.com/acme/demo/issue/2",
        relation_type="mentions",
        basis="text_explicit",
        verification="verified",
        evidence_ids=["ev-derived"],
        scope={"generator": "prepare"},
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        [imported],
        key="id",
        schema_name="relation",
    )

    total, merged = _write_relations(store, [derived])

    assert (total, merged) == (1, 1)
    relation = store.read_jsonl("normalized/relations.jsonl")[0]
    assert relation["verification"] == "disputed"
    assert relation["evidence_ids"] == ["ev-derived", "ev-import"]
    assert relation["scope"]["generator"] == "import-source"


def test_write_relations_consolidates_duplicate_imported_identity(
    tmp_path: Path,
) -> None:
    store = make_empty_run(tmp_path)
    first = make_relation(
        relation_id="rel-a",
        from_id="github.com/acme/demo/pull/1",
        to_id="github.com/acme/demo/issue/2",
        relation_type="mentions",
        basis="text_explicit",
        verification="verified",
        evidence_ids=["ev-a"],
        scope={"generator": "import-source"},
    )
    second = make_relation(
        relation_id="rel-b",
        from_id="github.com/acme/demo/pull/1",
        to_id="github.com/acme/demo/issue/2",
        relation_type="mentions",
        basis="text_explicit",
        verification="disputed",
        evidence_ids=["ev-b"],
        scope={"generator": "import-source"},
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        [first, second],
        key="id",
        schema_name="relation",
    )

    total, merged = _write_relations(store, [])

    assert (total, merged) == (1, 0)
    relation = store.read_jsonl("normalized/relations.jsonl")[0]
    assert relation["id"] == "rel-a"
    assert relation["verification"] == "disputed"
    assert relation["evidence_ids"] == ["ev-a", "ev-b"]
    assert relation["scope"]["aliases"] == ["rel-b"]
    assert len(relation["scope"]["observations"]) == 2


def test_prepare_prunes_stale_external_entities(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    document = load_pr_documents(store)[0]
    document["body"] = text_ref(store, "See https://github.com/other/project/issues/7")
    document["limitations"] = derive_limitations(document)
    write_pr_document(store, document)
    prepare(store)

    external_id = "github.com/other/project/issue/7"
    entities = store.read_jsonl("normalized/entities.jsonl")
    assert any(entity["id"] == external_id for entity in entities)

    document["body"] = text_ref(store, "No references now.")
    document["limitations"] = derive_limitations(document)
    write_pr_document(store, document)
    prepare(store)

    entities = store.read_jsonl("normalized/entities.jsonl")
    relations = store.read_jsonl("normalized/relations.jsonl")
    assert not any(entity["id"] == external_id for entity in entities)
    assert not any(relation["to_id"] == external_id for relation in relations)


def test_split_blocks_handles_non_positive_max_chars() -> None:
    text = "abc"

    blocks = split_blocks(text, max_chars=0)

    assert "".join(block.text for block in blocks) == text
    for block in blocks:
        assert text[block.start : block.end] == block.text
