"""Data contracts for entities, evidence, relations, research and manifests."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from functools import cache, lru_cache
from typing import Any
from urllib.parse import quote, urlsplit

from . import SCHEMA_VERSION, SKILL_ROOT, TOOL_VERSION
from .errors import InternalError, SchemaError, ToolError

SCHEMA_DIR = SKILL_ROOT / "assets" / "schemas"

SCHEMA_NAMES = (
    "config",
    "manifest",
    "entity",
    "evidence",
    "relation",
    "selection",
    "research",
    "source_profile",
    "activity_overview",
    "pr_selection",
    "pr_detail",
    "release_detail",
    "doc_snapshot",
    "issue_detail",
    "discussion_detail",
    "proposal_selection",
    "prepare_report",
    "candidates",
    "pack_index",
    "selection_report",
    "validation",
    "render_report",
    "source_envelope",
)

ENTITY_KINDS = (
    "repository",
    "pull_request",
    "issue",
    "discussion",
    "commit",
    "release",
    "doc",
    "source_file",
    "comment",
    "review",
)

ENTITY_ROLES = (
    "rfc",
    "design",
    "roadmap",
    "implementation",
    "bug",
    "feature_request",
    "enhancement",
    "architecture",
    "performance",
    "compatibility",
    "question",
    "release_notes",
    "documentation",
)

RELATION_TYPES = (
    "mentions",
    "addresses",
    "closing_reference",
    "implements_part_of",
    "contains_commit",
    "release_note_mentions",
    "verified_in_release",
    "documents",
    "supersedes",
    "reverts",
    "duplicates",
    "related_candidate",
)

RELATION_BASES = ("api_explicit", "text_explicit", "agent_inferred")
RELATION_VERIFICATIONS = ("candidate", "verified", "disputed")

CLAIM_KINDS = ("observed", "inferred", "unknown")
CONFIDENCE_LEVELS = ("high", "medium", "low")
STATE_TIME_BASES = ("observed_current", "historical_reconstructed")

PROPOSAL_STATES = ("unknown", "proposed", "discussing", "accepted", "rejected", "superseded")
IMPLEMENTATION_STATES = (
    "unknown",
    "not_started",
    "in_progress",
    "partially_merged",
    "merged",
    "reverted",
)
DELIVERY_STATES = ("unknown", "not_found", "prerelease", "released")
AVAILABILITY_STATES = ("unknown", "experimental", "opt_in", "default_on", "deprecated", "removed")

COVERAGE_STATUSES = (
    "complete",
    "sampled",
    "partial",
    "unavailable",
    "not_applicable",
    "not_requested",
)

RUN_STATES = (
    "initialized",
    "collecting",
    "prepared",
    "researching",
    "validated",
    "rendered",
    "partial",
    "interrupted",
    "failed",
)

STAGE_NAMES = ("initialized", "collect", "prepare", "research", "validate", "render")
STAGE_STATUSES = ("not_started", "in_progress", "complete", "partial", "failed", "skipped")

_ID_HOST = r"[A-Za-z0-9][A-Za-z0-9.-]*"
_ID_SEGMENT = r"(?!\.{1,2}(?:/|$))[A-Za-z0-9][A-Za-z0-9._-]*"
_ID_PATH_SEGMENT = r"(?!\.{1,2}(?:/|$))[A-Za-z0-9._%~+-]+"
_ID_PATH = rf"{_ID_PATH_SEGMENT}(?:/{_ID_PATH_SEGMENT})*"
_SHA = r"[0-9a-f]{40}"
_ID_TAIL = (
    rf"(?:/(?:pull|issue|discussion)/[0-9]+"
    rf"|/release/{_ID_PATH}"
    rf"|/comment/(?:issue|review)/[0-9]+"
    rf"|/review/[0-9]+"
    rf"|/{_SHA}(?:/{_ID_PATH})?"
    rf")?"
)
ENTITY_ID_RE = re.compile(rf"\A{_ID_HOST}/{_ID_SEGMENT}/{_ID_SEGMENT}{_ID_TAIL}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
FULL_SHA_RE = re.compile(rf"\A{_SHA}\Z")

_ID_KIND = {
    "pull_request": "pull",
    "issue": "issue",
    "discussion": "discussion",
}


@cache
def load_schema(name: str) -> dict[str, Any]:
    if name not in SCHEMA_NAMES:
        raise InternalError(f"未知 Schema：{name}", source="models.load_schema")
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise InternalError(f"缺少 Schema 文件：{path}", source="models.load_schema")
    return json.loads(path.read_text(encoding="utf-8"))


_DATETIME_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_URI_SCHEME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.-]*\Z")


def _check_datetime_format(value: str) -> None:
    from jsonschema.exceptions import FormatError

    if not isinstance(value, str):
        return True
    if not _DATETIME_RE.fullmatch(value):
        raise FormatError(f"{value!r} is not a valid RFC 3339 date-time")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise FormatError(f"{value!r} is not a valid RFC 3339 date-time") from exc
    return True


def _check_uri_format(value: str) -> None:
    from jsonschema.exceptions import FormatError

    if not isinstance(value, str):
        return True
    if any(character.isspace() for character in value):
        raise FormatError(f"{value!r} is not a valid URI")
    parts = urlsplit(value)
    if not parts.scheme or not _URI_SCHEME_RE.fullmatch(parts.scheme):
        raise FormatError(f"{value!r} is not a valid URI")
    if parts.scheme.lower() in ("http", "https") and not parts.netloc:
        raise FormatError(f"{value!r} is not a valid HTTP URI")
    return True


@lru_cache(maxsize=1)
def _format_checker() -> Any:
    try:
        from jsonschema import FormatChecker
    except ImportError as exc:
        raise ToolError(
            "缺少 jsonschema 依赖",
            source="models.jsonschema",
            recovery="安装 jsonschema>=4.23,<5",
        ) from exc
    checker = FormatChecker()
    checker.checks("date-time")(_check_datetime_format)
    checker.checks("uri")(_check_uri_format)
    return checker


def format_checker_status() -> dict[str, bool]:
    checker = _format_checker()
    return {
        name: name in checker.checkers
        for name in ("date-time", "uri")
    }


@cache
def _validator(name: str) -> Any:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise ToolError(
            "缺少 jsonschema 依赖",
            source="models.jsonschema",
            recovery="安装 jsonschema>=4.23,<5",
        ) from exc
    return Draft202012Validator(load_schema(name), format_checker=_format_checker())


def validate_document(document: Any, schema_name: str) -> None:
    """Validate a document and raise SchemaError with concrete paths."""
    validator = _validator(schema_name)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if not errors and schema_name == "entity":
        try:
            validate_entity_id(document.get("kind", ""), document.get("id", ""))
        except (TypeError, ValueError) as exc:
            raise SchemaError(
                "entity id 与 kind 不匹配",
                source="schema:entity",
                details={"path": "id", "message": str(exc)},
            ) from exc
    if not errors:
        return
    details = [
        {
            "path": "/".join(str(part) for part in error.absolute_path) or "<root>",
            "message": error.message,
            "validator": error.validator,
        }
        for error in errors[:20]
    ]
    raise SchemaError(
        f"{schema_name} 文档不符合 Schema（{len(errors)} 处）",
        source=f"schema:{schema_name}",
        details={"errors": details},
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def to_iso_z(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("to_iso_z requires a timezone-aware datetime")
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repository_id(host: str, owner: str, name: str) -> str:
    return f"{host.lower()}/{owner.lower()}/{name.lower()}"


def _encode_path(value: str) -> str:
    text = value.strip("/")
    segments = text.split("/")
    if not text or any(segment in ("", ".", "..") for segment in segments):
        raise ValueError(f"invalid path or tag: {value!r}")
    return "/".join(quote(segment, safe="") for segment in segments)


def validate_entity_id(kind: str, value: str) -> None:
    """Enforce the frozen kind-to-ID binding."""
    if not isinstance(value, str) or not ENTITY_ID_RE.fullmatch(value):
        raise ValueError(f"invalid entity id: {value!r}")
    parts = value.split("/")
    rest = parts[3:]
    if kind == "repository":
        if rest:
            raise ValueError("repository id must be host/owner/repo")
        return
    if kind in _ID_KIND:
        if len(rest) != 2 or rest[0] != _ID_KIND[kind] or not rest[1].isdigit():
            raise ValueError(f"{kind} id must end with /{_ID_KIND[kind]}/<number>")
        return
    if kind == "release":
        if len(rest) < 2 or rest[0] != "release":
            raise ValueError("release id must end with /release/<tag>")
        return
    if kind == "commit":
        if len(rest) != 1 or not FULL_SHA_RE.fullmatch(rest[0]):
            raise ValueError("commit id must end with /<40-char-sha>")
        return
    if kind in ("doc", "source_file"):
        if len(rest) < 2 or not FULL_SHA_RE.fullmatch(rest[0]):
            raise ValueError("doc/source_file id must contain /<sha>/<path>")
        return
    if kind == "comment":
        if (
            len(rest) != 3
            or rest[0] != "comment"
            or rest[1] not in ("issue", "review")
            or not rest[2].isdigit()
        ):
            raise ValueError("comment id must end with /comment/{issue|review}/<number>")
        return
    if kind == "review":
        if len(rest) != 2 or rest[0] != "review" or not rest[1].isdigit():
            raise ValueError("review id must end with /review/<number>")
        return
    raise ValueError(f"unsupported entity kind for id: {kind}")


def entity_id(
    host: str,
    owner: str,
    name: str,
    kind: str,
    ref: str,
    *,
    revision: str | None = None,
    subtype: str | None = None,
) -> str:
    """Build a frozen logical ID.

    Paths and tags are percent-encoded per segment.

      repository                     host/owner/repo
      pull_request / issue           host/owner/repo/{pull|issue}/<number>
      discussion                     host/owner/repo/discussion/<number>
      release                        host/owner/repo/release/<tag>
      commit                         host/owner/repo/<40-char-sha>
      doc / source_file              host/owner/repo/<40-char-sha>/<path>
      comment                        host/owner/repo/comment/{issue|review}/<number>
      review                         host/owner/repo/review/<number>
    """
    base = repository_id(host, owner, name)
    if kind == "repository":
        result = base
    elif kind in _ID_KIND:
        if not ref.isdigit():
            raise ValueError(f"{kind} reference must be numeric: {ref!r}")
        result = f"{base}/{_ID_KIND[kind]}/{ref}"
    elif kind == "release":
        result = f"{base}/release/{_encode_path(ref)}"
    elif kind == "commit":
        sha = (revision or ref).strip()
        if not FULL_SHA_RE.fullmatch(sha):
            raise ValueError(f"commit reference must be a full 40-char SHA: {sha!r}")
        result = f"{base}/{sha}"
    elif kind in ("doc", "source_file"):
        sha = (revision or "").strip()
        if not FULL_SHA_RE.fullmatch(sha):
            raise ValueError("doc/source_file requires a full 40-char revision SHA")
        result = f"{base}/{sha}/{_encode_path(ref)}"
    elif kind == "comment":
        if subtype not in ("issue", "review"):
            raise ValueError("comment requires subtype='issue' or subtype='review'")
        if not ref.isdigit():
            raise ValueError(f"comment reference must be numeric: {ref!r}")
        result = f"{base}/comment/{subtype}/{ref}"
    elif kind == "review":
        if not ref.isdigit():
            raise ValueError(f"review reference must be numeric: {ref!r}")
        result = f"{base}/review/{ref}"
    else:
        raise ValueError(f"unsupported entity kind for id: {kind}")

    validate_entity_id(kind, result)
    return result


def is_entity_id(value: str) -> bool:
    return bool(ENTITY_ID_RE.fullmatch(value))


def make_entity(
    *,
    entity_id: str,
    kind: str,
    title: str,
    url: str,
    observed_at: str,
    raw_state: dict[str, Any] | None = None,
    normalized_state: dict[str, Any] | None = None,
    roles: Iterable[str] = (),
    number: int | None = None,
    node_id: str | None = None,
    created_at: str | None = None,
    updated_at: str | None = None,
    source_event_at: str | None = None,
    evidence_ids: Iterable[str] = (),
    discovery_reasons: Iterable[str] = (),
    external: bool = False,
) -> dict[str, Any]:
    validate_entity_id(kind, entity_id)
    return {
        "id": entity_id,
        "kind": kind,
        "url": url,
        "title": title,
        "observed_at": observed_at,
        "source_event_at": source_event_at,
        "number": number,
        "node_id": node_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "roles": list(roles),
        "raw_state": raw_state or {},
        "normalized_state": normalized_state or {},
        "evidence_ids": list(evidence_ids),
        "discovery_reasons": list(discovery_reasons),
        "external": external,
    }


def make_locator(
    format: str,
    *,
    pointer: str | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    url_fragment: str | None = None,
) -> dict[str, Any]:
    return {
        "format": format,
        "pointer": pointer,
        "char_start": char_start,
        "char_end": char_end,
        "line_start": line_start,
        "line_end": line_end,
        "url_fragment": url_fragment,
    }


def make_evidence(
    *,
    evidence_id: str,
    entity_id: str,
    source_url: str,
    source_kind: str,
    observed_at: str,
    text: str,
    locator: dict[str, Any],
    blob_sha256: str,
    completeness: str = "complete",
    verification: str = "unverifiable",
    source_event_at: str | None = None,
    source_revision: str | None = None,
) -> dict[str, Any]:
    normalized = make_locator(
        locator["format"],
        pointer=locator.get("pointer"),
        char_start=locator.get("char_start"),
        char_end=locator.get("char_end"),
        line_start=locator.get("line_start"),
        line_end=locator.get("line_end"),
        url_fragment=locator.get("url_fragment"),
    )
    return {
        "id": evidence_id,
        "entity_id": entity_id,
        "source_url": source_url,
        "source_kind": source_kind,
        "observed_at": observed_at,
        "source_event_at": source_event_at,
        "source_revision": source_revision,
        "blob_sha256": blob_sha256,
        "locator": normalized,
        "text": text,
        "completeness": completeness,
        "verification": verification,
    }


def make_relation(
    *,
    relation_id: str,
    from_id: str,
    to_id: str,
    relation_type: str,
    basis: str,
    verification: str,
    evidence_ids: Iterable[str] = (),
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": relation_id,
        "from_id": from_id,
        "to_id": to_id,
        "type": relation_type,
        "basis": basis,
        "verification": verification,
        "evidence_ids": list(evidence_ids),
        "scope": scope,
    }


def make_claim(
    *,
    claim_id: str,
    text: str,
    kind: str,
    evidence_ids: Iterable[str] = (),
    confidence: str = "medium",
    confidence_reason: str = "",
    scope: dict[str, Any] | None = None,
    contradicting_evidence_ids: Iterable[str] = (),
    reason: str | None = None,
    missing_evidence: Iterable[str] = (),
    state_time_basis: str = "observed_current",
) -> dict[str, Any]:
    return {
        "id": claim_id,
        "text": text,
        "kind": kind,
        "evidence_ids": list(evidence_ids),
        "contradicting_evidence_ids": list(contradicting_evidence_ids),
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "scope": scope
        or {"entity_ids": [], "capability_id": None, "revision": None, "version": None},
        "reason": reason,
        "missing_evidence": list(missing_evidence),
        "state_time_basis": state_time_basis,
    }


def make_capability(
    *,
    capability_id: str,
    topic_id: str,
    title: str,
    scope: str = "",
    proposal_state: str = "unknown",
    implementation_state: str = "unknown",
    delivery_state: str = "unknown",
    availability: str = "unknown",
    target_branch: str | None = None,
    observed_revision: str | None = None,
    verified_versions: Iterable[str] = (),
    requirements_done: Iterable[str] = (),
    requirements_remaining: Iterable[str] = (),
    evidence_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "id": capability_id,
        "topic_id": topic_id,
        "title": title,
        "scope": scope,
        "proposal_state": proposal_state,
        "implementation_state": implementation_state,
        "delivery_state": delivery_state,
        "availability": availability,
        "target_branch": target_branch,
        "observed_revision": observed_revision,
        "verified_versions": list(verified_versions),
        "requirements": {
            "done": list(requirements_done),
            "remaining": list(requirements_remaining),
        },
        "evidence_ids": list(evidence_ids),
    }


def make_topic(
    *,
    topic_id: str,
    title: str,
    research_question: str,
    summary: str = "",
    background_claim_ids: Iterable[str] = (),
    problem_claim_ids: Iterable[str] = (),
    design_claim_ids: Iterable[str] = (),
    implementation_claim_ids: Iterable[str] = (),
    tradeoff_claim_ids: Iterable[str] = (),
    unresolved_claim_ids: Iterable[str] = (),
    outlook_claim_ids: Iterable[str] = (),
    capability_ids: Iterable[str] = (),
    reading_item_ids: Iterable[str] = (),
    gaps: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "id": topic_id,
        "title": title,
        "research_question": research_question,
        "summary": summary,
        "claim_ids": {
            "background": list(background_claim_ids),
            "problem": list(problem_claim_ids),
            "design": list(design_claim_ids),
            "implementation": list(implementation_claim_ids),
            "tradeoff": list(tradeoff_claim_ids),
            "unresolved": list(unresolved_claim_ids),
            "outlook": list(outlook_claim_ids),
        },
        "capability_ids": list(capability_ids),
        "reading_item_ids": list(reading_item_ids),
        "gaps": list(gaps),
    }


def build_manifest(config: dict[str, Any], run_id: str, created_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "tool_version": TOOL_VERSION,
        "state": "initialized",
        "created_at": created_at,
        "updated_at": created_at,
        "repository": config["repository"],
        "config_hash": config["config_hash"],
        "activity_window": {
            "start": config["window"]["start"],
            "end": config["window"]["end"],
            "timezone": config["window"]["timezone"],
        },
        "observed": {"started_at": None, "finished_at": None},
        "collection_started_at": None,
        "collection_finished_at": None,
        "state_time_basis": "observed_current",
        "default_branch": None,
        "default_branch_sha": None,
        "coverage": [],
        "budget": config["budget"],
        "budget_consumed": {
            "requests": 0,
            "response_bytes": 0,
            "total_bytes": 0,
            "deep_entities": 0,
            "runtime_seconds": 0,
        },
        "stages": {
            name: ("complete" if name == "initialized" else "not_started")
            for name in STAGE_NAMES
        },
        "notes": [],
    }


def verify_evidence_quote(evidence: dict[str, Any], payload: bytes) -> tuple[bool, str]:
    """Check that evidence.text equals the located slice of the saved payload."""
    locator = evidence.get("locator")
    if not isinstance(locator, dict):
        return False, "invalid_locator"
    fmt = locator.get("format")
    text = normalize_newlines(evidence.get("text", ""))

    if fmt == "url_fragment":
        return False, "unverifiable_locator"
    if fmt == "json_pointer":
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, "payload_not_json"
        pointer = locator.get("pointer")
        if pointer is None:
            return False, "pointer_missing"
        if not isinstance(pointer, str):
            return False, "pointer_invalid"
        try:
            value = resolve_json_pointer(document, pointer)
        except (KeyError, IndexError, ValueError, TypeError):
            return False, "pointer_not_found"
        if not isinstance(value, str):
            return False, "pointer_target_not_text"
        return _compare_slice(normalize_newlines(value), locator, text)
    if fmt == "char_range":
        try:
            source = payload.decode("utf-8")
        except UnicodeDecodeError:
            return False, "payload_not_text"
        return _compare_slice(normalize_newlines(source), locator, text)
    if fmt == "line_range":
        try:
            lines = payload.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return False, "payload_not_text"
        start = locator.get("line_start")
        end = locator.get("line_end")
        if not isinstance(start, int) or not isinstance(end, int):
            return False, "line_range_missing"
        if start < 1 or end < start or end > len(lines):
            return False, "line_range_out_of_bounds"
        return _compare_slice(normalize_newlines("".join(lines[start - 1 : end])), locator, text)
    return False, "unknown_locator_format"


def resolve_json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("JSON pointer must start with '/'")
    current = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(token)]
        elif isinstance(current, dict):
            current = current[token]
        else:
            raise KeyError(token)
    return current


def normalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _compare_slice(source: str, locator: dict[str, Any], text: str) -> tuple[bool, str]:
    start = locator.get("char_start")
    end = locator.get("char_end")
    if start is None and end is None:
        return (source == text, "quote_matches" if source == text else "quote_mismatch")
    if not isinstance(start, int) or not isinstance(end, int):
        return False, "char_range_missing"
    if start < 0 or end < start or end > len(source):
        return False, "char_range_out_of_bounds"
    matches = source[start:end] == text
    return matches, "quote_matches" if matches else "quote_mismatch"
