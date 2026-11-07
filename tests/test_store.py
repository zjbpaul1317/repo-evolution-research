from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rer.config import build_config
from rer.errors import ArtifactError, InternalError, LockError, SchemaError, StoreError
from rer.models import build_manifest
from rer.store import RUN_ID_RE, SHA256_RE, STAGE_RE, SnapshotStore, create_run, new_run_id

FIXTURES = Path(__file__).parent / "fixtures" / "example"
NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
CREATED_AT = "2026-11-01T08:00:00Z"


def make_store(tmp_path: Path) -> tuple[SnapshotStore, dict]:
    config = build_config(repository="example/demo", timezone_name="UTC", now=NOW)
    store = create_run(tmp_path, config["repository"], new_run_id(NOW))
    store.write_json("config.resolved.json", config, schema_name="config")
    store.write_manifest(build_manifest(config, store.run_dir.name, CREATED_AT))
    return store, config


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def write_fixture_entities(store: SnapshotStore) -> None:
    store.write_jsonl(
        "normalized/entities.jsonl",
        load_fixture("entities.sample.json"),
        key="id",
    )


def test_create_run_layout(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    assert store.run_dir.is_dir()
    subdirs = ("blobs", "raw", "normalized", "analysis/packs", "analysis/dossiers", "checkpoints")
    for subdir in subdirs:
        assert (store.run_dir / subdir).is_dir()


def test_create_run_refuses_existing_run(tmp_path: Path) -> None:
    config = build_config(repository="example/demo", timezone_name="UTC", now=NOW)
    run_id = new_run_id(NOW)
    create_run(tmp_path, config["repository"], run_id)
    with pytest.raises(StoreError):
        create_run(tmp_path, config["repository"], run_id)


def test_create_run_rejects_unsafe_repository_segments(tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        create_run(
            tmp_path,
            {"host": "github.com", "owner": "..", "name": "demo"},
            new_run_id(NOW),
        )


def test_blob_dedupe_and_hash_checks(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    digest = store.save_blob(b"hello")
    assert store.save_blob(b"hello") == digest
    assert store.read_blob(digest) == b"hello"
    assert len(list((store.run_dir / "blobs").iterdir())) == 1

    path = store.secure_path(Path("blobs") / digest)
    path.write_bytes(b"tampered")
    with pytest.raises(StoreError):
        store.read_blob(digest)


def test_write_json_is_atomic_and_schema_checked(tmp_path: Path) -> None:
    store, config = make_store(tmp_path)
    path = store.write_json("config.resolved.json", config, schema_name="config")
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert not list(store.run_dir.glob(".*.tmp-*"))

    broken = copy.deepcopy(config)
    broken["mode"] = "fast"
    with pytest.raises(SchemaError) as error:
        store.write_json("bad.json", broken, schema_name="config")
    assert error.value.exit_code == 2


def test_write_jsonl_deduplicates_by_id(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    records = [
        {"id": "a", "value": 1},
        {"id": "b", "value": 2},
        {"id": "a", "value": 1},
    ]
    store.write_jsonl("normalized/entities.jsonl", records)
    assert store.read_jsonl("normalized/entities.jsonl") == [
        {"id": "a", "value": 1},
        {"id": "b", "value": 2},
    ]


def test_upsert_jsonl_preserves_unrelated_records(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    stream = "normalized/entities.jsonl"
    store.write_jsonl(
        stream,
        [{"id": "github.com/o/r", "value": 1}, {"id": "github.com/o/r/issue/1", "value": 2}],
    )
    store.upsert_jsonl(stream, [{"id": "github.com/o/r", "value": 3}])
    assert store.read_jsonl(stream) == [
        {"id": "github.com/o/r", "value": 3},
        {"id": "github.com/o/r/issue/1", "value": 2},
    ]

    store.upsert_jsonl(
        stream,
        [
            {"id": "github.com/o/r/issue/2", "value": 4},
            {"id": "github.com/o/r/issue/1", "value": 5},
        ],
    )
    assert store.read_jsonl(stream) == [
        {"id": "github.com/o/r", "value": 3},
        {"id": "github.com/o/r/issue/1", "value": 5},
        {"id": "github.com/o/r/issue/2", "value": 4},
    ]


def test_upsert_jsonl_requires_key_and_validates_schema(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(StoreError):
        store.upsert_jsonl("normalized/entities.jsonl", [{"value": 1}])
    with pytest.raises(SchemaError):
        store.upsert_jsonl(
            "normalized/entities.jsonl",
            [{"id": "github.com/o/r/issue/1"}],
            schema_name="entity",
        )


def test_write_manifest_refreshes_updated_at(tmp_path: Path) -> None:
    store, config = make_store(tmp_path)
    manifest = build_manifest(config, store.run_dir.name, CREATED_AT)
    manifest["updated_at"] = "2000-01-01T00:00:00Z"
    store.write_manifest(manifest)
    assert manifest["updated_at"] != "2000-01-01T00:00:00Z"
    assert store.read_manifest() == manifest


def test_jsonl_schema_validation(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(SchemaError):
        store.write_jsonl(
            "normalized/entities.jsonl",
            [{"id": "github.com/o/r/issue/1"}],
            schema_name="entity",
        )
    with pytest.raises(SchemaError):
        store.append_jsonl(
            "normalized/entities.jsonl",
            {"id": "github.com/o/r/issue/1"},
            schema_name="entity",
        )


def test_regexes_reject_trailing_newline(tmp_path: Path) -> None:
    assert not RUN_ID_RE.fullmatch("20261101T080000Z-deadbeef\n")
    assert not SHA256_RE.fullmatch("a" * 64 + "\n")
    assert not STAGE_RE.fullmatch("collect\n")
    with pytest.raises(StoreError):
        create_run(
            tmp_path,
            {"host": "github.com", "owner": "o", "name": "r"},
            "20261101T080000Z-deadbeef\n",
        )


def test_write_jsonl_rejects_conflicting_duplicate(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(StoreError):
        store.write_jsonl(
            "normalized/entities.jsonl",
            [{"id": "a", "value": 1}, {"id": "a", "value": 2}],
        )


def test_append_jsonl_reuses_the_stream_index(tmp_path: Path, monkeypatch) -> None:
    store, _ = make_store(tmp_path)
    calls = 0
    original = store._load_jsonl_unlocked

    def counted(path: Path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(store, "_load_jsonl_unlocked", counted)
    for index in range(50):
        store.append_jsonl("normalized/entities.jsonl", {"id": str(index)})
    assert calls == 1
    assert len(store.read_jsonl("normalized/entities.jsonl")) == 50


def test_append_jsonl_can_replace_existing_record(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    stream = store.secure_path("normalized/entities.jsonl")
    store.append_jsonl(stream.relative_to(store.run_dir), {"id": "a", "value": 1})
    store.append_jsonl(
        stream.relative_to(store.run_dir),
        {"id": "a", "value": 2},
        on_conflict="replace",
    )
    assert store.read_jsonl("normalized/entities.jsonl") == [{"id": "a", "value": 2}]


def test_append_jsonl_recovers_partial_tail(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    stream = store.secure_path("normalized/entities.jsonl")
    store.append_jsonl(stream.relative_to(store.run_dir), {"id": "a", "value": 1})
    with stream.open("ab") as handle:
        handle.write(b'{"id": "b", "value":')

    with pytest.raises(StoreError):
        store.read_jsonl("normalized/entities.jsonl")

    assert store.read_jsonl("normalized/entities.jsonl", repair=True) == [
        {"id": "a", "value": 1}
    ]
    assert stream.read_bytes().endswith(b"\n")


def test_evidence_roundtrip_and_integrity(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    write_fixture_entities(store)
    evidence = load_fixture("evidence.sample.json")
    payload = load_fixture("payloads.json")["ev-001"].encode("utf-8")

    saved = store.write_evidence(evidence, payload)
    assert saved["blob_sha256"] == evidence["blob_sha256"]
    assert store.read_evidence() == [evidence]
    report = store.verify_integrity()
    assert report["ok"] is True
    assert report["manifest_hash_ok"] is True
    assert report["schema_ok"] is True
    assert report["quote_reverified"] == 1

    blob = store.secure_path(Path("blobs") / evidence["blob_sha256"])
    blob.unlink()
    report = store.verify_integrity()
    assert not report["ok"]
    assert any(item["problem"] == "store_error" for item in report["problems"])


def test_fixture_evidence_stream_roundtrip(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    write_fixture_entities(store)
    payloads = load_fixture("payloads.json")
    records = [
        json.loads(line)
        for line in (FIXTURES / "evidence.sample.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for record in records:
        store.write_evidence(record, payloads[record["id"]].encode("utf-8"))
    assert store.read_evidence() == records
    assert store.verify_integrity()["ok"]


def test_integrity_detects_evidence_without_entity(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    evidence = load_fixture("evidence.sample.json")
    payload = load_fixture("payloads.json")["ev-001"].encode("utf-8")
    store.write_evidence(evidence, payload)

    report = store.verify_integrity()
    assert not report["ok"]
    assert report["problems"][0]["problem"] == "missing_entity"


def test_url_fragment_evidence_is_marked_unverifiable(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    write_fixture_entities(store)
    evidence = copy.deepcopy(load_fixture("evidence.sample.json"))
    evidence["locator"] = {
        "format": "url_fragment",
        "pointer": None,
        "char_start": None,
        "char_end": None,
        "line_start": None,
        "line_end": None,
        "url_fragment": "section",
    }
    payload = b"<html>external page</html>"
    evidence["blob_sha256"] = hashlib.sha256(payload).hexdigest()
    saved = store.write_evidence(evidence, payload)
    assert saved["verification"] == "unverifiable"
    assert store.read_evidence()[0]["verification"] == "unverifiable"


def test_write_jsonl_rejects_removed_skip_policy(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(InternalError):
        store.write_jsonl(
            "normalized/entities.jsonl",
            [{"id": "a", "value": 1}, {"id": "a", "value": 2}],
            on_conflict="skip",
        )


def test_init_marker_lifecycle(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    assert not store.initialization_pending()
    store.begin_init()
    assert store.initialization_pending()
    store.finish_init()
    assert not store.initialization_pending()


def test_evidence_quote_mismatch_is_rejected(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    evidence = copy.deepcopy(load_fixture("evidence.sample.json"))
    evidence["text"] = "A different quote"
    payload = load_fixture("payloads.json")["ev-001"].encode("utf-8")
    with pytest.raises(StoreError):
        store.write_evidence(evidence, payload)
    assert store.read_evidence() == []


def test_single_writer_lock_is_recoverable(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with store.lock():
        with pytest.raises(LockError) as error:
            with store.lock():
                pass
    assert error.value.exit_code == 4
    assert error.value.retryable is True


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(StoreError):
        store.write_json("../evil.json", {"x": 1})
    with pytest.raises(StoreError):
        store.read_json("/tmp/evil.json")


def test_checkpoint_roundtrip(tmp_path: Path) -> None:
    store, config = make_store(tmp_path)
    store.write_checkpoint("collect", {"config_hash": config["config_hash"], "cursor": "abc"})
    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["stage"] == "collect"
    assert checkpoint["cursor"] == "abc"
    assert checkpoint["config_hash"] == config["config_hash"]
    with pytest.raises(StoreError):
        store.write_checkpoint("Bad Stage", {"config_hash": config["config_hash"]})
    with pytest.raises(StoreError):
        store.write_checkpoint("collect", {"cursor": "missing-hash"})


def test_corrupt_manifest_is_an_artifact_error(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    manifest = json.loads((store.run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["state"] = "done"
    (store.run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError) as error:
        store.read_manifest()
    assert error.value.exit_code == 6


def test_verify_integrity_detects_tampered_quote(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    write_fixture_entities(store)
    evidence = load_fixture("evidence.sample.json")
    payload = load_fixture("payloads.json")["ev-001"].encode("utf-8")
    store.write_evidence(evidence, payload)

    tampered = copy.deepcopy(evidence)
    tampered["text"] = "A different quote"
    tampered["verification"] = "verified"
    store.write_jsonl(
        "normalized/evidence.jsonl",
        [tampered],
        key="id",
        on_conflict="replace",
        schema_name="evidence",
    )
    report = store.verify_integrity()
    assert not report["ok"]
    assert any(item["problem"] == "quote_mismatch" for item in report["problems"])


def test_verify_integrity_reports_partial_tail_without_repair(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    write_fixture_entities(store)
    evidence = load_fixture("evidence.sample.json")
    payload = load_fixture("payloads.json")["ev-001"].encode("utf-8")
    store.write_evidence(evidence, payload)
    stream = store.secure_path("normalized/evidence.jsonl")
    with stream.open("ab") as handle:
        handle.write(b'{"id": "partial"')

    report = store.verify_integrity()
    assert report["repaired"] is False
    assert any(item["problem"] == "incomplete_tail" for item in report["problems"])

    repaired = store.verify_integrity(repair=True)
    assert repaired["repaired"] is True
    assert repaired["ok"] is True


def test_verify_integrity_detects_schema_and_config_drift(tmp_path: Path) -> None:
    store, config = make_store(tmp_path)
    with (store.run_dir / "normalized/entities.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": "github.com/o/r/issue/1"}) + "\n")

    tampered_config = copy.deepcopy(config)
    tampered_config["window"]["timezone"] = "UTC"
    tampered_config["mode"] = "deep"
    (store.run_dir / "config.resolved.json").write_text(
        json.dumps(tampered_config),
        encoding="utf-8",
    )

    report = store.verify_integrity()
    assert report["schema_ok"] is False
    assert report["manifest_hash_ok"] is False
    assert any(item["problem"] == "schema_error" for item in report["problems"])
    assert any(item["problem"] == "invalid_config" for item in report["problems"])


def test_manifest_roundtrip_and_validation(tmp_path: Path) -> None:
    store, config = make_store(tmp_path)
    manifest = build_manifest(config, store.run_dir.name, CREATED_AT)
    store.write_manifest(manifest)
    assert store.read_manifest() == manifest

    manifest["state"] = "done"
    with pytest.raises(SchemaError):
        store.write_manifest(manifest)


def test_run_lock_blocks_a_second_writer(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    other = SnapshotStore(store.run_dir)

    with store.run_lock():
        with pytest.raises(LockError):
            with other.run_lock():
                pass


def test_written_files_are_private(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    path = store.write_text("analysis/private.txt", "secret")

    assert path.stat().st_mode & 0o777 == 0o600


def test_write_bytes_invalidates_stream_index(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    stream = "normalized/entities.jsonl"
    store.append_jsonl(stream, {"id": "a", "value": 1})
    original = store.secure_path(stream).read_bytes()
    store.append_jsonl(stream, {"id": "b", "value": 2})
    assert store.read_jsonl(stream) == [
        {"id": "a", "value": 1},
        {"id": "b", "value": 2},
    ]

    store.write_bytes(stream, original)
    store.append_jsonl(stream, {"id": "b", "value": 2})

    assert store.read_jsonl(stream) == [
        {"id": "a", "value": 1},
        {"id": "b", "value": 2},
    ]


def test_remove_file_invalidates_stream_index(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    stream = "normalized/entities.jsonl"
    store.append_jsonl(stream, {"id": "a", "value": 1})
    store.remove_file(stream)
    store.append_jsonl(stream, {"id": "a", "value": 1})

    assert store.read_jsonl(stream) == [{"id": "a", "value": 1}]
