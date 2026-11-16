from __future__ import annotations

from pathlib import Path

import test_pull_requests as pr_helpers

from rer.collectors.entity_store import detail_path
from rer.collectors.progress import merge_coverage
from rer.collectors.pull_requests import collect_pull_requests
from rer.integrity import SELECTION_PATH, _selection_required


def collect(tmp_path: Path, monkeypatch, handler=None) -> tuple[object, Path]:
    code, run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        handler or pr_helpers.default_handler(),
    )
    assert code == 0
    return store, run_dir


def problems(report, code: str) -> list[dict]:
    return [item for item in report["problems"] if item["problem"] == code]


def test_verify_integrity_accepts_authoritative_documents(tmp_path: Path, monkeypatch) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    report = store.verify_integrity()
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["manifest_hash_ok"] is True
    assert report["text_blobs_checked"] > 0


def test_verify_integrity_reports_missing_pr_text_blob(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    document = store.read_json(detail_path(10))
    digest = document["body"]["sha256"]
    assert isinstance(digest, str)
    store.secure_path(Path("blobs") / digest).unlink()

    report = store.verify_integrity()

    assert not report["ok"]
    found = problems(report, "missing_text_blob")
    assert found
    assert found[0]["artifact"] == detail_path(10)
    assert found[0]["json_path"] == "$.body"
    assert found[0]["recoverable"] is False


def test_verify_integrity_reports_nested_pr_text_blob_loss(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    document = store.read_json(detail_path(10))
    digest = document["files"]["items"][0]["patch"]["sha256"]
    assert isinstance(digest, str)
    store.secure_path(Path("blobs") / digest).unlink()

    report = store.verify_integrity()

    assert not report["ok"]
    found = problems(report, "missing_text_blob")
    assert found
    assert found[0]["json_path"] == "$.files.items[0].patch"


def test_verify_integrity_reports_pr_text_blob_size_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    document = store.read_json(detail_path(10))
    document["body"]["bytes"] += 1
    store.write_json(detail_path(10), document, schema_name="pr_detail")

    report = store.verify_integrity()

    assert not report["ok"]
    found = problems(report, "text_blob_size_mismatch")
    assert found
    assert found[0]["json_path"] == "$.body"


def test_verify_integrity_reports_pr_text_preview_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    document = store.read_json(detail_path(10))
    document["body"]["preview"] = "tampered"
    store.write_json(detail_path(10), document, schema_name="pr_detail")

    report = store.verify_integrity()

    assert not report["ok"]
    found = problems(report, "text_preview_mismatch")
    assert found
    assert found[0]["json_path"] == "$.body"


def test_verify_integrity_reports_pr_text_blob_hash_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    document = store.read_json(detail_path(10))
    digest = document["body"]["sha256"]
    assert isinstance(digest, str)
    payload = store.read_blob(digest)
    tampered = bytes([payload[0] ^ 0xFF]) + payload[1:]
    store.secure_path(Path("blobs") / digest).write_bytes(tampered)

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "text_blob_hash_mismatch")


def test_verify_integrity_repair_does_not_restore_pr_text_blob(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    document = store.read_json(detail_path(10))
    digest = document["body"]["sha256"]
    assert isinstance(digest, str)
    blob = store.secure_path(Path("blobs") / digest)
    blob.unlink()

    report = store.verify_integrity(repair=True)

    assert not report["ok"]
    assert report["repaired"] is False
    assert problems(report, "missing_text_blob")
    assert not blob.exists()


def test_verify_integrity_accepts_empty_pr_text_ref(tmp_path: Path, monkeypatch) -> None:
    store, _ = collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(details={10: pr_helpers.detail_body(10, body="")}),
    )

    report = store.verify_integrity()

    assert report["ok"] is True
    assert report["problems"] == []


def test_verify_integrity_reports_missing_document(tmp_path: Path, monkeypatch) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    (store.run_dir / detail_path(10)).unlink()

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "missing_pr_document")
    assert problems(report, "stale_deep_read")


def test_verify_integrity_repairs_deep_read_projection(tmp_path: Path, monkeypatch) -> None:
    store, _ = collect(tmp_path, monkeypatch)
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

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "missing_deep_read_projection")

    repaired = store.verify_integrity(repair=True)
    assert repaired["repaired"] is True
    assert repaired["ok"] is True
    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["id"].endswith("/pull/10")
    )
    assert entity["raw_state"]["deep_read"] == {
        "detail_path": detail_path(10),
        "head_sha": pr_helpers.HEAD,
        "observed_at": "2026-11-01T08:00:10Z",
    }
    assert "deep_read" not in entity["normalized_state"]


def test_verify_integrity_rebuilds_deleted_entity_stream(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    (store.run_dir / "normalized/entities.jsonl").unlink()

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "missing_deep_read_projection")

    repaired = store.verify_integrity(repair=True)
    assert repaired["repaired"] is True
    assert repaired["ok"] is True
    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["id"].endswith("/pull/10")
    )
    assert entity["raw_state"]["deep_read"]["detail_path"] == detail_path(10)


def test_verify_integrity_reports_legacy_entity_summary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    entities = store.read_jsonl("normalized/entities.jsonl")
    for entity in entities:
        if entity["id"].endswith("/pull/10"):
            entity["normalized_state"]["files_truncated"] = False
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "legacy_deep_read_projection")

    repaired = store.verify_integrity(repair=True)
    assert repaired["ok"] is True
    entity = next(
        item
        for item in store.read_jsonl("normalized/entities.jsonl")
        if item["id"].endswith("/pull/10")
    )
    assert "files_truncated" not in entity["normalized_state"]


def test_verify_integrity_reports_legacy_snapshot(tmp_path: Path, monkeypatch) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    store.secure_path("raw/pr_details.jsonl").write_text("", encoding="utf-8")
    legacy_dir = store.secure_path("analysis/pr-details")
    legacy_dir.mkdir()
    (legacy_dir / "10.json").write_text("{}", encoding="utf-8")

    report = store.verify_integrity()
    assert not report["ok"]
    assert len(problems(report, "legacy_snapshot")) == 2


def test_verify_integrity_reports_coverage_mismatch(tmp_path: Path, monkeypatch) -> None:
    handler = pr_helpers.default_handler(
        details={10: pr_helpers.detail_body(10, changed_files=5)}
    )
    store, _ = collect(tmp_path, monkeypatch, handler)
    manifest = store.read_manifest()
    entry = next(item for item in manifest["coverage"] if item["channel"] == "pr_detail")
    entry["coverage_status"] = "complete"
    entry["detail_status"] = "complete"
    store.write_manifest(manifest)

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "pr_detail_coverage_mismatch")


def test_verify_integrity_reports_invalid_document(tmp_path: Path, monkeypatch) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    store.secure_path(detail_path(10)).write_text("{}", encoding="utf-8")

    report = store.verify_integrity()
    assert not report["ok"]
    assert problems(report, "invalid_pr_document")


def test_selection_required_by_stage_and_pr_artifacts() -> None:
    assert not _selection_required(None, [])
    assert not _selection_required(
        {"stages": {"collect": "not_started"}, "coverage": []},
        [],
    )
    assert not _selection_required(
        {"stages": {"collect": "partial"}, "coverage": []},
        [],
    )
    assert _selection_required(
        {"stages": {"collect": "complete"}, "coverage": []},
        [],
    )
    assert _selection_required(
        {"stages": {"collect": "partial"}, "coverage": [{"channel": "pr_detail"}]},
        [],
    )
    assert _selection_required(None, [{"number": 10}])


def test_verify_integrity_reports_missing_selection(tmp_path: Path, monkeypatch) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    store.secure_path(SELECTION_PATH).unlink()

    report = store.verify_integrity()

    assert not report["ok"]
    assert problems(report, "missing_pr_selection")


def test_verify_integrity_reports_selection_counts_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    selection = store.read_json(SELECTION_PATH)
    selection["counts"]["failed"] += 1
    store.write_json(SELECTION_PATH, selection, schema_name="pr_selection")

    report = store.verify_integrity()

    assert not report["ok"]
    found = problems(report, "pr_selection_counts_mismatch")
    assert found
    assert found[0]["details"]["mismatched"]["failed"] == {
        "stored": 1,
        "derived": 0,
    }


def test_verify_integrity_reports_coverage_count_mismatch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    manifest = store.read_manifest()
    entry = next(item for item in manifest["coverage"] if item["channel"] == "pr_detail")
    entry["fetched_count"] = 0
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert not report["ok"]
    found = problems(report, "pr_detail_coverage_mismatch")
    assert found
    assert found[0]["details"]["derived_fetched_count"] == 1


def test_verify_integrity_accepts_retained_document_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, _ = collect(tmp_path, monkeypatch)
    base = pr_helpers.default_handler()

    def failing(endpoint: str, params: dict[str, str], headers: dict[str, str]):
        if endpoint == "repos/acme/demo/issues/10/comments":
            return pr_helpers.http_response(404, {"message": "not found"})
        return base(endpoint, params, headers)

    client = pr_helpers.build_client(store, failing)
    result = collect_pull_requests(
        client,
        store,
        pr_helpers.REPOSITORY,
        activity_window=pr_helpers.WINDOW,
        clock=lambda: "2026-11-01T08:00:20Z",
    )
    manifest = store.read_manifest()
    manifest["coverage"] = merge_coverage(manifest["coverage"], result.coverage)
    store.write_manifest(manifest)

    report = store.verify_integrity()

    assert report["ok"] is True
    entry = next(item for item in manifest["coverage"] if item["channel"] == "pr_detail")
    assert entry["coverage_status"] == "partial"
    assert entry["expected_count"] == 1
    assert entry["fetched_count"] == 0


def test_verify_integrity_accepts_fatal_not_attempted_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base = pr_helpers.default_handler()

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]):
        if endpoint == "repos/acme/demo/pulls/11/files":
            return pr_helpers.http_response(
                429,
                {"message": "slow down"},
                {"Retry-After": "120"},
            )
        return base(endpoint, params, headers)

    code, _, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        handler,
        numbers=[10, 11],
        records={
            10: pr_helpers.pr_record(
                10,
                merged_at="2026-10-01T00:00:00Z",
                comments=5,
            ),
            11: pr_helpers.pr_record(11, merged_at="2026-10-02T00:00:00Z"),
        },
    )

    assert code == 4
    selection = store.read_json(SELECTION_PATH)
    candidates = {item["number"]: item for item in selection["candidates"]}
    assert candidates[11]["status"] == "not_attempted"
    assert store.verify_integrity()["ok"] is True


def test_verify_integrity_accepts_failed_first_attempt_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base = pr_helpers.default_handler()

    def handler(endpoint: str, params: dict[str, str], headers: dict[str, str]):
        if endpoint == "repos/acme/demo/pulls/11":
            return pr_helpers.http_response(404, {"message": "not found"})
        return base(endpoint, params, headers)

    code, _, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        handler,
        numbers=[10, 11],
    )

    assert code == 0
    selection = store.read_json(SELECTION_PATH)
    candidates = {item["number"]: item for item in selection["candidates"]}
    assert candidates[11]["status"] == "failed"
    assert not (store.run_dir / detail_path(11)).exists()
    manifest = store.read_manifest()
    entry = next(item for item in manifest["coverage"] if item["channel"] == "pr_detail")
    assert entry["expected_count"] == 2
    assert entry["fetched_count"] == 1
    assert store.verify_integrity()["ok"] is True
