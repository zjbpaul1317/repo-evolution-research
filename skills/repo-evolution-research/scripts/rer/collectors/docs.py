"""Collect fixed-revision document snapshots discovered by repository collect."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

from .. import SCHEMA_VERSION
from ..discovery import read_text_file
from ..errors import (
    BudgetExceededError,
    GitHubError,
    InternalError,
    RERError,
    StoreError,
    ValidationFailure,
)
from ..imported_source import IMPORTED_SOURCE_KINDS
from ..models import FULL_SHA_RE, entity_id, make_entity, validate_document
from ..store import SnapshotStore
from .base import (
    FATAL_ERRORS,
    CollectContext,
    is_run_budget_error,
    mark_run_budget_exhausted,
)
from .entity_store import merge_entity
from .progress import coverage_entry
from .texts import blob_problem, text_ref, text_ref_problem

DOC_DIR = "raw/docs"
MAX_DOCS = 40
TEXT_EXTENSIONS = frozenset(
    {".md", ".markdown", ".rst", ".txt", ".adoc", ".ipynb"}
)
REQUEST_FAILURE_STATUSES = frozenset({"unavailable"})
_DOC_LIMITS = (
    ("docs_paths", 20, "documentation"),
    ("design_paths", 10, "design"),
    ("rfc_paths", 10, "rfc"),
)
_NON_REUSABLE = frozenset({"unavailable"})
_LOCAL_ERRORS = (StoreError, ValidationFailure, InternalError)


@dataclass
class DocCollection:
    coverage: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    counts: dict[str, int]


def doc_path(revision: str, path: str) -> str:
    clean = _clean_repo_path(path)
    return f"{DOC_DIR}/{revision}/{clean}.json"


def collect_documents(
    context: CollectContext,
    *,
    source_profile: Mapping[str, Any],
    revision: str,
    include_docs: bool = True,
) -> DocCollection:
    store = context.store
    started = context.now()
    _require_revision(revision)
    host, owner, name = _repository_parts(context.repository)
    candidates, selection_truncated, filtered = select_doc_candidates(source_profile)
    counts = _empty_counts(len(candidates), selection_truncated, filtered)
    if not include_docs:
        counts["selected"] = 0
        return _finalize(
            context,
            counts,
            {},
            [],
            started,
            revision,
            candidate_count=0,
            status="not_requested",
        )
    if not candidates:
        return _finalize(
            context,
            counts,
            {},
            [],
            started,
            revision,
            candidate_count=0,
            status="not_applicable",
        )

    documents = load_revision_snapshots(store, revision)
    reconcile_doc_projection(
        store,
        documents,
        host=host,
        owner=owner,
        name=name,
        active_revision=revision,
    )
    profile_files = _profile_files(source_profile)
    current: list[dict[str, Any]] = []
    statuses: dict[str, int] = {}
    try:
        for index, (path, _role) in enumerate(candidates):
            try:
                document = _snapshot_for(
                    context,
                    revision,
                    path,
                    host=host,
                    owner=owner,
                    name=name,
                    profile_files=profile_files,
                    counts=counts,
                )
            except BudgetExceededError as exc:
                if is_run_budget_error(exc):
                    mark_run_budget_exhausted(context)
                counts["budget_skipped"] = len(candidates) - index
                break
            write_doc_snapshot(store, document)
            counts["written"] += 1
            current.append(document)
            status = str(document["status"])
            statuses[status] = statuses.get(status, 0) + 1
            if status == "read":
                counts["read"] += 1
            else:
                counts["failed"] += 1
    except _LOCAL_ERRORS:
        _finalize(
            context,
            counts,
            statuses,
            current,
            started,
            revision,
            candidate_count=len(candidates),
            best_effort=True,
        )
        raise
    reconcile_doc_projection(
        store,
        current,
        host=host,
        owner=owner,
        name=name,
        active_revision=revision,
    )
    return _finalize(
        context,
        counts,
        statuses,
        current,
        started,
        revision,
        candidate_count=len(candidates),
    )


def _require_revision(revision: str) -> None:
    if not isinstance(revision, str) or not FULL_SHA_RE.fullmatch(revision):
        raise StoreError(
            "docs revision 必须是完整 40 位 commit SHA",
            source="docs.revision",
        )


def _repository_parts(repository: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(repository.get("host") or "github.com"),
        str(repository["owner"]),
        str(repository["name"]),
    )


def _empty_counts(
    selected: int,
    selection_truncated: bool,
    filtered: int,
) -> dict[str, int]:
    return {
        "selected": selected,
        "selection_truncated": int(selection_truncated),
        "filtered": filtered,
        "written": 0,
        "fetched": 0,
        "profile_reused": 0,
        "reused": 0,
        "read": 0,
        "failed": 0,
        "budget_skipped": 0,
    }


def _snapshot_for(
    context: CollectContext,
    revision: str,
    path: str,
    *,
    host: str,
    owner: str,
    name: str,
    profile_files: Mapping[str, Mapping[str, Any]],
    counts: dict[str, int],
) -> dict[str, Any]:
    store = context.store
    existing = _reusable_snapshot(store, revision, path)
    if existing is not None:
        counts["reused"] += 1
        return existing
    profile_entry = profile_files.get(path)
    if _profile_entry_usable(profile_entry):
        try:
            counts["profile_reused"] += 1
            return _build_doc_snapshot(
                store,
                _profile_result(path, profile_entry),
                host=host,
                owner=owner,
                name=name,
                revision=revision,
                observed_at=context.now(),
            )
        except StoreError:
            counts["profile_reused"] -= 1
    try:
        result = read_text_file(
            context.client,
            store,
            owner,
            name,
            path,
            revision,
            auth_fatal=True,
        )
    except BudgetExceededError:
        raise
    except FATAL_ERRORS:
        raise
    except GitHubError:
        result = {"path": path, "status": "unavailable"}
    document = _build_doc_snapshot(
        store,
        result,
        host=host,
        owner=owner,
        name=name,
        revision=revision,
        observed_at=context.now(),
    )
    counts["fetched"] += 1
    return document


def _docs_status(
    counts: Mapping[str, int],
    statuses: Mapping[str, int],
    candidate_count: int,
    explicit: str | None,
) -> str:
    if explicit is not None:
        return explicit
    if candidate_count == 0:
        return "not_applicable"
    if counts["read"] == candidate_count:
        if counts["selection_truncated"]:
            return "partial"
        return "complete"
    request_failures = statuses.get("unavailable", 0) if statuses else 0
    if counts["read"] == 0 and request_failures == candidate_count:
        return "unavailable"
    return "partial"


def _finalize(
    context: CollectContext,
    counts: dict[str, int],
    statuses: Mapping[str, int],
    current: list[dict[str, Any]],
    started: str,
    revision: str,
    *,
    candidate_count: int,
    status: str | None = None,
    best_effort: bool = False,
) -> DocCollection:
    finished = context.now()
    coverage_status = _docs_status(counts, statuses, candidate_count, status)
    reasons = [
        f"{name}:{count}"
        for name, count in sorted(statuses.items())
        if name != "read"
    ]
    if counts["selection_truncated"]:
        reasons.append("docs_selection_truncated")
    if counts["filtered"]:
        reasons.append(f"non_text_paths_filtered:{counts['filtered']}")
    if counts["budget_skipped"]:
        reasons.append(f"budget_skipped:{counts['budget_skipped']}")
    discovery_status = "complete"
    if coverage_status == "not_requested":
        discovery_status = "not_requested"
    entry = coverage_entry(
        channel="docs",
        query_scope="default_branch",
        query=f"GET repository documents at {revision}",
        expected=counts["selected"] if status is None else 0,
        fetched=counts["read"] if status is None else 0,
        pages=counts["fetched"] if status is None else 0,
        exhausted=coverage_status == "complete",
        truncation_reason="; ".join(reasons) or None,
        coverage_status=coverage_status,
        discovery_status=discovery_status,
        detail_status=coverage_status,
        observed_start=started,
        observed_end=finished,
    )
    if context.progress is not None:
        context.progress.finish_channel(
            "docs",
            entry,
            observed_at=finished,
            best_effort=best_effort,
        )
    return DocCollection(coverage=[entry], documents=current, counts=counts)


def _profile_files(source_profile: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    discovery = source_profile.get("discovery")
    if not isinstance(discovery, Mapping):
        return {}
    files = discovery.get("files")
    if not isinstance(files, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in files:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if isinstance(path, str) and path:
            result[path] = item
    return result


def _profile_entry_usable(entry: Mapping[str, Any] | None) -> bool:
    if entry is None:
        return False
    status = entry.get("status")
    if status == "unavailable":
        return False
    if status in ("read", "unsupported_encoding") and not entry.get("blob_sha256"):
        return False
    return isinstance(status, str)


def _profile_result(
    path: str,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "path": path,
        "status": entry.get("status"),
        "size": entry.get("size"),
        "chars": entry.get("chars"),
        "blob_sha256": entry.get("blob_sha256"),
        "sha": entry.get("sha"),
        "url": entry.get("url"),
    }


def select_doc_candidates(
    source_profile: Mapping[str, Any],
) -> tuple[list[tuple[str, str]], bool, int]:
    """Return capped (path, role) candidates, truncation flag and filtered count."""
    discovery = source_profile.get("discovery")
    if not isinstance(discovery, Mapping):
        return [], False, 0
    entries: list[tuple[str, str, bool]] = []
    seen: set[str] = set()

    def normalize(path: Any) -> str | None:
        if not isinstance(path, str):
            return None
        try:
            return _clean_repo_path(path)
        except ValueError:
            return None

    def add(clean: str, role: str, allowed: bool) -> None:
        if clean in seen:
            return
        seen.add(clean)
        entries.append((clean, role, allowed))

    files = discovery.get("files")
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, Mapping):
                continue
            clean = normalize(item.get("path"))
            if clean is None:
                continue
            add(clean, _role_for_kind(item.get("kind")), True)
    for key, limit, role in _DOC_LIMITS:
        values = discovery.get(key)
        if not isinstance(values, list):
            continue
        for index, path in enumerate(values):
            clean = normalize(path)
            if clean is None:
                continue
            effective_role = role
            if role == "documentation":
                effective_role = role_for_doc_path(clean)
            add(clean, effective_role, index < limit)
    selected: list[tuple[str, str]] = []
    filtered = 0
    truncated = False
    for clean, role, allowed in entries:
        if not _is_text_path(clean):
            filtered += 1
            continue
        if not allowed:
            truncated = True
            continue
        selected.append((clean, role))
    capped = selected[:MAX_DOCS]
    return capped, truncated or len(selected) > len(capped), filtered


def _is_text_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in TEXT_EXTENSIONS


def _role_for_kind(kind: Any) -> str:
    if kind in ("rfc", "design", "architecture", "roadmap"):
        return str(kind)
    return "documentation"


def _clean_repo_path(path: str) -> str:
    clean = path.strip("/")
    segments = clean.split("/")
    if not clean or any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"invalid repository path: {path!r}")
    return clean


def _reusable_snapshot(
    store: SnapshotStore,
    revision: str,
    path: str,
) -> dict[str, Any] | None:
    relpath = doc_path(revision, path)
    if not store.secure_path(relpath).is_file():
        return None
    try:
        document = store.read_json(relpath)
        validate_doc_snapshot(document)
    except RERError:
        return None
    if document["revision"] != revision or document["path"] != path:
        return None
    if document["status"] in _NON_REUSABLE:
        return None
    if document["status"] == "read":
        if text_ref_problem(store, document["text"]) is not None:
            return None
        if (
            document["text"]["sha256"] is not None
            and document["blob_sha256"] != document["text"]["sha256"]
        ):
            return None
    elif document["blob_sha256"] is not None:
        if blob_problem(store, document["blob_sha256"], document["size"]) is not None:
            return None
    return document


def _build_doc_snapshot(
    store: SnapshotStore,
    result: Mapping[str, Any],
    *,
    host: str,
    owner: str,
    name: str,
    revision: str,
    observed_at: str,
) -> dict[str, Any]:
    path = str(result.get("path") or "")
    status = str(result.get("status") or "unavailable")
    size = result.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        size = None
    chars = result.get("chars")
    if isinstance(chars, bool) or not isinstance(chars, int):
        chars = None
    blob_sha256 = result.get("blob_sha256")
    if not isinstance(blob_sha256, str) or not blob_sha256:
        blob_sha256 = None
    git_blob_sha = result.get("sha")
    if not isinstance(git_blob_sha, str) or not FULL_SHA_RE.fullmatch(git_blob_sha):
        git_blob_sha = None
    url = result.get("url")
    if not isinstance(url, str) or not url:
        url = (
            f"https://github.com/{owner}/{name}/blob/{revision}/"
            f"{quote(path, safe='/')}"
        )
    text = None
    if status == "read" and blob_sha256 is not None:
        payload = store.read_blob(blob_sha256)
        text = text_ref(store, payload.decode("utf-8"))
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repository": {"host": host, "owner": owner, "name": name},
        "path": path,
        "revision": revision,
        "url": url,
        "status": status,
        "size": size,
        "chars": chars,
        "text": text,
        "blob_sha256": blob_sha256,
        "git_blob_sha": git_blob_sha,
        "limitations": [],
    }
    document["limitations"] = derive_doc_limitations(document)
    validate_doc_snapshot(document)
    return document


def validate_doc_snapshot(document: Mapping[str, Any]) -> None:
    validate_document(document, "doc_snapshot")
    status = document["status"]
    text = document["text"]
    if status == "read":
        if not isinstance(text, Mapping):
            raise StoreError(
                "doc_snapshot.status=read 但缺少 text_ref",
                source="doc_store.invariant",
                details={"path": document.get("path")},
            )
        if (
            text.get("sha256") is not None
            and document["blob_sha256"] != text.get("sha256")
        ):
            raise StoreError(
                "doc_snapshot.text.sha256 与 blob_sha256 不一致",
                source="doc_store.invariant",
                details={"path": document.get("path")},
            )
        if document["size"] is not None and document["size"] != text.get("bytes"):
            raise StoreError(
                "doc_snapshot.size 与 text_ref.bytes 不一致",
                source="doc_store.invariant",
                details={"path": document.get("path")},
            )
    elif status == "unsupported_encoding":
        if text is not None:
            raise StoreError(
                "doc_snapshot.status=unsupported_encoding 时 text 必须为 null",
                source="doc_store.invariant",
                details={"path": document.get("path")},
            )
        if document["blob_sha256"] is None:
            raise StoreError(
                "doc_snapshot.status=unsupported_encoding 时缺少 blob",
                source="doc_store.invariant",
                details={"path": document.get("path")},
            )
    elif text is not None:
        raise StoreError(
            "非 read 文档的 text 必须为 null",
            source="doc_store.invariant",
            details={"path": document.get("path")},
        )
    if document["limitations"] != derive_doc_limitations(document):
        raise StoreError(
            "doc_snapshot.limitations 不是从文档派生",
            source="doc_store.invariant",
            details={"path": document.get("path")},
        )


def derive_doc_limitations(document: Mapping[str, Any]) -> list[str]:
    limitations = {"observed_current_state"}
    status = document["status"]
    if status != "read":
        limitations.add(f"doc_{status}")
    elif document["text"]["sha256"] is None:
        limitations.add("empty_document")
    return sorted(limitations)


def write_doc_snapshot(
    store: SnapshotStore,
    document: Mapping[str, Any],
) -> None:
    validate_doc_snapshot(document)
    store.write_json(
        doc_path(str(document["revision"]), str(document["path"])),
        document,
        schema_name="doc_snapshot",
    )


def iter_doc_snapshots(store: SnapshotStore) -> Iterator[dict[str, Any]]:
    directory = store.secure_path(DOC_DIR)
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("*.json")):
        relpath = path.relative_to(directory).as_posix()
        document = store.read_json(f"{DOC_DIR}/{relpath}")
        validate_doc_snapshot(document)
        yield document


def load_doc_snapshots(store: SnapshotStore) -> list[dict[str, Any]]:
    return list(iter_doc_snapshots(store))


def iter_revision_snapshots(
    store: SnapshotStore,
    revision: str,
) -> Iterator[dict[str, Any]]:
    """Load snapshots for one revision; corrupt archives are skipped."""
    _require_revision(revision)
    directory = store.secure_path(f"{DOC_DIR}/{revision}")
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("*.json")):
        relpath = path.relative_to(store.secure_path(DOC_DIR)).as_posix()
        try:
            document = store.read_json(f"{DOC_DIR}/{relpath}")
            validate_doc_snapshot(document)
        except RERError:
            continue
        if document["revision"] != revision:
            continue
        yield document


def load_revision_snapshots(
    store: SnapshotStore,
    revision: str,
) -> list[dict[str, Any]]:
    return list(iter_revision_snapshots(store, revision))


def reconcile_doc_projection(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]] | None = None,
    *,
    host: str,
    owner: str,
    name: str,
    active_revision: str | None = None,
) -> None:
    """Project doc snapshots in one pass; drop entities from older revisions."""
    materialized = documents if documents is not None else load_doc_snapshots(store)
    if active_revision is not None:
        materialized = [
            document
            for document in materialized
            if str(document["revision"]) == active_revision
        ]
    protected_ids = (
        _imported_endpoint_ids(store) if active_revision is not None else set()
    )
    existing_records = store.read_jsonl("normalized/entities.jsonl")
    records = [
        entity
        for entity in existing_records
        if not _is_stale_doc_entity(entity, active_revision, protected_ids)
    ]
    positions = {
        record["id"]: index
        for index, record in enumerate(records)
        if isinstance(record.get("id"), str)
    }
    for document in materialized:
        incoming = _doc_entity(host, owner, name, document)
        identifier = incoming["id"]
        if identifier in positions:
            index = positions[identifier]
            merged = merge_entity(records[index], incoming)
            validate_document(merged, "entity")
            records[index] = merged
        else:
            validate_document(incoming, "entity")
            positions[identifier] = len(records)
            records.append(incoming)
    if len(records) == len(existing_records) and not materialized:
        return
    store.write_jsonl(
        "normalized/entities.jsonl",
        records,
        key="id",
        on_conflict="replace",
    )


def _imported_endpoint_ids(store: SnapshotStore) -> set[str]:
    protected: set[str] = set()
    for evidence in store.read_jsonl("normalized/evidence.jsonl", repair=True):
        if evidence.get("source_kind") not in IMPORTED_SOURCE_KINDS:
            continue
        identifier = evidence.get("entity_id")
        if isinstance(identifier, str):
            protected.add(identifier)
    for relation in store.read_jsonl("normalized/relations.jsonl", repair=True):
        scope = relation.get("scope")
        if isinstance(scope, Mapping) and scope.get("generator") == "prepare":
            continue
        for field in ("from_id", "to_id"):
            identifier = relation.get(field)
            if isinstance(identifier, str):
                protected.add(identifier)
    return protected


def _is_stale_doc_entity(
    entity: Mapping[str, Any],
    active_revision: str | None,
    protected_ids: set[str],
) -> bool:
    if active_revision is None or entity.get("kind") != "doc":
        return False
    identifier = entity.get("id")
    if isinstance(identifier, str) and identifier in protected_ids:
        return False
    raw_state = entity.get("raw_state")
    if not isinstance(raw_state, Mapping):
        return False
    revision = raw_state.get("revision")
    return isinstance(revision, str) and revision != active_revision


def _doc_entity(
    host: str,
    owner: str,
    name: str,
    document: Mapping[str, Any],
    *,
    role: str | None = None,
) -> dict[str, Any]:
    identifier = entity_id(
        host,
        owner,
        name,
        "doc",
        str(document["path"]),
        revision=str(document["revision"]),
    )
    return make_entity(
        entity_id=identifier,
        kind="doc",
        title=str(document["path"]),
        url=str(document["url"]),
        observed_at=str(document["observed_at"]),
        raw_state=doc_pointer(document),
        normalized_state={
            "revision": document["revision"],
            "path": document["path"],
            "status": document["status"],
        },
        roles=[role or role_for_doc_path(str(document["path"]))],
        discovery_reasons=["doc_snapshot"],
    )


def project_doc(
    store: SnapshotStore,
    host: str,
    owner: str,
    name: str,
    document: Mapping[str, Any],
    *,
    role: str,
) -> None:
    incoming = _doc_entity(host, owner, name, document, role=role)
    existing = next(
        (
            entity
            for entity in store.read_jsonl("normalized/entities.jsonl")
            if isinstance(entity, Mapping) and entity.get("id") == incoming["id"]
        ),
        None,
    )
    merged = merge_entity(existing, incoming) if existing is not None else incoming
    validate_document(merged, "entity")
    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [merged],
        key="id",
        schema_name="entity",
    )


def role_for_doc_path(path: str) -> str:
    lowered = path.lower()
    if "rfc" in lowered:
        return "rfc"
    if "design" in lowered:
        return "design"
    if "architecture" in lowered:
        return "architecture"
    if "roadmap" in lowered:
        return "roadmap"
    return "documentation"


def doc_pointer(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision": document["revision"],
        "path": document["path"],
        "status": document["status"],
        "detail_path": doc_path(
            str(document["revision"]),
            str(document["path"]),
        ),
        "observed_at": document["observed_at"],
    }


__all__ = [
    "DOC_DIR",
    "DocCollection",
    "MAX_DOCS",
    "REQUEST_FAILURE_STATUSES",
    "TEXT_EXTENSIONS",
    "collect_documents",
    "derive_doc_limitations",
    "doc_path",
    "doc_pointer",
    "iter_doc_snapshots",
    "iter_revision_snapshots",
    "load_doc_snapshots",
    "load_revision_snapshots",
    "project_doc",
    "reconcile_doc_projection",
    "role_for_doc_path",
    "select_doc_candidates",
    "validate_doc_snapshot",
    "write_doc_snapshot",
]
