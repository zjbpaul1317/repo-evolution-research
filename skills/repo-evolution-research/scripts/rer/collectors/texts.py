"""Shared text references backed by content-addressed blobs."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ..errors import StoreError
from ..store import SHA256_RE, SnapshotStore

PREVIEW_CHARS = 2000
_TEXT_REF_KEYS = frozenset({"preview", "sha256", "bytes", "preview_truncated"})


def text_ref(
    store: SnapshotStore,
    value: Any,
    *,
    preview_chars: int = PREVIEW_CHARS,
) -> dict[str, Any]:
    text = value if isinstance(value, str) else ""
    payload = text.encode("utf-8")
    digest = store.save_blob(payload) if payload else None
    return {
        "preview": text[:preview_chars],
        "sha256": digest,
        "bytes": len(payload),
        "preview_truncated": len(text) > preview_chars,
    }


def iter_text_refs(
    value: Any,
    path: str = "$",
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Yield every text_ref-like mapping with its JSON path."""
    if isinstance(value, Mapping):
        if _TEXT_REF_KEYS <= set(value):
            yield path, value
            return
        for key, item in value.items():
            yield from iter_text_refs(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_text_refs(item, f"{path}[{index}]")


def _resolve_blob(
    store: SnapshotStore,
    digest: Any,
    declared: Any,
) -> tuple[dict[str, Any] | None, bytes | None]:
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        return (
            {
                "problem": "invalid_text_blob_hash",
                "message": "blob hash 不是合法 SHA-256",
                "blob_sha256": digest,
            },
            None,
        )
    try:
        path = store.secure_path(Path("blobs") / digest)
        present = path.is_file()
    except (OSError, StoreError) as exc:
        return (
            {
                "problem": "text_blob_unreadable",
                "message": str(exc),
                "blob_sha256": digest,
                "declared_bytes": declared,
            },
            None,
        )
    if not present:
        return (
            {
                "problem": "missing_text_blob",
                "message": "引用的 blob 不存在",
                "blob_sha256": digest,
                "declared_bytes": declared,
            },
            None,
        )
    try:
        payload = store.read_blob(digest)
    except StoreError as exc:
        try:
            present = path.is_file()
        except OSError as check_exc:
            return (
                {
                    "problem": "text_blob_unreadable",
                    "message": str(check_exc),
                    "blob_sha256": digest,
                    "declared_bytes": declared,
                },
                None,
            )
        return (
            {
                "problem": (
                    "text_blob_hash_mismatch"
                    if present
                    else "missing_text_blob"
                ),
                "message": exc.message,
                "blob_sha256": digest,
                "declared_bytes": declared,
            },
            None,
        )
    except OSError as exc:
        return (
            {
                "problem": "text_blob_unreadable",
                "message": str(exc),
                "blob_sha256": digest,
                "declared_bytes": declared,
            },
            None,
        )
    if (
        not isinstance(declared, int)
        or isinstance(declared, bool)
        or len(payload) != declared
    ):
        return (
            {
                "problem": "text_blob_size_mismatch",
                "message": "blob 实际大小与声明不一致",
                "blob_sha256": digest,
                "declared_bytes": declared,
                "actual_bytes": len(payload),
            },
            None,
        )
    return None, payload


def blob_problem(
    store: SnapshotStore,
    digest: Any,
    declared: Any,
) -> dict[str, Any] | None:
    """Return a problem when a declared blob cannot be resolved."""
    problem, _payload = _resolve_blob(store, digest, declared)
    return problem


def text_ref_problem(
    store: SnapshotStore,
    ref: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return a problem when a text_ref cannot be resolved to a valid blob."""
    digest = ref.get("sha256")
    declared = ref.get("bytes")
    if digest is None:
        if declared not in (0, None) or ref.get("preview"):
            return {
                "problem": "text_ref_inconsistent",
                "message": "text_ref 没有 blob hash，但 bytes 或 preview 非空",
                "declared_bytes": declared,
            }
        if ref.get("preview_truncated") is not False:
            return {
                "problem": "text_preview_truncated_mismatch",
                "message": "空 text_ref 的 preview_truncated 必须为 false",
                "declared_bytes": declared,
            }
        return None
    problem, payload = _resolve_blob(store, digest, declared)
    if problem is not None:
        return problem
    if payload is None:
        return {
            "problem": "text_blob_unreadable",
            "message": "blob 读取失败",
            "blob_sha256": digest,
            "declared_bytes": declared,
        }
    preview = ref.get("preview")
    truncated = ref.get("preview_truncated")
    if not isinstance(preview, str) or not isinstance(truncated, bool):
        return {
            "problem": "text_ref_inconsistent",
            "message": "text_ref.preview/preview_truncated 类型不合法",
            "blob_sha256": digest,
            "declared_bytes": declared,
        }
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "problem": "text_blob_not_utf8",
            "message": "text_ref 指向的 blob 不是 UTF-8",
            "blob_sha256": digest,
            "declared_bytes": declared,
        }
    if preview != text[: len(preview)]:
        return {
            "problem": "text_preview_mismatch",
            "message": "text_ref.preview 与 blob 内容不一致",
            "blob_sha256": digest,
            "declared_bytes": declared,
        }
    if text and not preview:
        return {
            "problem": "text_ref_inconsistent",
            "message": "非空文本的 text_ref.preview 不能为空",
            "blob_sha256": digest,
            "declared_bytes": declared,
        }
    if truncated != (len(text) > len(preview)):
        return {
            "problem": "text_preview_truncated_mismatch",
            "message": "text_ref.preview_truncated 与 blob 长度不一致",
            "blob_sha256": digest,
            "declared_bytes": declared,
        }
    return None


__all__ = [
    "PREVIEW_CHARS",
    "blob_problem",
    "iter_text_refs",
    "text_ref",
    "text_ref_problem",
]
