from __future__ import annotations

from pathlib import Path
from typing import Any

import test_proposals as proposal_helpers
import test_t10_integrity as t10_helpers

from rer.collectors.base import CollectContext
from rer.collectors.progress import merge_coverage
from rer.collectors.proposal_store import (
    load_discussion_documents,
    load_issue_documents,
)
from rer.collectors.proposals import collect_proposals
from rer.github import GitHubClient
from rer.store import SnapshotStore

NOW = proposal_helpers.NOW
WINDOW = proposal_helpers.WINDOW
REPOSITORY = proposal_helpers.REPOSITORY


def make_client(store: SnapshotStore) -> GitHubClient:
    def handler(argv: list[str], input_text: str):
        if argv[1:3] == ["api", "graphql"]:
            return proposal_helpers.http_response(
                200,
                proposal_helpers.discussion_payload(),
            )
        endpoint = argv[argv.index("--include") + 1]
        if endpoint.endswith("/comments") or endpoint.endswith("/timeline"):
            return proposal_helpers.http_response(200, [])
        return proposal_helpers.http_response(200, proposal_helpers.issue_body())

    return GitHubClient(
        store,
        proposal_helpers.BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: handler(argv, input_text),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )


def collect_all(tmp_path: Path) -> tuple[SnapshotStore, dict[str, Any]]:
    store = t10_helpers.make_run(tmp_path)
    proposal_helpers.write_issue_stream(store, [proposal_helpers.issue_body()])
    proposal_helpers.write_discussion_stream(
        store,
        [proposal_helpers.discussion_raw()],
    )
    client = make_client(store)
    context = CollectContext(
        client=client,
        store=store,
        repository=REPOSITORY,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    result = collect_proposals(context, activity_window=WINDOW)
    manifest = store.read_manifest()
    manifest["coverage"] = merge_coverage(manifest["coverage"], result.coverage)
    store.write_manifest(manifest)
    return store, result.selection


def problems(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [item for item in report["problems"] if item["problem"] == code]


def test_verify_integrity_accepts_proposal_documents(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)

    report = store.verify_integrity()

    assert report["ok"] is True
    assert report["text_blobs_checked"] >= 2
    assert load_issue_documents(store)
    assert load_discussion_documents(store)


def test_verify_integrity_reports_missing_issue_coverage(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    manifest = store.read_manifest()
    manifest["coverage"] = [
        item for item in manifest["coverage"] if item["channel"] != "issue_detail"
    ]
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "missing_issue_coverage")


def test_verify_integrity_reports_missing_discussion_coverage(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    manifest = store.read_manifest()
    manifest["coverage"] = [
        item
        for item in manifest["coverage"]
        if item["channel"] != "discussion_detail"
    ]
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "missing_discussion_coverage")


def test_verify_integrity_reports_missing_proposal_selection(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    store.secure_path("analysis/proposal-selection.json").unlink()

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "missing_proposal_selection")


def test_verify_integrity_reports_selection_counts_mismatch(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    selection = store.read_json("analysis/proposal-selection.json")
    selection["counts"]["selected"] = 99
    store.write_json(
        "analysis/proposal-selection.json",
        selection,
        schema_name="proposal_selection",
    )

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "proposal_selection_counts_mismatch")


def test_verify_integrity_reports_coverage_mismatch(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    manifest = store.read_manifest()
    for item in manifest["coverage"]:
        if item["channel"] == "issue_detail":
            item["fetched_count"] = 0
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "issue_coverage_mismatch")


def test_verify_integrity_reports_stale_projection_and_repairs(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for entity in entities:
        if entity["kind"] == "issue" and "proposal_read" in entity.get(
            "raw_state", {}
        ):
            entity["raw_state"]["proposal_read"]["observed_at"] = "2020-01-01T00:00:00Z"
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "stale_issue_projection")

    repaired = store.verify_integrity(repair=True)
    assert repaired["repaired"] is True
    assert repaired["ok"] is True


def test_verify_integrity_reports_missing_proposal_blob(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    document = load_issue_documents(store)[0]
    blob = store.secure_path(Path("blobs") / document["body"]["sha256"])
    blob.unlink()

    report = store.verify_integrity()

    assert not report["ok"]
    found = problems(report, "missing_text_blob")
    assert any(item["artifact"] == "raw/issues/42.json" for item in found)


def test_verify_integrity_reports_document_number_mismatch(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    document = load_issue_documents(store)[0]
    store.write_json("raw/issues/7.json", document, schema_name="issue_detail")

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "issue_document_number_mismatch")


def test_cli_collect_integrates_proposals(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from rer import cli

    store = t10_helpers.make_run(tmp_path)
    proposal_helpers.write_issue_stream(store, [proposal_helpers.issue_body()])
    proposal_helpers.write_discussion_stream(
        store,
        [proposal_helpers.discussion_raw()],
    )
    client = make_client(store)

    def factory(client_store, budget, **kwargs):
        return client

    monkeypatch.setattr(cli, "GitHubClient", factory)
    monkeypatch.setattr(
        cli,
        "collect_repository",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            source_profile={},
            default_branch="main",
            default_branch_sha="a" * 40,
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_activity",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            overview=None,
            counts={},
            candidate_count=0,
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_pull_requests",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            selection={"candidates": []},
            details=[],
            counts={"details_written": 0, "selected": 0},
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_releases",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            documents=[],
            counts={"written": 0, "selected": 0},
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_documents",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            documents=[],
            counts={"read": 0, "selected": 0, "reused": 0},
        ),
    )

    args = cli.build_parser().parse_args(["collect", "--run", str(store.run_dir)])
    result, _ = cli.command_collect(args)

    assert result["ok"] is True
    manifest = store.read_manifest()
    channels = {item["channel"]: item for item in manifest["coverage"]}
    assert channels["issue_detail"]["coverage_status"] == "complete"
    assert channels["discussion_detail"]["coverage_status"] == "complete"
    assert store.secure_path("analysis/proposal-selection.json").is_file()
    assert load_issue_documents(store)
    assert load_discussion_documents(store)


def test_verify_integrity_reports_missing_issue_document(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    store.secure_path("raw/issues/42.json").unlink()
    entities = store.read_jsonl("normalized/entities.jsonl")
    for entity in entities:
        if entity["kind"] == "issue":
            entity["raw_state"].pop("proposal_read", None)
            entity["normalized_state"].pop("scope", None)
            entity["normalized_state"].pop("role_hints", None)
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "missing_issue_document")


def test_verify_integrity_reports_detail_path_mismatch(tmp_path: Path) -> None:
    store, _ = collect_all(tmp_path)
    selection = store.read_json("analysis/proposal-selection.json")
    issue = next(
        candidate
        for candidate in selection["candidates"]
        if candidate["carrier"] == "issue"
    )
    issue["detail_path"] = "raw/issues/99.json"
    store.write_json(
        "analysis/proposal-selection.json",
        selection,
        schema_name="proposal_selection",
    )

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "proposal_detail_path_mismatch")
