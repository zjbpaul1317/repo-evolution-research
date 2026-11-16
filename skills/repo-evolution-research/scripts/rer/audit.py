"""Read-only snapshot diagnosis; strict integrity checks remain unchanged."""

from __future__ import annotations

from typing import Any

from . import SCHEMA_VERSION
from .errors import EXIT_INTERNAL, EXIT_OK, EXIT_PARTIAL, RERError
from .store import SnapshotStore

_LEGACY_INDEXES = {
    "analysis/candidates.json": "invalid_candidates",
    "analysis/pack-index.json": "invalid_pack_index",
}


def audit_snapshot(store: SnapshotStore) -> dict[str, Any]:
    """Separate known legacy index rebuilds from unresolved integrity errors."""
    if not store.read_only:
        store = SnapshotStore(store.run_dir, read_only=True)
    with store.read_only_lock(store.run_lock_path):
        integrity = store.verify_integrity(repair=False, include_research=True)
        manifest = None
        try:
            manifest = store.read_manifest()
        except RERError:
            # The original invalid_manifest problem remains in integrity.
            pass
        rebuild_required = []
        attention_required = []
        for problem in integrity["problems"]:
            artifact = problem.get("artifact")
            legacy = False
            if _LEGACY_INDEXES.get(artifact) == problem.get("problem"):
                try:
                    document = store.read_json(artifact)
                    legacy = isinstance(document, dict) and document.get("schema_version") == "1.2"
                except RERError:
                    pass
            if legacy:
                rebuild_required.append({**problem, "reason": "legacy_derived_schema"})
            else:
                attention_required.append(problem)

    if attention_required:
        status, exit_code = "attention_required", EXIT_INTERNAL
    elif rebuild_required:
        status, exit_code = "rebuild_required", EXIT_PARTIAL
    else:
        status, exit_code = "ok", EXIT_OK
    recovery = []
    if rebuild_required:
        recovery.extend([
            "在新的 output root 下复制 run，保留 host/owner/repo/run_id 目录结构。",
            "在副本执行 prepare --run <COPY>，从权威 raw/blob 重建派生索引。",
            "重建会使旧 validation/report 失效；检查 research 后重新 validate / render / audit。",
            "不要只修改 schema_version；旧 run 的 collect/import-source 仍需重新 init。",
        ])
    if attention_required:
        if any(
            problem.get("artifact") in (
                "analysis/validation.json", "analysis/render-report.json", "report.md"
            )
            for problem in attention_required
        ):
            recovery.append(
                "校验/报告产物已过期或无效；在副本检查 research 后重新 validate，"
                "按当前门禁修正 claim 引文与状态，再 render / audit。"
            )
        recovery.append(
            "逐条检查 attention_required；这类问题不能仅靠修改版本号消除，"
            "先保留快照，缺失权威 blob 需恢复来源或重新采集。"
        )
    return {
        "status": status,
        "exit_code": exit_code,
        "snapshot_schema_version": (manifest or {}).get("schema_version"),
        "current_schema_version": SCHEMA_VERSION,
        "collection_schema_compatible": (manifest or {}).get("schema_version") == SCHEMA_VERSION,
        "integrity": integrity,
        "rebuild_required": rebuild_required,
        "attention_required": attention_required,
        "recovery": recovery,
    }
