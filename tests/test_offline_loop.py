from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from rer import cli
from rer import store as store_module
from rer.collectors import activity, base, pull_requests, repository
from rer.github import GitHubClient
from rer.models import make_capability, make_claim, make_topic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from support.fake_github import (  # noqa: E402
    ISSUE_NUMBER,
    NAME,
    OWNER,
    PR_NUMBER,
    REVISION,
    FakeGitHub,
)

FIXED_DT = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
FIXED_ISO = "2026-11-01T08:00:00Z"
RUN_CREATED = "2026-11-01T09:00:00Z"
PR_ID = f"github.com/{OWNER}/{NAME}/pull/{PR_NUMBER}"
ISSUE_ID = f"github.com/{OWNER}/{NAME}/issue/{ISSUE_NUMBER}"
GOLDEN = ROOT / "tests/fixtures/loop/report.golden.md"


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_DT if tz is None else FIXED_DT.astimezone(tz)


def _invoke(argv: list[str]):
    args = cli.build_parser().parse_args(argv)
    command = getattr(cli, f"command_{args.command}")
    return command(args)


def _freeze(monkeypatch) -> None:
    monkeypatch.setattr(cli, "datetime", FrozenDateTime)
    monkeypatch.setattr(repository, "datetime", FrozenDateTime)
    monkeypatch.setattr(activity, "now_iso", lambda: FIXED_ISO)
    monkeypatch.setattr(pull_requests, "now_iso", lambda: FIXED_ISO)
    monkeypatch.setattr(base.CollectContext, "now", lambda self: FIXED_ISO)
    monkeypatch.setattr(GitHubClient, "elapsed_seconds", lambda self: 60.0)
    monkeypatch.setattr(store_module.secrets, "token_hex", lambda size: "a" * (size * 2))


_ORIGINAL_GITHUB_CLIENT = cli.GitHubClient


def _use_fake_client(monkeypatch, fake: FakeGitHub) -> None:
    def factory(store, budget, **kwargs):
        return _ORIGINAL_GITHUB_CLIENT(
            store,
            budget,
            gh_path="fake-gh",
            runner=fake,
            **kwargs,
        )

    monkeypatch.setattr(cli, "GitHubClient", factory)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _evidence_ids(run_dir: Path) -> tuple[str, str]:
    records = [
        json.loads(line)
        for line in (run_dir / "normalized/evidence.jsonl").read_text().splitlines()
        if line.strip()
    ]
    issue = next(
        item["id"]
        for item in records
        if item["entity_id"] == ISSUE_ID and item["source_kind"] == "issue_body"
    )
    pull = next(
        item["id"]
        for item in records
        if item["entity_id"] == PR_ID and item["source_kind"] == "pull_request_body"
    )
    return issue, pull


def _write_selection(run_dir: Path, run_id: str) -> Path:
    path = run_dir / "analysis/selection.json"
    _write_json(
        path,
        {
            "schema_version": "1.3",
            "run_id": run_id,
            "created_at": RUN_CREATED,
            "entities": [
                {
                    "entity_id": PR_ID,
                    "reason": "实现 PR",
                    "depth": "deep",
                    "read": {
                        "body": True,
                        "comments": True,
                        "reviews": True,
                        "files": True,
                        "diff": False,
                    },
                    "questions": ["实现了什么？"],
                    "theme_hint": "cache",
                },
                {
                    "entity_id": ISSUE_ID,
                    "reason": "设计 RFC",
                    "depth": "deep",
                    "read": {
                        "body": True,
                        "comments": True,
                        "reviews": False,
                        "files": False,
                        "diff": False,
                    },
                    "questions": ["为什么需要清理接口？"],
                    "theme_hint": "cache",
                },
            ],
            "notes": [],
        },
    )
    return path


def _write_research(run_dir: Path, run_id: str) -> None:
    issue_evidence, pull_evidence = _evidence_ids(run_dir)
    claims = [
        make_claim(
            claim_id="claim-001",
            text="RFC 提出增加显式缓存清理接口。",
            kind="observed",
            evidence_ids=[issue_evidence],
            confidence="high",
            confidence_reason="来自 Issue 正文。",
            scope={
                "entity_ids": [ISSUE_ID],
                "capability_id": "cap-001",
                "revision": None,
                "version": None,
            },
        ),
        make_claim(
            claim_id="claim-002",
            text="PR 合并了清理接口骨架。",
            kind="observed",
            evidence_ids=[pull_evidence],
            confidence="high",
            confidence_reason="来自 PR 正文。",
            scope={
                "entity_ids": [PR_ID],
                "capability_id": "cap-001",
                "revision": None,
                "version": None,
            },
        ),
    ]
    capability = make_capability(
        capability_id="cap-001",
        topic_id="topic-001",
        title="Explicit cache cleanup API",
        scope="cleanup API skeleton",
        proposal_state="proposed",
        implementation_state="merged",
        delivery_state="unknown",
        availability="unknown",
        target_branch="main",
        observed_revision=REVISION,
        requirements_done=["cleanup API skeleton"],
        evidence_ids=[issue_evidence, pull_evidence],
    )
    topic = make_topic(
        topic_id="topic-001",
        title="Cache lifecycle",
        research_question="为什么以及如何引入显式缓存清理接口？",
        summary="RFC 已提出，第一个实现 PR 已合并，发布尚未核验。",
        problem_claim_ids=["claim-001"],
        implementation_claim_ids=["claim-002"],
        capability_ids=["cap-001"],
        reading_item_ids=["read-001", "read-002"],
    )
    document = {
        "schema_version": "1.3",
        "run_id": run_id,
        "created_at": RUN_CREATED,
        "topics": [topic],
        "claims": claims,
        "capabilities": [capability],
        "unknowns": [],
        "reading_list": [
            {
                "id": "read-001",
                "priority": "P0",
                "kind": "rfc",
                "title": "RFC: cache cleanup API",
                "url": f"https://github.com/{OWNER}/{NAME}/issues/{ISSUE_NUMBER}",
                "entity_id": ISSUE_ID,
                "why": "理解清理接口的动机。",
                "order": 0,
            },
            {
                "id": "read-002",
                "priority": "P0",
                "kind": "pr",
                "title": "Add cache cleanup API",
                "url": f"https://github.com/{OWNER}/{NAME}/pull/{PR_NUMBER}",
                "entity_id": PR_ID,
                "why": "核对实现范围。",
                "order": 1,
            },
        ],
        "limitations": [
            "budget: offline fixture uses a fixed budget.",
            "docs: only the design document was read; discussions were not requested.",
        ],
    }
    _write_json(run_dir / "analysis/research.json", document)


def run_loop(root: Path, monkeypatch) -> tuple[Path, FakeGitHub]:
    fake = FakeGitHub()
    _freeze(monkeypatch)
    _use_fake_client(monkeypatch, fake)

    init, _ = _invoke(
        [
            "init",
            "--repo",
            f"https://github.com/{OWNER}/{NAME}",
            "--days",
            "90",
            "--mode",
            "standard",
            "--timezone",
            "UTC",
            "--no-discussions",
            "--output-root",
            str(root),
        ]
    )
    run_dir = Path(init["result"]["run"])

    collect, _ = _invoke(["collect", "--run", str(run_dir)])
    assert collect["result"]["partial"] is False

    _invoke(["prepare", "--run", str(run_dir)])
    run_id = _read_json(run_dir / "manifest.json")["run_id"]
    selection = _write_selection(run_dir, run_id)
    _invoke(["collect", "--run", str(run_dir), "--selection", str(selection)])
    _invoke(["prepare", "--run", str(run_dir)])

    _write_research(run_dir, run_id)
    validation, _ = _invoke(["validate", "--run", str(run_dir)])
    assert validation["result"]["ok"] is True
    _invoke(["render", "--run", str(run_dir)])

    assert fake.unrouted == []
    return run_dir, fake


def test_offline_loop_is_deterministic(tmp_path: Path, monkeypatch) -> None:
    first, first_fake = run_loop(tmp_path / "first", monkeypatch)
    second, second_fake = run_loop(tmp_path / "second", monkeypatch)

    first_report = (first / "report.md").read_text(encoding="utf-8")
    second_report = (second / "report.md").read_text(encoding="utf-8")
    assert first_report == second_report
    assert first_fake.requests
    assert second_fake.requests

    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(first_report, encoding="utf-8")
    assert first_report == GOLDEN.read_text(encoding="utf-8")
