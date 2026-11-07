"""Select proposal carriers (issues/discussions) and deep-read issues.

RFC/design role detection is a candidate signal only: keywords and labels put
an object on the reading list, but the confirmed role belongs to the Agent's
deep read. This collector never turns a keyword hit into a research claim.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .. import SCHEMA_VERSION
from ..errors import (
    BudgetExceededError,
    GitHubError,
    GitHubNotFoundError,
    InternalError,
    StoreError,
    ValidationFailure,
)
from ..models import canonical_json, entity_id, validate_document
from ..store import SnapshotStore
from .base import (
    FATAL_ERRORS,
    CollectContext,
    error_code,
    is_run_budget_error,
    mark_run_budget_exhausted,
)
from .discussions import ProposalNotFound, read_discussion
from .pages import SectionSpec, dedupe_by_field, fetch_section
from .pr_store import load_pr_documents
from .progress import coverage_entry
from .proposal_store import (
    detail_path,
    load_discussion_documents,
    load_issue_documents,
    project_proposal_read,
    reconcile_proposal_projection,
    write_discussion_document,
    write_issue_document,
)
from .status import (
    derive_detail_channel_status,
    derive_detail_status,
    derive_discussion_detail_status,
    derive_issue_detail_status,
    derive_issue_limitations,
    derive_proposal_selection_limitations,
)
from .texts import text_ref
from .values import (
    as_int,
    as_optional_int,
    as_optional_str,
    comment_count,
    get_in,
    iso_or_none,
    parse_datetime,
    reaction_count,
    user_login,
    window_bounds,
)
from .values import (
    labels as label_names,
)

PROPOSAL_QUOTA = 8
ISSUE_SHARE = 6
MAX_CONTEXT_CANDIDATES = 20
MAX_PROPOSAL_REQUESTS = 80
COMMENTS_CAP = 500
TIMELINE_CAP = 300
_LOCAL_ERRORS = (StoreError, ValidationFailure, InternalError)
_NON_MATERIAL_FIELDS = frozenset({"observed_at", "selection_reason"})
_ROLE_LABELS = {
    "rfc": "rfc",
    "rfcs": "rfc",
    "proposal": "proposal",
    "proposals": "proposal",
    "design": "design",
    "design-proposal": "design",
    "architecture": "architecture",
    "feature-request": "feature_request",
    "feature request": "feature_request",
    "enhancement": "enhancement",
    "roadmap": "roadmap",
}
_TITLE_ROLE_RE = re.compile(
    r"\A\s*[\[\]]?\s*(rfc|proposal|design)\b",
    re.IGNORECASE,
)
_ISSUE_REFERENCE_RE = re.compile(r"(?<![\w/])#(\d{1,9})\b")
_DISCUSSION_URL_RE = re.compile(r"/discussions/(\d{1,9})\b")
_DISCUSSION_ROLE_CATEGORIES = {
    "architecture": "architecture",
    "ideas": "design",
    "design": "design",
    "proposals": "proposal",
    "rfc": "rfc",
}
_STATE_REASONS = frozenset({"completed", "not_planned", "reopened", "duplicate"})


class ReferencedPullRequest(ProposalNotFound):
    """A referenced #number resolved to a pull request, not a proposal."""

    code = "referenced_pull_request"


@dataclass
class ProposalCandidate:
    carrier: str
    number: int
    entity_id: str
    title: str
    scope: str
    raw: dict[str, Any]
    score: int
    components: dict[str, int]
    role_hints: list[str]
    reasons: list[str]
    selected: bool = False
    skipped_reason: str | None = None
    status: str = "not_attempted"


@dataclass
class _Read:
    document: dict[str, Any]
    pages: int
    changed: bool


@dataclass
class ProposalCollection:
    coverage: list[dict[str, Any]]
    selection: dict[str, Any]
    documents: list[dict[str, Any]]
    counts: dict[str, int]


@dataclass
class _RunState:
    results: list[dict[str, Any]]
    retained: list[tuple[str, Mapping[str, Any]]]
    current: dict[str, list[dict[str, Any]]]
    pages: dict[str, int]
    notes: dict[str, list[str]]

    @classmethod
    def empty(cls) -> _RunState:
        return cls(
            results=[],
            retained=[],
            current={"issue": [], "discussion": []},
            pages={"issue": 0, "discussion": 0},
            notes={},
        )


class _ProposalBudgetStop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def collect_proposals(
    context: CollectContext,
    *,
    activity_window: Mapping[str, Any],
    activity_overview: Mapping[str, Any] | None = None,
    pr_selection: Mapping[str, Any] | None = None,
    include_discussions: bool = True,
) -> ProposalCollection:
    store = context.store
    client = context.client
    host, owner, name = _repository_parts(context.repository)
    start, end = _parse_window(activity_window)
    observed_at = context.now()
    documents = _load_proposal_documents(store)
    existing = {
        carrier: {int(item["number"]): item for item in items}
        for carrier, items in documents.items()
    }
    reconcile_proposal_projection(
        store,
        _projection_pairs(documents),
        host=host,
        owner=owner,
        name=name,
    )
    candidates = build_candidates(
        store,
        pr_selection=pr_selection,
        focus=_focus(store),
        start=start,
        end=end,
        host=host,
        owner=owner,
        name=name,
        include_discussions=include_discussions,
    )
    quota, issue_quota, discussion_quota = quota_for_budget(
        client.budget,
        client.deep_entities_used(),
    )
    choose(candidates, quota, issue_quota, discussion_quota)
    counts = _empty_counts(candidates)
    state = _RunState.empty()

    def flush(status: str) -> None:
        _flush_stages(context, candidates, status)

    def finalize(*, best_effort: bool = False) -> ProposalCollection:
        return _finalize(
            context,
            candidates,
            state,
            counts,
            activity_window,
            observed_at,
            activity_overview,
            quota=quota,
            issue_quota=issue_quota,
            discussion_quota=discussion_quota,
            best_effort=best_effort,
        )

    try:
        _run_candidates(
            context,
            candidates,
            existing=existing,
            observed_at=observed_at,
            state=state,
            flush=flush,
        )
    except FATAL_ERRORS + _LOCAL_ERRORS:
        finalize(best_effort=True)
        raise
    return finalize()


def _flush_stages(
    context: CollectContext,
    candidates: list[ProposalCandidate],
    status: str,
) -> None:
    if context.progress is None:
        return
    for carrier in ("issue", "discussion"):
        completed = [
            candidate.number
            for candidate in candidates
            if candidate.carrier == carrier
            and candidate.status in ("selected", "skipped_unchanged")
        ]
        pending = [
            candidate.number
            for candidate in candidates
            if candidate.carrier == carrier
            and candidate.selected
            and candidate.status == "not_attempted"
        ]
        context.progress.flush_stage(
            f"proposal_{carrier}",
            status=status,
            completed=completed,
            pending=pending,
            observed_at=context.now(),
            best_effort=True,
        )


def _run_candidates(
    context: CollectContext,
    candidates: list[ProposalCandidate],
    *,
    existing: Mapping[str, Mapping[int, Mapping[str, Any]]],
    observed_at: str,
    state: _RunState,
    flush,
) -> None:
    requests_before = int(context.client.usage.get("requests", 0))
    for carrier in ("issue", "discussion"):
        carrier_existing = existing.get(carrier, {})
        for candidate in [
            item for item in candidates if item.selected and item.carrier == carrier
        ]:
            used = int(context.client.usage.get("requests", 0)) - requests_before
            if used >= MAX_PROPOSAL_REQUESTS:
                _note_pending(
                    state,
                    candidates,
                    f"proposal_request_limit:{MAX_PROPOSAL_REQUESTS}",
                )
                flush("partial")
                return
            try:
                _run_candidate(
                    context,
                    candidate,
                    existing=carrier_existing,
                    observed_at=observed_at,
                    state=state,
                    flush=flush,
                )
            except _ProposalBudgetStop as stop:
                _note_pending(state, candidates, stop.reason)
                flush("partial")
                return


def _note_pending(
    state: _RunState,
    candidates: list[ProposalCandidate],
    reason: str,
) -> None:
    for carrier in ("issue", "discussion"):
        if any(
            item.selected
            and item.carrier == carrier
            and item.status == "not_attempted"
            for item in candidates
        ):
            state.notes.setdefault(carrier, []).append(reason)


def _run_candidate(
    context: CollectContext,
    candidate: ProposalCandidate,
    *,
    existing: Mapping[int, Mapping[str, Any]],
    observed_at: str,
    state: _RunState,
    flush,
) -> None:
    try:
        read = _read_candidate(
            context,
            candidate,
            existing=existing,
            observed_at=observed_at,
        )
    except ReferencedPullRequest as exc:
        candidate.selected = False
        candidate.status = "skipped_reason"
        candidate.skipped_reason = error_code(exc, "referenced_pull_request")
        flush("in_progress")
        return
    except ProposalNotFound as exc:
        candidate.selected = False
        candidate.status = "skipped_reason"
        candidate.skipped_reason = error_code(exc, "proposal_not_found")
        flush("in_progress")
        return
    except BudgetExceededError as exc:
        if is_run_budget_error(exc):
            mark_run_budget_exhausted(context)
        candidate.status = "not_attempted"
        flush("partial")
        raise _ProposalBudgetStop(error_code(exc, "budget_exceeded")) from exc
    except FATAL_ERRORS:
        flush("partial")
        raise
    except _LOCAL_ERRORS as exc:
        candidate.status = "failed"
        candidate.skipped_reason = error_code(exc, "store_error")
        flush("partial")
        raise
    except GitHubError as exc:
        candidate.status = "failed"
        candidate.skipped_reason = error_code(exc, "github_error")
        flush("in_progress")
        return
    state.pages[candidate.carrier] += read.pages
    candidate.title = str(read.document["title"])
    previous = existing.get(candidate.number)
    if previous is not None and _would_downgrade(previous, read.document):
        candidate.status = "failed"
        candidate.skipped_reason = "refresh_incomplete"
        state.retained.append((candidate.carrier, dict(previous)))
        flush("in_progress")
        return
    if not read.changed:
        candidate.status = "skipped_unchanged"
        candidate.skipped_reason = "materials_unchanged"
        if previous is not None:
            state.current[candidate.carrier].append(dict(previous))
        flush("in_progress")
        return
    _persist_candidate(context, candidate, read.document)
    candidate.status = "selected"
    if previous is None:
        context.client.usage["deep_entities"] += 1
    state.current[candidate.carrier].append(read.document)
    state.results.append(read.document)
    flush("in_progress")


def _repository_parts(repository: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(repository.get("host") or "github.com"),
        str(repository["owner"]),
        str(repository["name"]),
    )


def _focus(store: SnapshotStore) -> list[str]:
    try:
        config = store.read_json("config.resolved.json")
    except Exception:
        return []
    values = config.get("focus") if isinstance(config, Mapping) else None
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if isinstance(value, str) and value.strip()]


def _parse_window(window: Mapping[str, Any]) -> tuple[datetime, datetime]:
    try:
        return window_bounds(window)
    except ValueError as exc:
        raise GitHubError("活动窗口缺少合法边界", source="proposals.window") from exc


def _load_proposal_documents(
    store: SnapshotStore,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "issue": load_issue_documents(store),
        "discussion": load_discussion_documents(store),
    }


def _projection_pairs(
    documents: Mapping[str, list[dict[str, Any]]],
) -> list[tuple[str, Mapping[str, Any]]]:
    pairs: list[tuple[str, Mapping[str, Any]]] = []
    for carrier in ("issue", "discussion"):
        pairs.extend((carrier, document) for document in documents[carrier])
    return pairs


def build_candidates(
    store: SnapshotStore,
    *,
    pr_selection: Mapping[str, Any] | None,
    focus: Iterable[str],
    start: datetime,
    end: datetime,
    host: str,
    owner: str,
    name: str,
    include_discussions: bool = True,
) -> list[ProposalCandidate]:
    discovery = _discovery_reasons(store, host, owner, name)
    focus_tokens = [item.strip().casefold() for item in focus if item.strip()]
    raw_streams = (("issue", "raw/issues.jsonl"), ("discussion", "raw/discussions.jsonl"))
    seen: set[tuple[str, int]] = set()
    candidates: list[ProposalCandidate] = []
    for carrier, path in raw_streams:
        if carrier == "discussion" and not include_discussions:
            continue
        for raw in store.read_jsonl(path):
            number = raw.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                continue
            seen.add((carrier, number))
            identifier = entity_id(host, owner, name, carrier, str(number))
            candidates.append(
                _candidate_from_raw(
                    carrier,
                    number,
                    identifier,
                    raw,
                    scope="in_window",
                    discovery=discovery.get(identifier, []),
                    focus_tokens=focus_tokens,
                    start=start,
                    end=end,
                )
            )
    references = _referenced_candidates(
        store,
        pr_selection=pr_selection,
        seen=seen,
        host=host,
        owner=owner,
        name=name,
        include_discussions=include_discussions,
    )
    candidates.extend(references)
    return sorted(candidates, key=lambda item: (-item.score, item.carrier, item.number))


def _candidate_from_raw(
    carrier: str,
    number: int,
    identifier: str,
    raw: Mapping[str, Any],
    *,
    scope: str,
    discovery: Iterable[str],
    focus_tokens: Iterable[str],
    start: datetime,
    end: datetime,
) -> ProposalCandidate:
    components: dict[str, int] = {}
    reasons: list[str] = []
    role_hints = _role_hints(carrier, raw)
    if role_hints:
        components["role_hints"] = min(len(role_hints), 3) * 20
        reasons.extend(f"role_hint:{hint}" for hint in role_hints)
    comments = comment_count(raw) or 0
    if comments:
        components["comments"] = min(comments, 20)
        if comments >= 5:
            reasons.append("active_discussion")
    reactions = reaction_count(raw) or 0
    if reactions:
        components["reactions"] = min(reactions, 10)
        if reactions >= 5:
            reasons.append("notable_reactions")
    updated = parse_datetime(raw.get("updated_at") or raw.get("updatedAt"))
    if updated is not None and start <= updated < end:
        components["recent_update"] = 10
        reasons.append("updated_in_window")
    created = parse_datetime(raw.get("created_at") or raw.get("createdAt"))
    if created is not None and created < start:
        reasons.append("created_before_window")
    opened = _is_open(carrier, raw)
    if opened:
        components["open"] = 5
        reasons.append("open")
    state_reason = _state_reason(raw)
    if state_reason == "not_planned":
        components["closed_not_planned"] = -10
        reasons.append("closed_not_planned")
    if state_reason == "duplicate":
        components["closed_duplicate"] = -15
        reasons.append("closed_as_duplicate")
    discovered = list(discovery)
    if discovered:
        components["discovery_reasons"] = min(len(discovered), 2) * 5
        reasons.extend(f"discovered:{value}" for value in discovered)
    haystack = " ".join(
        [str(raw.get("title") or "")]
        + [str(label) for label in label_names(raw)]
        + [str(value) for value in role_hints]
    ).casefold()
    matched = [token for token in focus_tokens if token in haystack]
    if matched:
        components["focus_match"] = min(len(matched), 2) * 15
        reasons.extend(f"focus_match:{token}" for token in matched)
    if scope == "context":
        components["referenced_by_pr"] = 25
        reasons.append("referenced_by_pr")
    score = sum(components.values())
    return ProposalCandidate(
        carrier=carrier,
        number=number,
        entity_id=identifier,
        title=str(raw.get("title") or f"#{number}"),
        scope=scope,
        raw=dict(raw),
        score=score,
        components=components,
        role_hints=role_hints,
        reasons=sorted(dict.fromkeys(reasons)),
    )


def _document_role_hints(
    carrier: str,
    document: Mapping[str, Any],
) -> list[str]:
    hints: set[str] = set()
    for label in document.get("labels") or []:
        value = _ROLE_LABELS.get(str(label).strip().casefold())
        if value:
            hints.add(value)
    match = _TITLE_ROLE_RE.match(str(document.get("title") or ""))
    if match:
        value = _ROLE_LABELS.get(match.group(1).casefold())
        if value:
            hints.add(value)
    if carrier == "discussion":
        category = document.get("category")
        if isinstance(category, str):
            value = _DISCUSSION_ROLE_CATEGORIES.get(category.strip().casefold())
            if value:
                hints.add(value)
    return sorted(hints)


def _role_hints(carrier: str, raw: Mapping[str, Any]) -> list[str]:
    hints: set[str] = set()
    for label in label_names(raw):
        value = _ROLE_LABELS.get(label.strip().casefold())
        if value:
            hints.add(value)
    title = str(raw.get("title") or "")
    match = _TITLE_ROLE_RE.match(title)
    if match:
        value = _ROLE_LABELS.get(match.group(1).casefold())
        if value:
            hints.add(value)
    if carrier == "discussion":
        category = get_in(raw, "category", "name")
        if isinstance(category, str):
            value = _DISCUSSION_ROLE_CATEGORIES.get(category.strip().casefold())
            if value:
                hints.add(value)
    body = raw.get("body")
    if carrier == "issue" and isinstance(body, str):
        lowered = body.casefold()
        if "## motivation" in lowered or "## alternatives" in lowered:
            hints.add("design")
        if "request for comments" in lowered:
            hints.add("rfc")
    return sorted(hints)


def _is_open(carrier: str, raw: Mapping[str, Any]) -> bool:
    if carrier == "discussion":
        return not bool(raw.get("closed"))
    return raw.get("state") == "open"


def _state_reason(raw: Mapping[str, Any]) -> str | None:
    value = raw.get("state_reason")
    if not isinstance(value, str):
        value = raw.get("stateReason")
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    return normalized if normalized in _STATE_REASONS else None


def _discovery_reasons(
    store: SnapshotStore,
    host: str,
    owner: str,
    name: str,
) -> dict[str, list[str]]:
    del host, owner, name
    result: dict[str, list[str]] = {}
    for entity in store.read_jsonl("normalized/entities.jsonl"):
        if not isinstance(entity, Mapping) or not isinstance(entity.get("id"), str):
            continue
        values = entity.get("discovery_reasons")
        if isinstance(values, list):
            result[entity["id"]] = [
                item for item in values if isinstance(item, str) and item
            ]
    return result


def _referenced_candidates(
    store: SnapshotStore,
    *,
    pr_selection: Mapping[str, Any] | None,
    seen: set[tuple[str, int]],
    host: str,
    owner: str,
    name: str,
    include_discussions: bool,
) -> list[ProposalCandidate]:
    if not isinstance(pr_selection, Mapping):
        return []
    selected = {
        int(candidate["number"])
        for candidate in pr_selection.get("candidates", [])
        if isinstance(candidate, Mapping)
        and candidate.get("selected")
        and isinstance(candidate.get("number"), int)
        and not isinstance(candidate.get("number"), bool)
    }
    if not selected:
        return []
    pr_numbers = {
        int(record["number"])
        for record in store.read_jsonl("raw/prs.jsonl")
        if isinstance(record.get("number"), int)
        and not isinstance(record.get("number"), bool)
    }
    issue_refs: set[int] = set()
    discussion_refs: set[int] = set()
    for document in load_pr_documents(store):
        if int(document["number"]) not in selected:
            continue
        text = _document_text(store, document.get("body"))
        if text is None:
            continue
        issue_refs.update(int(match.group(1)) for match in _ISSUE_REFERENCE_RE.finditer(text))
        discussion_refs.update(
            int(match.group(1)) for match in _DISCUSSION_URL_RE.finditer(text)
        )
    result: list[ProposalCandidate] = []
    for carrier, numbers in (
        ("issue", issue_refs),
        ("discussion", discussion_refs),
    ):
        if carrier == "discussion" and not include_discussions:
            continue
        for number in sorted(numbers)[:MAX_CONTEXT_CANDIDATES]:
            if number < 1 or (carrier, number) in seen:
                continue
            if carrier == "issue" and number in pr_numbers:
                continue
            identifier = entity_id(host, owner, name, carrier, str(number))
            result.append(
                ProposalCandidate(
                    carrier=carrier,
                    number=number,
                    entity_id=identifier,
                    title=f"#{number}",
                    scope="context",
                    raw={},
                    score=25,
                    components={"referenced_by_pr": 25},
                    role_hints=[],
                    reasons=["referenced_by_pr"],
                )
            )
    return result


def _document_text(store: SnapshotStore, ref: Any) -> str | None:
    if not isinstance(ref, Mapping):
        return None
    digest = ref.get("sha256")
    if not isinstance(digest, str) or not digest:
        return None
    try:
        return store.read_blob(digest).decode("utf-8")
    except (StoreError, UnicodeDecodeError):
        return None


def quota_for_budget(
    budget: Mapping[str, Any],
    used_deep_entities: int = 0,
) -> tuple[int, int, int]:
    max_entities = int(budget.get("max_deep_entities", 0))
    reserve = max(0, int(budget.get("agent_pr_reserve", 0)))
    quota = max(
        0,
        min(PROPOSAL_QUOTA, max_entities - used_deep_entities - reserve),
    )
    issue_quota = min(ISSUE_SHARE, quota)
    discussion_quota = quota - issue_quota
    return quota, issue_quota, discussion_quota


def choose(
    candidates: list[ProposalCandidate],
    quota: int,
    issue_quota: int,
    discussion_quota: int,
) -> None:
    if quota <= 0:
        for candidate in candidates:
            candidate.selected = False
            candidate.skipped_reason = "quota_exhausted"
            candidate.status = "skipped_quota"
        return
    chosen: set[tuple[str, int]] = set()

    def take(carrier: str, limit: int) -> None:
        taken = 0
        for candidate in candidates:
            if taken >= limit:
                break
            if candidate.carrier != carrier:
                continue
            if (candidate.carrier, candidate.number) in chosen:
                continue
            chosen.add((candidate.carrier, candidate.number))
            taken += 1

    take("issue", issue_quota)
    take("discussion", discussion_quota)
    for candidate in candidates:
        if len(chosen) >= quota:
            break
        key = (candidate.carrier, candidate.number)
        if key not in chosen:
            chosen.add(key)
    for candidate in candidates:
        key = (candidate.carrier, candidate.number)
        if key in chosen:
            candidate.selected = True
            candidate.status = "not_attempted"
        else:
            candidate.selected = False
            candidate.skipped_reason = "quota_exhausted"
            candidate.status = "skipped_quota"


def _read_candidate(
    context: CollectContext,
    candidate: ProposalCandidate,
    *,
    existing: Mapping[str, Mapping[str, Any]],
    observed_at: str,
) -> _Read:
    if candidate.carrier == "discussion":
        read = read_discussion(
            context,
            candidate.number,
            scope=candidate.scope,
            selection_reason=candidate.reasons,
            role_hints=candidate.role_hints,
            observed_at=observed_at,
        )
        document, pages = read.document, read.pages
    else:
        issue = _read_issue(
            context,
            candidate.number,
            scope=candidate.scope,
            selection_reason=candidate.reasons,
            role_hints=candidate.role_hints,
            observed_at=observed_at,
        )
        document, pages = issue.document, issue.pages
    if not candidate.role_hints:
        hints = _document_role_hints(candidate.carrier, document)
        if hints:
            document["role_hints"] = hints
            if candidate.carrier == "issue":
                validate_document(document, "issue_detail")
            else:
                validate_document(document, "discussion_detail")
            candidate.role_hints = hints
    previous = existing.get(candidate.number)
    path = detail_path(candidate.carrier, candidate.number)
    changed = True
    if previous is not None and context.store.secure_path(path).is_file():
        changed = not _same_material(previous, document)
    return _Read(document=document, pages=pages, changed=changed)


def _read_issue(
    context: CollectContext,
    number: int,
    *,
    scope: str,
    selection_reason: list[str],
    role_hints: list[str],
    observed_at: str,
    read: Mapping[str, bool] | None = None,
    existing: Mapping[str, Any] | None = None,
) -> _Read:
    client = context.client
    store = context.store
    host, owner, name = _repository_parts(context.repository)
    base = f"repos/{owner}/{name}"
    try:
        detail = client.rest(f"{base}/issues/{number}")
    except GitHubNotFoundError as exc:
        raise ProposalNotFound(
            f"Issue #{number} 不存在或不可访问",
            source="proposal_detail.not_found",
            details={"number": number},
        ) from exc
    if not isinstance(detail, Mapping):
        raise GitHubError(
            "Issue 详情响应格式异常",
            source="issue_detail.response",
            details={"number": number},
        )
    if "pull_request" in detail:
        raise ReferencedPullRequest(
            f"#{number} 是 Pull Request，不是 Issue",
            source="proposal_detail.referenced_pull_request",
            details={"number": number},
        )
    want_comments = read is None or read.get("comments", True)
    sections: dict[str, dict[str, Any]] = {}
    pages = 1
    for spec in _issue_sections(store):
        if not want_comments:
            sections[spec.name] = _existing_section(existing, spec.name)
            continue
        section, section_pages = fetch_section(
            client,
            spec,
            owner=owner,
            name=name,
            number=number,
            parent=detail,
        )
        sections[spec.name] = section
        pages += section_pages
    body_ref = text_ref(store, detail.get("body"))
    body_status = "complete"
    status_sections = {
        **sections,
        "body": {"status": body_status},
    }
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repository": {"host": host, "owner": owner, "name": name},
        "number": number,
        "url": str(
            detail.get("html_url") or f"https://github.com/{owner}/{name}/issues/{number}"
        ),
        "title": str(detail.get("title") or f"#{number}"),
        "author": user_login(detail),
        "state": str(detail.get("state") or "unknown"),
        "state_reason": _state_reason(detail),
        "locked": bool(detail.get("locked")),
        "created_at": iso_or_none(detail.get("created_at")),
        "updated_at": iso_or_none(detail.get("updated_at")),
        "closed_at": iso_or_none(detail.get("closed_at")),
        "closed_by": as_optional_str(get_in(detail, "closed_by", "login")),
        "labels": label_names(detail),
        "comments_count": as_int(comment_count(detail)),
        "reactions": as_int(reaction_count(detail)),
        "scope": scope,
        "selection_reason": list(selection_reason),
        "role_hints": list(role_hints),
        "body": body_ref,
        "comments": sections["comments"],
        "timeline": sections["timeline"],
        "coverage": {
            "detail": _proposal_detail_status(status_sections),
            "body": body_status,
            "comments": sections["comments"]["status"],
            "timeline": sections["timeline"]["status"],
        },
        "limitations": [],
    }
    document["limitations"] = derive_issue_limitations(document)
    validate_document(document, "issue_detail")
    return _Read(document=document, pages=pages, changed=True)


def _proposal_detail_status(sections: Mapping[str, Mapping[str, Any]]) -> str:
    status = derive_detail_status(
        sections=sections,
        head_changed=False,
        head_unverified=False,
        diff_status="not_requested",
    )
    return status if status in ("complete", "partial") else "partial"


def _not_requested_section() -> dict[str, Any]:
    return {
        "status": "not_requested",
        "fetched": 0,
        "total_known": None,
        "truncated": False,
        "truncation_reason": None,
        "duplicates": 0,
        "items": [],
    }


def _existing_section(
    existing: Mapping[str, Any] | None,
    name: str,
) -> dict[str, Any]:
    if isinstance(existing, Mapping):
        section = existing.get(name)
        if isinstance(section, Mapping):
            copied = dict(section)
            copied.setdefault("duplicates", 0)
            return copied
    return _not_requested_section()


def _issue_sections(store: SnapshotStore) -> tuple[SectionSpec, ...]:
    return (
        SectionSpec(
            name="comments",
            endpoint="repos/{owner}/{name}/issues/{number}/comments",
            parse_item=lambda raw: _issue_comment_item(store, raw),
            cap=COMMENTS_CAP,
            cap_reason=f"comments_limit_{COMMENTS_CAP}",
            total_known_key="comments",
            mismatch_reason="comments_total_mismatch",
            dedupe_key=dedupe_by_field("id"),
        ),
        SectionSpec(
            name="timeline",
            endpoint="repos/{owner}/{name}/issues/{number}/timeline",
            parse_item=_timeline_item,
            cap=TIMELINE_CAP,
            cap_reason=f"timeline_limit_{TIMELINE_CAP}",
            dedupe_key=dedupe_by_field("id"),
        ),
    )


def _issue_comment_item(
    store: SnapshotStore,
    raw: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "id": as_int(raw.get("id")),
        "user": user_login(raw),
        "author_association": as_optional_str(raw.get("author_association")),
        "created_at": iso_or_none(raw.get("created_at")),
        "updated_at": iso_or_none(raw.get("updated_at")),
        "body": text_ref(store, raw.get("body")),
    }


def _timeline_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    source = raw.get("source")
    source_issue = source.get("issue") if isinstance(source, Mapping) else None
    source_number = (
        as_optional_int(source_issue.get("number"))
        if isinstance(source_issue, Mapping)
        else None
    )
    source_kind = None
    if isinstance(source_issue, Mapping):
        source_kind = "pull_request" if "pull_request" in source_issue else "issue"
    actor = get_in(raw, "actor", "login") or user_login(raw)
    return {
        "event": str(raw.get("event") or "unknown"),
        "id": as_optional_int(raw.get("id")),
        "actor": actor,
        "created_at": iso_or_none(raw.get("created_at")),
        "commit_id": as_optional_str(raw.get("commit_id") or raw.get("sha")),
        "source_number": source_number,
        "source_kind": source_kind,
        "label": as_optional_str(get_in(raw, "label", "name")),
    }


def _persist_candidate(
    context: CollectContext,
    candidate: ProposalCandidate,
    document: Mapping[str, Any],
) -> None:
    store = context.store
    if candidate.carrier == "issue":
        write_issue_document(store, document)
    else:
        write_discussion_document(store, document)
    host, owner, name = _repository_parts(context.repository)
    try:
        project_proposal_read(
            store,
            host,
            owner,
            name,
            candidate.carrier,
            document,
        )
    except Exception as exc:
        print(
            f"warning: 无法投影 {candidate.carrier} #{candidate.number}: {exc}",
            file=sys.stderr,
        )


def _same_material(
    existing: Mapping[str, Any] | None,
    document: Mapping[str, Any],
) -> bool:
    if not isinstance(existing, Mapping):
        return False
    coverage = existing.get("coverage")
    if not isinstance(coverage, Mapping) or coverage.get("detail") != "complete":
        return False
    return _comparable(existing) == _comparable(document)


def _comparable(document: Mapping[str, Any]) -> str:
    return canonical_json(
        {
            key: value
            for key, value in document.items()
            if key not in _NON_MATERIAL_FIELDS
        }
    )


def _would_downgrade(
    previous: Mapping[str, Any],
    document: Mapping[str, Any],
) -> bool:
    old = previous.get("coverage")
    old_detail = old.get("detail") if isinstance(old, Mapping) else None
    new = document.get("coverage")
    new_detail = new.get("detail") if isinstance(new, Mapping) else None
    return old_detail == "complete" and new_detail != "complete"


def _empty_counts(candidates: list[ProposalCandidate]) -> dict[str, int]:
    return {
        "candidates": len(candidates),
        "selected": sum(1 for item in candidates if item.selected),
        "issue_selected": sum(
            1 for item in candidates if item.selected and item.carrier == "issue"
        ),
        "discussion_selected": sum(
            1 for item in candidates if item.selected and item.carrier == "discussion"
        ),
        "context_selected": sum(
            1 for item in candidates if item.selected and item.scope == "context"
        ),
        "skipped_quota": 0,
        "skipped_unchanged": 0,
        "failed": 0,
        "not_attempted": 0,
    }


def _recount(
    candidates: list[ProposalCandidate],
    counts: dict[str, int],
) -> None:
    counts["selected"] = sum(1 for item in candidates if item.selected)
    counts["issue_selected"] = sum(
        1 for item in candidates if item.selected and item.carrier == "issue"
    )
    counts["discussion_selected"] = sum(
        1 for item in candidates if item.selected and item.carrier == "discussion"
    )
    counts["context_selected"] = sum(
        1 for item in candidates if item.selected and item.scope == "context"
    )
    counts["skipped_quota"] = sum(
        1 for item in candidates if item.status == "skipped_quota"
    )
    counts["skipped_unchanged"] = sum(
        1 for item in candidates if item.status == "skipped_unchanged"
    )
    counts["failed"] = sum(1 for item in candidates if item.status == "failed")
    counts["not_attempted"] = sum(
        1
        for item in candidates
        if item.selected and item.status == "not_attempted"
    )


def _finalize(
    context: CollectContext,
    candidates: list[ProposalCandidate],
    state: _RunState,
    counts: dict[str, int],
    activity_window: Mapping[str, Any],
    observed_at: str,
    activity_overview: Mapping[str, Any] | None,
    *,
    quota: int,
    issue_quota: int,
    discussion_quota: int,
    best_effort: bool = False,
) -> ProposalCollection:
    _recount(candidates, counts)
    finished = context.now()
    coverage_entries = []
    statuses: dict[str, str] = {}
    for carrier, detail_of in (
        ("issue", derive_issue_detail_status),
        ("discussion", derive_discussion_detail_status),
    ):
        selected = [item for item in candidates if item.carrier == carrier and item.selected]
        carrier_docs = [
            item
            for item in state.current.get(carrier, [])
            if isinstance(item, Mapping)
        ]
        retained_docs = [
            document
            for retained_carrier, document in state.retained
            if retained_carrier == carrier
        ]
        status = derive_detail_channel_status(
            selected=len(selected),
            failed=sum(1 for item in selected if item.status == "failed"),
            skipped_unchanged=sum(
                1 for item in selected if item.status == "skipped_unchanged"
            ),
            results=[*carrier_docs, *retained_docs],
            not_attempted=sum(
                1 for item in selected if item.status == "not_attempted"
            ),
            detail_of=detail_of,
        )
        statuses[carrier] = status
        written = sum(1 for item in selected if item.status == "selected")
        entry = coverage_entry(
            channel=f"{carrier}_detail",
            query_scope="proposal_deep_read",
            query=", ".join(f"#{item.number}" for item in selected),
            expected=len(selected),
            fetched=written
            + sum(1 for item in selected if item.status == "skipped_unchanged"),
            pages=int(state.pages.get(carrier, 0)),
            exhausted=status == "complete",
            truncation_reason=_join_reasons(
                _coverage_reason(selected, carrier_docs, retained_docs),
                state.notes.get(carrier, []),
            ),
            coverage_status=status,
            discovery_status="complete",
            detail_status=status,
            observed_start=observed_at,
            observed_end=finished,
        )
        coverage_entries.append(entry)
        if context.progress is not None:
            completed = [
                item.number
                for item in selected
                if item.status in ("selected", "skipped_unchanged")
            ]
            pending = [
                item.number for item in selected if item.status == "not_attempted"
            ]
            context.progress.finish_channel(
                f"{carrier}_detail",
                entry,
                completed=completed,
                pending=pending,
                observed_at=finished,
                best_effort=True,
            )
    selection = _selection_document(
        context,
        candidates,
        counts,
        activity_window,
        observed_at,
        quota,
        issue_quota,
        discussion_quota,
        statuses,
        activity_overview,
        notes=state.notes,
    )
    try:
        validate_document(selection, "proposal_selection")
        context.store.write_json(
            "analysis/proposal-selection.json",
            selection,
            schema_name="proposal_selection",
        )
    except Exception as exc:
        if not best_effort:
            raise
        print(f"warning: 无法写入 proposal-selection: {exc}", file=sys.stderr)
    return ProposalCollection(
        coverage=coverage_entries,
        selection=selection,
        documents=state.results,
        counts=counts,
    )


def _join_reasons(reason: str | None, notes: list[str]) -> str | None:
    parts = [part for part in [reason, *notes] if part]
    return "; ".join(dict.fromkeys(parts)) or None


def _coverage_reason(
    selected: Iterable[ProposalCandidate],
    documents: Iterable[Mapping[str, Any]],
    retained: Iterable[Mapping[str, Any]],
) -> str | None:
    reasons: dict[str, list[str]] = {}
    for item in selected:
        if item.status == "failed":
            reasons.setdefault(item.skipped_reason or "failed", []).append(
                f"#{item.number}"
            )
    for document in [*documents, *retained]:
        number = f"#{document['number']}"
        detail = document.get("coverage", {}).get("detail")
        if detail != "complete":
            reasons.setdefault("detail_partial", []).append(number)
        for key in ("comments", "timeline"):
            section = document.get(key)
            if isinstance(section, Mapping) and section.get("truncated"):
                reasons.setdefault(f"{key}_truncated", []).append(number)
        duplicates = sum(
            int(document[key].get("duplicates") or 0)
            for key in ("comments", "timeline")
            if isinstance(document.get(key), Mapping)
        )
        if duplicates:
            reasons.setdefault("pagination_duplicates", []).append(number)
    if not reasons:
        return None
    return "; ".join(
        f"{reason}:{','.join(numbers)}" for reason, numbers in sorted(reasons.items())
    )


def _selection_document(
    context: CollectContext,
    candidates: list[ProposalCandidate],
    counts: dict[str, int],
    activity_window: Mapping[str, Any],
    observed_at: str,
    quota: int,
    issue_quota: int,
    discussion_quota: int,
    statuses: Mapping[str, str],
    activity_overview: Mapping[str, Any] | None,
    *,
    notes: Mapping[str, list[str]],
) -> dict[str, Any]:
    host, owner, name = _repository_parts(context.repository)
    extra: list[str] = []
    views = (
        activity_overview.get("views")
        if isinstance(activity_overview, Mapping)
        else None
    )
    if notes:
        extra.append("proposal_budget_limited")
    if isinstance(views, list):
        for view in views:
            if not isinstance(view, Mapping):
                continue
            kind = view.get("kind")
            if kind in ("issue", "discussion") and view.get("status") != "complete":
                extra.append(f"activity_{kind}_partial")
    candidates_records = [
        {
            "carrier": item.carrier,
            "number": item.number,
            "entity_id": item.entity_id,
            "title": item.title,
            "scope": item.scope,
            "bucket": item.scope,
            "score": item.score,
            "score_components": dict(item.components),
            "role_hints": list(item.role_hints),
            "selection_reason": list(item.reasons),
            "selected": item.selected,
            "skipped_reason": item.skipped_reason,
            "status": item.status,
            "detail_path": (
                detail_path(item.carrier, item.number)
                if item.status in ("selected", "skipped_unchanged")
                else None
            ),
        }
        for item in candidates
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repository": {"host": host, "owner": owner, "name": name},
        "activity_window": {
            "start": activity_window["start"],
            "end": activity_window["end"],
            "timezone": activity_window["timezone"],
        },
        "quota": {
            "proposal_quota": quota,
            "issue_quota": issue_quota,
            "discussion_quota": discussion_quota,
            "max_deep_entities": int(
                context.client.budget.get("max_deep_entities", 0)
            ),
            "deep_entities_used": context.client.deep_entities_used(),
        },
        "candidates": candidates_records,
        "counts": {
            "candidates": counts["candidates"],
            "selected": counts["selected"],
            "issue_selected": counts["issue_selected"],
            "discussion_selected": counts["discussion_selected"],
            "context_selected": counts["context_selected"],
            "skipped_quota": counts["skipped_quota"],
            "skipped_unchanged": counts["skipped_unchanged"],
            "failed": counts["failed"],
            "not_attempted": counts["not_attempted"],
        },
        "limitations": derive_proposal_selection_limitations(
            has_quota_skip=counts["skipped_quota"] > 0,
            issue_status=statuses.get("issue", "not_requested"),
            discussion_status=statuses.get("discussion", "not_requested"),
            extra=extra,
        ),
    }


__all__ = [
    "MAX_PROPOSAL_REQUESTS",
    "PROPOSAL_QUOTA",
    "ProposalCandidate",
    "ProposalCollection",
    "build_candidates",
    "choose",
    "collect_proposals",
    "quota_for_budget",
]
