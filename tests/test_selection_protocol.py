from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_normalize as normalize_helpers
import test_proposals as proposal_helpers
import test_pull_requests as pr_helpers

from rer import cli
from rer.collectors.discussions import read_discussion
from rer.collectors.pr_store import load_pr_document
from rer.collectors.proposal_store import (
    write_discussion_document,
    write_issue_document,
)
from rer.collectors.proposals import _read_issue
from rer.collectors.status import derive_limitations
from rer.config import compute_config_hash
from rer.errors import ValidationFailure
from rer.models import entity_id, make_entity, validate_document
from rer.selection_protocol import (
    REPORT_PATH,
    SELECTION_PATH,
    load_agent_selection,
)

NOW = "2026-11-01T08:00:00Z"


def read_flags(
    *,
    body: bool = True,
    comments: bool = True,
    reviews: bool = True,
    files: bool = True,
    diff: bool = False,
) -> dict:
    return {
        "body": body,
        "comments": comments,
        "reviews": reviews,
        "files": files,
        "diff": diff,
    }


def selection_document(store, entities: list[dict]) -> dict:
    return {
        "schema_version": "1.3",
        "run_id": store.run_dir.name,
        "created_at": NOW,
        "entities": entities,
        "notes": [],
    }


def selection_item(
    identifier: str,
    *,
    depth: str = "deep",
    read: dict | None = None,
    reason: str = "agent selected",
) -> dict:
    return {
        "entity_id": identifier,
        "reason": reason,
        "depth": depth,
        "read": read or read_flags(),
        "questions": ["What changed?"],
        "theme_hint": None,
    }


def add_unread_pr(store, number: int) -> str:
    identifier = entity_id("github.com", "acme", "demo", "pull_request", str(number))
    raw = store.read_jsonl("raw/prs.jsonl")
    raw.append(pr_helpers.pr_record(number, merged_at="2026-10-01T00:00:00Z"))
    store.write_jsonl("raw/prs.jsonl", raw, key="number")
    entities = store.read_jsonl("normalized/entities.jsonl")
    entities.append(
        make_entity(
            entity_id=identifier,
            kind="pull_request",
            title=f"PR {number}",
            url=f"https://github.com/acme/demo/pull/{number}",
            observed_at=NOW,
            number=number,
            source_event_at="2026-10-20T10:00:00Z",
            discovery_reasons=["pr_created"],
        )
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    return identifier


def add_unread_proposal(store, kind: str, number: int, title: str) -> str:
    identifier = entity_id("github.com", "acme", "demo", kind, str(number))
    path = "issues" if kind == "issue" else "discussions"
    entities = [
        entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
        if entity.get("id") != identifier
    ]
    entities.append(
        make_entity(
            entity_id=identifier,
            kind=kind,
            title=title,
            url=f"https://github.com/acme/demo/{path}/{number}",
            observed_at=NOW,
            number=number,
            raw_state={"state": "open"} if kind == "issue" else {"closed": False},
            normalized_state={"scope": "in_window", "role_hints": []},
        )
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    return identifier


def write_selection(store, document: dict) -> Path:
    path = store.secure_path(SELECTION_PATH)
    store.write_json(SELECTION_PATH, document)
    return path


def collect_with_selection(
    store,
    path: Path,
    monkeypatch,
    handler=None,
) -> tuple[dict, list[str]]:
    calls: list[str] = []
    selected_handler = handler or pr_helpers.default_handler(calls=calls)

    def factory(client_store, budget, **kwargs):
        from rer.github import GitHubClient

        def runner(argv, input_text):
            return selected_handler(
                argv[argv.index("--include") + 1],
                pr_helpers.parse_params(argv),
                pr_helpers._headers(argv),
            )

        return GitHubClient(
            client_store,
            budget,
            gh_path="gh",
            runner=runner,
            sleep=lambda _seconds: None,
            now=lambda: pr_helpers.NOW,
            initial_usage=kwargs.get("initial_usage"),
            max_attempts=1,
        )

    monkeypatch.setattr(cli, "GitHubClient", factory)
    args = cli.build_parser().parse_args(
        ["collect", "--run", str(store.run_dir), "--selection", str(path)]
    )
    result, _lines = cli.command_collect(args)
    return result, calls


def test_selection_only_fetches_unread_objects(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    path = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item("github.com/acme/demo/pull/10"),
                selection_item(unread, read=read_flags(comments=False, reviews=False)),
            ],
        ),
    )
    selection_bytes = path.read_bytes()

    result, calls = collect_with_selection(store, path, monkeypatch)

    assert result["result"]["counts"]["selected"] == 2
    assert result["result"]["counts"]["fetched"] == 1
    assert result["result"]["counts"]["skipped_unchanged"] == 1
    assert any("/pulls/11/files" in endpoint for endpoint in calls)
    assert not any("/pulls/10/files" in endpoint for endpoint in calls)
    assert path.read_bytes() == selection_bytes
    report = store.read_json(REPORT_PATH)
    validate_document(report, "selection_report")
    statuses = {item["entity_id"]: item["status"] for item in report["items"]}
    assert statuses["github.com/acme/demo/pull/10"] == "skipped_unchanged"
    assert statuses[unread] == "fetched"
    assert report["budget"]["deep_prs_before"] == 1
    assert report["budget"]["deep_prs_after"] == 2
    assert store.read_manifest()["budget_consumed"]["deep_entities"] == 2


def test_selection_read_flags_trim_sections(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    path = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item(
                    unread,
                    read=read_flags(
                        body=False,
                        comments=False,
                        reviews=False,
                        files=False,
                    ),
                )
            ],
        ),
    )

    collect_with_selection(store, path, monkeypatch)

    document = load_pr_document(store, 11)
    assert document is not None
    assert document["body"]["sha256"] is None
    assert document["body"]["bytes"] == 0
    for section in ("files", "issue_comments", "reviews", "review_comments"):
        assert document[section]["status"] == "not_requested"
        assert document[section]["items"] == []
    assert document["commits"]["status"] == "complete"
    assert document["diff"]["status"] == "not_requested"


def test_selection_records_release_and_doc_without_fetching(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    entities = store.read_jsonl("normalized/entities.jsonl")
    release = make_entity(
        entity_id=entity_id("github.com", "acme", "demo", "release", "v1.0.0"),
        kind="release",
        title="v1.0.0",
        url="https://github.com/acme/demo/releases/tag/v1.0.0",
        observed_at=NOW,
        raw_state={"release_id": 1, "tag_name": "v1.0.0"},
    )
    revision = "a" * 40
    doc = make_entity(
        entity_id=entity_id(
            "github.com",
            "acme",
            "demo",
            "doc",
            "docs/design.md",
            revision=revision,
        ),
        kind="doc",
        title="docs/design.md",
        url=f"https://github.com/acme/demo/blob/{revision}/docs/design.md",
        observed_at=NOW,
        raw_state={"revision": revision, "path": "docs/design.md"},
    )
    entities.extend([release, doc])
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    path = write_selection(
        store,
        selection_document(
            store,
            [selection_item(release["id"]), selection_item(doc["id"])],
        ),
    )

    result, calls = collect_with_selection(store, path, monkeypatch)

    assert result["result"]["counts"]["recorded"] == 2
    assert calls == []
    report = store.read_json(REPORT_PATH)
    assert all(item["status"] == "recorded" for item in report["items"])
    assert all(item["requests"] == 0 for item in report["items"])


def test_selection_budget_exhaustion_is_partial_and_visible(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    manifest = store.read_manifest()
    manifest["budget_consumed"]["deep_entities"] = manifest["budget"]["max_deep_entities"]
    store.write_manifest(manifest)
    path = write_selection(
        store,
        selection_document(store, [selection_item(unread)]),
    )

    result, calls = collect_with_selection(store, path, monkeypatch)

    assert result["result"]["partial"] is True
    assert result["result"]["counts"]["not_attempted"] == 1
    assert calls == []
    report = store.read_json(REPORT_PATH)
    assert report["partial"] is True
    assert "budget_exhausted" in report["limitations"]
    assert report["items"][0]["skipped_reason"] == "deep_entity_budget_exhausted"
    assert store.read_manifest()["state"] == "partial"


def test_repeated_selection_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    path = write_selection(store, selection_document(store, [selection_item(unread)]))

    first, _calls = collect_with_selection(store, path, monkeypatch)
    second, calls = collect_with_selection(store, path, monkeypatch)

    assert first["result"]["counts"]["fetched"] == 1
    assert second["result"]["counts"]["fetched"] == 0
    assert second["result"]["counts"]["skipped_unchanged"] == 1
    assert calls == []
    assert store.read_manifest()["budget_consumed"]["deep_entities"] == 2


def test_invalid_selection_fails_clearly(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    path = write_selection(
        store,
        selection_document(
            store,
            [selection_item(unread), selection_item(unread)],
        ),
    )
    manifest = store.read_manifest()

    with pytest.raises(ValidationFailure) as error:
        load_agent_selection(store, manifest, path=path)
    assert "重复" in error.value.message

    missing = selection_document(
        store,
        [selection_item("github.com/acme/demo/pull/9999")],
    )
    with pytest.raises(ValidationFailure):
        load_agent_selection(store, manifest, path=write_selection(store, missing))


def test_selection_run_id_mismatch_fails(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    document = selection_document(store, [])
    document["run_id"] = "20260101T000000Z-deadbeef"
    path = write_selection(store, document)

    with pytest.raises(ValidationFailure):
        load_agent_selection(store, store.read_manifest(), path=path)


def test_prepare_marks_agent_selected_and_keeps_selection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    path = write_selection(store, selection_document(store, [selection_item(unread)]))
    collect_with_selection(store, path, monkeypatch)
    selection_bytes = path.read_bytes()

    normalize_helpers.prepare(store)

    assert path.read_bytes() == selection_bytes
    candidates = store.read_json("analysis/candidates.json")
    item = next(
        candidate
        for candidate in candidates["candidates"]
        if candidate["entity_id"] == unread
    )
    assert item["agent_selected"] is True
    assert item["agent_depth"] == "deep"
    assert item["agent_read"]["diff"] is False
    assert candidates["counts"]["agent_selected"] == 1
    pack_index = store.read_json("analysis/pack-index.json")
    packed = [
        item
        for pack in pack_index["packs"]
        for item in pack["items"]
        if item["entity_id"] == unread
    ]
    assert packed and packed[0]["agent_selected"] is True


def test_verify_integrity_reports_invalid_selection(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    document = selection_document(
        store,
        [selection_item("github.com/acme/demo/pull/9999")],
    )
    write_selection(store, document)

    report = store.verify_integrity()

    problems = {item["problem"] for item in report["problems"]}
    assert "invalid_selection" in problems


def test_selection_report_budget_is_per_run(tmp_path: Path, monkeypatch) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    path = write_selection(store, selection_document(store, [selection_item(unread)]))

    collect_with_selection(store, path, monkeypatch)

    report = store.read_json(REPORT_PATH)
    assert report["budget"]["deep_entities_before"] == 1
    assert report["budget"]["deep_entities_after"] == 2
    assert report["budget"]["requests"] > 0
    assert report["budget"]["pages"] > 0
    assert json.dumps(report, ensure_ascii=False)


def test_prepare_invalidates_downstream_when_candidates_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    normalize_helpers.prepare(store)
    path = write_selection(
        store,
        selection_document(
            store,
            [selection_item("github.com/acme/demo/pull/10")],
        ),
    )
    manifest = store.read_manifest()
    manifest["stages"]["research"] = "complete"
    manifest["stages"]["validate"] = "complete"
    manifest["stages"]["render"] = "complete"
    store.write_manifest(manifest)
    before = path.read_bytes()

    normalize_helpers.prepare(store)

    manifest = store.read_manifest()
    assert manifest["stages"]["research"] == "not_started"
    assert manifest["stages"]["validate"] == "not_started"
    assert manifest["stages"]["render"] == "not_started"
    assert path.read_bytes() == before
    candidates = store.read_json("analysis/candidates.json")
    assert any(
        item["entity_id"] == "github.com/acme/demo/pull/10"
        and item["agent_selected"] is True
        for item in candidates["candidates"]
    )

    manifest["stages"]["research"] = "complete"
    manifest["stages"]["validate"] = "complete"
    manifest["stages"]["render"] = "complete"
    store.write_manifest(manifest)
    normalize_helpers.prepare(store)

    manifest = store.read_manifest()
    assert manifest["stages"]["research"] == "complete"
    assert manifest["stages"]["validate"] == "complete"
    assert manifest["stages"]["render"] == "complete"


def test_selection_false_flags_skip_section_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    path = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item(
                    unread,
                    read=read_flags(
                        body=False,
                        comments=False,
                        reviews=False,
                        files=False,
                    ),
                )
            ],
        ),
    )

    _result, calls = collect_with_selection(store, path, monkeypatch)

    report = store.read_json(REPORT_PATH)
    item = report["items"][0]
    assert item["status"] == "fetched"
    assert any("/pulls/11/commits" in endpoint for endpoint in calls)
    assert not any("/pulls/11/files" in endpoint for endpoint in calls)
    assert not any("/issues/11/comments" in endpoint for endpoint in calls)
    assert not any("/pulls/11/reviews" in endpoint for endpoint in calls)


def test_selection_backfills_previously_unread_sections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    first_selection = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item(
                    unread,
                    read=read_flags(
                        comments=False,
                        reviews=False,
                        files=False,
                    ),
                )
            ],
        ),
    )
    first, first_calls = collect_with_selection(store, first_selection, monkeypatch)
    assert first["result"]["counts"]["fetched"] == 1
    assert not any("/pulls/11/files" in endpoint for endpoint in first_calls)
    document = load_pr_document(store, 11)
    assert document is not None
    assert document["files"]["status"] == "not_requested"
    body_sha = document["body"]["sha256"]
    before_second = store.read_manifest()["budget_consumed"]["deep_entities"]

    second_selection = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item(
                    unread,
                    read=read_flags(body=False),
                )
            ],
        ),
    )
    second, second_calls = collect_with_selection(store, second_selection, monkeypatch)

    assert second["result"]["counts"]["fetched"] == 1
    assert (
        store.read_manifest()["budget_consumed"]["deep_entities"]
        == before_second
    )
    assert any("/pulls/11/files" in endpoint for endpoint in second_calls)
    assert any("/issues/11/comments" in endpoint for endpoint in second_calls)
    assert any("/pulls/11/reviews" in endpoint for endpoint in second_calls)
    document = load_pr_document(store, 11)
    assert document is not None
    assert document["files"]["status"] != "not_requested"
    assert document["issue_comments"]["items"]
    assert document["reviews"]["items"]
    assert document["body"]["sha256"] == body_sha


def test_selection_subset_is_skipped_after_full_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    unread = add_unread_pr(store, 11)
    path = write_selection(store, selection_document(store, [selection_item(unread)]))
    collect_with_selection(store, path, monkeypatch)

    subset = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item(
                    unread,
                    read=read_flags(
                        body=False,
                        comments=False,
                        reviews=False,
                        files=False,
                        diff=True,
                    ),
                )
            ],
        ),
    )
    result, calls = collect_with_selection(store, subset, monkeypatch)

    assert result["result"]["counts"]["fetched"] == 0
    assert result["result"]["counts"]["skipped_unchanged"] == 1
    assert calls == []


def test_partial_section_is_refetched_instead_of_skipped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    document = load_pr_document(store, 10)
    assert document is not None
    document["files"]["status"] = "partial"
    document["files"]["truncated"] = True
    document["files"]["truncation_reason"] = "files_truncated"
    document["coverage"]["files"] = "partial"
    document["coverage"]["detail"] = "partial"
    document["limitations"] = derive_limitations(document)
    store.write_json("raw/pr-details/10.json", document, schema_name="pr_detail")
    path = write_selection(
        store,
        selection_document(
            store,
            [selection_item("github.com/acme/demo/pull/10")],
        ),
    )

    result, calls = collect_with_selection(store, path, monkeypatch)

    assert result["result"]["counts"]["fetched"] == 1
    assert result["result"]["counts"]["skipped_unchanged"] == 0
    assert any("/pulls/10/files" in endpoint for endpoint in calls)


def test_external_entity_is_not_fetched_from_the_local_repository(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    identifier = entity_id("github.com", "other", "repo", "issue", "5")
    entities = store.read_jsonl("normalized/entities.jsonl")
    entities.append(
        make_entity(
            entity_id=identifier,
            kind="issue",
            title="External issue",
            url="https://github.com/other/repo/issues/5",
            observed_at=NOW,
            number=5,
            raw_state={"state": "open"},
            normalized_state={"external_reference": True},
            discovery_reasons=["external_reference"],
            external=True,
        )
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    path = write_selection(
        store,
        selection_document(store, [selection_item(identifier)]),
    )

    result, calls = collect_with_selection(store, path, monkeypatch)

    assert result["result"]["counts"]["skipped_unsupported"] == 1
    assert calls == []
    report = store.read_json(REPORT_PATH)
    item = report["items"][0]
    assert item["status"] == "skipped_unsupported"
    assert item["skipped_reason"] == "external_entity"
    assert not store.secure_path("raw/issues/5.json").exists()


def test_issue_read_flags_skip_comments_and_timeline(tmp_path: Path) -> None:
    store = proposal_helpers.make_store(tmp_path)
    calls: list[str] = []

    def handler(argv, input_text):
        calls.append(argv[argv.index("--include") + 1])
        return proposal_helpers.rest_handler({42: proposal_helpers.issue_body()})(
            argv, input_text
        )

    client = proposal_helpers.make_client(store, handler)
    result = _read_issue(
        proposal_helpers.context_for(client, store),
        42,
        scope="in_window",
        selection_reason=["agent_selection"],
        role_hints=[],
        observed_at=NOW,
        read=read_flags(body=False, comments=False),
        existing=None,
    )

    document = result.document
    assert result.pages == 1
    assert not any("/comments" in endpoint for endpoint in calls)
    assert not any("/timeline" in endpoint for endpoint in calls)
    assert document["body"]["sha256"] is not None
    assert document["comments"]["status"] == "not_requested"
    assert document["timeline"]["status"] == "not_requested"
    assert document["coverage"]["body"] == "complete"
    assert document["coverage"]["detail"] == "complete"

    write_issue_document(store, document)
    assert store.secure_path("raw/issues/42.json").is_file()


def test_discussion_read_flags_skip_comment_queries(tmp_path: Path) -> None:
    store = proposal_helpers.make_store(tmp_path)

    def handler(argv, input_text):
        if argv[1:3] != ["api", "graphql"]:
            raise AssertionError("unexpected rest call")
        return proposal_helpers.http_response(
            200, proposal_helpers.discussion_payload()
        )

    client = proposal_helpers.make_client(store, handler)
    result = read_discussion(
        proposal_helpers.context_for(client, store),
        7,
        scope="in_window",
        selection_reason=["agent_selection"],
        role_hints=[],
        observed_at=NOW,
        read=read_flags(body=False, comments=False),
        existing=None,
    )

    document = result.document
    assert result.pages == 1
    assert document["body"]["sha256"] is not None
    assert document["comments"]["status"] == "not_requested"
    assert document["coverage"]["body"] == "complete"
    assert document["coverage"]["detail"] == "complete"

    write_discussion_document(store, document)
    assert store.secure_path("raw/discussions/7.json").is_file()


def test_selection_persists_issue_body_when_body_flag_false(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    identifier = add_unread_proposal(store, "issue", 42, "RFC: cache")
    path = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item(
                    identifier,
                    read=read_flags(body=False, comments=False),
                )
            ],
        ),
    )
    calls: list[str] = []
    issue_handler = proposal_helpers.rest_handler(
        {42: proposal_helpers.issue_body(42)}
    )
    pr_handler = pr_helpers.default_handler(calls=calls)

    def factory(client_store, budget, **kwargs):
        from rer.github import GitHubClient

        def runner(argv, input_text):
            endpoint = argv[argv.index("--include") + 1]
            if "/issues/42" in endpoint and "/pulls/" not in endpoint:
                calls.append(endpoint)
                return issue_handler(argv, input_text)
            return pr_handler(
                endpoint,
                pr_helpers.parse_params(argv),
                pr_helpers._headers(argv),
            )

        return GitHubClient(
            client_store,
            budget,
            gh_path="gh",
            runner=runner,
            sleep=lambda _seconds: None,
            now=lambda: pr_helpers.NOW,
            initial_usage=kwargs.get("initial_usage"),
            max_attempts=1,
        )

    monkeypatch.setattr(cli, "GitHubClient", factory)
    args = cli.build_parser().parse_args(
        ["collect", "--run", str(store.run_dir), "--selection", str(path)]
    )
    result, _lines = cli.command_collect(args)

    report = store.read_json(REPORT_PATH)
    by_entity = {item["entity_id"]: item for item in report["items"]}
    assert by_entity[identifier]["status"] == "fetched"
    assert result["result"]["partial"] is False
    document = store.read_json("raw/issues/42.json")
    validate_document(document, "issue_detail")
    assert document["body"]["sha256"] is not None
    assert document["comments"]["status"] == "not_requested"
    assert document["timeline"]["status"] == "not_requested"
    assert document["coverage"]["body"] == "complete"
    assert document["coverage"]["detail"] == "complete"
    assert not any(endpoint.endswith("/comments") for endpoint in calls)
    assert not any(endpoint.endswith("/timeline") for endpoint in calls)
    entities = {
        entity["id"]: entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
    }
    pointer = entities[identifier]["raw_state"]["proposal_read"]
    assert pointer["detail_path"] == "raw/issues/42.json"


def test_selection_persists_discussion_body_when_body_flag_false(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    identifier = add_unread_proposal(store, "discussion", 7, "Scheduler ownership")
    path = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item(
                    identifier,
                    read=read_flags(body=False, comments=False),
                )
            ],
        ),
    )
    calls: list[str] = []
    pr_handler = pr_helpers.default_handler(calls=calls)

    def factory(client_store, budget, **kwargs):
        from rer.github import GitHubClient

        def runner(argv, input_text):
            if argv[1:3] == ["api", "graphql"]:
                calls.append("graphql")
                return proposal_helpers.http_response(
                    200,
                    proposal_helpers.discussion_payload(7),
                )
            endpoint = argv[argv.index("--include") + 1]
            return pr_handler(
                endpoint,
                pr_helpers.parse_params(argv),
                pr_helpers._headers(argv),
            )

        return GitHubClient(
            client_store,
            budget,
            gh_path="gh",
            runner=runner,
            sleep=lambda _seconds: None,
            now=lambda: pr_helpers.NOW,
            initial_usage=kwargs.get("initial_usage"),
            max_attempts=1,
        )

    monkeypatch.setattr(cli, "GitHubClient", factory)
    args = cli.build_parser().parse_args(
        ["collect", "--run", str(store.run_dir), "--selection", str(path)]
    )
    result, _lines = cli.command_collect(args)

    report = store.read_json(REPORT_PATH)
    by_entity = {item["entity_id"]: item for item in report["items"]}
    assert by_entity[identifier]["status"] == "fetched"
    assert result["result"]["partial"] is False
    document = store.read_json("raw/discussions/7.json")
    validate_document(document, "discussion_detail")
    assert document["body"]["sha256"] is not None
    assert document["comments"]["status"] == "not_requested"
    assert document["coverage"]["body"] == "complete"
    assert document["coverage"]["detail"] == "complete"
    assert calls == ["graphql"]
    entities = {
        entity["id"]: entity
        for entity in store.read_jsonl("normalized/entities.jsonl")
    }
    pointer = entities[identifier]["raw_state"]["proposal_read"]
    assert pointer["detail_path"] == "raw/discussions/7.json"


def test_pr_budget_exhaustion_does_not_skip_later_issue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    first = add_unread_pr(store, 11)
    second = add_unread_pr(store, 12)
    issue_identifier = entity_id("github.com", "acme", "demo", "issue", "42")
    entities = store.read_jsonl("normalized/entities.jsonl")
    entities.append(
        make_entity(
            entity_id=issue_identifier,
            kind="issue",
            title="RFC: cache",
            url="https://github.com/acme/demo/issues/42",
            observed_at=NOW,
            number=42,
            raw_state={"state": "open", "labels": ["rfc"], "comments": 0},
            normalized_state={"role_hints": ["rfc"], "scope": "in_window"},
        )
    )
    store.write_jsonl(
        "normalized/entities.jsonl",
        entities,
        key="id",
        schema_name="entity",
    )
    config = store.read_json("config.resolved.json")
    config["budget"]["max_deep_prs"] = 1
    config["config_hash"] = compute_config_hash(config)
    store.write_json("config.resolved.json", config, schema_name="config")
    manifest = store.read_manifest()
    manifest["config_hash"] = config["config_hash"]
    store.write_manifest(manifest)
    path = write_selection(
        store,
        selection_document(
            store,
            [
                selection_item(first),
                selection_item(second),
                selection_item(issue_identifier),
            ],
        ),
    )
    calls: list[str] = []
    pr_handler = pr_helpers.default_handler(calls=calls)
    issue_handler = proposal_helpers.rest_handler(
        {42: proposal_helpers.issue_body(42)}
    )

    def factory(client_store, budget, **kwargs):
        from rer.github import GitHubClient

        def runner(argv, input_text):
            endpoint = argv[argv.index("--include") + 1]
            if "/issues/42" in endpoint and "/pulls/" not in endpoint:
                calls.append(endpoint)
                return issue_handler(argv, input_text)
            return pr_handler(
                endpoint,
                pr_helpers.parse_params(argv),
                pr_helpers._headers(argv),
            )

        return GitHubClient(
            client_store,
            budget,
            gh_path="gh",
            runner=runner,
            sleep=lambda _seconds: None,
            now=lambda: pr_helpers.NOW,
            initial_usage=kwargs.get("initial_usage"),
            max_attempts=1,
        )

    monkeypatch.setattr(cli, "GitHubClient", factory)
    args = cli.build_parser().parse_args(
        ["collect", "--run", str(store.run_dir), "--selection", str(path)]
    )
    result, _lines = cli.command_collect(args)

    report = store.read_json(REPORT_PATH)
    by_entity = {item["entity_id"]: item for item in report["items"]}
    assert by_entity[second]["skipped_reason"] == "deep_pr_budget_exhausted"
    assert by_entity[issue_identifier]["status"] == "fetched"
    assert any("/issues/42" in endpoint for endpoint in calls)
    assert "deep_pr_budget_exhausted" in report["limitations"]
    assert result["result"]["partial"] is True


def test_partial_comments_reuse_complete_files_and_commits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    document = load_pr_document(store, 10)
    assert document is not None
    document["issue_comments"]["status"] = "partial"
    document["issue_comments"]["truncated"] = True
    document["issue_comments"]["truncation_reason"] = "comments_truncated"
    document["coverage"]["issue_comments"] = "partial"
    document["coverage"]["detail"] = "partial"
    document["limitations"] = derive_limitations(document)
    store.write_json("raw/pr-details/10.json", document, schema_name="pr_detail")
    path = write_selection(
        store,
        selection_document(
            store,
            [selection_item("github.com/acme/demo/pull/10")],
        ),
    )

    _result, calls = collect_with_selection(store, path, monkeypatch)

    assert not any("/pulls/10/files" in endpoint for endpoint in calls)
    assert not any("/pulls/10/commits" in endpoint for endpoint in calls)
    assert any("/issues/10/comments" in endpoint for endpoint in calls)


def test_permanently_truncated_section_stops_consuming_deep_entities(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    original = load_pr_document(store, 10)
    assert original is not None
    original["files"]["status"] = "partial"
    original["files"]["truncated"] = True
    original["files"]["truncation_reason"] = "files_truncated"
    original["coverage"]["files"] = "partial"
    original["coverage"]["detail"] = "partial"
    original["limitations"] = derive_limitations(original)
    store.write_json("raw/pr-details/10.json", original, schema_name="pr_detail")
    changed = json.loads(json.dumps(original))
    changed["title"] = "Changed title"
    responses = [changed, json.loads(json.dumps(changed))]

    def fake_fetch_pr(*args, **kwargs):
        return responses.pop(0), 1, True

    from rer import selection_protocol

    monkeypatch.setattr(selection_protocol, "_fetch_pr", fake_fetch_pr)
    path = write_selection(
        store,
        selection_document(
            store,
            [selection_item("github.com/acme/demo/pull/10")],
        ),
    )

    first, _calls = collect_with_selection(store, path, monkeypatch)
    after_first = store.read_manifest()["budget_consumed"]["deep_entities"]
    second, _calls = collect_with_selection(store, path, monkeypatch)
    after_second = store.read_manifest()["budget_consumed"]["deep_entities"]

    assert first["result"]["counts"]["fetched"] == 1
    assert second["result"]["counts"]["skipped_unchanged"] == 1
    assert after_second == after_first


def test_real_collector_skips_unchanged_unavailable_section(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _code, _run_dir, store = pr_helpers._run_cli_collect(
        tmp_path,
        monkeypatch,
        pr_helpers.default_handler(),
    )
    document = load_pr_document(store, 10)
    assert document is not None
    document["files"]["status"] = "partial"
    document["files"]["truncated"] = True
    document["files"]["truncation_reason"] = "files_truncated"
    document["coverage"]["files"] = "partial"
    document["coverage"]["detail"] = "partial"
    document["limitations"] = derive_limitations(document)
    store.write_json("raw/pr-details/10.json", document, schema_name="pr_detail")
    calls: list[str] = []
    base = pr_helpers.default_handler(calls=calls)

    def handler(endpoint, params, headers):
        if endpoint.endswith("/files"):
            calls.append(endpoint)
            return pr_helpers.http_response(404, {"message": "Not Found"})
        return base(endpoint, params, headers)

    path = write_selection(
        store,
        selection_document(
            store,
            [selection_item("github.com/acme/demo/pull/10")],
        ),
    )

    first, _first_calls = collect_with_selection(store, path, monkeypatch, handler=handler)
    after_first = store.read_manifest()["budget_consumed"]["deep_entities"]
    second, _second_calls = collect_with_selection(
        store, path, monkeypatch, handler=handler
    )
    after_second = store.read_manifest()["budget_consumed"]["deep_entities"]

    assert first["result"]["counts"]["fetched"] == 1
    assert second["result"]["counts"]["skipped_unchanged"] == 1
    report = store.read_json(REPORT_PATH)
    assert report["items"][0]["skipped_reason"] == "materials_unchanged"
    assert after_second == after_first


def test_incremental_observation_extends_window_but_noop_preserves_it(tmp_path, monkeypatch):
    _, _, store = pr_helpers._run_cli_collect(
        tmp_path, monkeypatch, pr_helpers.default_handler(),
    )
    before = store.read_manifest()
    unread = add_unread_pr(store, 11)
    path = write_selection(store, selection_document(store, [selection_item(unread)]))
    finished = "2026-11-07T08:02:54Z"
    monkeypatch.setattr(cli, "to_iso_z", lambda _: finished)
    result, _ = collect_with_selection(store, path, monkeypatch)
    assert result["result"]["counts"]["fetched"] == 1
    after = store.read_manifest()
    assert after["collection_finished_at"] == finished
    assert after["observed"]["finished_at"] == finished
    assert after["observed"]["started_at"] == before["observed"]["started_at"]
    monkeypatch.setattr(cli, "to_iso_z", lambda _: "2026-11-08T00:00:00Z")
    _, calls = collect_with_selection(store, path, monkeypatch)
    assert calls == []
    assert store.read_manifest()["observed"] == after["observed"]
