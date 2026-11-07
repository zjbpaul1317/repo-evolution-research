from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_normalize import prepare
from test_validation_render import seed_run

from rer import cli
from rer.errors import LockError, StoreError
from rer.store import SnapshotStore


def _snapshot(run: Path) -> dict[str, bytes]:
    return {p.relative_to(run).as_posix(): p.read_bytes() for p in run.rglob("*") if p.is_file()}


def _legacy_indexes(store: SnapshotStore) -> None:
    evidence = store.read_evidence()
    prepare(store)
    store.write_jsonl("normalized/evidence.jsonl", evidence, key="id", schema_name="evidence")
    for path in ("analysis/candidates.json", "analysis/pack-index.json"):
        doc = store.read_json(path)
        doc["schema_version"] = "1.2"
        store.write_json(path, doc)
    manifest = store.read_manifest()
    manifest["schema_version"] = "1.2"
    store.write_manifest(manifest)


def _audit(store, capsys):
    code = cli.main(["audit", "--run", str(store.run_dir)])
    return code, json.loads(capsys.readouterr().out)


def test_audit_legacy_indexes_is_read_only_and_explains_rebuild(tmp_path, capsys) -> None:
    store = seed_run(tmp_path)
    _legacy_indexes(store)
    store.lock_path.unlink(missing_ok=True)
    store.run_lock_path.unlink(missing_ok=True)
    before = _snapshot(store.run_dir)

    code, result = _audit(store, capsys)

    assert code == 4
    assert result["ok"] is False
    report = result["result"]
    assert report["status"] == "rebuild_required"
    assert len(report["rebuild_required"]) == 2
    assert report["attention_required"] == []
    assert report["collection_schema_compatible"] is False
    assert any("prepare" in step for step in report["recovery"])
    assert _snapshot(store.run_dir) == before

    # Explicit prepare repairs the derived indexes but does not promote the manifest.
    prepare(store)
    code, result = _audit(store, capsys)
    assert code == 0
    assert result["result"]["rebuild_required"] == []
    assert result["result"]["collection_schema_compatible"] is False


def test_audit_does_not_hide_missing_evidence_behind_legacy_indexes(tmp_path, capsys) -> None:
    store = seed_run(tmp_path)
    _legacy_indexes(store)
    evidence = store.read_evidence()[0]
    store.secure_path(f"blobs/{evidence['blob_sha256']}").unlink()
    before = _snapshot(store.run_dir)

    code, result = _audit(store, capsys)

    assert code == 6
    assert result["result"]["status"] == "attention_required"
    assert len(result["result"]["rebuild_required"]) == 2
    assert any(p.get("evidence_id") == evidence["id"]
               for p in result["result"]["attention_required"])
    assert _snapshot(store.run_dir) == before


def test_audit_current_invalid_index_is_not_legacy(tmp_path, capsys) -> None:
    store = seed_run(tmp_path)
    prepare(store)
    store.write_json("analysis/candidates.json", {"schema_version": "1.3"})
    code, result = _audit(store, capsys)
    assert code == 6
    assert result["result"]["rebuild_required"] == []
    assert any(p["problem"] == "invalid_candidates"
               for p in result["result"]["attention_required"])


def test_audit_refuses_active_writer_without_modifying_snapshot(tmp_path, capsys) -> None:
    store = seed_run(tmp_path)
    with store.run_lock():
        before = _snapshot(store.run_dir)
        code, result = _audit(store, capsys)
        assert code == 4
        assert result["error"]["code"] == "run_locked"
        assert _snapshot(store.run_dir) == before


def test_read_only_store_refuses_writes_and_uses_existing_lock(tmp_path) -> None:
    store = seed_run(tmp_path)
    read_only = SnapshotStore(store.run_dir, read_only=True)
    before = _snapshot(store.run_dir)
    with pytest.raises(StoreError):
        read_only.write_text("unexpected.txt", "no")
    with store.lock(), pytest.raises(LockError), read_only.lock_shared():
        pytest.fail("must not read a stream while its writer holds the lock")
    assert _snapshot(store.run_dir) == before


def test_audit_valid_run_has_json_and_text_output(tmp_path, capsys) -> None:
    store = seed_run(tmp_path)
    before = _snapshot(store.run_dir)
    code, result = _audit(store, capsys)
    assert code == 0
    assert result["ok"] is True
    assert result["result"]["integrity"]["quote_reverified"] == 2
    assert cli.main(["audit", "--run", str(store.run_dir), "--format", "text"]) == 0
    assert "audit: ok" in capsys.readouterr().out
    assert _snapshot(store.run_dir) == before


def test_audit_explains_revalidation_after_inputs_change(tmp_path, capsys) -> None:
    store = seed_run(tmp_path)
    assert cli.main(["validate", "--run", str(store.run_dir)]) == 0
    capsys.readouterr()
    research = store.read_json("analysis/research.json")
    research["limitations"].append("额外局限说明。")
    store.write_json("analysis/research.json", research)
    before = _snapshot(store.run_dir)

    code, result = _audit(store, capsys)

    assert code == 6
    assert any(p["problem"] == "stale_validation"
               for p in result["result"]["attention_required"])
    assert any("重新 validate" in step for step in result["result"]["recovery"])
    assert _snapshot(store.run_dir) == before
