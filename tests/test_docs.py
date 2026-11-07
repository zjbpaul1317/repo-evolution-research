from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rer.collectors.base import CollectContext
from rer.collectors.docs import (
    collect_documents,
    doc_path,
    load_doc_snapshots,
    load_revision_snapshots,
)
from rer.errors import GitHubAuthError
from rer.github import CommandResult, GitHubClient
from rer.models import make_evidence, make_locator, make_relation
from rer.store import SnapshotStore, create_run, new_run_id

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
REVISION = "a" * 40
OTHER_REVISION = "b" * 40
BUDGET = {
    "max_requests": 100,
    "max_discovery_entities": 100,
    "max_deep_entities": 40,
    "max_deep_prs": 22,
    "max_relation_hops": 2,
    "max_external_entities": 5,
    "max_response_bytes": 1024 * 1024,
    "max_total_bytes": 8 * 1024 * 1024,
    "max_runtime_minutes": 35,
    "max_pack_chars": 100000,
}
REPOSITORY = {"host": "github.com", "owner": "acme", "name": "demo"}


def http_response(status: int, body: Any) -> CommandResult:
    lines = [f"HTTP/2.0 {status} OK", "", json.dumps(body)]
    return CommandResult(0 if status < 400 else 1, "\n".join(lines), "")


def content_body(path: str, text: str, *, sha: str = "c" * 40) -> dict[str, Any]:
    raw = text.encode("utf-8")
    return {
        "path": path,
        "encoding": "base64",
        "size": len(raw),
        "sha": sha,
        "html_url": f"https://github.com/acme/demo/blob/{REVISION}/{path}",
        "content": base64.b64encode(raw).decode("ascii"),
    }


def source_profile(
    *,
    files: list[dict[str, Any]] | None = None,
    docs: list[str] | None = None,
    design: list[str] | None = None,
    rfc: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "discovery": {
            "files": files or [],
            "docs_paths": docs or [],
            "design_paths": design or [],
            "rfc_paths": rfc or [],
        }
    }


def context_for(
    client: GitHubClient,
    store: SnapshotStore,
    clock,
) -> CollectContext:
    return CollectContext(
        client=client,
        store=store,
        repository=REPOSITORY,
        clock=clock,
    )


def make_client(tmp_path: Path, handler) -> tuple[SnapshotStore, GitHubClient]:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    client = GitHubClient(
        store,
        BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: handler(
            argv[argv.index("--include") + 1],
            input_text,
        ),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )
    return store, client


def test_collects_fixed_revision_document(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(endpoint: str, input_text: str) -> CommandResult:
        calls.append(endpoint)
        if endpoint == "repos/acme/demo/contents/docs/design.md":
            return http_response(200, content_body("docs/design.md", "hello world"))
        return http_response(404, {"message": "unhandled"})

    store, client = make_client(tmp_path, handler)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )

    assert result.counts["selected"] == 1
    assert result.counts["read"] == 1
    assert result.coverage[0]["coverage_status"] == "complete"
    document = load_doc_snapshots(store)[0]
    assert document["revision"] == REVISION
    assert document["status"] == "read"
    assert document["text"]["preview"] == "hello world"
    assert store.read_blob(document["text"]["sha256"]) == b"hello world"
    assert store.secure_path(
        doc_path(REVISION, "docs/design.md")
    ).is_file()

    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["kind"] == "doc"
    )
    assert entity["roles"] == ["design"]
    assert entity["raw_state"]["detail_path"] == doc_path(REVISION, "docs/design.md")


def test_budget_exhaustion_marks_run_level_budget(tmp_path: Path) -> None:
    store, _client = make_client(tmp_path, lambda endpoint, input_text: http_response(404, {}))
    client = GitHubClient(
        store,
        {**BUDGET, "max_requests": 0},
        gh_path="gh",
        runner=lambda argv, input_text: http_response(404, {}),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )
    context = context_for(client, store, lambda: "2026-11-01T08:00:10Z")

    result = collect_documents(
        context,
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )

    assert context.budget_exhausted is True
    assert result.counts["budget_skipped"] == 1
    assert result.coverage[0]["coverage_status"] == "partial"
    assert "budget_skipped" in result.coverage[0]["truncation_reason"]


def test_reuses_existing_fixed_revision_snapshot(tmp_path: Path) -> None:
    calls: list[str] = []

    def first_handler(endpoint: str, input_text: str) -> CommandResult:
        if endpoint == "repos/acme/demo/contents/docs/design.md":
            return http_response(200, content_body("docs/design.md", "hello world"))
        return http_response(404, {"message": "unhandled"})

    store, client = make_client(tmp_path, first_handler)
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )
    calls.clear()

    def second_handler(endpoint: str, input_text: str) -> CommandResult:
        calls.append(endpoint)
        return http_response(500, {"message": "must not be called"})

    client2 = GitHubClient(
        store,
        BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: second_handler(
            argv[argv.index("--include") + 1],
            input_text,
        ),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )
    result = collect_documents(
        context_for(client2, store, lambda: "2026-11-01T08:00:20Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )

    assert result.counts["reused"] == 1
    assert result.counts["fetched"] == 0
    assert calls == []


def test_missing_page_is_documented_and_partial(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(404, {"message": "not found"})

    store, client = make_client(tmp_path, handler)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/missing.md"]),
        revision=REVISION,
    )

    document = load_doc_snapshots(store)[0]
    assert document["status"] == "not_found"
    assert document["text"] is None
    assert "doc_not_found" in document["limitations"]
    assert result.coverage[0]["coverage_status"] == "partial"
    assert result.coverage[0]["fetched_count"] == 0


def test_non_utf8_document_keeps_blob_and_status(tmp_path: Path) -> None:
    payload = b"\xff\xfe"

    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(
            200,
            {
                "path": "docs/raw.md",
                "encoding": "base64",
                "size": len(payload),
                "sha": "d" * 40,
                "html_url": f"https://github.com/acme/demo/blob/{REVISION}/docs/raw.md",
                "content": base64.b64encode(payload).decode("ascii"),
            },
    )

    store, client = make_client(tmp_path, handler)
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/raw.md"]),
        revision=REVISION,
    )

    document = load_doc_snapshots(store)[0]
    assert document["status"] == "unsupported_encoding"
    assert document["text"] is None
    assert document["blob_sha256"] is not None
    assert store.read_blob(document["blob_sha256"]) == payload


def test_too_large_document_is_recorded(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(
            200,
            {
                "path": "docs/large.md",
                "encoding": "base64",
                "size": 600000,
                "sha": "e" * 40,
                "content": "",
            },
    )

    store, client = make_client(tmp_path, handler)
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/large.md"]),
        revision=REVISION,
    )

    document = load_doc_snapshots(store)[0]
    assert document["status"] == "too_large"
    assert document["size"] == 600000
    assert document["text"] is None


def test_include_docs_false_skips_requests(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        raise AssertionError("docs collector must not make requests")

    store, client = make_client(tmp_path, handler)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
        include_docs=False,
    )

    assert result.coverage[0]["coverage_status"] == "not_requested"
    assert result.counts["selected"] == 0
    assert load_doc_snapshots(store) == []


def test_no_document_candidates_is_not_applicable(tmp_path: Path) -> None:
    store, client = make_client(tmp_path, lambda endpoint, input_text: None)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(),
        revision=REVISION,
    )

    assert result.coverage[0]["coverage_status"] == "not_applicable"
    assert result.counts["selected"] == 0


def test_moved_revision_creates_new_snapshot_path(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(200, content_body("docs/design.md", "hello world"))

    store, client = make_client(tmp_path, handler)
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:20Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=OTHER_REVISION,
    )

    assert store.secure_path(doc_path(REVISION, "docs/design.md")).is_file()
    assert store.secure_path(doc_path(OTHER_REVISION, "docs/design.md")).is_file()


def test_profile_file_is_reused_without_http(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        raise AssertionError("profile file should avoid HTTP")

    store, client = make_client(tmp_path, handler)
    payload = b"profile document"
    digest = store.save_blob(payload)
    profile = source_profile(
        files=[
            {
                "path": "README.md",
                "kind": "readme",
                "status": "read",
                "size": len(payload),
                "chars": len(payload),
                "blob_sha256": digest,
                "sha": "f" * 40,
                "url": f"https://github.com/acme/demo/blob/{REVISION}/README.md",
            }
        ],
        docs=["README.md"],
    )
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=profile,
        revision=REVISION,
    )

    document = load_doc_snapshots(store)[0]
    assert document["text"]["preview"] == "profile document"
    assert result.counts["profile_reused"] == 1
    assert result.counts["fetched"] == 0


def test_doc_path_rejects_traversal() -> None:
    with pytest.raises(ValueError):
        doc_path(REVISION, "../secret.md")


def test_empty_document_is_readable(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(200, content_body("docs/empty.md", ""))

    store, client = make_client(tmp_path, handler)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/empty.md"]),
        revision=REVISION,
    )

    document = load_doc_snapshots(store)[0]
    assert document["status"] == "read"
    assert document["text"]["sha256"] is None
    assert document["chars"] == 0
    assert "empty_document" in document["limitations"]
    assert result.coverage[0]["coverage_status"] == "complete"


def test_document_selection_truncation_is_visible(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        path = endpoint.split("/contents/", 1)[1]
        return http_response(200, content_body(path, "page"))

    store, client = make_client(tmp_path, handler)
    paths = [f"docs/page-{index}.md" for index in range(45)]
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=paths),
        revision=REVISION,
    )

    assert result.counts["selected"] == 20
    assert result.counts["selection_truncated"] == 1
    assert result.coverage[0]["coverage_status"] == "partial"
    assert "docs_selection_truncated" in result.coverage[0]["truncation_reason"]


def test_docs_auth_failure_is_fatal(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(401, {"message": "bad credentials"})

    store, client = make_client(tmp_path, handler)
    with pytest.raises(GitHubAuthError):
        collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )


def test_non_text_candidates_are_filtered(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        path = endpoint.split("/contents/", 1)[1]
        return http_response(200, content_body(path, "manual"))

    store, client = make_client(tmp_path, handler)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(
            docs=["docs/logo.png", "docs/manual.md", "docs/archive.zip"]
        ),
        revision=REVISION,
    )

    assert result.counts["selected"] == 1
    assert result.counts["filtered"] == 2
    assert result.coverage[0]["coverage_status"] == "complete"
    assert "non_text_paths_filtered:2" in result.coverage[0]["truncation_reason"]
    assert [document["path"] for document in load_doc_snapshots(store)] == [
        "docs/manual.md"
    ]


def test_only_non_text_candidates_is_not_applicable(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        raise AssertionError("non-text paths must not be fetched")

    store, client = make_client(tmp_path, handler)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/logo.png"]),
        revision=REVISION,
    )

    assert result.counts["selected"] == 0
    assert result.coverage[0]["coverage_status"] == "not_applicable"


def test_request_failures_make_channel_unavailable(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(422, {"message": "unprocessable"})

    store, client = make_client(tmp_path, handler)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )

    assert result.coverage[0]["coverage_status"] == "unavailable"
    assert result.coverage[0]["fetched_count"] == 0


def test_unreadable_content_stays_partial(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(404, {"message": "not found"})

    store, client = make_client(tmp_path, handler)
    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/missing.md"]),
        revision=REVISION,
    )

    assert result.coverage[0]["coverage_status"] == "partial"


def test_doc_projection_tracks_only_current_revision(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(200, content_body("docs/design.md", "hello"))

    store, client = make_client(tmp_path, handler)
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:20Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=OTHER_REVISION,
    )

    entities = [
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["kind"] == "doc"
    ]
    assert len(entities) == 1
    assert entities[0]["raw_state"]["revision"] == OTHER_REVISION


def test_revision_switch_keeps_imported_doc_endpoints(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(200, content_body("docs/design.md", "hello"))

    store, client = make_client(tmp_path, handler)
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )
    old_entity = next(
        entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if entity["kind"] == "doc"
    )
    text = "Imported confirmation for the archived design document."
    evidence = make_evidence(
        evidence_id="ev-import-doc",
        entity_id=old_entity["id"],
        source_url="https://example.com/archived-design",
        source_kind="external_page",
        observed_at="2026-11-01T08:00:00Z",
        text=text,
        locator=make_locator(
            "char_range",
            char_start=0,
            char_end=len(text),
            line_start=1,
            line_end=1,
        ),
        blob_sha256="0" * 64,
    )
    store.write_jsonl(
        "normalized/evidence.jsonl",
        [evidence],
        key="id",
        schema_name="evidence",
    )
    relation = make_relation(
        relation_id="rel-import-doc",
        from_id=old_entity["id"],
        to_id="github.com/acme/demo/repository/",
        relation_type="documents",
        basis="agent_inferred",
        verification="candidate",
        evidence_ids=[evidence["id"]],
        scope={"generator": "import-source"},
    )
    store.write_jsonl(
        "normalized/relations.jsonl",
        [relation],
        key="id",
        schema_name="relation",
    )

    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:20Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=OTHER_REVISION,
    )

    documents = {
        entity["id"]: entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if entity["kind"] == "doc"
    }
    assert old_entity["id"] in documents
    assert documents[old_entity["id"]]["raw_state"]["revision"] == REVISION
    assert any(
        entity["raw_state"]["revision"] == OTHER_REVISION
        for entity in documents.values()
    )


def test_corrupt_historical_snapshot_does_not_block_collect(tmp_path: Path) -> None:
    def handler(endpoint: str, input_text: str) -> CommandResult:
        return http_response(200, content_body("docs/design.md", "hello"))

    store, client = make_client(tmp_path, handler)
    collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:10Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )
    store.secure_path(doc_path(REVISION, "docs/design.md")).write_text(
        "{not json",
        encoding="utf-8",
    )

    result = collect_documents(
        context_for(client, store, lambda: "2026-11-01T08:00:20Z"),
        source_profile=source_profile(docs=["docs/design.md"]),
        revision=OTHER_REVISION,
    )

    assert result.coverage[0]["coverage_status"] == "complete"
    assert load_revision_snapshots(store, OTHER_REVISION)


def test_write_failure_finalizes_partial_then_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from rer.collectors import docs as docs_module
    from rer.collectors.progress import CollectProgress
    from rer.errors import StoreError

    def handler(endpoint: str, input_text: str) -> CommandResult:
        path = endpoint.split("/contents/", 1)[1]
        return http_response(200, content_body(path, "page"))

    store, client = make_client(tmp_path, handler)
    progress = CollectProgress(store, "a" * 64)
    context = CollectContext(
        client=client,
        store=store,
        repository=REPOSITORY,
        clock=lambda: "2026-11-01T08:00:10Z",
        progress=progress,
    )
    real_write = docs_module.write_doc_snapshot
    calls = {"count": 0}

    def fail_second(store_arg, document) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise StoreError("disk full", source="test.write")
        real_write(store_arg, document)

    monkeypatch.setattr(docs_module, "write_doc_snapshot", fail_second)

    with pytest.raises(StoreError):
        docs_module.collect_documents(
            context,
            source_profile=source_profile(docs=["docs/a.md", "docs/b.md"]),
            revision=REVISION,
        )

    checkpoint = store.read_checkpoint("collect")
    entries = {item["channel"]: item for item in checkpoint["coverage"]}
    assert entries["docs"]["coverage_status"] == "partial"
    assert entries["docs"]["fetched_count"] == 1
    assert checkpoint["stages"]["docs"]["status"] == "partial"
