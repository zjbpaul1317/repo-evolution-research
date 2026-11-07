"""Incremental collection coverage and checkpoint bookkeeping.

`CollectProgress` is the only writer of `checkpoints/collect.json`. Stage
progress (completed/pending documents) is a hint: authoritative state is always
rescanned from the artifacts when a run resumes.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..errors import InternalError
from ..models import to_iso_z
from ..store import SnapshotStore


@dataclass(frozen=True)
class ChannelPolicy:
    """Coverage statuses that make a channel block the run."""

    blocking_statuses: frozenset[str] = frozenset()


_BLOCKING = ChannelPolicy(frozenset({"partial", "unavailable"}))
_PR_DETAIL = ChannelPolicy(frozenset({"unavailable"}))
_RELEASES = ChannelPolicy(frozenset({"unavailable"}))
_DOCS = ChannelPolicy(frozenset({"unavailable"}))

CHANNEL_POLICIES: dict[str, ChannelPolicy] = {
    "tree": _BLOCKING,
    "discovery_paths": _BLOCKING,
    "discussions_capability": _BLOCKING,
    "activity_prs": _BLOCKING,
    "activity_issues": _BLOCKING,
    "activity_discussions": _BLOCKING,
    "pr_detail": _PR_DETAIL,
    "issue_detail": _PR_DETAIL,
    "discussion_detail": _PR_DETAIL,
    "releases": _RELEASES,
    "docs": _DOCS,
}
LEGACY_CHANNELS = {"discussions": ("discussions_capability", "discussions_items")}
_COVERAGE_ENTRY_KEYS = frozenset(
    {
        "channel",
        "query_scope",
        "query",
        "expected_count",
        "fetched_count",
        "pages",
        "exhausted",
        "truncation_reason",
        "remaining_cursor",
        "observed_start",
        "observed_end",
        "coverage_status",
        "discovery_status",
        "detail_status",
    }
)


def coverage_entry(
    *,
    channel: str,
    query_scope: str,
    query: str,
    expected: int | None,
    fetched: int,
    pages: int,
    observed_start: str,
    observed_end: str | None = None,
    coverage_status: str = "complete",
    discovery_status: str | None = None,
    detail_status: str = "not_requested",
    exhausted: bool = True,
    truncation_reason: str | None = None,
    remaining_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "channel": channel,
        "query_scope": query_scope,
        "query": query,
        "expected_count": expected,
        "fetched_count": fetched,
        "pages": pages,
        "exhausted": exhausted,
        "truncation_reason": truncation_reason,
        "remaining_cursor": remaining_cursor,
        "observed_start": observed_start,
        "observed_end": observed_end or observed_start,
        "coverage_status": coverage_status,
        "discovery_status": discovery_status or coverage_status,
        "detail_status": detail_status,
    }


def merge_coverage(
    existing: Iterable[Mapping[str, Any]],
    incoming: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    incoming_records = [dict(item) for item in incoming]
    incoming_channels = {
        item.get("channel") for item in incoming_records if isinstance(item.get("channel"), str)
    }
    legacy_channels = {
        old
        for old, replacements in LEGACY_CHANNELS.items()
        if any(replacement in incoming_channels for replacement in replacements)
    }
    merged = [
        dict(item)
        for item in existing
        if not (isinstance(item, Mapping) and item.get("channel") in legacy_channels)
    ]
    positions = {
        item["channel"]: index
        for index, item in enumerate(merged)
        if isinstance(item, Mapping) and "channel" in item
    }
    for record in incoming_records:
        channel = record.get("channel")
        if not isinstance(channel, str):
            continue
        if channel in positions:
            merged[positions[channel]] = record
        else:
            positions[channel] = len(merged)
            merged.append(record)
    return merged


def has_blocking_gaps(coverage: Iterable[Mapping[str, Any]]) -> bool:
    for item in coverage:
        channel = item.get("channel")
        policy = CHANNEL_POLICIES.get(channel) if isinstance(channel, str) else None
        if policy is not None and item.get("coverage_status") in policy.blocking_statuses:
            return True
    return False


@dataclass
class CollectProgress:
    store: SnapshotStore
    config_hash: str
    usage_provider: Callable[[], Mapping[str, Any]] | None = None
    status: str = "in_progress"
    coverage: list[dict[str, Any]] = field(default_factory=list)
    cursors: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    channel_states: dict[str, str] = field(default_factory=dict)
    budget_exhausted: bool = False

    def record(self, entry: Mapping[str, Any], *, flush: bool = True) -> None:
        validated = _validate_coverage_entry(entry)
        channel = str(validated["channel"])
        self.channel_states[channel] = str(validated["coverage_status"])
        self.coverage = merge_coverage(self.coverage, [validated])
        if flush:
            self.flush()

    def set_cursor(self, channel: str, cursor: Any) -> None:
        self.cursors[channel] = cursor

    def set_state(self, key: str, value: Any) -> None:
        self.state[key] = value

    def flush_stage(
        self,
        stage: str,
        *,
        status: str,
        completed: Iterable[int] = (),
        pending: Iterable[int] = (),
        observed_at: str | None = None,
        flush: bool = True,
        best_effort: bool = True,
    ) -> None:
        self.stages[stage] = {
            "status": status,
            "completed": sorted({int(number) for number in completed}),
            "pending": sorted({int(number) for number in pending}),
        }
        if flush:
            self.flush(observed_at=observed_at, best_effort=best_effort)

    def finish_channel(
        self,
        channel: str,
        entry: Mapping[str, Any],
        *,
        completed: Iterable[int] = (),
        pending: Iterable[int] = (),
        observed_at: str | None = None,
        best_effort: bool = True,
    ) -> None:
        self.record(entry, flush=False)
        self.flush_stage(
            channel,
            status=str(entry["coverage_status"]),
            completed=completed,
            pending=pending,
            flush=False,
        )
        self.flush(observed_at=observed_at, best_effort=best_effort)

    def snapshot(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "status": self.status,
            "config_hash": self.config_hash,
            "observed_at": to_iso_z(datetime.now(UTC)),
            "stages": {
                stage: {
                    "status": state["status"],
                    "completed": list(state["completed"]),
                    "pending": list(state["pending"]),
                }
                for stage, state in self.stages.items()
            },
            "coverage": [dict(item) for item in self.coverage],
        }
        if self.cursors:
            document["cursors"] = dict(self.cursors)
        document.update(self.state)
        if self.budget_exhausted:
            document["budget_exhausted"] = True
        if self.usage_provider is not None:
            document["budget_consumed"] = dict(self.usage_provider())
        return document

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Restore run state from a previous checkpoint; artifacts stay authoritative."""
        self.status = str(snapshot.get("status", self.status))
        coverage = snapshot.get("coverage")
        self.coverage = (
            [dict(item) for item in coverage if isinstance(item, Mapping)]
            if isinstance(coverage, list)
            else []
        )
        self.channel_states = {
            str(item["channel"]): str(item.get("coverage_status", ""))
            for item in self.coverage
            if isinstance(item.get("channel"), str)
        }
        self.budget_exhausted = bool(snapshot.get("budget_exhausted"))
        stages = snapshot.get("stages")
        self.stages = (
            {
                str(stage): {
                    "status": str(state.get("status", "")),
                    "completed": [int(number) for number in state.get("completed", [])],
                    "pending": [int(number) for number in state.get("pending", [])],
                }
                for stage, state in stages.items()
                if isinstance(state, Mapping)
            }
            if isinstance(stages, Mapping)
            else {}
        )
        cursors = snapshot.get("cursors")
        self.cursors = dict(cursors) if isinstance(cursors, Mapping) else {}
        self.state = {
            key: snapshot[key]
            for key in (
                "default_branch",
                "default_branch_sha",
                "error",
                "exit_code",
            )
            if key in snapshot
        }

    def flush(
        self,
        *,
        status: str | None = None,
        error: str | None = None,
        exit_code: int | None = None,
        observed_at: str | None = None,
        extra: Mapping[str, Any] | None = None,
        best_effort: bool = True,
    ) -> None:
        if status is not None:
            self.status = status
        document = self.snapshot()
        if observed_at is not None:
            document["observed_at"] = observed_at
        if error is not None:
            document["error"] = error
        if exit_code is not None:
            document["exit_code"] = exit_code
        if extra:
            document.update(extra)
        try:
            self.store.write_checkpoint("collect", document)
        except Exception as exc:
            if not best_effort:
                raise
            print(f"warning: 无法写入 collect checkpoint: {exc}", file=sys.stderr)


def _validate_coverage_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise InternalError("coverage entry 必须是对象", source="progress.record")
    missing = sorted(_COVERAGE_ENTRY_KEYS - set(entry))
    if missing:
        raise InternalError(
            "coverage entry 缺少字段",
            source="progress.record",
            details={"missing": missing},
        )
    return dict(entry)
