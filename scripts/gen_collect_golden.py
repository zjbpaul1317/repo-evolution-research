#!/usr/bin/env python3
"""Regenerate deterministic collector golden fixtures.

Run from any directory:

    .venv/bin/python scripts/gen_collect_golden.py

The fixtures are generated from the same deterministic fake GitHub transport
used by the test suite. To prove they reflect the pre-refactor behavior, run
this script in a clean worktree of the base commit and compare the output.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/repo-evolution-research/scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import test_activity as activity_helpers  # noqa: E402
import test_pull_requests as pr_helpers  # noqa: E402

from rer import cli  # noqa: E402
from rer.collectors.pr_store import load_pr_document  # noqa: E402
from rer.collectors.pull_requests import collect_pull_requests  # noqa: E402
from rer.github import GitHubClient  # noqa: E402
from rer.store import SnapshotStore  # noqa: E402

OUTPUT = ROOT / "tests" / "fixtures" / "collect"
CLOCK = "2026-11-01T08:00:10Z"


def dump(name: str, document: Any) -> None:
    (OUTPUT / name).write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def collect_pr(
    tmp_path: Path,
    handler,
) -> tuple[SnapshotStore, Any, dict[str, Any], dict[str, Any]]:
    store, client = pr_helpers.make_client(tmp_path, handler)
    pr_helpers.seed(store, [10])
    result = collect_pull_requests(
        client,
        store,
        pr_helpers.REPOSITORY,
        activity_window=pr_helpers.WINDOW,
        clock=lambda: CLOCK,
    )
    document = load_pr_document(store, 10)
    if document is None:
        raise SystemExit("collect did not write the authoritative PR document")
    selection = json.loads(
        (store.run_dir / "analysis/pr-selection.json").read_text(encoding="utf-8")
    )
    return store, result, document, selection


def cli_factory(store: SnapshotStore, budget: dict[str, Any], **kwargs: Any) -> GitHubClient:
    return GitHubClient(
        store,
        budget,
        gh_path="gh",
        runner=lambda argv, input_text: _handler(
            argv[argv.index("--include") + 1],
            pr_helpers.parse_params(argv),
            pr_helpers._headers(argv),
        ),
        sleep=lambda _seconds: None,
        now=lambda: pr_helpers.NOW,
        initial_usage=kwargs.get("initial_usage"),
        max_attempts=1,
    )


_handler = pr_helpers.default_handler()


def collect_manifest_coverage(tmp_path: Path, handler) -> list[dict[str, Any]]:
    global _handler
    _handler = handler
    init_args = cli.build_parser().parse_args(
        [
            "init",
            "--repo",
            "acme/demo",
            "--timezone",
            "UTC",
            "--output-root",
            str(tmp_path),
        ]
    )
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        init_result, _ = cli.command_init(init_args)
    run_dir = Path(init_result["result"]["run"])
    store = SnapshotStore(run_dir)
    pr_helpers.seed(store, [10])
    overview = store.read_json("analysis/activity-overview.json")

    original = (
        cli.collect_repository,
        cli.collect_activity,
        cli.GitHubClient,
        cli.collect_pull_requests,
    )
    cli.collect_repository = lambda *args, **kwargs: SimpleNamespace(
        coverage=[],
        source_profile={},
        default_branch="main",
        default_branch_sha="a" * 40,
    )
    cli.collect_activity = lambda *args, **kwargs: SimpleNamespace(
        coverage=[],
        overview=overview,
        counts={"pr_created": 1},
        candidate_count=1,
    )
    cli.GitHubClient = cli_factory
    real_collect = cli.collect_pull_requests
    cli.collect_pull_requests = lambda *args, **kwargs: real_collect(
        *args,
        clock=lambda: CLOCK,
        **kwargs,
    )
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            code = cli.main(["collect", "--run", str(run_dir)])
    finally:
        (
            cli.collect_repository,
            cli.collect_activity,
            cli.GitHubClient,
            cli.collect_pull_requests,
        ) = original
    if code != 0:
        raise SystemExit(f"collect failed with exit code {code}")
    return store.read_manifest()["coverage"]


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    scenarios = {
        "complete": pr_helpers.default_handler(),
        "partial_truncated": pr_helpers.default_handler(
            details={10: pr_helpers.detail_body(10, changed_files=5)}
        ),
        "head_changed": pr_helpers.default_handler(
            detail_sequence={
                10: [
                    pr_helpers.detail_body(10, head_sha=pr_helpers.HEAD),
                    pr_helpers.detail_body(10, head_sha=pr_helpers.OTHER_HEAD),
                ]
            }
        ),
    }
    for name, handler in scenarios.items():
        with tempfile.TemporaryDirectory() as tmp:
            _, _, document, selection = collect_pr(Path(tmp), handler)
        dump(f"pr_detail.{name}.json", document)
        if name == "complete":
            dump("pr_selection.complete.json", selection)

    with tempfile.TemporaryDirectory() as tmp:
        dump(
            "manifest_coverage.complete.json",
            collect_manifest_coverage(Path(tmp), pr_helpers.default_handler()),
        )
    with tempfile.TemporaryDirectory() as tmp:
        dump(
            "manifest_coverage.partial.json",
            collect_manifest_coverage(
                Path(tmp),
                pr_helpers.default_handler(
                    details={10: pr_helpers.detail_body(10, changed_files=5)}
                ),
            ),
        )
    with tempfile.TemporaryDirectory() as tmp:
        item = activity_helpers.pr_item(10, merged_at="2026-09-20T10:00:00Z")

        def search(query: str):
            if "is:merged" in query:
                return activity_helpers.search_body([item])
            return activity_helpers.empty_search()

        _, result = activity_helpers.run_activity(
            Path(tmp),
            activity_helpers.make_runner(search_handler=search),
        )
    dump("activity_overview.complete.json", result.overview)
    print(f"wrote fixtures to {OUTPUT}")


if __name__ == "__main__":
    main()
