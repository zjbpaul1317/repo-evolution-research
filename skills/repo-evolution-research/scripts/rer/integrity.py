"""Cross-artifact integrity checks for authoritative PR detail documents."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .collectors.docs import (
    DOC_DIR,
    REQUEST_FAILURE_STATUSES,
    doc_path,
    doc_pointer,
    project_doc,
    role_for_doc_path,
    select_doc_candidates,
    validate_doc_snapshot,
)
from .collectors.entity_store import (
    deep_read_state,
    detail_path,
    project_pr_detail,
    prune_deep_read,
)
from .collectors.pr_store import PR_DETAIL_DIR, validate_pr_document
from .collectors.proposal_store import (
    DISCUSSION_DIR,
    ISSUE_DIR,
    discussion_detail_path,
    issue_detail_path,
    project_proposal_read,
    proposal_pointer,
    validate_discussion_document,
    validate_issue_document,
)
from .collectors.releases import (
    RELEASE_DIR,
    project_release,
    release_path,
    release_pointer,
    validate_release_document,
)
from .collectors.selection import derive_fetched_count, derive_selection_counts
from .collectors.status import (
    derive_detail_channel_status,
    derive_discussion_detail_status,
    derive_issue_detail_status,
    derive_pr_detail_channel_status,
)
from .collectors.texts import blob_problem, iter_text_refs, text_ref_problem
from .errors import RERError
from .models import FULL_SHA_RE, entity_id, validate_document
from .store import SnapshotStore

SELECTION_PATH = "analysis/pr-selection.json"
PROPOSAL_SELECTION_PATH = "analysis/proposal-selection.json"
LEGACY_ARTIFACTS = (
    "raw/pr_details.jsonl",
    "raw/pr_files.jsonl",
    "raw/pr_commits.jsonl",
    "raw/pr_issue_comments.jsonl",
    "raw/pr_reviews.jsonl",
    "raw/pr_review_comments.jsonl",
)
LEGACY_DETAIL_DIR = "analysis/pr-details"
LEGACY_ENTITY_FIELDS = ("deep_read", "files_truncated", "commits_truncated")


def audit_pr_artifacts(
    store: SnapshotStore,
    *,
    manifest: Mapping[str, Any] | None,
    entities: Sequence[Mapping[str, Any]],
    repair: bool,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Audit documents, text blobs, selection, entities and coverage."""
    problems: list[dict[str, Any]] = []
    documents, numbers = _load_documents(store, problems)
    text_blob_problems, text_blobs_checked = _text_blob_problems(store, documents)
    problems.extend(text_blob_problems)
    problems.extend(_legacy_problems(store))
    selection_path = store.secure_path(SELECTION_PATH)
    selection = _load_selection(store, problems)
    if selection is None and not selection_path.is_file() and _selection_required(
        manifest,
        documents,
    ):
        problems.append(
            {
                "artifact": SELECTION_PATH,
                "problem": "missing_pr_selection",
                "message": (
                    "collect 已产生 PR 深读产物，但 analysis/pr-selection.json 缺失"
                ),
            }
        )
    if selection is not None:
        problems.extend(_selection_problems(selection, numbers))
    entity_repaired, entity_problems = _entity_problems(
        store,
        documents,
        numbers,
        entities,
        repair=repair,
    )
    problems.extend(entity_problems)
    if manifest is not None and selection is not None:
        problems.extend(_coverage_problems(manifest, selection, documents))
    return problems, entity_repaired, text_blobs_checked


def _selection_required(
    manifest: Mapping[str, Any] | None,
    documents: Sequence[Mapping[str, Any]],
) -> bool:
    if documents:
        return True
    if manifest is None:
        return False
    coverage = manifest.get("coverage")
    if isinstance(coverage, list) and any(
        isinstance(item, Mapping) and item.get("channel") == "pr_detail"
        for item in coverage
    ):
        return True
    stages = manifest.get("stages")
    return isinstance(stages, Mapping) and stages.get("collect") == "complete"


def _load_documents(
    store: SnapshotStore,
    problems: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[int]]:
    directory = store.secure_path(PR_DETAIL_DIR)
    if not directory.is_dir():
        return [], set()
    paths = [path for path in directory.iterdir() if path.is_file() and path.suffix == ".json"]
    documents: list[dict[str, Any]] = []
    numbers: set[int] = set()
    for path in sorted(paths, key=lambda item: int(item.stem) if item.stem.isdigit() else -1):
        relpath = f"{PR_DETAIL_DIR}/{path.name}"
        if not path.stem.isdigit():
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "unexpected_pr_document",
                    "message": "raw/pr-details 只允许 <number>.json",
                }
            )
            continue
        try:
            document = store.read_json(relpath)
            validate_pr_document(document)
        except RERError as exc:
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "invalid_pr_document",
                    "message": exc.message,
                    "details": getattr(exc, "details", None),
                }
            )
            continue
        number = int(document["number"])
        if path.stem != str(number):
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "document_number_mismatch",
                    "message": f"文件名 {path.stem} 与 document.number={number} 不一致",
                }
            )
        documents.append(document)
        numbers.add(number)
    return documents, numbers


def _text_blob_problems(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    problems: list[dict[str, Any]] = []
    checked = 0
    for document in documents:
        artifact = detail_path(int(document["number"]))
        for json_path, ref in iter_text_refs(document):
            if ref.get("sha256") is not None:
                checked += 1
            problem = text_ref_problem(store, ref)
            if problem is not None:
                problems.append(
                    _blob_problem_record(artifact, json_path, problem)
                )
        diff = document.get("diff")
        if isinstance(diff, Mapping) and diff.get("sha256") is not None:
            checked += 1
            problem = blob_problem(store, diff.get("sha256"), diff.get("bytes"))
            if problem is not None:
                problems.append(
                    _blob_problem_record(artifact, "$.diff", problem)
                )
    return problems, checked


def _blob_problem_record(
    artifact: str,
    json_path: str,
    problem: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "artifact": artifact,
        "json_path": json_path,
        **problem,
        "recoverable": False,
        "recovery": (
            "权威原文无法由快照重建；重新 collect 该 PR，"
            "或保留快照并标记证据缺口"
        ),
    }


def _legacy_problems(store: SnapshotStore) -> list[dict[str, Any]]:
    problems = []
    for relpath in LEGACY_ARTIFACTS:
        if store.secure_path(relpath).exists():
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "legacy_snapshot",
                    "message": (
                        "旧快照不兼容：Phase 2B 已删除该派生产物，"
                        "请重新 init + collect，不要静默读取"
                    ),
                }
            )
    directory = store.secure_path(LEGACY_DETAIL_DIR)
    if directory.is_dir() and any(directory.glob("*.json")):
        problems.append(
            {
                "artifact": LEGACY_DETAIL_DIR,
                "problem": "legacy_snapshot",
                "message": (
                    "旧快照不兼容：analysis/pr-details 已删除，"
                    "权威文档位于 raw/pr-details/<number>.json"
                ),
            }
        )
    return problems


def _load_selection(
    store: SnapshotStore,
    problems: list[dict[str, Any]],
) -> Mapping[str, Any] | None:
    if not store.secure_path(SELECTION_PATH).is_file():
        return None
    try:
        selection = store.read_json(SELECTION_PATH)
        validate_document(selection, "pr_selection")
    except RERError as exc:
        problems.append(
            {
                "artifact": SELECTION_PATH,
                "problem": "invalid_selection",
                "message": exc.message,
                "details": getattr(exc, "details", None),
            }
        )
        return None
    return selection


def _selected_candidates(selection: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates = selection.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        candidate
        for candidate in candidates
        if isinstance(candidate, Mapping) and candidate.get("selected") is True
    ]


def _selection_problems(
    selection: Mapping[str, Any],
    numbers: set[int],
) -> list[dict[str, Any]]:
    problems = []
    candidates = [
        candidate
        for candidate in selection.get("candidates", [])
        if isinstance(candidate, Mapping)
    ]
    derived_counts = derive_selection_counts(candidates)
    stored_counts = selection.get("counts")
    if isinstance(stored_counts, Mapping):
        mismatched = {
            key: {"stored": stored_counts.get(key), "derived": derived}
            for key, derived in derived_counts.items()
            if stored_counts.get(key) != derived
        }
        if mismatched:
            problems.append(
                {
                    "artifact": SELECTION_PATH,
                    "problem": "pr_selection_counts_mismatch",
                    "message": "selection.counts 与 candidates 派生结果不一致",
                    "details": {"mismatched": mismatched},
                }
            )
    for candidate in _selected_candidates(selection):
        status = candidate.get("status")
        if status not in ("selected", "skipped_unchanged"):
            continue
        number = candidate.get("number")
        if isinstance(number, int) and not isinstance(number, bool) and number in numbers:
            continue
        problems.append(
            {
                "artifact": SELECTION_PATH,
                "candidate": number,
                "problem": "missing_pr_document",
                "message": (
                    f"selection 标记 #{number} 为 {status}，"
                    "但 raw/pr-details 没有对应文档"
                ),
            }
        )
    return problems


def _entity_problems(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]],
    numbers: set[int],
    entities: Sequence[Mapping[str, Any]],
    *,
    repair: bool,
) -> tuple[bool, list[dict[str, Any]]]:
    problems = []
    repaired = False
    entity_map = {
        entity["id"]: entity
        for entity in entities
        if isinstance(entity.get("id"), str)
    }
    for document in documents:
        identifier = _entity_identifier(document)
        number = int(document["number"])
        expected = deep_read_state(document)
        entity = entity_map.get(identifier)
        raw_state = entity.get("raw_state") if isinstance(entity, Mapping) else None
        deep_read = raw_state.get("deep_read") if isinstance(raw_state, Mapping) else None
        normalized_state = (
            entity.get("normalized_state") if isinstance(entity, Mapping) else None
        )
        legacy_normalized = isinstance(normalized_state, Mapping) and any(
            key in normalized_state for key in LEGACY_ENTITY_FIELDS
        )
        if (
            isinstance(deep_read, Mapping)
            and dict(deep_read) == expected
            and not legacy_normalized
        ):
            continue
        if repair:
            repository = document["repository"]
            project_pr_detail(
                store,
                str(repository["host"]),
                str(repository["owner"]),
                str(repository["name"]),
                document,
            )
            repaired = True
            continue
        if not isinstance(deep_read, Mapping):
            problem = "missing_deep_read_projection"
            message = "entity.raw_state.deep_read 缺失"
        elif legacy_normalized:
            problem = "legacy_deep_read_projection"
            message = "entity.normalized_state 仍保留已删除的 deep_read 摘要字段"
        else:
            problem = "stale_deep_read_projection"
            message = "entity.raw_state.deep_read 与权威文档不一致"
        problems.append(
            {
                "entity_id": identifier,
                "artifact": detail_path(number),
                "problem": problem,
                "message": message,
            }
        )

    stale = [
        entity
        for entity in entities
        if entity.get("kind") == "pull_request"
        and _has_stale_deep_read(entity, numbers)
    ]
    if stale:
        if repair:
            repaired = prune_deep_read(store, numbers) or repaired
        else:
            for entity in stale:
                problems.append(
                    {
                        "entity_id": entity.get("id"),
                        "problem": "stale_deep_read",
                        "message": "entity.raw_state.deep_read 指向不存在的文档",
                    }
                )
    return repaired, problems


def _has_stale_deep_read(entity: Mapping[str, Any], numbers: set[int]) -> bool:
    raw_state = entity.get("raw_state")
    normalized_state = entity.get("normalized_state")
    has_marker = (
        isinstance(raw_state, Mapping)
        and isinstance(raw_state.get("deep_read"), Mapping)
    ) or (
        isinstance(normalized_state, Mapping)
        and any(key in normalized_state for key in LEGACY_ENTITY_FIELDS)
    )
    if not has_marker:
        return False
    number = entity.get("number")
    return not (
        isinstance(number, int)
        and not isinstance(number, bool)
        and number in numbers
    )


def _coverage_problems(
    manifest: Mapping[str, Any],
    selection: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates = [
        candidate
        for candidate in selection.get("candidates", [])
        if isinstance(candidate, Mapping)
    ]
    selected = _selected_candidates(selection)
    selected_numbers = {
        int(candidate["number"])
        for candidate in selected
        if isinstance(candidate.get("number"), int)
    }
    results = [
        document
        for document in documents
        if int(document["number"]) in selected_numbers
    ]
    counts = derive_selection_counts(candidates)
    derived_status = derive_pr_detail_channel_status(
        selected=counts["selected"],
        failed=counts["failed"],
        skipped_unchanged=counts["skipped_unchanged"],
        results=results,
        not_attempted=counts["not_attempted"],
    )
    expected_count = counts["selected"]
    fetched_count = derive_fetched_count(candidates)
    coverage = manifest.get("coverage")
    entries = coverage if isinstance(coverage, list) else []
    entry = next(
        (
            item
            for item in entries
            if isinstance(item, Mapping) and item.get("channel") == "pr_detail"
        ),
        None,
    )
    if entry is None:
        return [
            {
                "artifact": "manifest.json",
                "problem": "missing_pr_detail_coverage",
                "message": "selection 存在，但 manifest.coverage 没有 pr_detail 通道",
            }
        ]
    stored = entry.get("coverage_status")
    stored_detail = entry.get("detail_status")
    stored_expected = entry.get("expected_count")
    stored_fetched = entry.get("fetched_count")
    if (
        stored == derived_status
        and stored_detail == derived_status
        and stored_expected == expected_count
        and stored_fetched == fetched_count
    ):
        return []
    return [
        {
            "artifact": "manifest.json",
            "problem": "pr_detail_coverage_mismatch",
            "message": "manifest.coverage 的 pr_detail 与文档/selection 派生结果不一致",
            "details": {
                "stored": stored,
                "stored_detail": stored_detail,
                "derived": derived_status,
                "stored_expected_count": stored_expected,
                "stored_fetched_count": stored_fetched,
                "derived_expected_count": expected_count,
                "derived_fetched_count": fetched_count,
            },
        }
    ]


def _entity_identifier(document: Mapping[str, Any]) -> str:
    repository = document["repository"]
    return entity_id(
        str(repository["host"]),
        str(repository["owner"]),
        str(repository["name"]),
        "pull_request",
        str(document["number"]),
    )


def audit_release_artifacts(
    store: SnapshotStore,
    *,
    manifest: Mapping[str, Any] | None,
    entities: Sequence[Mapping[str, Any]],
    repair: bool,
) -> tuple[list[dict[str, Any]], bool, int]:
    problems: list[dict[str, Any]] = []
    documents = _load_release_documents(store, problems)
    text_problems, checked = _release_text_blob_problems(store, documents)
    problems.extend(text_problems)
    repaired, projection_problems = _release_projection_problems(
        store,
        documents,
        entities,
        repair=repair,
    )
    problems.extend(projection_problems)
    coverage_problem = _missing_channel_coverage(
        manifest,
        channel="releases",
        documents_present=bool(documents),
        problem="missing_release_coverage",
        message="存在 Release 权威文档，但 manifest.coverage 没有 releases 通道",
    )
    if coverage_problem is not None:
        problems.append(coverage_problem)
    problems.extend(_releases_coverage_problems(manifest))
    return problems, repaired, checked


def _releases_coverage_problems(
    manifest: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    entry = _channel_entry(manifest, "releases")
    if entry is None:
        return []
    expected = entry.get("expected_count")
    fetched = entry.get("fetched_count")
    if (
        isinstance(expected, int)
        and not isinstance(expected, bool)
        and isinstance(fetched, int)
        and not isinstance(fetched, bool)
        and 0 <= fetched <= expected
    ):
        return []
    return [
        {
            "artifact": "manifest.json",
            "problem": "releases_coverage_mismatch",
            "message": "manifest.coverage 的 releases 计数不自洽",
            "details": {
                "stored_expected_count": expected,
                "stored_fetched_count": fetched,
            },
        }
    ]


def audit_doc_artifacts(
    store: SnapshotStore,
    *,
    manifest: Mapping[str, Any] | None,
    entities: Sequence[Mapping[str, Any]],
    repair: bool,
) -> tuple[list[dict[str, Any]], bool, int]:
    problems: list[dict[str, Any]] = []
    documents = _load_doc_snapshots(store, problems)
    text_problems, checked = _doc_text_blob_problems(store, documents)
    problems.extend(text_problems)
    active_revision = _active_revision(manifest)
    repaired, projection_problems = _doc_projection_problems(
        store,
        documents,
        entities,
        repair=repair,
        active_revision=active_revision,
    )
    problems.extend(projection_problems)
    coverage_problem = _missing_channel_coverage(
        manifest,
        channel="docs",
        documents_present=bool(documents),
        problem="missing_docs_coverage",
        message="存在文档快照，但 manifest.coverage 没有 docs 通道",
    )
    if coverage_problem is not None:
        problems.append(coverage_problem)
    problems.extend(_docs_coverage_problems(store, manifest, documents))
    return problems, repaired, checked


def _active_revision(manifest: Mapping[str, Any] | None) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    value = manifest.get("default_branch_sha")
    if isinstance(value, str) and FULL_SHA_RE.fullmatch(value):
        return value
    return None


def _docs_coverage_problems(
    store: SnapshotStore,
    manifest: Mapping[str, Any] | None,
    documents: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    entry = _channel_entry(manifest, "docs")
    if entry is None:
        return []
    stored = entry.get("coverage_status")
    if stored not in ("complete", "partial", "unavailable"):
        return []
    revision = _active_revision(manifest)
    if revision is None:
        return []
    expected = _expected_doc_count(store)
    if expected is None:
        return []
    scoped = [
        document
        for document in documents
        if str(document["revision"]) == revision
    ]
    fetched = sum(1 for document in scoped if document["status"] == "read")
    failures = sum(
        1
        for document in scoped
        if document["status"] in REQUEST_FAILURE_STATUSES
    )
    if expected == fetched:
        derived = "complete"
    elif fetched == 0 and failures == expected:
        derived = "unavailable"
    else:
        derived = "partial"
    reason = entry.get("truncation_reason")
    reason_text = reason if isinstance(reason, str) else ""
    if derived == "complete" and (
        "docs_selection_truncated" in reason_text
        or "budget_skipped" in reason_text
    ):
        derived = "partial"
    stored_expected = entry.get("expected_count")
    stored_fetched = entry.get("fetched_count")
    if (
        stored == derived
        and stored_expected == expected
        and stored_fetched == fetched
    ):
        return []
    return [
        {
            "artifact": "manifest.json",
            "problem": "docs_coverage_mismatch",
            "message": "manifest.coverage 的 docs 与文档快照派生结果不一致",
            "details": {
                "stored": stored,
                "derived": derived,
                "stored_expected_count": stored_expected,
                "stored_fetched_count": stored_fetched,
                "derived_expected_count": expected,
                "derived_fetched_count": fetched,
            },
        }
    ]


def _expected_doc_count(store: SnapshotStore) -> int | None:
    try:
        profile = store.read_json("analysis/source-profile.json")
    except RERError:
        return None
    if not isinstance(profile, Mapping):
        return None
    candidates, _, _ = select_doc_candidates(profile)
    return len(candidates)


def _channel_entry(
    manifest: Mapping[str, Any] | None,
    channel: str,
) -> Mapping[str, Any] | None:
    if not isinstance(manifest, Mapping):
        return None
    coverage = manifest.get("coverage")
    entries = coverage if isinstance(coverage, list) else []
    return next(
        (
            item
            for item in entries
            if isinstance(item, Mapping) and item.get("channel") == channel
        ),
        None,
    )


def _load_release_documents(
    store: SnapshotStore,
    problems: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    directory = store.secure_path(RELEASE_DIR)
    if not directory.is_dir():
        return []
    documents: list[dict[str, Any]] = []
    for path in sorted(
        (item for item in directory.iterdir() if item.is_file()),
        key=lambda item: item.name,
    ):
        relpath = f"{RELEASE_DIR}/{path.name}"
        if path.suffix != ".json" or not path.stem.isdigit():
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "unexpected_release_document",
                    "message": "raw/releases 只允许 <release_id>.json",
                }
            )
            continue
        try:
            document = store.read_json(relpath)
            validate_release_document(document)
        except RERError as exc:
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "invalid_release_document",
                    "message": exc.message,
                    "details": getattr(exc, "details", None),
                }
            )
            continue
        if path.stem != str(document["release_id"]):
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "release_document_id_mismatch",
                    "message": (
                        f"文件名 {path.stem} 与 release_id="
                        f"{document['release_id']} 不一致"
                    ),
                }
            )
        documents.append(document)
    return documents


def _load_doc_snapshots(
    store: SnapshotStore,
    problems: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    directory = store.secure_path(DOC_DIR)
    if not directory.is_dir():
        return []
    documents: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*.json")):
        relative = path.relative_to(directory).as_posix()
        relpath = f"{DOC_DIR}/{relative}"
        try:
            document = store.read_json(relpath)
            validate_doc_snapshot(document)
        except RERError as exc:
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "invalid_doc_snapshot",
                    "message": exc.message,
                    "details": getattr(exc, "details", None),
                }
            )
            continue
        revision = str(document["revision"])
        if not FULL_SHA_RE.fullmatch(revision):
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "invalid_doc_revision",
                    "message": "doc_snapshot.revision 必须是完整 40 位 commit SHA",
                }
            )
            continue
        try:
            expected = doc_path(revision, str(document["path"]))
        except ValueError as exc:
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "invalid_doc_path",
                    "message": f"doc_snapshot.path 非法：{exc}",
                }
            )
            continue
        if relpath != expected:
            problems.append(
                {
                    "artifact": relpath,
                    "problem": "doc_snapshot_path_mismatch",
                    "message": (
                        "文件路径与 revision/path 不一致，应为 "
                        f"{expected}"
                    ),
                }
            )
        documents.append(document)
    return documents


def _release_text_blob_problems(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    problems: list[dict[str, Any]] = []
    checked = 0
    for document in documents:
        artifact = release_path(int(document["release_id"]))
        body = document["body"]
        if body.get("sha256") is not None:
            checked += 1
        problem = text_ref_problem(store, body)
        if problem is not None:
            problems.append(_blob_problem_record(artifact, "$.body", problem))
    return problems, checked


def _doc_text_blob_problems(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    problems: list[dict[str, Any]] = []
    checked = 0
    for document in documents:
        artifact = doc_path(str(document["revision"]), str(document["path"]))
        if document["status"] == "read":
            text = document["text"]
            if text.get("sha256") is not None:
                checked += 1
            problem = text_ref_problem(store, text)
            if problem is not None:
                problems.append(_blob_problem_record(artifact, "$.text", problem))
        elif document["status"] == "unsupported_encoding":
            digest = document["blob_sha256"]
            if digest is not None:
                checked += 1
                problem = blob_problem(store, digest, document["size"])
                if problem is not None:
                    problems.append(
                        _blob_problem_record(artifact, "$.blob_sha256", problem)
                    )
    return problems, checked


def _release_projection_problems(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
    *,
    repair: bool,
) -> tuple[bool, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    repaired = False
    entity_map = {
        entity["id"]: entity
        for entity in entities
        if isinstance(entity.get("id"), str)
    }
    expected_ids: set[str] = set()
    for document in documents:
        repository = document["repository"]
        host = str(repository["host"])
        owner = str(repository["owner"])
        name = str(repository["name"])
        identifier = entity_id(
            host,
            owner,
            name,
            "release",
            str(document["tag_name"]),
        )
        expected = release_pointer(document)
        artifact = release_path(int(document["release_id"]))
        expected_ids.add(identifier)
        entity = entity_map.get(identifier)
        raw_state = entity.get("raw_state") if isinstance(entity, Mapping) else None
        if isinstance(raw_state, Mapping) and all(
            raw_state.get(key) == value for key, value in expected.items()
        ):
            continue
        if repair:
            project_release(store, host, owner, name, document)
            repaired = True
            continue
        problems.append(
            {
                "entity_id": identifier,
                "artifact": artifact,
                "problem": (
                    "missing_release_projection"
                    if not isinstance(raw_state, Mapping)
                    else "stale_release_projection"
                ),
                "message": "entity.raw_state 与 Release 权威文档不一致",
            }
        )
    for entity in entities:
        if entity.get("kind") != "release":
            continue
        if entity.get("id") in expected_ids:
            continue
        raw_state = entity.get("raw_state")
        if not isinstance(raw_state, Mapping):
            continue
        if not any(key in raw_state for key in ("release_id", "detail_path")):
            continue
        detail_path = raw_state.get("detail_path")
        if repair:
            _prune_entity(
                store,
                entity,
                raw_keys=(
                    "release_id",
                    "tag_name",
                    "commit_sha",
                    "detail_path",
                    "observed_at",
                ),
                normalized_keys=("draft", "prerelease", "published_at"),
            )
            repaired = True
            continue
        problems.append(
            {
                "entity_id": entity.get("id"),
                "artifact": (
                    detail_path if isinstance(detail_path, str) else "normalized/entities.jsonl"
                ),
                "problem": "stale_release_projection",
                "message": "entity.raw_state 指向不存在的 Release 文档",
            }
        )
    return repaired, problems


def _doc_projection_problems(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]],
    entities: Sequence[Mapping[str, Any]],
    *,
    repair: bool,
    active_revision: str | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    repaired = False
    entity_map = {
        entity["id"]: entity
        for entity in entities
        if isinstance(entity.get("id"), str)
    }
    expected_ids: set[str] = set()
    for document in documents:
        if (
            active_revision is not None
            and str(document["revision"]) != active_revision
        ):
            continue
        repository = document["repository"]
        host = str(repository["host"])
        owner = str(repository["owner"])
        name = str(repository["name"])
        identifier = entity_id(
            host,
            owner,
            name,
            "doc",
            str(document["path"]),
            revision=str(document["revision"]),
        )
        expected = doc_pointer(document)
        artifact = doc_path(str(document["revision"]), str(document["path"]))
        expected_ids.add(identifier)
        entity = entity_map.get(identifier)
        raw_state = entity.get("raw_state") if isinstance(entity, Mapping) else None
        if isinstance(raw_state, Mapping) and all(
            raw_state.get(key) == value for key, value in expected.items()
        ):
            continue
        if repair:
            project_doc(
                store,
                host,
                owner,
                name,
                document,
                role=role_for_doc_path(str(document["path"])),
            )
            repaired = True
            continue
        problems.append(
            {
                "entity_id": identifier,
                "artifact": artifact,
                "problem": (
                    "missing_doc_projection"
                    if not isinstance(raw_state, Mapping)
                    else "stale_doc_projection"
                ),
                "message": "entity.raw_state 与文档快照不一致",
            }
        )
    for entity in entities:
        if entity.get("kind") != "doc":
            continue
        if entity.get("id") in expected_ids:
            continue
        raw_state = entity.get("raw_state")
        if not isinstance(raw_state, Mapping):
            continue
        if "detail_path" not in raw_state:
            continue
        detail_path = raw_state.get("detail_path")
        if repair:
            _prune_entity(
                store,
                entity,
                raw_keys=(
                    "revision",
                    "path",
                    "status",
                    "detail_path",
                    "observed_at",
                ),
                normalized_keys=("revision", "path", "status"),
            )
            repaired = True
            continue
        problems.append(
            {
                "entity_id": entity.get("id"),
                "artifact": (
                    detail_path if isinstance(detail_path, str) else "normalized/entities.jsonl"
                ),
                "problem": "stale_doc_projection",
                "message": "entity.raw_state 指向不存在的文档快照",
            }
        )
    return repaired, problems


def _prune_entity(
    store: SnapshotStore,
    entity: Mapping[str, Any],
    *,
    raw_keys: Sequence[str],
    normalized_keys: Sequence[str],
) -> None:
    raw_state = dict(entity.get("raw_state") or {})
    normalized_state = dict(entity.get("normalized_state") or {})
    for key in raw_keys:
        raw_state.pop(key, None)
    for key in normalized_keys:
        normalized_state.pop(key, None)
    merged = {
        **entity,
        "raw_state": raw_state,
        "normalized_state": normalized_state,
    }
    validate_document(merged, "entity")
    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [merged],
        key="id",
        schema_name="entity",
    )


def _missing_channel_coverage(
    manifest: Mapping[str, Any] | None,
    *,
    channel: str,
    documents_present: bool,
    problem: str,
    message: str,
) -> dict[str, Any] | None:
    if not documents_present or not isinstance(manifest, Mapping):
        return None
    coverage = manifest.get("coverage")
    entries = coverage if isinstance(coverage, list) else []
    if any(
        isinstance(item, Mapping) and item.get("channel") == channel
        for item in entries
    ):
        return None
    return {
        "artifact": "manifest.json",
        "problem": problem,
        "message": message,
    }


def audit_proposal_artifacts(
    store: SnapshotStore,
    *,
    manifest: Mapping[str, Any] | None,
    entities: Sequence[Mapping[str, Any]],
    repair: bool,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Audit issue/discussion documents, blobs, selection and projections."""
    problems: list[dict[str, Any]] = []
    issues = _load_proposal_documents(
        store,
        ISSUE_DIR,
        "issue",
        validate_issue_document,
        problems,
    )
    discussions = _load_proposal_documents(
        store,
        DISCUSSION_DIR,
        "discussion",
        validate_discussion_document,
        problems,
    )
    documents: dict[str, list[dict[str, Any]]] = {
        "issue": issues,
        "discussion": discussions,
    }
    blob_problems, checked = _proposal_blob_problems(store, documents)
    problems.extend(blob_problems)
    repaired, projection_problems = _proposal_projection_problems(
        store,
        documents,
        entities,
        repair=repair,
    )
    problems.extend(projection_problems)
    selection = _load_proposal_selection(store, problems)
    if selection is None:
        if documents["issue"] or documents["discussion"]:
            problems.append(
                {
                    "artifact": PROPOSAL_SELECTION_PATH,
                    "problem": "missing_proposal_selection",
                    "message": (
                        "存在 Issue/Discussion 深读文档，"
                        "但 proposal-selection.json 缺失"
                    ),
                }
            )
    else:
        problems.extend(
            _proposal_selection_problems(selection, documents, manifest)
        )
    for carrier in ("issue", "discussion"):
        coverage_problem = _missing_channel_coverage(
            manifest,
            channel=f"{carrier}_detail",
            documents_present=bool(documents[carrier]),
            problem=f"missing_{carrier}_coverage",
            message=(
                f"存在 {carrier} 深读文档，但 manifest.coverage "
                f"没有 {carrier}_detail 通道"
            ),
        )
        if coverage_problem is not None:
            problems.append(coverage_problem)
    return problems, repaired, checked


def _load_proposal_documents(
    store: SnapshotStore,
    directory: str,
    carrier: str,
    validate,
    problems: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    base = store.secure_path(directory)
    if not base.is_dir():
        return []
    documents: list[dict[str, Any]] = []
    for path in sorted(
        (item for item in base.iterdir() if item.is_file()),
        key=lambda item: item.name,
    ):
        relpath = f"{directory}/{path.name}"
        if path.suffix != ".json" or not path.stem.isdigit():
            problems.append(
                {
                    "artifact": relpath,
                    "problem": f"unexpected_{carrier}_document",
                    "message": f"{directory} 只允许 <number>.json",
                }
            )
            continue
        try:
            document = store.read_json(relpath)
            validate(document)
        except RERError as exc:
            problems.append(
                {
                    "artifact": relpath,
                    "problem": f"invalid_{carrier}_document",
                    "message": exc.message,
                    "details": getattr(exc, "details", None),
                }
            )
            continue
        expected = (
            issue_detail_path(int(document["number"]))
            if carrier == "issue"
            else discussion_detail_path(int(document["number"]))
        )
        if relpath != expected:
            problems.append(
                {
                    "artifact": relpath,
                    "problem": f"{carrier}_document_number_mismatch",
                    "message": f"文件名与 number={document['number']} 不一致",
                }
            )
        documents.append(document)
    return documents


def _proposal_blob_problems(
    store: SnapshotStore,
    documents: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], int]:
    problems: list[dict[str, Any]] = []
    checked = 0
    for carrier, items in documents.items():
        for document in items:
            number = int(document["number"])
            artifact = (
                issue_detail_path(number)
                if carrier == "issue"
                else discussion_detail_path(number)
            )
            for json_path, ref in iter_text_refs(document):
                if ref.get("sha256") is not None:
                    checked += 1
                problem = text_ref_problem(store, ref)
                if problem is not None:
                    problems.append(
                        _blob_problem_record(artifact, json_path, problem)
                    )
    return problems, checked


def _proposal_projection_problems(
    store: SnapshotStore,
    documents: Mapping[str, Sequence[Mapping[str, Any]]],
    entities: Sequence[Mapping[str, Any]],
    *,
    repair: bool,
) -> tuple[bool, list[dict[str, Any]]]:
    problems: list[dict[str, Any]] = []
    repaired = False
    entity_map = {
        entity["id"]: entity
        for entity in entities
        if isinstance(entity.get("id"), str)
    }
    expected_ids: set[str] = set()
    for carrier, items in documents.items():
        for document in items:
            repository = document["repository"]
            host = str(repository["host"])
            owner = str(repository["owner"])
            name = str(repository["name"])
            identifier = entity_id(
                host,
                owner,
                name,
                carrier,
                str(document["number"]),
            )
            expected_ids.add(identifier)
            expected = proposal_pointer(carrier, document)
            artifact = (
                issue_detail_path(int(document["number"]))
                if carrier == "issue"
                else discussion_detail_path(int(document["number"]))
            )
            entity = entity_map.get(identifier)
            raw_state = entity.get("raw_state") if isinstance(entity, Mapping) else None
            pointer = (
                raw_state.get("proposal_read")
                if isinstance(raw_state, Mapping)
                else None
            )
            if isinstance(pointer, Mapping) and all(
                pointer.get(key) == value for key, value in expected.items()
            ):
                continue
            if repair:
                try:
                    project_proposal_read(
                        store,
                        host,
                        owner,
                        name,
                        carrier,
                        document,
                    )
                except Exception as exc:
                    problems.append(
                        {
                            "entity_id": identifier,
                            "artifact": artifact,
                            "problem": "proposal_projection_failed",
                            "message": f"无法重建 proposal 投影：{exc}",
                        }
                    )
                    continue
                repaired = True
                continue
            problems.append(
                {
                    "entity_id": identifier,
                    "artifact": artifact,
                    "problem": (
                        f"missing_{carrier}_projection"
                        if not isinstance(pointer, Mapping)
                        else f"stale_{carrier}_projection"
                    ),
                    "message": "entity.raw_state.proposal_read 与文档不一致",
                }
            )
    for entity in entities:
        if entity.get("kind") not in ("issue", "discussion"):
            continue
        if entity.get("id") in expected_ids:
            continue
        raw_state = entity.get("raw_state")
        if not isinstance(raw_state, Mapping):
            continue
        if "proposal_read" not in raw_state:
            continue
        if repair:
            _prune_entity(
                store,
                entity,
                raw_keys=("proposal_read",),
                normalized_keys=("scope", "role_hints"),
            )
            repaired = True
            continue
        problems.append(
            {
                "entity_id": entity.get("id"),
                "artifact": "normalized/entities.jsonl",
                "problem": "stale_proposal_projection",
                "message": "entity.raw_state.proposal_read 指向不存在的文档",
            }
        )
    return repaired, problems


def _load_proposal_selection(
    store: SnapshotStore,
    problems: list[dict[str, Any]],
) -> dict[str, Any] | None:
    path = store.secure_path(PROPOSAL_SELECTION_PATH)
    if not path.is_file():
        return None
    try:
        selection = store.read_json(PROPOSAL_SELECTION_PATH)
        validate_document(selection, "proposal_selection")
    except RERError as exc:
        problems.append(
            {
                "artifact": PROPOSAL_SELECTION_PATH,
                "problem": "invalid_proposal_selection",
                "message": exc.message,
                "details": getattr(exc, "details", None),
            }
        )
        return None
    return selection


def _proposal_selection_problems(
    selection: Mapping[str, Any],
    documents: Mapping[str, Sequence[Mapping[str, Any]]],
    manifest: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    candidates = [
        candidate
        for candidate in selection.get("candidates", [])
        if isinstance(candidate, Mapping)
    ]
    derived_counts = _proposal_counts(candidates)
    stored_counts = selection.get("counts")
    if isinstance(stored_counts, Mapping):
        mismatched = {
            key: {
                "stored": stored_counts.get(key),
                "derived": value,
            }
            for key, value in derived_counts.items()
            if stored_counts.get(key) != value
        }
        if mismatched:
            problems.append(
                {
                    "artifact": PROPOSAL_SELECTION_PATH,
                    "problem": "proposal_selection_counts_mismatch",
                    "message": "selection.counts 不能由 candidates 派生",
                    "details": mismatched,
                }
            )
    for carrier in ("issue", "discussion"):
        selected = [
            candidate
            for candidate in candidates
            if candidate.get("carrier") == carrier and candidate.get("selected")
        ]
        if not selected:
            continue
        numbers = {
            int(candidate["number"])
            for candidate in selected
            if isinstance(candidate.get("number"), int)
        }
        docs = {
            int(document["number"]): document
            for document in documents[carrier]
        }
        for candidate in selected:
            number = int(candidate["number"])
            expected_path = (
                issue_detail_path(number)
                if carrier == "issue"
                else discussion_detail_path(number)
            )
            detail_path_value = candidate.get("detail_path")
            status = candidate.get("status")
            if (
                status in ("selected", "skipped_unchanged")
                and not isinstance(detail_path_value, str)
            ):
                problems.append(
                    {
                        "artifact": PROPOSAL_SELECTION_PATH,
                        "problem": "proposal_detail_path_missing",
                        "message": f"candidate #{number} 缺少 detail_path",
                    }
                )
            if (
                isinstance(detail_path_value, str)
                and detail_path_value != expected_path
            ):
                problems.append(
                    {
                        "artifact": PROPOSAL_SELECTION_PATH,
                        "problem": "proposal_detail_path_mismatch",
                        "message": (
                            f"candidate #{number} 的 detail_path 与 "
                            f"{expected_path} 不一致"
                        ),
                    }
                )
            if status in ("selected", "skipped_unchanged") and number not in docs:
                problems.append(
                    {
                        "artifact": expected_path,
                        "problem": f"missing_{carrier}_document",
                        "message": (
                            f"selection 标记 #{number} 为 "
                            f"{candidate.get('status')}，但文档不存在"
                        ),
                    }
                )
        results = [docs[number] for number in sorted(numbers) if number in docs]
        status = derive_detail_channel_status(
            selected=len(selected),
            failed=sum(
                1 for candidate in selected if candidate.get("status") == "failed"
            ),
            skipped_unchanged=sum(
                1
                for candidate in selected
                if candidate.get("status") == "skipped_unchanged"
            ),
            results=results,
            not_attempted=sum(
                1
                for candidate in selected
                if candidate.get("status") == "not_attempted"
            ),
            detail_of=(
                derive_issue_detail_status
                if carrier == "issue"
                else derive_discussion_detail_status
            ),
        )
        fetched = sum(
            1
            for candidate in selected
            if candidate.get("status") in ("selected", "skipped_unchanged")
        )
        entry = _channel_entry(manifest, f"{carrier}_detail")
        if entry is None:
            continue
        if (
            entry.get("coverage_status") != status
            or entry.get("expected_count") != len(selected)
            or entry.get("fetched_count") != fetched
        ):
            problems.append(
                {
                    "artifact": "manifest.json",
                    "problem": f"{carrier}_coverage_mismatch",
                    "message": (
                        f"manifest.coverage 的 {carrier}_detail "
                        "与 selection/文档派生结果不一致"
                    ),
                    "details": {
                        "stored": entry.get("coverage_status"),
                        "derived": status,
                        "stored_expected_count": entry.get("expected_count"),
                        "stored_fetched_count": entry.get("fetched_count"),
                        "derived_expected_count": len(selected),
                        "derived_fetched_count": fetched,
                    },
                }
            )
    return problems


def _proposal_counts(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    return {
        "candidates": len(candidates),
        "selected": sum(1 for item in candidates if item.get("selected")),
        "issue_selected": sum(
            1
            for item in candidates
            if item.get("selected") and item.get("carrier") == "issue"
        ),
        "discussion_selected": sum(
            1
            for item in candidates
            if item.get("selected") and item.get("carrier") == "discussion"
        ),
        "context_selected": sum(
            1
            for item in candidates
            if item.get("selected") and item.get("scope") == "context"
        ),
        "skipped_quota": sum(
            1 for item in candidates if item.get("status") == "skipped_quota"
        ),
        "skipped_unchanged": sum(
            1 for item in candidates if item.get("status") == "skipped_unchanged"
        ),
        "failed": sum(1 for item in candidates if item.get("status") == "failed"),
        "not_attempted": sum(
            1
            for item in candidates
            if item.get("selected") and item.get("status") == "not_attempted"
        ),
    }


__all__ = [
    "LEGACY_ARTIFACTS",
    "LEGACY_DETAIL_DIR",
    "LEGACY_ENTITY_FIELDS",
    "PROPOSAL_SELECTION_PATH",
    "SELECTION_PATH",
    "audit_doc_artifacts",
    "audit_pr_artifacts",
    "audit_proposal_artifacts",
    "audit_release_artifacts",
]
