from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_pull_requests as pr_helpers

from rer.collectors import pull_requests as pr_module
from rer.collectors.entity_store import detail_path
from rer.collectors.pr_store import (
    PR_DETAIL_DIR,
    iter_pr_documents,
    load_pr_document,
    load_pr_summaries,
    reconcile_entity_projection,
    write_pr_document,
)
from rer.collectors.progress import CollectProgress
from rer.collectors.pull_requests import collect_pull_requests
from rer.errors import StoreError
from rer.models import make_entity

LEGACY_PATHS = (
    "raw/pr_details.jsonl",
    "raw/pr_files.jsonl",
    "raw/pr_commits.jsonl",
    "raw/pr_issue_comments.jsonl",
    "raw/pr_reviews.jsonl",
    "raw/pr_review_comments.jsonl",
    "analysis/pr-details",
)


def collect_once(tmp_path: Path):
    store, client = pr_helpers.make_client(tmp_path, pr_helpers.default_handler())
    pr_helpers.seed(store, [10])
    collect_pull_requests(
        client,
        store,
        pr_helpers.REPOSITORY,
        activity_window=pr_helpers.WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    return store


def durable_documents(store) -> list[int]:
    return [int(document["number"]) for document in iter_pr_documents(store)]


def pull_entity(store, number: int = 10) -> dict:
    return next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["id"].endswith(f"/pull/{number}")
    )


def test_collect_writes_only_the_authoritative_document(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    assert (store.run_dir / PR_DETAIL_DIR / "10.json").is_file()
    assert durable_documents(store) == [10]
    for relpath in LEGACY_PATHS:
        assert not (store.run_dir / relpath).exists()

    document = load_pr_document(store, 10)
    assert document is not None
    assert document["coverage"]["detail"] == "complete"
    assert load_pr_summaries(store)[10]["head_sha"] == pr_helpers.HEAD

    deep_read = pull_entity(store)["raw_state"]["deep_read"]
    assert deep_read == {
        "detail_path": detail_path(10),
        "head_sha": pr_helpers.HEAD,
        "observed_at": "2026-11-01T08:00:10Z",
    }
    assert "deep_read" not in pull_entity(store)["normalized_state"]


def test_write_document_rejects_inconsistent_status(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    document = load_pr_document(store, 10)
    assert document is not None
    document["files"]["status"] = "partial"
    document["coverage"]["files"] = "partial"

    with pytest.raises(StoreError, match="coverage.detail"):
        write_pr_document(store, document)


def test_write_document_rejects_inconsistent_limitations(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    document = load_pr_document(store, 10)
    assert document is not None
    document["limitations"] = []

    with pytest.raises(StoreError, match="limitations"):
        write_pr_document(store, document)


def test_resume_uses_document_without_rewriting_it(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    before = (store.run_dir / detail_path(10)).read_bytes()

    client = pr_helpers.build_client(store, pr_helpers.default_handler())
    result = collect_pull_requests(
        client,
        store,
        pr_helpers.REPOSITORY,
        activity_window=pr_helpers.WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["skipped_unchanged"] == 1
    assert result.counts["details_written"] == 0
    assert result.coverage[0]["coverage_status"] == "complete"
    assert (store.run_dir / detail_path(10)).read_bytes() == before


def test_reconcile_entity_projection_is_noop_when_consistent(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    assert reconcile_entity_projection(store) is False


def test_resume_restores_entity_projection(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for entity in entities:
        if entity["id"].endswith("/pull/10"):
            entity["raw_state"].pop("deep_read", None)
            entity["normalized_state"].pop("deep_read", None)
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    client = pr_helpers.build_client(store, pr_helpers.default_handler())
    result = collect_pull_requests(
        client,
        store,
        pr_helpers.REPOSITORY,
        activity_window=pr_helpers.WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["skipped_unchanged"] == 1
    deep_read = pull_entity(store)["raw_state"]["deep_read"]
    assert deep_read["detail_path"] == detail_path(10)
    assert deep_read["head_sha"] == pr_helpers.HEAD


def test_reconcile_repairs_stale_pointer_fields(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for entity in entities:
        if entity["id"].endswith("/pull/10"):
            entity["raw_state"]["deep_read"]["head_sha"] = "b" * 40
            entity["raw_state"]["deep_read"]["status"] = "complete"
            entity["normalized_state"]["deep_read"] = True
            entity["normalized_state"]["files_truncated"] = False
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    assert reconcile_entity_projection(store) is True
    entity = pull_entity(store)
    assert entity["raw_state"]["deep_read"] == {
        "detail_path": detail_path(10),
        "head_sha": pr_helpers.HEAD,
        "observed_at": "2026-11-01T08:00:10Z",
    }
    assert "deep_read" not in entity["normalized_state"]
    assert "files_truncated" not in entity["normalized_state"]


def test_reconcile_runs_before_selection(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    before = json.loads(
        (store.run_dir / "analysis/pr-selection.json").read_text(encoding="utf-8")
    )
    (store.run_dir / "normalized/entities.jsonl").unlink()

    client = pr_helpers.build_client(store, pr_helpers.default_handler())
    result = collect_pull_requests(
        client,
        store,
        pr_helpers.REPOSITORY,
        activity_window=pr_helpers.WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert result.counts["selected"] == 1
    assert result.counts["skipped_unchanged"] == 1
    candidate = next(
        item for item in result.selection["candidates"] if item["number"] == 10
    )
    assert "discovered:pr_detail" in candidate["selection_reason"]
    assert candidate["score_components"]["discovery_reasons"] == 5
    assert [
        item["number"] for item in result.selection["candidates"] if item["selected"]
    ] == [
        item["number"] for item in before["candidates"] if item["selected"]
    ]
    assert result.selection["counts"]["selected"] == before["counts"]["selected"]
    assert pull_entity(store)["raw_state"]["deep_read"]["head_sha"] == pr_helpers.HEAD


def test_reconcile_prunes_projection_without_document(tmp_path: Path) -> None:
    store = collect_once(tmp_path)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for entity in entities:
        if entity["id"].endswith("/pull/10"):
            entity["raw_state"].pop("deep_read", None)
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [
            make_entity(
                entity_id="github.com/acme/demo/pull/99",
                kind="pull_request",
                title="PR 99",
                url="https://github.com/acme/demo/pull/99",
                observed_at="2026-11-01T00:00:00Z",
                raw_state={
                    "deep_read": {
                        "detail_path": detail_path(99),
                        "head_sha": pr_helpers.HEAD,
                        "observed_at": "2026-11-01T00:00:00Z",
                    }
                },
                discovery_reasons=["pr_detail"],
                number=99,
            )
        ],
        key="id",
        schema_name="entity",
    )

    assert reconcile_entity_projection(store) is True
    assert pull_entity(store)["raw_state"]["deep_read"]["head_sha"] == pr_helpers.HEAD
    pruned = pull_entity(store, 99)
    assert "deep_read" not in pruned["raw_state"]
    assert "deep_read" not in pruned["normalized_state"]


def test_document_is_commit_point_when_entity_projection_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, client = pr_helpers.make_client(tmp_path, pr_helpers.default_handler())
    pr_helpers.seed(store, [10])

    def boom(*args, **kwargs):
        raise StoreError("projection failed", source="store.test")

    monkeypatch.setattr(pr_module, "project_pr_detail", boom)
    with pytest.raises(StoreError):
        collect_pull_requests(
            client,
            store,
            pr_helpers.REPOSITORY,
            activity_window=pr_helpers.WINDOW,
            progress=CollectProgress(store, "0" * 64),
            clock=lambda: "2026-11-01T08:00:10Z",
        )
    assert load_pr_document(store, 10) is not None
    assert "deep_read" not in pull_entity(store)["raw_state"]
    selection = store.read_json("analysis/pr-selection.json")
    assert selection["candidates"][0]["status"] == "failed"
    assert selection["candidates"][0]["skipped_reason"] == "store_error"
    stage = store.read_checkpoint("collect")["stages"]["pr_detail"]
    assert stage == {"status": "partial", "completed": [], "pending": [10]}

    monkeypatch.undo()
    client2 = pr_helpers.build_client(store, pr_helpers.default_handler())
    second = collect_pull_requests(
        client2,
        store,
        pr_helpers.REPOSITORY,
        activity_window=pr_helpers.WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    assert second.counts["skipped_unchanged"] == 1
    assert pull_entity(store)["raw_state"]["deep_read"]["head_sha"] == pr_helpers.HEAD
