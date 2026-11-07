"""Offline normalization for evidence, entities and relation inputs.

The module never talks to GitHub. It derives deterministic records from the
authoritative collect artifacts so ``prepare`` is idempotent and cannot grow the
request ledger.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import SCHEMA_VERSION
from .collectors.docs import doc_path, load_doc_snapshots
from .collectors.entity_store import detail_path as pr_detail_path
from .collectors.entity_store import merge_entity
from .collectors.pr_store import load_pr_documents
from .collectors.proposal_store import (
    load_discussion_documents,
    load_issue_documents,
)
from .collectors.releases import load_release_documents, release_path
from .errors import StoreError, ValidationFailure
from .models import (
    FULL_SHA_RE,
    entity_id,
    make_entity,
    make_evidence,
    make_locator,
    normalize_newlines,
    validate_document,
    verify_evidence_quote,
)
from .relation_policy import (
    collapse_relations_by_identity,
    merge_relation_evidence,
    merge_relation_verification,
    relation_identity,
)
from .store import SHA256_RE, SnapshotStore

EVIDENCE_PATH = "normalized/evidence.jsonl"
RELATIONS_PATH = "normalized/relations.jsonl"
PREPARE_REPORT_PATH = "analysis/prepare-report.json"
MAX_EVIDENCE_BLOCKS = 200
MAX_BLOCK_CHARS = 4000
PREPARE_SOURCE_KINDS = frozenset(
    {
        "pull_request_body",
        "pull_request_comment",
        "pull_request_review",
        "pull_request_review_comment",
        "issue_body",
        "issue_comment",
        "discussion_body",
        "discussion_comment",
        "commit_message",
        "release_body",
        "doc_content",
    }
)

_NUMBER_KINDS = ("issue", "pull_request", "discussion")


@dataclass(frozen=True)
class Block:
    """An exact ``char_range`` slice of one normalized text source."""

    start: int
    end: int
    line_start: int
    line_end: int
    text: str


@dataclass(frozen=True)
class TextSource:
    """One authoritative text field ready for block slicing."""

    artifact: str
    document_key: str
    json_pointer: str
    entity_id: str
    source_kind: str
    source_url: str
    observed_at: str
    source_event_at: str | None
    source_revision: str | None
    blob_sha256: str
    text: str


@dataclass
class NormalizeIndex:
    """Lookup tables over normalized entities for reference resolution."""

    entities: tuple[dict[str, Any], ...]
    by_id: dict[str, dict[str, Any]]
    by_kind_number: dict[tuple[str, int], dict[str, Any]]
    by_sha: dict[str, dict[str, Any]]
    by_revision_path: dict[tuple[str, str], dict[str, Any]]
    by_release_tag: dict[str, dict[str, Any]]

    @classmethod
    def build(cls, entities: Iterable[Mapping[str, Any]]) -> NormalizeIndex:
        materialized: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        by_kind_number: dict[tuple[str, int], dict[str, Any]] = {}
        by_sha: dict[str, dict[str, Any]] = {}
        by_revision_path: dict[tuple[str, str], dict[str, Any]] = {}
        by_release_tag: dict[str, dict[str, Any]] = {}
        for raw in entities:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
                continue
            entity = dict(raw)
            materialized.append(entity)
            by_id[entity["id"]] = entity
            if entity.get("external"):
                continue
            kind = entity.get("kind")
            number = entity.get("number")
            if (
                kind in _NUMBER_KINDS
                and isinstance(number, int)
                and not isinstance(number, bool)
                and number >= 1
            ):
                by_kind_number[(kind, number)] = entity
            if kind == "commit":
                sha = _entity_sha(entity)
                if sha is not None:
                    by_sha[sha] = entity
            elif kind in ("doc", "source_file"):
                key = _entity_revision_path(entity)
                if key is not None:
                    by_revision_path[key] = entity
            elif kind == "release":
                tag = _entity_release_tag(entity)
                if tag is not None:
                    by_release_tag[tag] = entity
        return cls(
            entities=tuple(materialized),
            by_id=by_id,
            by_kind_number=by_kind_number,
            by_sha=by_sha,
            by_revision_path=by_revision_path,
            by_release_tag=by_release_tag,
        )

    def get(self, identifier: str) -> dict[str, Any] | None:
        return self.by_id.get(identifier)

    def number(self, kind: str, number: int) -> dict[str, Any] | None:
        return self.by_kind_number.get((kind, number))

    def resolve_number(
        self,
        number: int,
        kinds: Sequence[str] = _NUMBER_KINDS,
    ) -> dict[str, Any] | None:
        for kind in kinds:
            entity = self.by_kind_number.get((kind, number))
            if entity is not None:
                return entity
        return None

    def resolve_sha(self, value: str) -> tuple[dict[str, Any] | None, str]:
        normalized = value.strip().lower()
        if len(normalized) == 40:
            entity = self.by_sha.get(normalized)
            return (entity, "unique" if entity is not None else "missing")
        matches = sorted(
            sha for sha in self.by_sha if sha.startswith(normalized)
        )
        if len(matches) == 1:
            return self.by_sha[matches[0]], "unique"
        if matches:
            return None, "ambiguous"
        return None, "missing"

    def resolve_revision_path(
        self,
        revision: str,
        path: str,
    ) -> dict[str, Any] | None:
        return self.by_revision_path.get((revision, path))

    def resolve_release(self, tag: str) -> dict[str, Any] | None:
        return self.by_release_tag.get(tag)


def _entity_sha(entity: Mapping[str, Any]) -> str | None:
    raw_state = entity.get("raw_state")
    if isinstance(raw_state, Mapping):
        value = raw_state.get("sha")
        if isinstance(value, str) and FULL_SHA_RE.fullmatch(value):
            return value
    identifier = str(entity.get("id") or "")
    tail = identifier.rsplit("/", 1)[-1]
    return tail if FULL_SHA_RE.fullmatch(tail) else None


def _entity_revision_path(entity: Mapping[str, Any]) -> tuple[str, str] | None:
    for key in ("raw_state", "normalized_state"):
        state = entity.get(key)
        if not isinstance(state, Mapping):
            continue
        revision = state.get("revision")
        path = state.get("path")
        if (
            isinstance(revision, str)
            and FULL_SHA_RE.fullmatch(revision)
            and isinstance(path, str)
            and path
        ):
            return revision, path
    return None


def _entity_release_tag(entity: Mapping[str, Any]) -> str | None:
    raw_state = entity.get("raw_state")
    if isinstance(raw_state, Mapping):
        value = raw_state.get("tag_name")
        if isinstance(value, str) and value:
            return value
    return None


def split_blocks(
    text: str,
    *,
    max_chars: int | None = None,
) -> list[Block]:
    """Split text on blank-line paragraphs, then by lines at ``max_chars``."""
    limit = MAX_BLOCK_CHARS if max_chars is None else max_chars
    normalized = normalize_newlines(text)
    if not normalized.strip():
        return []
    paragraphs: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    offset = 0
    for line in normalized.splitlines(keepends=True):
        start = offset
        end = offset + len(line)
        offset = end
        if line.strip():
            current.append((start, end))
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)

    blocks: list[Block] = []
    for lines in paragraphs:
        for start, end in _split_paragraph(lines, max_chars=limit):
            value = normalized[start:end]
            if not value.strip():
                continue
            line_start, line_end = _line_numbers(normalized, start, end)
            blocks.append(
                Block(
                    start=start,
                    end=end,
                    line_start=line_start,
                    line_end=line_end,
                    text=value,
                )
            )
    return blocks


def _split_paragraph(
    lines: list[tuple[int, int]],
    *,
    max_chars: int,
) -> Iterator[tuple[int, int]]:
    max_chars = max(1, max_chars)
    chunk_start: int | None = None
    chunk_end: int | None = None
    for start, end in lines:
        if end - start > max_chars:
            if chunk_start is not None and chunk_end is not None:
                yield chunk_start, chunk_end
                chunk_start = None
                chunk_end = None
            position = start
            while end - position > max_chars:
                yield position, position + max_chars
                position += max_chars
            chunk_start = position
            chunk_end = end
            continue
        if chunk_start is None:
            chunk_start = start
            chunk_end = end
            continue
        if end - chunk_start > max_chars:
            yield chunk_start, chunk_end
            chunk_start = start
            chunk_end = end
        else:
            chunk_end = end
    if chunk_start is not None and chunk_end is not None:
        yield chunk_start, chunk_end


def _line_numbers(text: str, start: int, end: int) -> tuple[int, int]:
    line_start = text.count("\n", 0, start) + 1
    last = end - 1
    while last >= start and text[last] == "\n":
        last -= 1
    if last < start:
        return line_start, line_start
    return line_start, text.count("\n", 0, last) + 1


def evidence_id(source: TextSource, block: Block) -> str:
    material = (
        f"{source.source_kind}|{source.entity_id}|{source.source_url}|"
        f"{source.artifact}|{source.blob_sha256}|{block.start}|{block.end}"
    )
    return f"ev-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def build_evidence(source: TextSource, blocks: Iterable[Block]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block in blocks:
        records.append(
            make_evidence(
                evidence_id=evidence_id(source, block),
                entity_id=source.entity_id,
                source_url=source.source_url,
                source_kind=source.source_kind,
                observed_at=source.observed_at,
                source_event_at=source.source_event_at,
                source_revision=source.source_revision,
                blob_sha256=source.blob_sha256,
                locator=make_locator(
                    "char_range",
                    char_start=block.start,
                    char_end=block.end,
                    line_start=block.line_start,
                    line_end=block.line_end,
                ),
                text=block.text,
                completeness="complete",
                verification="unverifiable",
            )
        )
    return records


def write_evidence(
    store: SnapshotStore,
    records: Iterable[Mapping[str, Any]],
    *,
    contexts: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Verify every quote once, then replace only prepare-owned evidence."""
    payloads: dict[str, bytes] = {}
    verified: list[dict[str, Any]] = []
    context_map = contexts or {}
    for raw in sorted(records, key=lambda item: str(item.get("id"))):
        record = dict(raw)
        digest = str(record["blob_sha256"])
        context = context_map.get(str(record.get("id")), {})
        payload = payloads.get(digest)
        if payload is None:
            try:
                payload = store.read_blob(digest)
            except StoreError as exc:
                raise StoreError(
                    f"无法读取 evidence blob：{record['source_url']}",
                    source="prepare.evidence_blob",
                    details={
                        "artifact": context.get("artifact"),
                        "json_pointer": context.get("json_pointer"),
                        "evidence_id": record.get("id"),
                        "blob_sha256": digest,
                    },
                    recovery="重新 collect 该来源，或保留快照并记录证据缺口",
                ) from exc
            payloads[digest] = payload
        matched, reason = verify_evidence_quote(record, payload)
        if not matched and reason != "unverifiable_locator":
            raise StoreError(
                f"evidence 引文与定位内容不一致：{reason}",
                source="prepare.evidence_quote",
                details={
                    "artifact": context.get("artifact"),
                    "json_pointer": context.get("json_pointer"),
                    "evidence_id": record.get("id"),
                    "source_url": record.get("source_url"),
                    "reason": reason,
                },
                recovery="重新 prepare；若仍失败，检查源文档与 blob 是否一致",
            )
        record["verification"] = "verified" if matched else "unverifiable"
        validate_document(record, "evidence")
        verified.append(record)

    existing = store.read_jsonl(EVIDENCE_PATH, repair=True)
    combined: dict[str, dict[str, Any]] = {
        str(item["id"]): dict(item)
        for item in existing
        if item.get("source_kind") not in PREPARE_SOURCE_KINDS
    }
    for record in verified:
        identifier = str(record["id"])
        if identifier in combined:
            raise StoreError(
                "prepare evidence ID 与导入证据冲突",
                source="prepare.evidence_id",
                details={"evidence_id": identifier},
            )
        combined[identifier] = record
    store.write_jsonl(
        EVIDENCE_PATH,
        [combined[key] for key in sorted(combined)],
        key="id",
        on_conflict="replace",
        schema_name="evidence",
    )
    return verified


def build_evidence_set(
    sources: Iterable[TextSource],
    *,
    max_blocks: int | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[TextSource, Block, str]], list[dict[str, Any]]]:
    """Index saved text completely; callers may request an explicit sampling cap."""
    limit = max_blocks
    records: dict[str, dict[str, Any]] = {}
    pairs: list[tuple[TextSource, Block, str]] = []
    kept: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    artifacts: dict[str, str] = {}
    for source in sources:
        blocks = split_blocks(source.text)
        totals[source.document_key] += len(blocks)
        artifacts.setdefault(source.document_key, source.artifact)
        remaining = None if limit is None else max(0, limit - kept[source.document_key])
        accepted = blocks if remaining is None else blocks[:remaining]
        kept[source.document_key] += len(accepted)
        for block in accepted:
            identifier = evidence_id(source, block)
            if identifier in records:
                raise StoreError(
                    "evidence ID 冲突，来源标识不足以区分文本块",
                    source="prepare.evidence_id",
                    details={
                        "evidence_id": identifier,
                        "artifact": source.artifact,
                        "json_pointer": source.json_pointer,
                        "source_url": source.source_url,
                    },
                    recovery="检查 source_url 是否唯一，或扩充 evidence ID 的来源标识",
                )
            records[identifier] = build_evidence(source, [block])[0]
            pairs.append((source, block, identifier))
    truncated = [
        {
            "artifact": artifacts[key],
            "document_key": key,
            "blocks": totals[key],
            "kept": kept[key],
            "problem": "evidence_truncated",
        }
        for key in sorted(totals)
        if limit is not None and totals[key] > limit
    ]
    return list(records.values()), pairs, truncated


def project_commits(
    store: SnapshotStore,
    documents: Sequence[Mapping[str, Any]],
    *,
    host: str,
    owner: str,
    name: str,
) -> list[dict[str, Any]]:
    """Project PR commit items into commit entities and return link facts."""
    incoming: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    for document in documents:
        number = int(document["number"])
        pull_id = entity_id(host, owner, name, "pull_request", str(number))
        artifact = pr_detail_path(number)
        commits = document.get("commits")
        items = commits.get("items") if isinstance(commits, Mapping) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            sha = item.get("sha")
            if not isinstance(sha, str) or not FULL_SHA_RE.fullmatch(sha):
                continue
            commit_id = entity_id(host, owner, name, "commit", sha)
            message = item.get("message")
            title = _commit_title(message, sha)
            date = item.get("date")
            entity = make_entity(
                entity_id=commit_id,
                kind="commit",
                title=title,
                url=f"https://{host}/{owner}/{name}/commit/{sha}",
                observed_at=str(document["observed_at"]),
                raw_state={
                    "sha": sha,
                    "pr_number": number,
                    "author": item.get("author"),
                    "date": date,
                    "detail_path": artifact,
                },
                normalized_state={"sha": sha, "short_sha": sha[:7]},
                created_at=date if isinstance(date, str) else None,
                updated_at=date if isinstance(date, str) else None,
                source_event_at=date if isinstance(date, str) else None,
                discovery_reasons=["pr_commit"],
                external=False,
            )
            incoming.setdefault(commit_id, entity)
            links.append(
                {
                    "from_id": pull_id,
                    "to_id": commit_id,
                    "artifact": artifact,
                    "observed_at": str(document["observed_at"]),
                }
            )
    if incoming:
        _upsert_entities(store, incoming.values())
    return links


def _commit_title(message: Any, sha: str) -> str:
    if isinstance(message, Mapping):
        preview = message.get("preview")
        if isinstance(preview, str):
            first = preview.splitlines()[0].strip() if preview.splitlines() else ""
            if first:
                return first[:200]
    return sha[:7]


def _upsert_entities(
    store: SnapshotStore,
    entities: Iterable[Mapping[str, Any]],
) -> None:
    existing = {
        entity["id"]: entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if isinstance(entity, Mapping) and isinstance(entity.get("id"), str)
    }
    merged: list[dict[str, Any]] = []
    for incoming in sorted(entities, key=lambda item: str(item["id"])):
        previous = existing.get(str(incoming["id"]))
        entity = merge_entity(previous, incoming) if previous is not None else dict(incoming)
        validate_document(entity, "entity")
        merged.append(entity)
    if merged:
        store.upsert_jsonl(
            "normalized/entities.jsonl",
            merged,
            key="id",
            schema_name="entity",
        )


def _prepare_owned_relation(record: Mapping[str, Any]) -> bool:
    scope = record.get("scope")
    if not isinstance(scope, Mapping):
        return False
    return scope.get("generator") == "prepare"


def _merge_relation_into(
    existing: dict[str, Any],
    incoming: Mapping[str, Any],
) -> None:
    existing["evidence_ids"] = merge_relation_evidence(
        existing.get("evidence_ids") or [],
        incoming.get("evidence_ids") or [],
    )
    existing["verification"] = merge_relation_verification(
        str(existing.get("verification") or "candidate"),
        str(incoming.get("verification") or "candidate"),
    )


def _write_relations(
    store: SnapshotStore,
    relations: Iterable[Mapping[str, Any]],
) -> tuple[int, int]:
    existing = store.read_jsonl(RELATIONS_PATH, repair=True)
    combined: dict[str, dict[str, Any]] = {
        str(item["id"]): dict(item)
        for item in existing
        if not _prepare_owned_relation(item)
    }
    combined = {
        str(item["id"]): item
        for item in collapse_relations_by_identity(
            combined.values(),
            merge_scope=True,
        )
    }
    by_identity: dict[tuple[str, str, str], str] = {}
    for identifier, relation in combined.items():
        by_identity.setdefault(relation_identity(relation), identifier)
    merged = 0
    for relation in relations:
        identifier = str(relation["id"])
        identity = relation_identity(relation)
        prior_id = identifier if identifier in combined else by_identity.get(identity)
        if prior_id is None:
            combined[identifier] = dict(relation)
            by_identity[identity] = identifier
            continue
        prior = combined[prior_id]
        if relation_identity(prior) != identity:
            raise StoreError(
                "prepare relation ID 与导入关系冲突",
                source="prepare.relation_id",
                details={
                    "relation_id": identifier,
                    "existing": list(relation_identity(prior)),
                    "derived": list(identity),
                },
            )
        _merge_relation_into(prior, relation)
        merged += 1
    store.write_jsonl(
        RELATIONS_PATH,
        [combined[key] for key in sorted(combined)],
        key="id",
        on_conflict="replace",
        schema_name="relation",
    )
    return len(combined), merged


def _prune_prepare_entities(
    store: SnapshotStore,
    *,
    commit_ids: set[str],
    external_ids: set[str],
    protected_ids: set[str],
) -> None:
    records = store.read_jsonl("normalized/entities.jsonl")
    kept: list[dict[str, Any]] = []
    changed = False
    for record in records:
        if not isinstance(record, Mapping):
            continue
        reasons = set(record.get("discovery_reasons") or [])
        identifier = str(record.get("id"))
        if identifier in protected_ids:
            kept.append(dict(record))
            continue
        if (
            record.get("kind") == "commit"
            and "pr_commit" in reasons
            and identifier not in commit_ids
        ):
            changed = True
            continue
        if (
            record.get("external") is True
            and "external_reference" in reasons
            and identifier not in external_ids
        ):
            changed = True
            continue
        kept.append(dict(record))
    if changed:
        store.write_jsonl(
            "normalized/entities.jsonl",
            kept,
            key="id",
            on_conflict="replace",
            schema_name="entity",
        )


def _validate_references(
    evidence: Iterable[Mapping[str, Any]],
    relations: Iterable[Mapping[str, Any]],
    known_ids: set[str],
) -> None:
    problems: list[dict[str, Any]] = []
    for item in evidence:
        identifier = item.get("entity_id")
        if identifier not in known_ids:
            problems.append(
                {
                    "artifact": "normalized/evidence.jsonl",
                    "evidence_id": item.get("id"),
                    "entity_id": identifier,
                    "problem": "missing_evidence_entity",
                }
            )
    for relation in relations:
        for field in ("from_id", "to_id"):
            if relation.get(field) not in known_ids:
                problems.append(
                    {
                        "artifact": "normalized/relations.jsonl",
                        "relation_id": relation.get("id"),
                        "field": field,
                        "entity_id": relation.get(field),
                        "problem": "missing_relation_endpoint",
                    }
                )
    if problems:
        raise ValidationFailure(
            "prepare 产物存在悬挂引用",
            source="prepare.references",
            details={"problems": problems[:20], "count": len(problems)},
            recovery="修复 collect 实体投影，或先在 partial run 上重新 collect",
        )


def prepare_run(
    store: SnapshotStore,
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive commits, evidence and explicit relations without network access."""
    from .relations import RelationBuilder, mask_markdown

    repository = config["repository"]
    host = str(repository["host"])
    owner = str(repository["owner"])
    name = str(repository["name"])
    pr_documents = load_pr_documents(store)
    commit_links = project_commits(
        store,
        pr_documents,
        host=host,
        owner=owner,
        name=name,
    )
    index = NormalizeIndex.build(store.read_jsonl("normalized/entities.jsonl"))
    sources = list(
        iter_text_sources(
            store,
            host=host,
            owner=owner,
            name=name,
            entity_index=index,
        )
    )
    evidence, pairs, truncated = build_evidence_set(sources)
    builder = RelationBuilder(
        index,
        host=host,
        owner=owner,
        name=name,
        max_external_entities=None,  # Offline reference metadata costs no GitHub requests.
    )
    for link in commit_links:
        builder.add_contains_commit(
            link["from_id"],
            link["to_id"],
            artifact=str(link["artifact"]),
            observed_at=str(link["observed_at"]),
        )
    masks: dict[tuple[str, str, str], Any] = {}
    contexts: dict[str, dict[str, str]] = {}
    for source, block, identifier in pairs:
        key = (source.artifact, source.json_pointer, source.blob_sha256)
        mask = masks.get(key)
        if mask is None:
            mask = mask_markdown(source.text)
            masks[key] = mask
        builder.add_text(source, block, identifier, mask=mask)
        contexts[identifier] = {
            "artifact": source.artifact,
            "json_pointer": source.json_pointer,
        }
    relations, external_entities, counts = builder.result()
    _upsert_entities(store, external_entities.values())
    commit_ids = {str(link["to_id"]) for link in commit_links}
    protected_ids = {
        str(item.get("entity_id"))
        for item in store.read_jsonl(EVIDENCE_PATH, repair=True)
        if item.get("source_kind") not in PREPARE_SOURCE_KINDS
    }
    for item in store.read_jsonl(RELATIONS_PATH, repair=True):
        if _prepare_owned_relation(item):
            continue
        protected_ids.add(str(item.get("from_id")))
        protected_ids.add(str(item.get("to_id")))
    _prune_prepare_entities(
        store,
        commit_ids=commit_ids,
        external_ids=set(external_entities),
        protected_ids=protected_ids,
    )
    known_ids = {
        str(entity["id"])
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if isinstance(entity, Mapping) and isinstance(entity.get("id"), str)
    }
    _validate_references(evidence, relations, known_ids)
    verified = write_evidence(store, evidence, contexts=contexts)
    relations_total, relations_merged = _write_relations(store, relations)
    evidence_total = len(store.read_jsonl(EVIDENCE_PATH))
    from . import packs, ranking
    from .selection_protocol import load_agent_selection

    agent_selection = load_agent_selection(store, manifest)
    candidates_document = ranking.build_candidates(
        store,
        config,
        manifest,
        evidence=verified,
        relations=relations,
        agent_selection=agent_selection,
    )
    ranking.write_candidates(store, candidates_document)
    packs.write_pack_index(
        store,
        packs.build_pack_index(
            store,
            config,
            manifest,
            candidates_document,
            research_handoff=agent_selection is not None,
        ),
    )
    observed_at = str(
        manifest.get("collection_finished_at")
        or manifest.get("observed", {}).get("finished_at")
        or manifest.get("created_at")
        or ""
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "repository": {"host": host, "owner": owner, "name": name},
        "text_sources": len(sources),
        "evidence_blocks": len(verified),
        "evidence_derived": len(verified),
        "evidence_total": evidence_total,
        "relations": len(relations),
        "relations_derived": len(relations),
        "relations_total": relations_total,
        "relations_merged": relations_merged,
        "relation_counts": dict(sorted(Counter(item["type"] for item in relations).items())),
        "commit_entities": len(commit_ids),
        "external_entities": len(external_entities),
        "evidence_truncated": truncated,
        "counts": dict(sorted(counts.items())),
    }
    store.write_json(
        PREPARE_REPORT_PATH,
        report,
        schema_name="prepare_report",
    )
    return report


def _read_text_ref(
    store: SnapshotStore,
    ref: Any,
    *,
    artifact: str,
    json_pointer: str,
) -> tuple[str, str] | None:
    if not isinstance(ref, Mapping):
        return None
    digest = ref.get("sha256")
    if digest is None:
        return None
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise StoreError(
            f"文本引用 blob hash 非法：{artifact}{json_pointer}",
            source="prepare.text_ref",
            details={"artifact": artifact, "json_pointer": json_pointer},
        )
    try:
        payload = store.read_blob(digest)
    except StoreError as exc:
        raise StoreError(
            f"缺少文本 blob：{artifact}{json_pointer}",
            source="prepare.text_blob",
            details={
                "artifact": artifact,
                "json_pointer": json_pointer,
                "blob_sha256": digest,
            },
            recovery="重新 collect 该来源，或保留快照并记录证据缺口",
        ) from exc
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StoreError(
            f"文本 blob 不是 UTF-8：{artifact}{json_pointer}",
            source="prepare.text_blob",
            details={
                "artifact": artifact,
                "json_pointer": json_pointer,
                "blob_sha256": digest,
            },
        ) from exc
    return digest, text


def _source(
    store: SnapshotStore,
    ref: Any,
    *,
    artifact: str,
    document_key: str,
    json_pointer: str,
    entity_id_value: str,
    source_kind: str,
    source_url: str,
    observed_at: str,
    source_event_at: str | None,
    source_revision: str | None = None,
) -> TextSource | None:
    resolved = _read_text_ref(
        store,
        ref,
        artifact=artifact,
        json_pointer=json_pointer,
    )
    if resolved is None:
        return None
    digest, text = resolved
    if not text.strip():
        return None
    return TextSource(
        artifact=artifact,
        document_key=document_key,
        json_pointer=json_pointer,
        entity_id=entity_id_value,
        source_kind=source_kind,
        source_url=source_url,
        observed_at=observed_at,
        source_event_at=source_event_at,
        source_revision=source_revision,
        blob_sha256=digest,
        text=text,
    )


def iter_text_sources(
    store: SnapshotStore,
    *,
    host: str,
    owner: str,
    name: str,
    entity_index: NormalizeIndex | None = None,
) -> Iterator[TextSource]:
    """Yield all supported text fields in deterministic document order."""
    for document in load_pr_documents(store):
        number = int(document["number"])
        artifact = pr_detail_path(number)
        pull_id = entity_id(host, owner, name, "pull_request", str(number))
        url = str(document["url"])
        observed_at = str(document["observed_at"])
        event_at = document.get("updated_at") or document.get("created_at")
        source = _source(
            store,
            document.get("body"),
            artifact=artifact,
            document_key=artifact,
            json_pointer="/body",
            entity_id_value=pull_id,
            source_kind="pull_request_body",
            source_url=url,
            observed_at=observed_at,
            source_event_at=event_at,
        )
        if source is not None:
            yield source
        comments = document.get("issue_comments")
        for index, item in enumerate(_items(comments)):
            identifier = item.get("id")
            source = _source(
                store,
                item.get("body"),
                artifact=artifact,
                document_key=artifact,
                json_pointer=f"/issue_comments/items/{index}/body",
                entity_id_value=pull_id,
                source_kind="pull_request_comment",
                source_url=f"{url}#issuecomment-{identifier}",
                observed_at=observed_at,
                source_event_at=item.get("updated_at") or item.get("created_at"),
            )
            if source is not None:
                yield source
        reviews = document.get("reviews")
        for index, item in enumerate(_items(reviews)):
            identifier = item.get("id")
            source = _source(
                store,
                item.get("body"),
                artifact=artifact,
                document_key=artifact,
                json_pointer=f"/reviews/items/{index}/body",
                entity_id_value=pull_id,
                source_kind="pull_request_review",
                source_url=f"{url}#pullrequestreview-{identifier}",
                observed_at=observed_at,
                source_event_at=item.get("submitted_at"),
            )
            if source is not None:
                yield source
        review_comments = document.get("review_comments")
        for index, item in enumerate(_items(review_comments)):
            identifier = item.get("id")
            source = _source(
                store,
                item.get("body"),
                artifact=artifact,
                document_key=artifact,
                json_pointer=f"/review_comments/items/{index}/body",
                entity_id_value=pull_id,
                source_kind="pull_request_review_comment",
                source_url=f"{url}#discussion_r{identifier}",
                observed_at=observed_at,
                source_event_at=item.get("updated_at") or item.get("created_at"),
            )
            if source is not None:
                yield source
        seen_commits: set[str] = set()
        for index, item in enumerate(_items(document.get("commits"))):
            sha = item.get("sha")
            if not isinstance(sha, str) or not FULL_SHA_RE.fullmatch(sha):
                continue
            if sha in seen_commits:
                continue
            seen_commits.add(sha)
            source = _source(
                store,
                item.get("message"),
                artifact=artifact,
                document_key=f"commit/{sha}",
                json_pointer=f"/commits/items/{index}/message",
                entity_id_value=entity_id(host, owner, name, "commit", sha),
                source_kind="commit_message",
                source_url=f"https://{host}/{owner}/{name}/commit/{sha}",
                observed_at=observed_at,
                source_event_at=item.get("date"),
                source_revision=sha,
            )
            if source is not None:
                yield source

    for document in load_issue_documents(store):
        number = int(document["number"])
        artifact = f"raw/issues/{number}.json"
        issue_id = entity_id(host, owner, name, "issue", str(number))
        url = str(document["url"])
        observed_at = str(document["observed_at"])
        source = _source(
            store,
            document.get("body"),
            artifact=artifact,
            document_key=artifact,
            json_pointer="/body",
            entity_id_value=issue_id,
            source_kind="issue_body",
            source_url=url,
            observed_at=observed_at,
            source_event_at=document.get("updated_at") or document.get("created_at"),
        )
        if source is not None:
            yield source
        comments = document.get("comments")
        for index, item in enumerate(_items(comments)):
            identifier = item.get("id")
            source = _source(
                store,
                item.get("body"),
                artifact=artifact,
                document_key=artifact,
                json_pointer=f"/comments/items/{index}/body",
                entity_id_value=issue_id,
                source_kind="issue_comment",
                source_url=f"{url}#issuecomment-{identifier}",
                observed_at=observed_at,
                source_event_at=item.get("updated_at") or item.get("created_at"),
            )
            if source is not None:
                yield source

    for document in load_discussion_documents(store):
        number = int(document["number"])
        artifact = f"raw/discussions/{number}.json"
        discussion_id = entity_id(host, owner, name, "discussion", str(number))
        url = str(document["url"])
        observed_at = str(document["observed_at"])
        source = _source(
            store,
            document.get("body"),
            artifact=artifact,
            document_key=artifact,
            json_pointer="/body",
            entity_id_value=discussion_id,
            source_kind="discussion_body",
            source_url=url,
            observed_at=observed_at,
            source_event_at=document.get("updated_at") or document.get("created_at"),
        )
        if source is not None:
            yield source
        comments = document.get("comments")
        for index, item in enumerate(_items(comments)):
            identifier = item.get("database_id") or item.get("id")
            source = _source(
                store,
                item.get("body"),
                artifact=artifact,
                document_key=artifact,
                json_pointer=f"/comments/items/{index}/body",
                entity_id_value=discussion_id,
                source_kind="discussion_comment",
                source_url=f"{url}#discussioncomment-{identifier}",
                observed_at=observed_at,
                source_event_at=item.get("updated_at") or item.get("created_at"),
            )
            if source is not None:
                yield source
            for reply_index, reply in enumerate(item.get("replies") or []):
                if not isinstance(reply, Mapping):
                    continue
                reply_id = reply.get("database_id") or reply.get("id")
                source = _source(
                    store,
                    reply.get("body"),
                    artifact=artifact,
                    document_key=artifact,
                    json_pointer=(
                        f"/comments/items/{index}/replies/{reply_index}/body"
                    ),
                    entity_id_value=discussion_id,
                    source_kind="discussion_comment",
                    source_url=f"{url}#discussioncomment-{reply_id}",
                    observed_at=observed_at,
                    source_event_at=reply.get("updated_at") or reply.get("created_at"),
                )
                if source is not None:
                    yield source

    for document in load_release_documents(store):
        release_id = int(document["release_id"])
        artifact = release_path(release_id)
        source = _source(
            store,
            document.get("body"),
            artifact=artifact,
            document_key=artifact,
            json_pointer="/body",
            entity_id_value=entity_id(
                host,
                owner,
                name,
                "release",
                str(document["tag_name"]),
            ),
            source_kind="release_body",
            source_url=str(document["url"]),
            observed_at=str(document["observed_at"]),
            source_event_at=document.get("published_at") or document.get("created_at"),
        )
        if source is not None:
            yield source

    for document in load_doc_snapshots(store):
        if document.get("status") != "read":
            continue
        identifier = entity_id(
            host,
            owner,
            name,
            "doc",
            str(document["path"]),
            revision=str(document["revision"]),
        )
        if entity_index is not None and entity_index.get(identifier) is None:
            continue
        artifact = doc_path(str(document["revision"]), str(document["path"]))
        source = _source(
            store,
            document.get("text"),
            artifact=artifact,
            document_key=artifact,
            json_pointer="/text",
            entity_id_value=identifier,
            source_kind="doc_content",
            source_url=str(document["url"]),
            observed_at=str(document["observed_at"]),
            source_event_at=None,
            source_revision=str(document["revision"]),
        )
        if source is not None:
            yield source


def _items(section: Any) -> list[Mapping[str, Any]]:
    if not isinstance(section, Mapping):
        return []
    items = section.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


__all__ = [
    "Block",
    "EVIDENCE_PATH",
    "MAX_BLOCK_CHARS",
    "MAX_EVIDENCE_BLOCKS",
    "NormalizeIndex",
    "PREPARE_REPORT_PATH",
    "RELATIONS_PATH",
    "TextSource",
    "build_evidence",
    "build_evidence_set",
    "evidence_id",
    "iter_text_sources",
    "prepare_run",
    "project_commits",
    "split_blocks",
    "write_evidence",
]
