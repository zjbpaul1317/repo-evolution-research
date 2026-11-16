from __future__ import annotations

# Reuse the deterministic fake transport from the collector test modules so the
# golden fixtures and behavioral tests exercise the same payloads.
import json
from pathlib import Path
from typing import Any

import test_activity as activity_helpers
import test_pull_requests as pr_helpers

from rer.collectors.pr_store import load_pr_document
from rer.collectors.pull_requests import collect_pull_requests
from rer.collectors.status import (
    PR_DETAIL_SECTIONS,
    combine_status,
    derive_detail_status,
    derive_limitations,
    derive_pr_detail_channel_status,
)

FIXTURES = Path(__file__).parent / "fixtures" / "collect"


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def collect_pr(tmp_path: Path, handler=None):
    store, client = pr_helpers.make_client(
        tmp_path,
        handler or pr_helpers.default_handler(),
    )
    pr_helpers.seed(store, [10])
    result = collect_pull_requests(
        client,
        store,
        pr_helpers.REPOSITORY,
        activity_window=pr_helpers.WINDOW,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    document = load_pr_document(store, 10)
    assert document is not None
    selection = json.loads(
        (store.run_dir / "analysis/pr-selection.json").read_text(encoding="utf-8")
    )
    return store, result, document, selection


def test_pr_detail_matches_golden(tmp_path: Path) -> None:
    _, _, document, _ = collect_pr(tmp_path)
    assert document == load("pr_detail.complete.json")


def test_pr_selection_matches_golden(tmp_path: Path) -> None:
    _, _, _, selection = collect_pr(tmp_path)
    assert selection == load("pr_selection.complete.json")


def test_partial_truncated_scenario_matches_golden(tmp_path: Path) -> None:
    handler = pr_helpers.default_handler(
        details={10: pr_helpers.detail_body(10, changed_files=5)}
    )
    _, _, document, _ = collect_pr(tmp_path, handler)
    assert document == load("pr_detail.partial_truncated.json")
    assert document["files"]["truncated"] is True
    assert document["coverage"]["detail"] == "partial"


def test_merged_at_without_merged_flag_still_marks_squash_unverified(
    tmp_path: Path,
) -> None:
    handler = pr_helpers.default_handler(
        details={
            10: pr_helpers.detail_body(
                10,
                merged=False,
                merged_at="2026-10-20T10:00:00Z",
            )
        }
    )
    _, _, document, _ = collect_pr(tmp_path, handler)
    assert document["merged"] is True
    assert "squash_rebase_unverified" in document["limitations"]


def test_head_changed_scenario_matches_golden(tmp_path: Path) -> None:
    handler = pr_helpers.default_handler(
        detail_sequence={
            10: [
                pr_helpers.detail_body(10, head_sha=pr_helpers.HEAD),
                pr_helpers.detail_body(10, head_sha=pr_helpers.OTHER_HEAD),
            ]
        }
    )
    _, _, document, _ = collect_pr(tmp_path, handler)
    assert document == load("pr_detail.head_changed.json")
    assert document["head_changed"] is True
    assert document["coverage"]["detail"] == "partial"


def test_pr_detail_status_invariants(tmp_path: Path) -> None:
    _, result, document, selection = collect_pr(tmp_path)
    assert document["coverage"]["detail"] == derive_detail_status(
        sections={key: document[key] for key in PR_DETAIL_SECTIONS},
        head_changed=document["head_changed"],
        head_unverified=document["head_sha"] is None or document["head_sha_end"] is None,
        diff_status=document["diff"]["status"],
    )
    for key in PR_DETAIL_SECTIONS:
        assert document["coverage"][key] == document[key]["status"]
    assert document["limitations"] == derive_limitations(document)

    counts = selection["counts"]
    candidates = selection["candidates"]
    assert counts["selected"] == sum(1 for item in candidates if item["selected"])
    assert counts["skipped_quota"] == sum(
        1 for item in candidates if item["status"] == "skipped_quota"
    )
    assert counts["failed"] == sum(1 for item in candidates if item["status"] == "failed")

    assert result.coverage[0]["coverage_status"] == derive_pr_detail_channel_status(
        selected=counts["selected"],
        failed=counts["failed"],
        skipped_unchanged=counts["skipped_unchanged"],
        results=[document],
    )


def test_entity_projection_matches_document_status(tmp_path: Path) -> None:
    store, _, document, _ = collect_pr(tmp_path)
    assert not (store.run_dir / "analysis/pr-details").exists()
    assert store.read_jsonl("raw/pr_details.jsonl") == []

    entities = store.read_jsonl("normalized/entities.jsonl")
    entity = next(item for item in entities if item["id"].endswith("/pull/10"))
    deep_read = entity["raw_state"]["deep_read"]
    assert deep_read == {
        "detail_path": "raw/pr-details/10.json",
        "head_sha": pr_helpers.HEAD,
        "observed_at": "2026-11-01T08:00:10Z",
    }
    assert "deep_read" not in entity["normalized_state"]


def test_manifest_coverage_complete_matches_golden(tmp_path: Path, monkeypatch) -> None:
    code, _, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    assert code == 0
    assert store.read_manifest()["coverage"] == load(
        "manifest_coverage.complete.json"
    )


def test_manifest_coverage_partial_matches_golden(tmp_path: Path, monkeypatch) -> None:
    code, _, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(
            details={10: pr_helpers.detail_body(10, changed_files=5)}
        ),
    )
    assert code == 0
    assert store.read_manifest()["coverage"] == load(
        "manifest_coverage.partial.json"
    )


def test_activity_overview_matches_golden(tmp_path: Path) -> None:
    item = activity_helpers.pr_item(10, merged_at="2026-09-20T10:00:00Z")

    def search(query: str):
        if "is:merged" in query:
            return activity_helpers.search_body([item])
        return activity_helpers.empty_search()

    _, result = activity_helpers.run_activity(
        tmp_path,
        activity_helpers.make_runner(search_handler=search),
    )
    assert result.overview == load("activity_overview.complete.json")


def test_activity_coverage_uses_combined_view_status(tmp_path: Path) -> None:
    item = activity_helpers.pr_item(10, merged_at="2026-09-20T10:00:00Z")

    def search(query: str):
        if "is:merged" in query:
            return activity_helpers.search_body([item])
        return activity_helpers.empty_search()

    _, result = activity_helpers.run_activity(
        tmp_path,
        activity_helpers.make_runner(search_handler=search),
    )
    coverage = {entry["channel"]: entry for entry in result.coverage}
    pr_view_statuses = [
        view["status"] for view in result.overview["views"] if view["kind"] == "pull_request"
    ]
    assert coverage["activity_prs"]["coverage_status"] == combine_status(pr_view_statuses)
