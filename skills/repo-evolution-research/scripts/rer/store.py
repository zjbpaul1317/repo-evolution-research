"""Snapshot storage: atomic writes, single-writer lock, blobs and checkpoints."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .errors import ArtifactError, InternalError, LockError, RERError, SchemaError, StoreError
from .models import (
    canonical_json,
    to_iso_z,
    validate_document,
    verify_evidence_quote,
)
from .relation_policy import relation_identity

RUN_ID_RE = re.compile(r"\A\d{8}T\d{6}Z-[0-9a-f]{8}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
STAGE_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")
_HOST_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9.-]*\Z")
_REPO_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_RUN_SUBDIRS = (
    "blobs",
    "raw",
    "normalized",
    "analysis",
    "analysis/packs",
    "analysis/dossiers",
    "checkpoints",
)


def new_run_id(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("new_run_id requires a timezone-aware datetime")
    stamp = moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def create_run(output_root: Path, repository: dict[str, Any], run_id: str) -> SnapshotStore:
    if not RUN_ID_RE.fullmatch(run_id):
        raise StoreError(
            f"非法 run_id：{run_id}",
            source="store.run_id",
            recovery="使用 new_run_id() 生成的时间戳式 ID",
        )
    try:
        host = repository["host"]
        owner = repository["owner"]
        name = repository["name"]
    except (KeyError, TypeError) as exc:
        raise StoreError("repository 缺少 host/owner/name", source="store.create_run") from exc
    if (
        not isinstance(host, str)
        or not _HOST_RE.fullmatch(host)
        or ".." in host
        or not isinstance(owner, str)
        or not _REPO_SEGMENT_RE.fullmatch(owner)
        or not isinstance(name, str)
        or not _REPO_SEGMENT_RE.fullmatch(name)
    ):
        raise StoreError(
            "repository 的 host/owner/name 含非法路径字符",
            source="store.create_run",
            recovery="先通过 config.parse_repository 规范化仓库地址",
        )

    host = host.lower()
    owner = owner.lower()
    name = name.lower()
    run_dir = Path(output_root).expanduser().resolve() / host / owner / name / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise StoreError(
            f"run 目录已存在：{run_dir}",
            source="store.create_run",
            recovery="使用新的 run_id，不要覆盖已有快照",
        ) from exc
    except OSError as exc:
        raise StoreError(
            f"无法创建 run 目录：{run_dir}",
            source="store.create_run",
            details={"reason": str(exc)},
        ) from exc

    for subdir in _RUN_SUBDIRS:
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)
    return SnapshotStore(run_dir)


class SnapshotStore:
    """All writes stay inside one run directory and are serialized by flock."""

    def __init__(self, run_dir: Path, *, read_only: bool = False) -> None:
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.read_only = read_only
        if not self.run_dir.is_dir():
            raise StoreError(f"run 目录不存在：{self.run_dir}", source="store.init")
        self.lock_path = self.run_dir / ".lock"
        self.run_lock_path = self.run_dir / ".run-lock"
        self.init_marker = self.run_dir / ".initializing"
        self._stream_index: dict[tuple[str, str], dict[Any, str]] = {}

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self._locked(self.lock_path, fcntl.LOCK_EX | fcntl.LOCK_NB):
            yield

    @contextmanager
    def lock_shared(self) -> Iterator[None]:
        if self.read_only:
            with self.read_only_lock(self.lock_path):
                yield
            return
        with self._locked(self.lock_path, fcntl.LOCK_SH):
            yield

    @contextmanager
    def read_only_lock(self, path: Path) -> Iterator[None]:
        """Use an existing lock without creating files in an archived snapshot."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            yield
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockError(
                    "该 run 正在被另一个进程写入，暂不能审计",
                    source="store.audit",
                    recovery="等待写入结束后重跑 audit",
                ) from exc
            yield
        finally:
            os.close(fd)

    @contextmanager
    def run_lock(self) -> Iterator[None]:
        """Hold an exclusive lock for a whole command, not a single write."""
        with self._locked(self.run_lock_path, fcntl.LOCK_EX | fcntl.LOCK_NB):
            yield

    @contextmanager
    def _locked(self, path: Path, mode: int) -> Iterator[None]:
        if self.read_only:
            raise StoreError("只读快照不允许创建写入锁", source="store.lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise StoreError(
                f"无法创建锁文件：{path}",
                source="store.lock",
                details={"reason": str(exc)},
            ) from exc
        try:
            try:
                fcntl.flock(fd, mode)
            except BlockingIOError as exc:
                raise LockError(
                    "该 run 正在被另一个进程写入",
                    source="store.lock",
                    recovery="等待另一个进程结束，或新建 run",
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def begin_init(self) -> None:
        with self.lock():
            self._atomic_write_bytes(self.secure_path(".initializing"), b"")

    def finish_init(self) -> None:
        with self.lock():
            self.init_marker.unlink(missing_ok=True)
            self._fsync_dir(self.run_dir)

    def initialization_pending(self) -> bool:
        return self.init_marker.exists()

    def secure_path(self, relpath: str | Path) -> Path:
        candidate = Path(relpath)
        if candidate.is_absolute():
            raise StoreError(
                f"不允许绝对路径：{candidate}",
                source="store.path",
                recovery="使用 run 目录内的相对路径",
            )
        resolved = (self.run_dir / candidate).resolve()
        try:
            resolved.relative_to(self.run_dir)
        except ValueError as exc:
            raise StoreError(
                f"路径越出 run 目录：{candidate}",
                source="store.path",
                recovery="使用 run 目录内的相对路径",
            ) from exc
        return resolved

    # ---------------------------------------------------------------- blobs

    def save_blob(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        path = self.secure_path(Path("blobs") / digest)
        if path.exists():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise StoreError(
                    f"blob 内容损坏：{digest}",
                    source="store.blob",
                    recovery="删除损坏 blob 后重新采集",
                )
            return digest
        self._atomic_write_bytes(path, payload)
        return digest

    def read_blob(self, digest: str) -> bytes:
        if not SHA256_RE.fullmatch(digest):
            raise StoreError(f"非法 blob hash：{digest}", source="store.blob")
        path = self.secure_path(Path("blobs") / digest)
        if not path.is_file():
            raise StoreError(
                f"blob 不存在：{digest}",
                source="store.blob",
                recovery="重新采集该证据或标记缺失",
            )
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise StoreError(
                f"blob hash 不匹配：{digest}",
                source="store.blob",
                recovery="保留快照用于诊断，不要静默覆盖",
            )
        return payload

    def verify_blob(self, digest: str) -> None:
        self.read_blob(digest)

    # ----------------------------------------------------------------- json

    def write_json(
        self,
        relpath: str | Path,
        document: Any,
        *,
        schema_name: str | None = None,
    ) -> Path:
        if schema_name:
            validate_document(document, schema_name)
        payload = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        path = self.secure_path(relpath)
        with self.lock():
            self._atomic_write_bytes(path, payload)
        return path

    def write_text(self, relpath: str | Path, text: str) -> Path:
        path = self.secure_path(relpath)
        with self.lock():
            self._atomic_write_bytes(path, text.encode("utf-8"))
        return path

    def write_bytes(self, relpath: str | Path, payload: bytes) -> Path:
        path = self.secure_path(relpath)
        with self.lock():
            self._atomic_write_bytes(path, payload)
            self._invalidate_stream(path)
        return path

    def remove_file(self, relpath: str | Path) -> None:
        path = self.secure_path(relpath)
        with self.lock():
            if path.is_file():
                path.unlink()
                self._fsync_dir(path.parent)
            self._invalidate_stream(path)

    def read_json(self, relpath: str | Path) -> Any:
        path = self.secure_path(relpath)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StoreError(f"JSON 文件不存在：{relpath}", source="store.read_json") from exc
        except json.JSONDecodeError as exc:
            raise StoreError(
                f"JSON 损坏：{relpath}",
                source="store.read_json",
                details={"reason": str(exc)},
            ) from exc

    # ---------------------------------------------------------------- jsonl

    def write_jsonl(
        self,
        relpath: str | Path,
        records: Iterable[dict[str, Any]],
        *,
        key: str = "id",
        on_conflict: str = "error",
        schema_name: str | None = None,
    ) -> Path:
        """Replace the whole stream; use upsert_jsonl to preserve existing records."""
        if on_conflict not in ("error", "replace"):
            raise InternalError(f"未知 on_conflict 策略：{on_conflict}", source="store.jsonl")
        raw_records = list(records)
        if schema_name:
            for record in raw_records:
                validate_document(record, schema_name)
        materialized = self._dedupe(raw_records, key=key, on_conflict=on_conflict)
        payload = self._jsonl_bytes(materialized)
        path = self.secure_path(relpath)
        with self.lock():
            self._atomic_write_bytes(path, payload)
            self._replace_index(path, key, materialized)
        return path

    def upsert_jsonl(
        self,
        relpath: str | Path,
        records: Iterable[dict[str, Any]],
        *,
        key: str = "id",
        schema_name: str | None = None,
    ) -> Path:
        """Merge records into an existing stream, preserving unrelated entries."""
        raw_records = list(records)
        if schema_name:
            for record in raw_records:
                validate_document(record, schema_name)
        for record in raw_records:
            if not isinstance(record, dict):
                raise StoreError("JSONL 记录必须是对象", source="store.jsonl")
            if key not in record:
                raise StoreError(f"JSONL 记录缺少去重键 {key}", source="store.jsonl")

        path = self.secure_path(relpath)
        with self.lock():
            self._repair_stream_unlocked(path)
            merged = self._load_jsonl_unlocked(path)
            positions = {
                record[key]: index
                for index, record in enumerate(merged)
                if key in record
            }
            for record in raw_records:
                value = record[key]
                if value in positions:
                    merged[positions[value]] = record
                else:
                    positions[value] = len(merged)
                    merged.append(record)
            self._atomic_write_bytes(path, self._jsonl_bytes(merged))
            self._replace_index(path, key, merged)
        return path

    def append_jsonl(
        self,
        relpath: str | Path,
        record: dict[str, Any],
        *,
        key: str = "id",
        on_conflict: str = "error",
        schema_name: str | None = None,
    ) -> None:
        if on_conflict not in ("error", "replace"):
            raise InternalError(f"未知 on_conflict 策略：{on_conflict}", source="store.jsonl")
        if schema_name:
            validate_document(record, schema_name)
        path = self.secure_path(relpath)
        with self.lock():
            self._repair_stream_unlocked(path)
            index = self._index_for(path, key)
            if key in record and record[key] in index:
                previous = index[record[key]]
                if previous == canonical_json(record):
                    return
                if on_conflict == "error":
                    raise StoreError(
                        f"{key}={record[key]!r} 已存在且内容不同",
                        source="store.jsonl",
                        details={"stream": str(relpath)},
                        recovery="使用 on_conflict='replace' 明确覆盖",
                    )
                existing = self._load_jsonl_unlocked(path)
                replaced = [
                    record if item.get(key) == record[key] else item
                    for item in existing
                ]
                self._atomic_write_bytes(path, self._jsonl_bytes(replaced))
                self._replace_index(path, key, replaced)
                return
            with path.open("ab") as handle:
                handle.write(self._line(record))
                handle.flush()
                os.fsync(handle.fileno())
            if key in record:
                index[record[key]] = canonical_json(record)
            self._fsync_dir(path.parent)

    def read_jsonl(self, relpath: str | Path, *, repair: bool = False) -> list[dict[str, Any]]:
        path = self.secure_path(relpath)
        if not path.exists():
            return []
        if repair:
            with self.lock():
                self._repair_stream_unlocked(path)
                return self._load_jsonl_unlocked(path)
        with self.lock_shared():
            data = path.read_bytes()
            if data and not data.endswith(b"\n"):
                raise StoreError(
                    f"JSONL 末尾存在未完成写入：{relpath}",
                    source="store.jsonl",
                    retryable=False,
                    recovery="使用 read_jsonl(..., repair=True) 截断最后一行",
                )
            return self._load_jsonl_unlocked(path)

    # ------------------------------------------------------------- evidence

    def write_evidence(
        self,
        evidence: dict[str, Any],
        payload: bytes | None = None,
    ) -> dict[str, Any]:
        evidence = {**evidence}
        evidence.setdefault("verification", "unverifiable")
        digest = evidence["blob_sha256"]
        if payload is not None:
            actual = hashlib.sha256(payload).hexdigest()
            if digest and digest != actual:
                raise StoreError(
                    "evidence.blob_sha256 与 payload 不一致",
                    source="store.evidence",
                    details={"declared": digest, "actual": actual},
                )
            digest = actual
            evidence["blob_sha256"] = digest
            self.save_blob(payload)
        else:
            self.verify_blob(digest)

        matched, reason = verify_evidence_quote(evidence, self.read_blob(digest))
        if not matched and reason != "unverifiable_locator":
            raise StoreError(
                f"evidence 引文与定位内容不一致：{reason}",
                source="store.evidence",
                details={"evidence_id": evidence["id"], "reason": reason},
                recovery="修正 locator/text，或保存完整原文",
            )
        evidence["verification"] = "verified" if matched else "unverifiable"
        validate_document(evidence, "evidence")
        self.append_jsonl(
            "normalized/evidence.jsonl",
            evidence,
            key="id",
            schema_name="evidence",
        )
        return evidence

    def read_evidence(self, *, repair: bool = False) -> list[dict[str, Any]]:
        return self.read_jsonl("normalized/evidence.jsonl", repair=repair)

    def verify_integrity(
        self,
        *,
        repair: bool = False,
        include_research: bool = True,
    ) -> dict[str, Any]:
        """Audit a snapshot without mutating it unless repair=True."""
        problems: list[dict[str, Any]] = []
        schema_ok = True
        repaired = False

        def load_stream(relpath: str, schema_name: str) -> list[dict[str, Any]]:
            nonlocal schema_ok, repaired
            path = self.secure_path(relpath)
            if not path.exists():
                return []
            if self._has_partial_tail(path):
                if not repair:
                    problems.append(
                        {
                            "stream": relpath,
                            "problem": "incomplete_tail",
                            "message": (
                                "JSONL 末尾有未完成写入；"
                                "使用 repair=True 显式修复"
                            ),
                        }
                    )
                    return []
                repaired = True
            try:
                records = self.read_jsonl(relpath, repair=repair)
            except StoreError as exc:
                problems.append(
                    {
                        "stream": relpath,
                        "problem": "unreadable_stream",
                        "message": exc.message,
                    }
                )
                return []
            for record in records:
                try:
                    validate_document(record, schema_name)
                except SchemaError as exc:
                    schema_ok = False
                    problems.append(
                        {
                            "stream": relpath,
                            "record_id": record.get("id"),
                            "problem": "schema_error",
                            "message": exc.message,
                            "details": exc.details,
                        }
                    )
            return records

        entities = load_stream("normalized/entities.jsonl", "entity")
        evidence = load_stream("normalized/evidence.jsonl", "evidence")
        relations = load_stream("normalized/relations.jsonl", "relation")
        entity_ids = {entity.get("id") for entity in entities}
        known_evidence = {item.get("id") for item in evidence}
        evidence_entity = {
            str(item.get("id")): item.get("entity_id") for item in evidence
        }
        evidence_verification = {
            str(item.get("id")): item.get("verification") for item in evidence
        }
        seen_evidence: set[Any] = set()
        for item in evidence:
            identifier = item.get("id")
            if identifier in seen_evidence:
                problems.append(
                    {
                        "evidence_id": identifier,
                        "problem": "duplicate_evidence_id",
                        "message": "normalized/evidence.jsonl 存在重复 id",
                    }
                )
            seen_evidence.add(identifier)
        seen_relations: set[Any] = set()
        seen_identities: dict[tuple[str, str, str], Any] = {}
        for relation in relations:
            identifier = relation.get("id")
            identity = relation_identity(relation)
            if identifier in seen_relations:
                problems.append(
                    {
                        "relation_id": identifier,
                        "problem": "duplicate_relation_id",
                        "message": "normalized/relations.jsonl 存在重复 id",
                    }
                )
            seen_relations.add(identifier)
            prior_identity_id = seen_identities.get(identity)
            if prior_identity_id is not None and prior_identity_id != identifier:
                problems.append(
                    {
                        "relation_id": identifier,
                        "problem": "duplicate_relation_identity",
                        "message": "normalized/relations.jsonl 存在重复 identity",
                        "details": {
                            "other_relation_id": prior_identity_id,
                            "identity": list(identity),
                        },
                    }
                )
            else:
                seen_identities.setdefault(identity, identifier)
            for field in ("from_id", "to_id"):
                if relation.get(field) not in entity_ids:
                    problems.append(
                        {
                            "relation_id": identifier,
                            "problem": "missing_relation_endpoint",
                            "message": (
                                f"relation.{field}={relation.get(field)!r} "
                                "不在 entities 中"
                            ),
                        }
                    )
            if relation.get("from_id") == relation.get("to_id"):
                problems.append(
                    {
                        "relation_id": identifier,
                        "problem": "self_relation",
                        "message": "relation.from_id 与 to_id 相同",
                    }
                )
            for evidence_id in relation.get("evidence_ids") or []:
                if evidence_id not in known_evidence:
                    problems.append(
                        {
                            "relation_id": identifier,
                            "evidence_id": evidence_id,
                            "problem": "missing_relation_evidence",
                            "message": "relation.evidence_ids 不在 evidence 流中",
                        }
                    )
                    continue
                if evidence_entity.get(str(evidence_id)) not in (
                    relation.get("from_id"),
                    relation.get("to_id"),
                ):
                    problems.append(
                        {
                            "relation_id": identifier,
                            "evidence_id": evidence_id,
                            "problem": "evidence_entity_mismatch",
                            "message": "evidence.entity_id 不是 relation 的端点",
                        }
                    )
            relation_evidence = [
                str(value) for value in relation.get("evidence_ids") or []
            ]
            if relation.get("verification") == "verified":
                if not relation_evidence and relation.get("type") != "contains_commit":
                    problems.append(
                        {
                            "relation_id": identifier,
                            "problem": "verified_relation_without_evidence",
                            "message": "verification=verified 的关系没有证据",
                        }
                    )
                for evidence_id in relation_evidence:
                    if evidence_id not in known_evidence:
                        continue
                    if evidence_verification.get(evidence_id) != "verified":
                        problems.append(
                            {
                                "relation_id": identifier,
                                "evidence_id": evidence_id,
                                "problem": "verified_relation_unverified_evidence",
                                "message": (
                                    "verification=verified 的关系引用了未核验证据"
                                ),
                            }
                        )

        quote_reverified = 0
        for item in evidence:
            digest = item.get("blob_sha256", "")
            payload: bytes | None = None
            try:
                payload = self.read_blob(digest)
            except StoreError as exc:
                problems.append(
                    {
                        "evidence_id": item.get("id"),
                        "blob_sha256": digest,
                        "problem": exc.code,
                        "message": exc.message,
                    }
                )
            if item.get("entity_id") not in entity_ids:
                problems.append(
                    {
                        "evidence_id": item.get("id"),
                        "entity_id": item.get("entity_id"),
                        "problem": "missing_entity",
                        "message": "evidence.entity_id 不在 normalized/entities.jsonl 中",
                    }
                )
            if payload is not None and item.get("verification") == "verified":
                quote_reverified += 1
                matched, reason = verify_evidence_quote(item, payload)
                if not matched:
                    problems.append(
                        {
                            "evidence_id": item.get("id"),
                            "problem": "quote_mismatch",
                            "message": f"verification=verified 但引文不匹配：{reason}",
                        }
                    )

        manifest_hash_ok = False
        manifest: dict[str, Any] | None = None
        config: dict[str, Any] | None = None
        try:
            manifest = self.read_manifest()
        except RERError as exc:
            schema_ok = False
            problems.append(
                {"artifact": "manifest.json", "problem": "invalid_manifest", "message": exc.message}
            )
        try:
            from .config import read_resolved_config

            config = read_resolved_config(self.run_dir / "config.resolved.json", artifact=True)
        except RERError as exc:
            problems.append(
                {
                    "artifact": "config.resolved.json",
                    "problem": "invalid_config",
                    "message": exc.message,
                }
            )
        if manifest is not None and config is not None:
            manifest_hash_ok = manifest.get("config_hash") == config.get("config_hash")
            if not manifest_hash_ok:
                problems.append(
                    {
                        "artifact": "manifest.json",
                        "problem": "config_hash_mismatch",
                        "message": "manifest.config_hash 与 config.resolved.json 不一致",
                    }
                )

        from .imported_source import audit_imported_sources
        from .integrity import (
            audit_doc_artifacts,
            audit_pr_artifacts,
            audit_proposal_artifacts,
            audit_release_artifacts,
        )

        problems.extend(
            audit_imported_sources(self, evidence=evidence, relations=relations)
        )

        pr_problems, pr_repaired, text_blobs_checked = audit_pr_artifacts(
            self,
            manifest=manifest,
            entities=entities,
            repair=repair,
        )
        problems.extend(pr_problems)
        repaired = repaired or pr_repaired

        release_problems, release_repaired, release_blobs = audit_release_artifacts(
            self,
            manifest=manifest,
            entities=entities,
            repair=repair,
        )
        problems.extend(release_problems)
        repaired = repaired or release_repaired
        text_blobs_checked += release_blobs

        doc_problems, doc_repaired, doc_blobs = audit_doc_artifacts(
            self,
            manifest=manifest,
            entities=entities,
            repair=repair,
        )
        problems.extend(doc_problems)
        repaired = repaired or doc_repaired
        text_blobs_checked += doc_blobs

        proposal_problems, proposal_repaired, proposal_blobs = (
            audit_proposal_artifacts(
                self,
                manifest=manifest,
                entities=entities,
                repair=repair,
            )
        )
        problems.extend(proposal_problems)
        repaired = repaired or proposal_repaired
        text_blobs_checked += proposal_blobs

        from .packs import audit_pack_index
        from .ranking import CANDIDATES_PATH, audit_candidates

        problems.extend(
            audit_candidates(self, entities=entities, evidence=evidence)
        )
        candidates_document = None
        if self.secure_path(CANDIDATES_PATH).is_file():
            try:
                candidates_document = self.read_json(CANDIDATES_PATH)
            except RERError:
                candidates_document = None
        problems.extend(
            audit_pack_index(self, candidates=candidates_document)
        )
        from .selection_protocol import audit_selection_protocol

        problems.extend(
            audit_selection_protocol(
                self,
                manifest=manifest,
                entities=entities,
            )
        )

        if include_research and manifest is not None:
            from .render import audit_report_artifacts

            problems.extend(
                audit_report_artifacts(self, manifest=manifest)
            )

        return {
            "ok": not problems,
            "repaired": repaired,
            "manifest_hash_ok": manifest_hash_ok,
            "schema_ok": schema_ok,
            "quote_reverified": quote_reverified,
            "relations_checked": len(relations),
            "text_blobs_checked": text_blobs_checked,
            "problems": problems,
        }

    # -------------------------------------------------------------- manifest

    def write_manifest(self, manifest: dict[str, Any]) -> Path:
        manifest["updated_at"] = to_iso_z(datetime.now(UTC))
        return self.write_json("manifest.json", manifest, schema_name="manifest")

    def read_manifest(self) -> dict[str, Any]:
        manifest = self.read_json("manifest.json")
        try:
            validate_document(manifest, "manifest")
        except SchemaError as exc:
            raise ArtifactError(
                "manifest.json 损坏或与当前 Schema 不兼容",
                source="store.manifest",
                details=exc.details,
                recovery="保留快照用于诊断；不要继续写入该 run",
            ) from exc
        return manifest

    def update_manifest(self, **changes: Any) -> dict[str, Any]:
        with self.lock():
            manifest = self.read_manifest()
            manifest.update(changes)
            manifest["updated_at"] = to_iso_z(datetime.now(UTC))
            validate_document(manifest, "manifest")
            self._atomic_write_bytes(
                self.secure_path("manifest.json"),
                (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )
        return manifest

    # ----------------------------------------------------------- checkpoints

    def write_checkpoint(self, stage: str, checkpoint: dict[str, Any]) -> Path:
        if not STAGE_RE.fullmatch(stage):
            raise StoreError(f"非法 checkpoint 阶段名：{stage}", source="store.checkpoint")
        reserved = {"schema_version", "stage", "written_at"} & checkpoint.keys()
        if reserved:
            raise StoreError(
                f"checkpoint 不得覆盖保留字段：{sorted(reserved)}",
                source="store.checkpoint",
            )
        config_hash = checkpoint.get("config_hash")
        if not isinstance(config_hash, str) or not SHA256_RE.fullmatch(config_hash):
            raise StoreError(
                "checkpoint 必须包含合法的 config_hash",
                source="store.checkpoint",
                recovery="由调用方传入 config.resolved.json 的 config_hash",
            )
        document = {
            "schema_version": SCHEMA_VERSION,
            "stage": stage,
            "written_at": to_iso_z(datetime.now(UTC)),
            **checkpoint,
        }
        return self.write_json(Path("checkpoints") / f"{stage}.json", document)

    def read_checkpoint(self, stage: str) -> dict[str, Any]:
        if not STAGE_RE.fullmatch(stage):
            raise StoreError(f"非法 checkpoint 阶段名：{stage}", source="store.checkpoint")
        return self.read_json(Path("checkpoints") / f"{stage}.json")

    # --------------------------------------------------------------- requests

    def append_request(self, record: dict[str, Any]) -> None:
        if not isinstance(record, dict):
            raise StoreError("request 记录必须是对象", source="store.requests")
        self.append_jsonl("requests.jsonl", record, key="request_id")

    # --------------------------------------------------------------- helpers

    def _dedupe(
        self,
        records: Iterable[dict[str, Any]],
        *,
        key: str,
        on_conflict: str,
    ) -> list[dict[str, Any]]:
        ordered: list[dict[str, Any]] = []
        index: dict[Any, dict[str, Any]] = {}
        positions: dict[Any, int] = {}
        for record in records:
            if not isinstance(record, dict):
                raise StoreError("JSONL 记录必须是对象", source="store.jsonl")
            if key not in record:
                raise StoreError(f"JSONL 记录缺少去重键 {key}", source="store.jsonl")
            value = record[key]
            if value in index:
                previous = index[value]
                if canonical_json(previous) == canonical_json(record):
                    continue
                if on_conflict == "error":
                    raise StoreError(
                        f"{key}={value!r} 内容冲突",
                        source="store.jsonl",
                        recovery="使用 on_conflict='replace' 明确覆盖",
                    )
                ordered[positions[value]] = record
                index[value] = record
                continue
            positions[value] = len(ordered)
            index[value] = record
            ordered.append(record)
        return ordered

    def _index_key(self, path: Path, key: str) -> tuple[str, str]:
        return str(path), key

    def _index_for(self, path: Path, key: str) -> dict[Any, str]:
        cache_key = self._index_key(path, key)
        index = self._stream_index.get(cache_key)
        if index is None:
            index = {}
            for item in self._load_jsonl_unlocked(path):
                if key in item:
                    index[item[key]] = canonical_json(item)
            self._stream_index[cache_key] = index
        return index

    def _replace_index(self, path: Path, key: str, records: Iterable[dict[str, Any]]) -> None:
        self._invalidate_stream(path)
        self._stream_index[self._index_key(path, key)] = {
            item[key]: canonical_json(item) for item in records if key in item
        }

    def _invalidate_stream(self, path: Path) -> None:
        stream = str(path)
        for cache_key in [key for key in self._stream_index if key[0] == stream]:
            del self._stream_index[cache_key]

    @staticmethod
    def _has_partial_tail(path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return False
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"

    def _load_jsonl_unlocked(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise StoreError(
                        f"JSONL 第 {line_number} 行损坏：{path.name}",
                        source="store.jsonl",
                        details={"line": line_number, "reason": str(exc)},
                    ) from exc
                if not isinstance(record, dict):
                    raise StoreError(
                        f"JSONL 第 {line_number} 行不是对象：{path.name}",
                        source="store.jsonl",
                        details={"line": line_number},
                    )
                records.append(record)
        return records

    def _repair_stream_unlocked(self, path: Path) -> int:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            return 0
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size == 0:
                return 0
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return 0

            position = size
            last_newline = -1
            while position > 0 and last_newline < 0:
                start = max(0, position - 65536)
                handle.seek(start)
                chunk = handle.read(position - start)
                index = chunk.rfind(b"\n")
                if index >= 0:
                    last_newline = start + index
                    break
                position = start
            truncate_to = last_newline + 1 if last_newline >= 0 else 0

        with path.open("r+b") as handle:
            handle.truncate(truncate_to)
            handle.flush()
            os.fsync(handle.fileno())
        self._invalidate_stream(path)
        self._fsync_dir(path.parent)
        return 1

    def _jsonl_bytes(self, records: Iterable[dict[str, Any]]) -> bytes:
        return b"".join(self._line(record) for record in records)

    @staticmethod
    def _line(record: dict[str, Any]) -> bytes:
        try:
            return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise StoreError(
                "记录无法序列化为 JSON",
                source="store.jsonl",
                details={"reason": str(exc)},
            ) from exc

    @staticmethod
    def _atomic_write_bytes(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            with temp.open("wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            SnapshotStore._fsync_dir(path.parent)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            finally:
                raise StoreError(
                    f"原子写入失败：{path}",
                    source="store.atomic_write",
                    details={"reason": str(exc)},
                ) from exc

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
