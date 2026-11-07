from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import test_docs as doc_helpers
import test_releases as release_helpers

from rer.collectors.base import CollectContext
from rer.collectors.docs import collect_documents, load_doc_snapshots
from rer.collectors.progress import merge_coverage
from rer.collectors.releases import collect_releases, load_release_documents
from rer.config import build_config
from rer.github import GitHubClient
from rer.models import build_manifest
from rer.store import SnapshotStore, create_run, new_run_id

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
REVISION = "a" * 40
REPOSITORY = {"host": "github.com", "owner": "acme", "name": "demo"}
WINDOW = {
    "start": "2026-08-01T00:00:00Z",
    "end": "2026-11-01T00:00:00Z",
    "timezone": "UTC",
}


def make_run(tmp_path: Path) -> SnapshotStore:
    config = build_config(repository="acme/demo", timezone_name="UTC", now=NOW)
    store = create_run(tmp_path, config["repository"], new_run_id(NOW))
    store.write_json("config.resolved.json", config, schema_name="config")
    manifest = build_manifest(config, store.run_dir.name, "2026-11-01T08:00:00Z")
    manifest["stages"]["collect"] = "partial"
    store.write_manifest(manifest)
    return store


def make_client(store: SnapshotStore) -> GitHubClient:
    def handler(endpoint: str, input_text: str):
        if endpoint == "repos/acme/demo/releases":
            return release_helpers.http_response(200, [release_helpers.release_body()])
        if endpoint.startswith("repos/acme/demo/git/ref/tags/"):
            return release_helpers.http_response(
                200,
                release_helpers.tag_ref_body(),
            )
        if endpoint == "repos/acme/demo/contents/docs/design.md":
            return release_helpers.http_response(
                200,
                doc_helpers.content_body("docs/design.md", "hello world"),
            )
        return release_helpers.http_response(404, {"message": "unhandled"})

    return GitHubClient(
        store,
        release_helpers.BUDGET,
        gh_path="gh",
        runner=lambda argv, input_text: handler(
            argv[argv.index("--include") + 1],
            input_text,
        ),
        sleep=lambda _seconds: None,
        now=lambda: NOW,
        max_attempts=1,
    )


def collect_all(tmp_path: Path) -> tuple[SnapshotStore, dict[str, Any], dict[str, Any]]:
    store = make_run(tmp_path)
    client = make_client(store)
    context = CollectContext(
        client=client,
        store=store,
        repository=REPOSITORY,
        clock=lambda: "2026-11-01T08:00:10Z",
    )
    releases = collect_releases(context, activity_window=WINDOW)
    documents = collect_documents(
        context,
        source_profile=doc_helpers.source_profile(docs=["docs/design.md"]),
        revision=REVISION,
    )
    store.write_json(
        "analysis/source-profile.json",
        doc_helpers.source_profile(docs=["docs/design.md"]),
    )
    manifest = store.read_manifest()
    manifest["default_branch"] = "main"
    manifest["default_branch_sha"] = REVISION
    manifest["coverage"] = merge_coverage(
        manifest["coverage"],
        [*releases.coverage, *documents.coverage],
    )
    store.write_manifest(manifest)
    return store, releases, documents


def problems(report: dict[str, Any], code: str) -> list[dict[str, Any]]:
    return [item for item in report["problems"] if item["problem"] == code]


def test_verify_integrity_accepts_release_and_doc_documents(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)

    report = store.verify_integrity()

    assert report["ok"] is True
    assert report["text_blobs_checked"] >= 2
    assert load_release_documents(store)
    assert load_doc_snapshots(store)


def test_verify_integrity_reports_missing_release_coverage(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)
    manifest = store.read_manifest()
    manifest["coverage"] = [
        item for item in manifest["coverage"] if item["channel"] != "releases"
    ]
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "missing_release_coverage")


def test_verify_integrity_reports_missing_docs_coverage(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)
    manifest = store.read_manifest()
    manifest["coverage"] = [
        item for item in manifest["coverage"] if item["channel"] != "docs"
    ]
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "missing_docs_coverage")


def test_verify_integrity_reports_stale_release_projection(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for entity in entities:
        if entity["kind"] == "release":
            entity["raw_state"]["commit_sha"] = "b" * 40
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "stale_release_projection")

    repaired = store.verify_integrity(repair=True)
    assert repaired["repaired"] is True
    assert repaired["ok"] is True


def test_verify_integrity_reports_stale_doc_projection(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for entity in entities:
        if entity["kind"] == "doc":
            entity["raw_state"]["status"] = "not_found"
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "stale_doc_projection")

    repaired = store.verify_integrity(repair=True)
    assert repaired["repaired"] is True
    assert repaired["ok"] is True


def test_verify_integrity_reports_missing_release_and_doc_blobs(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)
    release = load_release_documents(store)[0]
    document = load_doc_snapshots(store)[0]
    release_blob = store.secure_path(Path("blobs") / release["body"]["sha256"])
    doc_blob = store.secure_path(Path("blobs") / document["text"]["sha256"])
    release_blob.unlink()
    doc_blob.unlink()

    report = store.verify_integrity()

    assert not report["ok"]
    found = problems(report, "missing_text_blob")
    artifacts = {item["artifact"] for item in found}
    assert "raw/releases/1.json" in artifacts
    assert "raw/docs/" + REVISION + "/docs/design.md.json" in artifacts


def test_cli_collect_integrates_releases_and_docs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from rer import cli

    store = make_run(tmp_path)

    def handler(endpoint: str, input_text: str):
        if endpoint == "repos/acme/demo/releases":
            return release_helpers.http_response(200, [release_helpers.release_body()])
        if endpoint.startswith("repos/acme/demo/git/ref/tags/"):
            return release_helpers.http_response(
                200,
                release_helpers.tag_ref_body(),
            )
        if endpoint == "repos/acme/demo/contents/docs/design.md":
            return release_helpers.http_response(
                200,
                doc_helpers.content_body("docs/design.md", "hello world"),
            )
        return release_helpers.http_response(404, {"message": "unhandled"})

    def factory(client_store, budget, **kwargs):
        return GitHubClient(
            client_store,
            budget,
            gh_path="gh",
            runner=lambda argv, input_text: handler(
                argv[argv.index("--include") + 1],
                input_text,
            ),
            sleep=lambda _seconds: None,
            now=lambda: NOW,
            max_attempts=1,
        )

    monkeypatch.setattr(cli, "GitHubClient", factory)
    monkeypatch.setattr(
        cli,
        "collect_repository",
        lambda *args, **kwargs: SimpleNamespace(
            coverage=[],
            source_profile=doc_helpers.source_profile(docs=["docs/design.md"]),
            default_branch="main",
            default_branch_sha=REVISION,
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
            selection={},
            details=[],
            counts={"details_written": 0, "selected": 0},
        ),
    )

    args = cli.build_parser().parse_args(["collect", "--run", str(store.run_dir)])
    result, _ = cli.command_collect(args)

    assert result["ok"] is True
    manifest = store.read_manifest()
    channels = {item["channel"]: item for item in manifest["coverage"]}
    assert channels["releases"]["coverage_status"] == "complete"
    assert channels["docs"]["coverage_status"] == "complete"
    assert load_release_documents(store)
    assert load_doc_snapshots(store)


def test_verify_integrity_reports_invalid_doc_path(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)
    document = dict(load_doc_snapshots(store)[0])
    document["path"] = "../secret.md"
    store.write_json(f"raw/docs/{REVISION}/docs/evil.json", document)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "invalid_doc_path")


def test_verify_integrity_reports_docs_coverage_mismatch(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)
    manifest = store.read_manifest()
    for item in manifest["coverage"]:
        if item["channel"] == "docs":
            item["expected_count"] = 5
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "docs_coverage_mismatch")


def test_verify_integrity_reports_releases_coverage_mismatch(tmp_path: Path) -> None:
    store, _, _ = collect_all(tmp_path)
    manifest = store.read_manifest()
    for item in manifest["coverage"]:
        if item["channel"] == "releases":
            item["fetched_count"] = item["expected_count"] + 1
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "releases_coverage_mismatch")


def test_cli_collect_runs_pr_detail_before_release_and_docs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from rer import cli

    store = make_run(tmp_path)
    calls: list[str] = []

    def factory(client_store, budget, **kwargs):
        return GitHubClient(
            client_store,
            budget,
            gh_path="gh",
            runner=lambda argv, input_text: None,
            sleep=lambda _seconds: None,
            now=lambda: NOW,
            max_attempts=1,
        )

    def record(name: str, **fields):
        return lambda *args, **kwargs: (
            calls.append(name) or SimpleNamespace(**fields)
        )

    monkeypatch.setattr(cli, "GitHubClient", factory)
    monkeypatch.setattr(
        cli,
        "collect_repository",
        record(
            "repository",
            coverage=[],
            source_profile=doc_helpers.source_profile(),
            default_branch="main",
            default_branch_sha=REVISION,
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_activity",
        record(
            "activity",
            coverage=[],
            overview=None,
            counts={},
            candidate_count=0,
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_pull_requests",
        record(
            "pr_detail",
            coverage=[],
            selection={},
            details=[],
            counts={"details_written": 0, "selected": 0},
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_releases",
        record(
            "releases",
            coverage=[],
            documents=[],
            counts={"written": 0, "selected": 0},
        ),
    )
    monkeypatch.setattr(
        cli,
        "collect_documents",
        record(
            "docs",
            coverage=[],
            documents=[],
            counts={"read": 0, "selected": 0, "reused": 0},
        ),
    )

    args = cli.build_parser().parse_args(["collect", "--run", str(store.run_dir)])
    result, _ = cli.command_collect(args)

    assert result["ok"] is True
    assert calls == ["repository", "activity", "pr_detail", "releases", "docs"]
