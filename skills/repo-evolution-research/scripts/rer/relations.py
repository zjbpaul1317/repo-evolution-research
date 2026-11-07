"""Explicit reference extraction, relation dedupe and a bounded graph index."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from .models import (
    entity_id,
    make_entity,
    make_relation,
    normalize_newlines,
)
from .normalize import Block, NormalizeIndex, TextSource
from .relation_policy import merge_relation_evidence, merge_relation_verification

_NUMBER_KINDS = ("issue", "pull_request", "discussion")
_CLOSING_KINDS = frozenset(
    {"pull_request_body", "pull_request_comment", "commit_message"}
)
_CLOSING_TARGET_KINDS = ("issue", "pull_request")
_CLOSING_RE = re.compile(
    r"\b(?P<keyword>fix(?:es|s)?|closes?|resolves?)\b",
    re.IGNORECASE,
)
_OBJECT_URL_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)"
    r"/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(?P<kind>pull|issues|discussions)/(?P<number>\d{1,9})",
    re.IGNORECASE,
)
_RELEASE_URL_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)"
    r"/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/releases/tag/(?P<tag>[^\s<>()\[\]{}#?]+)",
    re.IGNORECASE,
)
_BLOB_URL_RE = re.compile(
    r"https?://(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)"
    r"/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/blob/(?P<revision>[0-9a-fA-F]{40})/(?P<path>[^\s<>()\[\]{}#?]+)",
    re.IGNORECASE,
)
_REPO_REF_RE = re.compile(
    r"(?<![\w/])(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"/(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"#(?P<number>\d{1,9})\b"
)
_BARE_REF_RE = re.compile(r"(?<![\w/#])#(?P<number>\d{1,9})\b")
_SHA_RE = re.compile(
    r"(?<![0-9A-Za-z])(?P<sha>[0-9a-fA-F]{7,40})(?![0-9A-Za-z])"
)
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})[ \t]*$")
_QUOTE_RE = re.compile(r"^[ \t]*>")
_TRAILING_PUNCTUATION = ".,;:!?"


def _normalize_host(host: str) -> str:
    normalized = host.strip().casefold()
    return normalized.removeprefix("www.")
_COUNT_KEYS = (
    "ambiguous_external_refs",
    "ambiguous_short_sha",
    "external_entities",
    "external_entity_limit",
    "ignored_code_refs",
    "ignored_quote_refs",
    "invalid_closing_targets",
    "invalid_external_refs",
    "self_refs",
    "unresolved_refs",
)


@dataclass(frozen=True)
class Reference:
    kind: str
    start: int
    end: int
    raw: str
    host: str | None = None
    owner: str | None = None
    name: str | None = None
    number: int | None = None
    revision: str | None = None
    path: str | None = None
    tag: str | None = None
    sha: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class MarkdownMask:
    masked: str
    code: str
    quoted: str


@dataclass
class RelationBuilder:
    """Accumulates text relations plus API-explicit commit containment."""

    index: NormalizeIndex
    host: str
    owner: str
    name: str
    max_external_entities: int | None
    counts: Counter[str] = field(default_factory=Counter)
    _relations: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    _external: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_contains_commit(
        self,
        from_id: str,
        to_id: str,
        *,
        artifact: str,
        observed_at: str,
    ) -> None:
        relation = make_relation(
            relation_id=_relation_id(from_id, to_id, "contains_commit"),
            from_id=from_id,
            to_id=to_id,
            relation_type="contains_commit",
            basis="api_explicit",
            verification="verified",
            evidence_ids=[],
            scope={
                "artifact": artifact,
                "generator": "prepare",
                "section": "commits",
                "observed_at": observed_at,
            },
        )
        self._merge_relation(relation)

    def add_text(
        self,
        source: TextSource,
        block: Block,
        evidence_id: str,
        *,
        mask: MarkdownMask | None = None,
    ) -> None:
        if mask is None:
            mask = mask_markdown(block.text)
            masked, code, quoted = mask.masked, mask.code, mask.quoted
        else:
            start, end = block.start, block.end
            masked = mask.masked[start:end]
            code = mask.code[start:end]
            quoted = mask.quoted[start:end]
        self.counts["ignored_code_refs"] += len(_find_refs(code))
        self.counts["ignored_quote_refs"] += len(_find_refs(quoted))

        closing: list[Reference] = []
        closing_spans: list[tuple[int, int]] = []
        if source.source_kind in _CLOSING_KINDS:
            closing = _closing_refs(masked)
            closing_spans = [(item.start, item.end) for item in closing]
            for ref in closing:
                self._add_reference(
                    source,
                    block,
                    evidence_id,
                    ref,
                    relation_type="closing_reference",
                    verification="candidate",
                )
        allow_bare = source.source_kind != "doc_content"
        allow_sha = source.source_kind != "doc_content"
        relation_type = (
            "release_note_mentions"
            if source.source_kind == "release_body"
            else "mentions"
        )
        for ref in _find_refs(
            masked,
            allow_bare_numbers=allow_bare,
            allow_sha=allow_sha,
        ):
            if any(_overlaps(ref, start, end) for start, end in closing_spans):
                continue
            self._add_reference(
                source,
                block,
                evidence_id,
                ref,
                relation_type=relation_type,
                verification=None,
            )

    def _add_reference(
        self,
        source: TextSource,
        block: Block,
        evidence_id: str,
        ref: Reference,
        *,
        relation_type: str,
        verification: str | None,
    ) -> None:
        target: dict[str, Any] | None
        external = False
        if (
            relation_type == "closing_reference"
            and ref.kind not in ("number", "issue", "pull_request")
        ):
            self.counts["invalid_closing_targets"] += 1
            return
        if ref.kind == "number":
            if ref.owner is not None and ref.name is not None:
                if not self._is_local(ref.owner, ref.name, ref.host):
                    self.counts["ambiguous_external_refs"] += 1
                    return
            target = self.index.resolve_number(int(ref.number or 0))
            if target is None:
                self.counts["unresolved_refs"] += 1
                return
        elif ref.kind in _NUMBER_KINDS:
            if not self._is_local(str(ref.owner), str(ref.name), ref.host):
                target = self._external_entity(ref, source)
                external = True
            else:
                target = self.index.number(ref.kind, int(ref.number or 0))
                if target is None and ref.kind == "issue":
                    target = self.index.number("pull_request", int(ref.number or 0))
                if target is None:
                    self.counts["unresolved_refs"] += 1
                    return
        elif ref.kind == "release":
            if not self._is_local(str(ref.owner), str(ref.name), ref.host):
                target = self._external_entity(ref, source)
                external = True
            else:
                target = self.index.resolve_release(str(ref.tag))
                if target is None:
                    self.counts["unresolved_refs"] += 1
                    return
        elif ref.kind == "doc":
            if not self._is_local(str(ref.owner), str(ref.name), ref.host):
                target = self._external_entity(ref, source)
                external = True
            else:
                target = self.index.resolve_revision_path(
                    str(ref.revision),
                    str(ref.path),
                )
                if target is None:
                    self.counts["unresolved_refs"] += 1
                    return
        elif ref.kind == "commit":
            target, status = self.index.resolve_sha(str(ref.sha))
            if status == "ambiguous":
                self.counts["ambiguous_short_sha"] += 1
                return
            if target is None:
                self.counts["unresolved_refs"] += 1
                return
        else:
            self.counts["unresolved_refs"] += 1
            return
        if target is None:
            return
        if (
            relation_type == "closing_reference"
            and target.get("kind") not in _CLOSING_TARGET_KINDS
        ):
            self.counts["invalid_closing_targets"] += 1
            return
        target_id = str(target["id"])
        if target_id == source.entity_id:
            self.counts["self_refs"] += 1
            return
        if verification is None:
            verification = "candidate" if external else "verified"
        relation = make_relation(
            relation_id=_relation_id(source.entity_id, target_id, relation_type),
            from_id=source.entity_id,
            to_id=target_id,
            relation_type=relation_type,
            basis="text_explicit",
            verification=verification,
            evidence_ids=[evidence_id],
            scope={
                "artifact": source.artifact,
                "document_key": source.document_key,
                "json_pointer": source.json_pointer,
                "source_kind": source.source_kind,
                "generator": "prepare",
                "block": {"char_start": block.start, "char_end": block.end},
            },
        )
        self._merge_relation(relation)

    def _is_local(
        self,
        owner: str,
        name: str,
        host: str | None = None,
    ) -> bool:
        return (
            _normalize_host(host or self.host) == _normalize_host(self.host)
            and owner.casefold() == self.owner.casefold()
            and name.casefold() == self.name.casefold()
        )

    def _external_entity(
        self,
        ref: Reference,
        source: TextSource,
    ) -> dict[str, Any] | None:
        host = _normalize_host(ref.host or "github.com")
        owner = str(ref.owner or "").lower()
        name = str(ref.name or "").lower()
        try:
            if ref.kind == "release":
                identifier = entity_id(host, owner, name, "release", str(ref.tag))
                title = str(ref.tag)
            elif ref.kind == "doc":
                identifier = entity_id(
                    host,
                    owner,
                    name,
                    "doc",
                    str(ref.path),
                    revision=str(ref.revision),
                )
                title = str(ref.path)
            elif ref.kind in _NUMBER_KINDS:
                identifier = entity_id(
                    host,
                    owner,
                    name,
                    ref.kind,
                    str(ref.number),
                )
                title = f"#{ref.number}"
            else:
                return None
        except ValueError:
            self.counts["invalid_external_refs"] += 1
            return None
        if identifier in self._external:
            return self._external[identifier]
        if (self.max_external_entities is not None
                and len(self._external) >= max(0, self.max_external_entities)):
            self.counts["external_entity_limit"] += 1
            return None
        entity = make_entity(
            entity_id=identifier,
            kind=ref.kind,
            title=title,
            url=str(ref.url or f"https://{host}/{owner}/{name}"),
            observed_at=source.observed_at,
            number=ref.number if ref.kind in _NUMBER_KINDS else None,
            raw_state={
                "external_reference": True,
                "reference_url": ref.url,
            },
            normalized_state={"external_reference": True},
            discovery_reasons=["external_reference"],
            external=True,
        )
        self._external[identifier] = entity
        self.counts["external_entities"] += 1
        return entity

    def _merge_relation(self, incoming: dict[str, Any]) -> None:
        key = (incoming["from_id"], incoming["to_id"], incoming["type"])
        existing = self._relations.get(key)
        if existing is None:
            self._relations[key] = incoming
            return
        existing["evidence_ids"] = merge_relation_evidence(
            existing.get("evidence_ids") or [],
            incoming.get("evidence_ids") or [],
        )
        existing["verification"] = merge_relation_verification(
            str(existing.get("verification")),
            str(incoming.get("verification")),
        )

    def result(
        self,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, int]]:
        relations = sorted(self._relations.values(), key=lambda item: item["id"])
        external = dict(sorted(self._external.items()))
        counts = dict(self.counts)
        for key in _COUNT_KEYS:
            counts.setdefault(key, 0)
        return relations, external, dict(sorted(counts.items()))


def _relation_id(from_id: str, to_id: str, relation_type: str) -> str:
    material = f"{from_id}|{to_id}|{relation_type}"
    return f"rel-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _overlaps(ref: Reference, start: int, end: int) -> bool:
    return ref.start < end and start < ref.end


def _find_refs(
    text: str,
    *,
    allow_bare_numbers: bool = True,
    allow_sha: bool = True,
) -> list[Reference]:
    claimed: list[tuple[int, int]] = []
    found: list[Reference] = []

    def take(reference: Reference) -> None:
        if any(
            reference.start < end and start < reference.end
            for start, end in claimed
        ):
            return
        claimed.append((reference.start, reference.end))
        found.append(reference)

    for reference in _url_refs(text):
        take(reference)
    for reference in _repo_refs(text):
        take(reference)
    if allow_bare_numbers:
        for reference in _bare_refs(text):
            take(reference)
    if allow_sha:
        for reference in _sha_refs(text):
            take(reference)
    return sorted(found, key=lambda item: (item.start, item.end))


def _url_refs(text: str) -> list[Reference]:
    refs: list[Reference] = []
    for match in _OBJECT_URL_RE.finditer(text):
        number = int(match.group("number"))
        kind = {
            "pull": "pull_request",
            "issues": "issue",
            "discussions": "discussion",
        }[match.group("kind").lower()]
        refs.append(
            Reference(
                kind=kind,
                start=match.start(),
                end=match.end(),
                raw=match.group(0),
                host=_normalize_host(match.group("host")),
                owner=unquote(match.group("owner")),
                name=unquote(match.group("repo")),
                number=number,
                url=match.group(0),
            )
        )
    for match in _RELEASE_URL_RE.finditer(text):
        end = _trimmed_end(text, match.start("tag"), match.end("tag"))
        refs.append(
            Reference(
                kind="release",
                start=match.start(),
                end=end,
                raw=text[match.start() : end],
                host=_normalize_host(match.group("host")),
                owner=unquote(match.group("owner")),
                name=unquote(match.group("repo")),
                tag=unquote(text[match.start("tag") : end]),
                url=text[match.start() : end],
            )
        )
    for match in _BLOB_URL_RE.finditer(text):
        end = _trimmed_end(text, match.start("path"), match.end("path"))
        refs.append(
            Reference(
                kind="doc",
                start=match.start(),
                end=end,
                raw=text[match.start() : end],
                host=_normalize_host(match.group("host")),
                owner=unquote(match.group("owner")),
                name=unquote(match.group("repo")),
                revision=match.group("revision").lower(),
                path=unquote(text[match.start("path") : end]),
                url=text[match.start() : end],
            )
        )
    return refs


def _trimmed_end(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1] in _TRAILING_PUNCTUATION:
        end -= 1
    return end


def _repo_refs(text: str) -> list[Reference]:
    return [
        Reference(
            kind="number",
            start=match.start(),
            end=match.end(),
            raw=match.group(0),
            owner=match.group("owner"),
            name=match.group("repo"),
            number=int(match.group("number")),
        )
        for match in _REPO_REF_RE.finditer(text)
    ]


def _bare_refs(text: str) -> list[Reference]:
    return [
        Reference(
            kind="number",
            start=match.start(),
            end=match.end(),
            raw=match.group(0),
            number=int(match.group("number")),
        )
        for match in _BARE_REF_RE.finditer(text)
    ]


def _sha_refs(text: str) -> list[Reference]:
    return [
        Reference(
            kind="commit",
            start=match.start(),
            end=match.end(),
            raw=match.group(0),
            sha=match.group("sha").lower(),
        )
        for match in _SHA_RE.finditer(text)
    ]


def _closing_refs(text: str) -> list[Reference]:
    refs: list[Reference] = []
    for match in _CLOSING_RE.finditer(text):
        line_end = text.find("\n", match.end())
        if line_end < 0:
            line_end = len(text)
        position = match.end()
        while True:
            position = _skip_closing_separators(text, position, line_end)
            ref = _match_ref_at(text, position, line_end)
            if ref is None:
                break
            refs.append(ref)
            position = ref.end
    return sorted(refs, key=lambda item: (item.start, item.end))


def _skip_closing_separators(text: str, position: int, limit: int) -> int:
    while position < limit:
        character = text[position]
        if character.isspace() or character in ":,;/+&":
            position += 1
            continue
        if text[position : position + 3].casefold() == "and":
            after = position + 3
            if after >= limit or not text[after].isalnum():
                position = after
                continue
        break
    return position


def _match_ref_at(text: str, position: int, limit: int) -> Reference | None:
    match = _OBJECT_URL_RE.match(text, position, limit)
    if match is not None:
        kind = {
            "pull": "pull_request",
            "issues": "issue",
            "discussions": "discussion",
        }[match.group("kind").lower()]
        return Reference(
            kind=kind,
            start=match.start(),
            end=match.end(),
            raw=match.group(0),
            host=_normalize_host(match.group("host")),
            owner=unquote(match.group("owner")),
            name=unquote(match.group("repo")),
            number=int(match.group("number")),
            url=match.group(0),
        )
    match = _RELEASE_URL_RE.match(text, position, limit)
    if match is not None:
        end = _trimmed_end(text, match.start("tag"), match.end("tag"))
        return Reference(
            kind="release",
            start=match.start(),
            end=end,
            raw=text[match.start() : end],
            host=_normalize_host(match.group("host")),
            owner=unquote(match.group("owner")),
            name=unquote(match.group("repo")),
            tag=unquote(text[match.start("tag") : end]),
            url=text[match.start() : end],
        )
    match = _BLOB_URL_RE.match(text, position, limit)
    if match is not None:
        end = _trimmed_end(text, match.start("path"), match.end("path"))
        return Reference(
            kind="doc",
            start=match.start(),
            end=end,
            raw=text[match.start() : end],
            host=_normalize_host(match.group("host")),
            owner=unquote(match.group("owner")),
            name=unquote(match.group("repo")),
            revision=match.group("revision").lower(),
            path=unquote(text[match.start("path") : end]),
            url=text[match.start() : end],
        )
    match = _REPO_REF_RE.match(text, position, limit)
    if match is not None:
        return Reference(
            kind="number",
            start=match.start(),
            end=match.end(),
            raw=match.group(0),
            owner=match.group("owner"),
            name=match.group("repo"),
            number=int(match.group("number")),
        )
    match = _BARE_REF_RE.match(text, position, limit)
    if match is not None:
        return Reference(
            kind="number",
            start=match.start(),
            end=match.end(),
            raw=match.group(0),
            number=int(match.group("number")),
        )
    match = _SHA_RE.match(text, position, limit)
    if match is not None:
        return Reference(
            kind="commit",
            start=match.start(),
            end=match.end(),
            raw=match.group(0),
            sha=match.group("sha").lower(),
        )
    return None


def mask_markdown(text: str) -> MarkdownMask:
    normalized = normalize_newlines(text)
    length = len(normalized)
    code_mask = [False] * length
    quote_mask = [False] * length
    in_fence = False
    fence_character: str | None = None
    fence_length = 0
    offset = 0
    for line in normalized.splitlines(keepends=True):
        start = offset
        end = offset + len(line)
        offset = end
        fence = _FENCE_RE.match(line)
        if in_fence:
            _mark(code_mask, line, start, end)
            closing = _FENCE_CLOSE_RE.match(line)
            if (
                closing is not None
                and closing.group("fence")[0] == fence_character
                and len(closing.group("fence")) >= fence_length
            ):
                in_fence = False
                fence_character = None
                fence_length = 0
            continue
        if fence is not None:
            _mark(code_mask, line, start, end)
            in_fence = True
            fence_character = fence.group("fence")[0]
            fence_length = len(fence.group("fence"))
            continue
        if _QUOTE_RE.match(line):
            _mark(quote_mask, line, start, end)
            continue
        _mark_inline_code(line, start, code_mask)
    masked = "".join(
        character
        if character == "\n" or not (code_mask[index] or quote_mask[index])
        else " "
        for index, character in enumerate(normalized)
    )
    code = "".join(
        character
        if code_mask[index] and not quote_mask[index]
        else ("\n" if character == "\n" else " ")
        for index, character in enumerate(normalized)
    )
    quoted = "".join(
        character
        if quote_mask[index]
        else ("\n" if character == "\n" else " ")
        for index, character in enumerate(normalized)
    )
    return MarkdownMask(masked=masked, code=code, quoted=quoted)


def _mark(mask: list[bool], line: str, start: int, end: int) -> None:
    for index, character in enumerate(line):
        if character != "\n":
            mask[start + index] = True


def _mark_inline_code(line: str, offset: int, mask: list[bool]) -> None:
    index = 0
    while index < len(line):
        if line[index] != "`":
            index += 1
            continue
        run = 1
        while index + run < len(line) and line[index + run] == "`":
            run += 1
        closer = line.find("`" * run, index + run)
        if closer < 0:
            index += run
            continue
        end = closer + run
        for position in range(index, end):
            if line[position] != "\n":
                mask[offset + position] = True
        index = end


@dataclass
class Graph:
    """Directed relation index with bounded traversal."""

    relations: tuple[dict[str, Any], ...]
    by_entity: dict[str, list[str]]
    outgoing: dict[str, list[dict[str, Any]]]
    incoming: dict[str, list[dict[str, Any]]]

    @classmethod
    def from_relations(cls, relations: Iterable[Mapping[str, Any]]) -> Graph:
        return cls.build(relations)

    @classmethod
    def build(cls, relations: Iterable[Mapping[str, Any]]) -> Graph:
        materialized = tuple(dict(item) for item in relations)
        by_entity: dict[str, list[str]] = {}
        outgoing: dict[str, list[dict[str, Any]]] = {}
        incoming: dict[str, list[dict[str, Any]]] = {}
        for relation in sorted(materialized, key=lambda item: str(item.get("id"))):
            identifier = str(relation.get("id"))
            from_id = str(relation.get("from_id"))
            to_id = str(relation.get("to_id"))
            by_entity.setdefault(from_id, []).append(identifier)
            if to_id != from_id:
                by_entity.setdefault(to_id, []).append(identifier)
            outgoing.setdefault(from_id, []).append(dict(relation))
            incoming.setdefault(to_id, []).append(dict(relation))
        return cls(
            relations=materialized,
            by_entity=by_entity,
            outgoing=outgoing,
            incoming=incoming,
        )

    def traverse(
        self,
        start: str,
        *,
        max_hops: int = 2,
        budget: int | None = None,
    ) -> dict[str, Any]:
        if max_hops < 0:
            raise ValueError("max_hops must be non-negative")
        if budget is not None and budget < 0:
            raise ValueError("budget must be non-negative")
        nodes: list[dict[str, Any]] = [{"entity_id": start, "depth": 0}]
        edges: list[dict[str, Any]] = []
        visited = {start}
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        truncated = False
        limit = 1 if budget is not None and budget < 1 else budget
        while queue:
            current, depth = queue.popleft()
            if depth >= max_hops:
                continue
            for relation in self.outgoing.get(current, []):
                target = str(relation["to_id"])
                if target not in visited:
                    if limit is not None and len(nodes) >= limit:
                        truncated = True
                        continue
                    visited.add(target)
                    nodes.append({"entity_id": target, "depth": depth + 1})
                    queue.append((target, depth + 1))
                edge = {
                    "relation_id": relation["id"],
                    "from_id": relation["from_id"],
                    "to_id": target,
                    "type": relation["type"],
                    "depth": depth + 1,
                }
                if edge not in edges:
                    edges.append(edge)
        return {
            "start": start,
            "max_hops": max_hops,
            "budget": budget,
            "truncated": truncated,
            "nodes": nodes,
            "edges": edges,
        }


__all__ = [
    "Graph",
    "MarkdownMask",
    "Reference",
    "RelationBuilder",
    "mask_markdown",
]
