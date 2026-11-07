from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rer.errors import (
    BudgetExceededError,
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    UsageError,
)
from rer.github import (
    CommandResult,
    GitHubClient,
    default_runner,
    parse_include_output,
    probe_repository,
    sanitize_url,
    validate_headers,
)
from rer.store import create_run, new_run_id

NOW = datetime(2026, 11, 1, 8, 0, 0, tzinfo=UTC)
BUDGET = {
    "max_requests": 50,
    "max_discovery_entities": 100,
    "max_deep_entities": 10,
    "max_relation_hops": 2,
    "max_external_entities": 5,
    "max_response_bytes": 1024 * 1024,
    "max_total_bytes": 8 * 1024 * 1024,
    "max_runtime_minutes": 35,
    "max_pack_chars": 100000,
}


def http_response(
    status: int,
    body: Any,
    headers: dict[str, str] | None = None,
) -> CommandResult:
    lines = [f"HTTP/2.0 {status} OK"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append(json.dumps(body))
    return CommandResult(0 if status < 400 else 1, "\n".join(lines), "")


def make_client(
    tmp_path: Path,
    runner,
    budget: dict[str, Any] | None = None,
    initial_usage: dict[str, Any] | None = None,
):
    repository = {"host": "github.com", "owner": "o", "name": "r"}
    store = create_run(tmp_path, repository, new_run_id(NOW))
    sleeps: list[float] = []
    client = GitHubClient(
        store,
        budget or BUDGET,
        gh_path="gh",
        runner=runner,
        sleep=sleeps.append,
        now=lambda: NOW,
        initial_usage=initial_usage,
        max_attempts=2,
    )
    return client, store, sleeps


def test_rest_pagination_and_request_ledger(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(argv, input_text):
        endpoint = argv[argv.index("--include") + 1]
        calls.append(endpoint)
        if endpoint.endswith("/labels"):
            return http_response(
                200,
                [{"name": "bug"}],
                {"Link": '<https://api.github.com/repos/o/r/labels?page=2>; rel="next"'},
            )
        return http_response(200, [{"name": "rfc"}])

    client, store, _ = make_client(tmp_path, runner)
    pages = list(client.iter_rest_pages("repos/o/r/labels", params={"per_page": 100}))
    assert [page.data for page in pages] == [[{"name": "bug"}], [{"name": "rfc"}]]
    assert [page.page for page in pages] == [1, 2]
    assert calls[0] == "repos/o/r/labels"
    assert "page=2" in calls[1]

    requests = store.read_jsonl("requests.jsonl")
    assert len(requests) == 2
    assert {record["transport"] for record in requests} == {"rest"}
    assert all(record["status"] == 200 for record in requests)
    assert len(store.read_jsonl("raw/index.jsonl")) == 2


def test_graphql_pagination(tmp_path: Path) -> None:
    def runner(argv, input_text):
        assert argv[1:4] == ["api", "graphql", "--include"]
        payload = json.loads(input_text)
        cursor = payload["variables"].get("cursor")
        nodes = [{"title": "one"}] if cursor is None else [{"title": "two"}]
        return http_response(
            200,
            {
                "data": {
                    "repository": {
                        "discussions": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": cursor is None,
                                "endCursor": "next" if cursor is None else None,
                            },
                        }
                    }
                }
            },
        )

    client, store, _ = make_client(tmp_path, runner)
    pages = list(
        client.iter_graphql_pages(
            (
                "query($cursor: String) { repository { discussions { "
                "nodes { title } pageInfo { hasNextPage endCursor } } } }"
            ),
            variables={"owner": "o", "name": "r"},
            connection_path=("repository", "discussions"),
        )
    )
    assert [page.nodes for page in pages] == [[{"title": "one"}], [{"title": "two"}]]
    assert len(store.read_jsonl("requests.jsonl")) == 2


def test_graphql_200_with_errors_is_partial_not_fatal(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(
            200,
            {"data": {"repository": {}}, "errors": [{"message": "partial failure"}]},
        )

    client, _, _ = make_client(tmp_path, runner)
    result = client.graphql("query { repository { name } }")
    assert result.partial is True
    assert result.errors[0]["message"] == "partial failure"


@pytest.mark.parametrize(
    ("status", "headers", "body", "error_class"),
    [
        (403, {}, {"message": "Bad credentials"}, GitHubAuthError),
        (404, {}, {"message": "Not Found"}, GitHubNotFoundError),
        (500, {}, {"message": "server error"}, GitHubUnavailableError),
    ],
)
def test_http_errors_are_classified(
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
    body: dict[str, Any],
    error_class: type[Exception],
) -> None:
    def runner(argv, input_text):
        return http_response(status, body, headers)

    client, _, _ = make_client(tmp_path, runner)
    with pytest.raises(error_class):
        client.rest("repos/o/r")


def test_rate_limit_retries_with_retry_after(tmp_path: Path) -> None:
    calls = {"count": 0}

    def runner(argv, input_text):
        calls["count"] += 1
        if calls["count"] == 1:
            return http_response(429, {"message": "rate limit"}, {"Retry-After": "0"})
        return http_response(200, {"ok": True})

    client, store, sleeps = make_client(tmp_path, runner)
    assert client.rest("repos/o/r") == {"ok": True}
    assert sleeps == [0]
    assert len(store.read_jsonl("requests.jsonl")) == 2


def test_rate_limit_without_retry_budget_raises(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(429, {"message": "rate limit"}, {"Retry-After": "3600"})

    client, _, _ = make_client(tmp_path, runner)
    with pytest.raises(GitHubRateLimitError):
        client.rest("repos/o/r")


@pytest.mark.parametrize("body", [None, {"message": "slow down"}])
def test_429_without_rate_limit_hints_is_still_rate_limited(
    tmp_path: Path,
    body: dict[str, Any] | None,
) -> None:
    calls = {"count": 0}

    def runner(argv, input_text):
        calls["count"] += 1
        if body is None:
            return CommandResult(1, "HTTP/2.0 429 Too Many Requests\n\n", "")
        return http_response(429, body)

    client, store, sleeps = make_client(tmp_path, runner)
    with pytest.raises(GitHubRateLimitError):
        client.rest("repos/o/r")
    assert calls["count"] == 2
    assert sleeps == [1]
    assert all(
        record["error"] == "rate_limited"
        for record in store.read_jsonl("requests.jsonl")
    )


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"x-ratelimit-remaining": "0"}, {}),
        ({}, {"message": "You have exceeded a secondary rate limit"}),
    ],
)
def test_403_secondary_rate_limit_is_classified(
    tmp_path: Path,
    headers: dict[str, str],
    body: dict[str, Any],
) -> None:
    def runner(argv, input_text):
        return http_response(403, body, headers)

    client, _, _ = make_client(tmp_path, runner)
    with pytest.raises(GitHubRateLimitError):
        client.rest("repos/o/r")


def test_pagination_limit_is_enforced(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(
            200,
            [{"name": "x"}],
            {"Link": '<https://api.github.com/repos/o/r/labels?page=2>; rel="next"'},
        )

    client, _, _ = make_client(tmp_path, runner)
    client.max_pages = 2
    with pytest.raises(BudgetExceededError):
        list(client.iter_rest_pages("repos/o/r/labels"))


def test_transport_timeout_is_unavailable(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return CommandResult(124, "", "gh api timed out")

    client, _, _ = make_client(tmp_path, runner)
    with pytest.raises(GitHubUnavailableError):
        client.rest("repos/o/r")


def test_initial_usage_counts_toward_budget(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"ok": True})

    budget = {**BUDGET, "max_requests": 2}
    client, _, _ = make_client(
        tmp_path,
        runner,
        budget,
        initial_usage={"requests": 2},
    )
    with pytest.raises(BudgetExceededError):
        client.rest("repos/o/r")


def test_deep_entities_used_combines_initial_and_attempt(tmp_path: Path) -> None:
    client, _, _ = make_client(
        tmp_path,
        lambda argv, input_text: http_response(200, {"ok": True}),
        initial_usage={"deep_entities": 3},
    )
    client.usage["deep_entities"] = 2

    assert client.deep_entities_used() == 5


def test_request_budget_is_enforced(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"ok": True})

    budget = {**BUDGET, "max_requests": 1}
    client, store, _ = make_client(tmp_path, runner, budget)
    client.rest("repos/o/r")
    with pytest.raises(BudgetExceededError):
        client.rest("repos/o/r")
    assert len(store.read_jsonl("requests.jsonl")) == 1


def test_response_size_budget_is_enforced(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"data": "x" * 100})

    budget = {**BUDGET, "max_response_bytes": 10}
    client, store, _ = make_client(tmp_path, runner, budget)
    with pytest.raises(BudgetExceededError):
        client.rest("repos/o/r")
    records = store.read_jsonl("requests.jsonl")
    assert records[0]["error"] == "response_too_large"
    assert records[0]["blob_sha256"] is None


def test_request_ledger_redacts_url_tokens(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"ok": True})

    client, store, _ = make_client(tmp_path, runner)
    client.rest("https://api.github.com/repos/o/r?token=secret")
    record = store.read_jsonl("requests.jsonl")[0]
    assert "secret" not in record["endpoint"]
    assert "redacted" in record["endpoint"].lower()


def test_sensitive_params_are_rejected(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"ok": True})

    client, store, _ = make_client(tmp_path, runner)
    with pytest.raises(UsageError):
        client.rest("repos/o/r", params={"token": "secret"})
    with pytest.raises(UsageError):
        client.graphql(
            "query($token: String) { viewer { login } }",
            variables={"token": "secret"},
        )
    assert store.read_jsonl("requests.jsonl") == []


def test_probe_repository_reports_access(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"default_branch": "main", "archived": False})

    result = probe_repository("gh", "o", "r", runner=runner)
    assert result["status"] == "ok"
    assert result["default_branch"] == "main"


def test_parse_include_output_handles_status_and_body() -> None:
    status, headers, body = parse_include_output(
        "HTTP/2.0 200 OK\nContent-Type: application/json\nX-Test: 1\n\n{\"ok\": true}"
    )
    assert status == 200
    assert headers["x-test"] == "1"
    assert json.loads(body) == {"ok": True}


@pytest.mark.parametrize(
    "next_url",
    [
        "http://api.github.com/repos/o/r/labels?page=2",
        "https://evil.example/repos/o/r/labels?page=2",
        "https://user:pass@api.github.com/repos/o/r/labels?page=2",
        "https://api.github.com/repos/o/r/labels?page=2#fragment",
    ],
)
def test_unsafe_link_target_is_rejected(tmp_path: Path, next_url: str) -> None:
    calls: list[str] = []

    def runner(argv, input_text):
        endpoint = argv[argv.index("--include") + 1]
        calls.append(endpoint)
        return http_response(
            200,
            [{"name": "x"}],
            {"Link": f'<{next_url}>; rel="next"'},
        )

    client, _, _ = make_client(tmp_path, runner)
    with pytest.raises(GitHubError):
        list(client.iter_rest_pages("repos/o/r/labels"))
    assert calls == ["repos/o/r/labels"]


def test_sanitize_url_strips_userinfo_and_redacts_query() -> None:
    cleaned = sanitize_url("https://user:pass@api.github.com/x?token=secret&page=2")
    assert "user" not in cleaned
    assert "pass" not in cleaned
    assert "secret" not in cleaned
    assert "page=2" in cleaned
    assert cleaned.startswith("https://api.github.com/x?")


def test_probe_repository_rejects_non_object_body() -> None:
    def runner(argv, input_text):
        return http_response(200, [])

    result = probe_repository("gh", "o", "r", runner=runner)
    assert result["status"] == "error"
    assert "JSON 对象" in result["message"]


def test_graphql_query_and_cost_are_audited(tmp_path: Path) -> None:
    query = "query { viewer { login } }"

    def runner(argv, input_text):
        return http_response(
            200,
            {
                "data": {
                    "viewer": {"login": "x"},
                    "rateLimit": {
                        "cost": 2,
                        "remaining": 9,
                        "resetAt": "2026-11-01T09:00:00Z",
                    },
                }
            },
        )

    client, store, _ = make_client(tmp_path, runner)
    client.graphql(query, variables={"owner": "o", "name": "r"})
    record = store.read_jsonl("requests.jsonl")[0]
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert record["query_sha256"] == digest
    assert record["query_blob_sha256"] == digest
    assert store.read_blob(digest) == query.encode("utf-8")
    assert record["graphql_cost"] == 2
    assert record["graphql_remaining"] == 9
    assert record["graphql_reset_at"] == "2026-11-01T09:00:00Z"
    assert record["logical_request_id"]
    index = store.read_jsonl("raw/index.jsonl")[0]
    assert index["query_sha256"] == digest


def test_graphql_retry_shares_logical_request_id(tmp_path: Path) -> None:
    calls = {"count": 0}

    def runner(argv, input_text):
        calls["count"] += 1
        if calls["count"] == 1:
            return http_response(500, {"message": "boom"})
        return http_response(200, {"data": {"viewer": {"login": "x"}}})

    client, store, sleeps = make_client(tmp_path, runner)
    client.graphql("query { viewer { login } }")
    records = store.read_jsonl("requests.jsonl")
    assert len(records) == 2
    assert records[0]["logical_request_id"] == records[1]["logical_request_id"]
    assert records[0]["request_id"] != records[1]["request_id"]
    assert {record["attempt"] for record in records} == {1, 2}
    assert sleeps == [1]


def test_graphql_missing_cursor_is_an_error(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(
            200,
            {
                "data": {
                    "repository": {
                        "discussions": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True, "endCursor": None},
                        }
                    }
                }
            },
        )

    client, _, _ = make_client(tmp_path, runner)
    with pytest.raises(GitHubError):
        list(
            client.iter_graphql_pages(
                "query { repository { discussions { nodes { title } } } }",
                connection_path=("repository", "discussions"),
            )
        )


def test_5xx_retries_then_raises(tmp_path: Path) -> None:
    calls = {"count": 0}

    def runner(argv, input_text):
        calls["count"] += 1
        return http_response(500, {"message": "boom"})

    client, _, sleeps = make_client(tmp_path, runner)
    with pytest.raises(GitHubUnavailableError):
        client.rest("repos/o/r")
    assert calls["count"] == 2
    assert sleeps == [1]


def test_default_runner_decodes_utf8() -> None:
    result = default_runner([sys.executable, "-c", "print('中文')"], None)
    assert result.returncode == 0
    assert result.stdout.strip() == "中文"


def test_initial_runtime_is_treated_as_seconds(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"ok": True})

    client, _, _ = make_client(
        tmp_path,
        runner,
        initial_usage={"runtime_seconds": 35},
    )
    assert client.rest("repos/o/r") == {"ok": True}


def test_initial_runtime_blocks_after_cumulative_limit(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"ok": True})

    client, store, _ = make_client(
        tmp_path,
        runner,
        initial_usage={"runtime_seconds": 35 * 60},
    )
    with pytest.raises(BudgetExceededError):
        client.rest("repos/o/r")
    assert store.read_jsonl("requests.jsonl") == []


def test_initial_total_bytes_counts_toward_budget(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"ok": True})

    budget = {**BUDGET, "max_total_bytes": 100}
    client, store, _ = make_client(
        tmp_path,
        runner,
        budget,
        initial_usage={"total_bytes": 100},
    )
    with pytest.raises(BudgetExceededError):
        client.rest("repos/o/r")
    assert store.read_jsonl("requests.jsonl") == []


def test_total_bytes_accumulates_across_attempts(tmp_path: Path) -> None:
    def runner(argv, input_text):
        return http_response(200, {"data": "x" * 50})

    budget = {**BUDGET, "max_total_bytes": 100}
    client, store, _ = make_client(
        tmp_path,
        runner,
        budget,
        initial_usage={"total_bytes": 60},
    )
    with pytest.raises(BudgetExceededError):
        client.rest("repos/o/r")
    records = store.read_jsonl("requests.jsonl")
    assert len(records) == 1


def test_custom_headers_are_validated_and_forwarded(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(argv, input_text):
        seen.append(argv)
        return http_response(200, {"ok": True})

    client, _, _ = make_client(tmp_path, runner, BUDGET)
    assert client.rest("repos/o/r", headers={"Accept": "application/vnd.github.v3.diff"}) == {
        "ok": True
    }
    assert "Accept: application/vnd.github.v3.diff" in seen[0]

    with pytest.raises(GitHubError):
        client.rest("repos/o/r", headers={"Accept": "x\r\nX-Injected: 1"})
    with pytest.raises(GitHubError):
        validate_headers({"Bad Header": "value"})

    def diff_runner(argv, input_text):
        return CommandResult(0, "HTTP/2.0 200 OK\n\ndiff --git", "")

    text_client, _, _ = make_client(tmp_path, diff_runner, BUDGET)
    assert text_client.rest_text("repos/o/r") == "diff --git"
