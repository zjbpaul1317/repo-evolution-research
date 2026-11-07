from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rer.collectors.base import CollectContext
from rer.collectors.pages import SectionSpec, fetch_section
from rer.collectors.progress import CollectProgress, coverage_entry
from rer.collectors.texts import text_ref
from rer.errors import GitHubNotFoundError
from rer.github import RestPage
from rer.store import SnapshotStore, create_run, new_run_id

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
REPOSITORY = {"host": "github.com", "owner": "acme", "name": "demo"}


class FakeClient:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages

    def iter_rest_pages(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Iterator[RestPage]:
        for item in self.pages:
            if isinstance(item, BaseException):
                raise item
            yield item


def page(data: Any) -> RestPage:
    return RestPage(data=data, status=200, headers={}, request_id="r", page=1)


def collect_comments(
    context: CollectContext,
    number: int,
    total_known: int | None,
) -> dict[str, Any]:
    spec = SectionSpec(
        name="issue_comments",
        endpoint="repos/{owner}/{name}/issues/{number}/comments",
        parse_item=lambda raw: {
            "id": raw.get("id"),
            "body": text_ref(context.store, raw.get("body")),
        },
        total_known_key="comments",
        mismatch_reason="issue_comments_total_mismatch",
    )
    section, pages = fetch_section(
        context.client,
        spec,
        owner=str(context.repository["owner"]),
        name=str(context.repository["name"]),
        number=number,
        parent={"comments": total_known},
    )
    if context.progress is not None:
        observed_at = context.now()
        entry = coverage_entry(
            channel="issue_detail",
            query_scope="deep_read_selection",
            query=f"#{number}",
            expected=total_known,
            fetched=section["fetched"],
            pages=pages,
            coverage_status=section["status"],
            observed_start=observed_at,
            observed_end=observed_at,
        )
        context.progress.finish_channel(
            "issue_detail",
            entry,
            completed=[number],
            pending=[],
            observed_at=observed_at,
        )
    return section


def make_context(
    tmp_path: Path,
    pages: list[Any],
    *,
    clock: str = "2026-11-01T08:00:00Z",
    with_progress: bool = False,
) -> tuple[CollectContext, SnapshotStore]:
    store = create_run(tmp_path, REPOSITORY, new_run_id(NOW))
    progress = CollectProgress(store, "a" * 64) if with_progress else None
    context = CollectContext(
        FakeClient(pages),
        store,
        REPOSITORY,
        clock=lambda: clock,
        progress=progress,
    )
    return context, store


def test_fake_collector_expresses_complete_partial_and_unavailable(
    tmp_path: Path,
) -> None:
    complete, _ = make_context(
        tmp_path,
        [page([{"id": 1, "body": "a"}, {"id": 2, "body": "b"}])],
    )
    assert collect_comments(complete, 7, 2)["status"] == "complete"

    partial, _ = make_context(tmp_path, [page([{"id": 1, "body": "a"}])])
    section = collect_comments(partial, 7, 2)
    assert section["status"] == "partial"
    assert section["truncated"] is True
    assert section["truncation_reason"] == "issue_comments_total_mismatch"

    unavailable, _ = make_context(
        tmp_path,
        [GitHubNotFoundError("gone", source="test")],
    )
    section = collect_comments(unavailable, 7, 0)
    assert section["status"] == "unavailable"
    assert section["truncation_reason"] == "not_found"


def test_fake_collector_uses_shared_text_and_context(tmp_path: Path) -> None:
    context, store = make_context(
        tmp_path,
        [page([{"id": 1, "body": "hello"}])],
    )
    section = collect_comments(context, 7, 1)
    body = section["items"][0]["body"]
    assert body["preview"] == "hello"
    assert body["sha256"] is not None
    assert store.read_blob(body["sha256"]) == b"hello"
    assert context.now() == "2026-11-01T08:00:00Z"


def test_fake_collector_records_coverage_without_new_checkpoint(
    tmp_path: Path,
) -> None:
    context, store = make_context(
        tmp_path,
        [page([{"id": 1, "body": "a"}])],
        with_progress=True,
    )
    section = collect_comments(context, 7, 1)
    checkpoint = store.read_checkpoint("collect")
    assert checkpoint["coverage"][0]["channel"] == "issue_detail"
    assert checkpoint["coverage"][0]["coverage_status"] == section["status"]
    assert checkpoint["stages"]["issue_detail"]["status"] == section["status"]
    assert {path.name for path in (store.run_dir / "checkpoints").iterdir()} == {
        "collect.json"
    }


def test_fake_collector_stays_within_boilerplate_budget() -> None:
    source = inspect.getsource(collect_comments)
    assert len(source.splitlines()) <= 150
