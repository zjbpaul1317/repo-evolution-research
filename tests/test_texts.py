from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from rer.collectors.texts import iter_text_refs, text_ref, text_ref_problem
from rer.errors import StoreError
from rer.store import SnapshotStore, create_run, new_run_id

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
REPOSITORY = {"host": "github.com", "owner": "acme", "name": "demo"}


def make_store(tmp_path: Path) -> SnapshotStore:
    return create_run(tmp_path, REPOSITORY, new_run_id(NOW))


def test_iter_text_refs_finds_nested_refs(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    body = text_ref(store, "body")
    patch = text_ref(store, "patch")
    document = {
        "body": body,
        "files": [{"patch": patch}, {"patch": None}],
        "count": 1,
    }

    refs = dict(iter_text_refs(document))

    assert refs["$.body"] == body
    assert refs["$.files[0].patch"] == patch
    assert len(refs) == 2


def test_text_ref_problem_accepts_empty_and_valid_refs(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    empty = text_ref(store, "")
    valid = text_ref(store, "hello")

    assert text_ref_problem(store, empty) is None
    assert text_ref_problem(store, valid) is None


def test_text_ref_problem_reports_missing_blob(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")
    digest = ref["sha256"]
    assert isinstance(digest, str)
    store.secure_path(Path("blobs") / digest).unlink()

    problem = text_ref_problem(store, ref)

    assert problem is not None
    assert problem["problem"] == "missing_text_blob"


def test_text_ref_problem_reports_size_mismatch(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")

    problem = text_ref_problem(store, {**ref, "bytes": ref["bytes"] + 1})

    assert problem is not None
    assert problem["problem"] == "text_blob_size_mismatch"


def test_text_ref_problem_reports_hash_mismatch(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")
    digest = ref["sha256"]
    assert isinstance(digest, str)
    store.secure_path(Path("blobs") / digest).write_bytes(b"HELLO")

    problem = text_ref_problem(store, ref)

    assert problem is not None
    assert problem["problem"] == "text_blob_hash_mismatch"


def test_text_ref_problem_reports_inconsistent_empty_ref(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = {
        "preview": "not empty",
        "sha256": None,
        "bytes": 0,
        "preview_truncated": False,
    }

    problem = text_ref_problem(store, ref)

    assert problem is not None
    assert problem["problem"] == "text_ref_inconsistent"


def test_text_ref_problem_reports_empty_ref_truncated_mismatch(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    ref = {
        "preview": "",
        "sha256": None,
        "bytes": 0,
        "preview_truncated": True,
    }

    problem = text_ref_problem(store, ref)

    assert problem is not None
    assert problem["problem"] == "text_preview_truncated_mismatch"


def test_text_ref_problem_accepts_truncated_preview(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "x" * 2500)

    assert ref["preview_truncated"] is True
    assert len(ref["preview"]) == 2000
    assert text_ref_problem(store, ref) is None


def test_text_ref_problem_reports_preview_mismatch(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")

    problem = text_ref_problem(store, {**ref, "preview": "HELLO"})

    assert problem is not None
    assert problem["problem"] == "text_preview_mismatch"


def test_text_ref_problem_reports_preview_truncated_mismatch(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")

    problem = text_ref_problem(store, {**ref, "preview_truncated": True})

    assert problem is not None
    assert problem["problem"] == "text_preview_truncated_mismatch"


def test_text_ref_problem_reports_non_utf8_blob(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    payload = b"\xff\xfe"
    ref = {
        "preview": "",
        "sha256": store.save_blob(payload),
        "bytes": len(payload),
        "preview_truncated": False,
    }

    problem = text_ref_problem(store, ref)

    assert problem is not None
    assert problem["problem"] == "text_blob_not_utf8"


def test_text_ref_problem_reports_oserror(tmp_path: Path, monkeypatch) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")

    def boom(_digest: str) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(store, "read_blob", boom)

    problem = text_ref_problem(store, ref)

    assert problem is not None
    assert problem["problem"] == "text_blob_unreadable"


def test_text_ref_problem_reports_empty_preview_for_nonempty_text(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")

    problem = text_ref_problem(
        store,
        {**ref, "preview": "", "preview_truncated": True},
    )

    assert problem is not None
    assert problem["problem"] == "text_ref_inconsistent"


def test_text_ref_problem_reports_blob_path_check_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")

    class BrokenPath:
        def is_file(self) -> bool:
            raise PermissionError("denied")

    monkeypatch.setattr(store, "secure_path", lambda relpath: BrokenPath())

    problem = text_ref_problem(store, ref)

    assert problem is not None
    assert problem["problem"] == "text_blob_unreadable"


def test_text_ref_problem_reports_blob_recheck_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = make_store(tmp_path)
    ref = text_ref(store, "hello")

    class FlakyPath:
        def __init__(self) -> None:
            self.calls = 0

        def is_file(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                return True
            raise PermissionError("denied")

    path = FlakyPath()
    monkeypatch.setattr(store, "secure_path", lambda relpath: path)

    def boom(_digest: str) -> bytes:
        raise StoreError("hash mismatch", source="store.test")

    monkeypatch.setattr(store, "read_blob", boom)

    problem = text_ref_problem(store, ref)

    assert problem is not None
    assert problem["problem"] == "text_blob_unreadable"
