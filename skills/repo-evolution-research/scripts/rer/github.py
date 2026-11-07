"""GitHub access through the authenticated gh CLI with audit and budget controls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .errors import (
    BudgetExceededError,
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    ToolError,
    UsageError,
)
from .models import to_iso_z
from .store import SnapshotStore

_HEADER_NAME_RE = re.compile(r"\A[A-Za-z0-9-]+\Z")

Runner = Callable[[list[str], str | None], "CommandResult"]

_HTTP_STATUS_RE = re.compile(r"^HTTP/\S+\s+(\d{3})[^\n]*$", re.MULTILINE)
_LINK_RE = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')
_TOKEN_RE = re.compile(r"\b(?:gh[pous]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{20,})\b")
_SENSITIVE_KEYS = {
    "access_token",
    "auth",
    "authorization",
    "client_secret",
    "key",
    "password",
    "private_token",
    "secret",
    "token",
}


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class HttpResponse:
    status: int | None
    headers: dict[str, str]
    body: str
    request_id: str


@dataclass
class RestPage:
    data: Any
    status: int
    headers: dict[str, str]
    request_id: str
    page: int


@dataclass
class GraphQLResult:
    data: dict[str, Any]
    errors: list[dict[str, Any]]
    request_id: str
    page: int

    @property
    def partial(self) -> bool:
        return bool(self.errors)


@dataclass
class GraphQLPage:
    nodes: list[dict[str, Any]]
    page_info: dict[str, Any]
    errors: list[dict[str, Any]]
    request_id: str
    page: int


def default_runner(argv: list[str], input_text: str | None) -> CommandResult:
    environment = dict(os.environ)
    environment["GH_HOST"] = "github.com"
    try:
        completed = subprocess.run(
            argv,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
            shell=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "gh api timed out")
    except OSError as exc:
        return CommandResult(127, "", f"{type(exc).__name__}: {exc}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def redact_secrets(text: str) -> str:
    redacted = _TOKEN_RE.sub("<redacted>", text)
    return re.sub(
        r"(?i)\b(access_token|token|secret|password|client_secret|private_token)=([^&\s]+)",
        r"\1=<redacted>",
        redacted,
    )


def sanitize_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    if not params:
        return {}
    return {
        key: ("<redacted>" if key.lower() in _SENSITIVE_KEYS else value)
        for key, value in params.items()
    }


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    query = [
        (key, "<redacted>" if key.lower() in _SENSITIVE_KEYS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))


def reject_sensitive_params(
    params: Mapping[str, Any] | None,
    *,
    source: str,
) -> None:
    if not params:
        return
    fields = sorted(
        str(key) for key in params if str(key).lower() in _SENSITIVE_KEYS
    )
    if fields:
        raise UsageError(
            "请求参数包含敏感字段",
            source=source,
            recovery="使用 gh 认证，不要在参数中传递令牌或密码",
            details={"fields": fields},
        )


def validate_headers(headers: Mapping[str, str] | None) -> Mapping[str, str]:
    for name, value in (headers or {}).items():
        if (
            not isinstance(name, str)
            or not _HEADER_NAME_RE.fullmatch(name)
            or not isinstance(value, str)
            or any(char in value for char in "\r\n")
        ):
            raise GitHubError(
                "非法请求头",
                source="github.headers",
                details={"name": str(name)[:40]},
            )
    return headers or {}


def validate_next_url(url: str) -> None:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise GitHubError(
            "Link 分页目标端口非法",
            source="github.link",
            details={"url": sanitize_url(url)},
        ) from exc
    if (
        parts.scheme != "https"
        or parts.hostname != "api.github.com"
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or port not in (None, 443)
    ):
        raise GitHubError(
            "不安全的 Link 分页目标",
            source="github.link",
            details={"url": sanitize_url(url)},
            recovery="检查代理或服务端响应；只允许 https://api.github.com",
        )


def parse_include_output(text: str) -> tuple[int | None, dict[str, str], str]:
    """Parse `gh api --include` stdout into status, headers and body."""
    normalized = text.replace("\r\n", "\n")
    matches = list(_HTTP_STATUS_RE.finditer(normalized))
    if not matches:
        return None, {}, normalized
    last = matches[-1]
    status = int(last.group(1))
    rest = normalized[last.end() :]
    separator = rest.find("\n\n")
    if separator < 0:
        header_text, body = rest, ""
    else:
        header_text, body = rest[:separator], rest[separator + 2 :]
    headers: dict[str, str] = {}
    for line in header_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        headers[key] = f"{headers[key]}, {value}" if key in headers else value
    return status, headers, body


def parse_link_header(value: str | None) -> dict[str, str]:
    return {rel: url for url, rel in _LINK_RE.findall(value or "")}


def probe_repository(
    gh_path: str,
    owner: str,
    name: str,
    *,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Lightweight access probe for doctor; no snapshot or budget involvement."""
    runner = runner or default_runner
    argv = [gh_path, "api", "--method", "GET", "--include", f"repos/{owner}/{name}"]
    result = runner(argv, None)
    status, _headers, body = parse_include_output(result.stdout)
    if status is None:
        message = redact_secrets((result.stderr or result.stdout).strip())[:300]
        return {"status": "error", "http_status": None, "message": message or "gh api 无响应"}
    if status == 200:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return {"status": "error", "http_status": status, "message": "仓库响应不是 JSON"}
        if not isinstance(data, dict):
            return {
                "status": "error",
                "http_status": status,
                "message": "仓库响应不是 JSON 对象",
            }
        return {
            "status": "ok",
            "http_status": status,
            "default_branch": data.get("default_branch"),
            "archived": data.get("archived"),
        }
    if status == 404:
        return {"status": "not_found", "http_status": status}
    if status in (401, 403):
        return {
            "status": "forbidden",
            "http_status": status,
            "message": redact_secrets(body.strip())[:300],
        }
    return {
        "status": "error",
        "http_status": status,
        "message": redact_secrets(body.strip())[:300],
    }


class GitHubClient:
    """A bounded, auditable gh CLI client for REST and GraphQL."""

    def __init__(
        self,
        store: SnapshotStore,
        budget: Mapping[str, Any],
        *,
        gh_path: str | None = None,
        runner: Runner | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        initial_usage: Mapping[str, Any] | None = None,
        max_attempts: int = 3,
        max_rate_wait: int = 60,
        max_pages: int = 100,
    ) -> None:
        self.gh_path = gh_path or shutil.which("gh")
        if not self.gh_path:
            raise ToolError(
                "未找到 gh CLI",
                source="github.client",
                recovery="安装并认证 GitHub CLI 后重试",
            )
        self.store = store
        self.budget = dict(budget)
        self.initial_usage = dict(initial_usage or {})
        self.runner = runner or default_runner
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(UTC))
        self.max_attempts = max(1, max_attempts)
        self.max_rate_wait = max(0, max_rate_wait)
        self.max_pages = max(1, max_pages)
        self.started_monotonic = time.monotonic()
        self.usage: dict[str, int] = {
            "requests": 0,
            "response_bytes": 0,
            "total_bytes": 0,
            "deep_entities": 0,
        }
        self._counter = 0

    # ---------------------------------------------------------------- public

    def elapsed_seconds(self) -> float:
        return round(time.monotonic() - self.started_monotonic, 3)

    def usage_snapshot(self) -> dict[str, float | int]:
        return {**self.usage, "runtime_seconds": self.elapsed_seconds()}

    def deep_entities_used(self) -> int:
        return int(self.initial_usage.get("deep_entities", 0) or 0) + int(
            self.usage.get("deep_entities", 0) or 0
        )

    def rest(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = self._rest_page(endpoint, params, page=1, headers=headers)
        return self._decode_json(response.body, endpoint)

    def rest_text(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """Fetch a non-JSON REST payload (for example a raw diff)."""
        response = self._rest_page(endpoint, params, page=1, headers=headers)
        return response.body

    def iter_rest_pages(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> Iterator[RestPage]:
        url = endpoint
        query = dict(params or {})
        page = 1
        while True:
            response = self._rest_page(url, query, page=page)
            data = self._decode_json(response.body, url)
            yield RestPage(
                data=data,
                status=response.status or 0,
                headers=response.headers,
                request_id=response.request_id,
                page=page,
            )
            next_url = parse_link_header(response.headers.get("link")).get("next")
            if not next_url:
                return
            validate_next_url(next_url)
            if page >= self.max_pages:
                raise BudgetExceededError(
                    f"分页超过上限 {self.max_pages}",
                    source="github.pagination",
                    details={"endpoint": sanitize_url(url)},
                )
            url = next_url
            query = {}
            page += 1

    def graphql(
        self,
        query: str,
        *,
        variables: Mapping[str, Any] | None = None,
        page: int = 1,
    ) -> GraphQLResult:
        reject_sensitive_params(variables, source="github.graphql.params")
        payload = json.dumps(
            {"query": query, "variables": dict(variables or {})},
            ensure_ascii=False,
        )
        query_bytes = query.encode("utf-8")
        query_sha256 = hashlib.sha256(query_bytes).hexdigest()
        query_blob_sha256 = self.store.save_blob(query_bytes)
        argv = [self.gh_path, "api", "graphql", "--include", "--input", "-"]
        response = self._execute(
            transport="graphql",
            endpoint="graphql",
            params=variables or {},
            page=page,
            argv=argv,
            input_text=payload,
            audit_extra={
                "query_sha256": query_sha256,
                "query_blob_sha256": query_blob_sha256,
                "query_length": len(query),
            },
        )
        try:
            document = json.loads(response.body or "{}")
        except json.JSONDecodeError as exc:
            raise GitHubError(
                "GraphQL 响应不是 JSON",
                source="github.graphql",
                details={"body": redact_secrets(response.body[:300])},
            ) from exc
        data = document.get("data") if isinstance(document, dict) else None
        errors = document.get("errors") if isinstance(document, dict) else None
        return GraphQLResult(
            data=data if isinstance(data, dict) else {},
            errors=errors if isinstance(errors, list) else [],
            request_id=response.request_id,
            page=page,
        )

    def iter_graphql_pages(
        self,
        query: str,
        *,
        variables: Mapping[str, Any] | None = None,
        connection_path: tuple[str, ...],
    ) -> Iterator[GraphQLPage]:
        cursor: str | None = None
        page = 1
        while True:
            current = dict(variables or {})
            if cursor is not None:
                current["cursor"] = cursor
            result = self.graphql(query, variables=current, page=page)
            connection = _get_path(result.data, connection_path)
            if not isinstance(connection, dict):
                yield GraphQLPage([], {}, result.errors, result.request_id, page)
                return
            nodes = connection.get("nodes")
            page_info = connection.get("pageInfo")
            yield GraphQLPage(
                [node for node in nodes or [] if isinstance(node, dict)],
                page_info if isinstance(page_info, dict) else {},
                result.errors,
                result.request_id,
                page,
            )
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                return
            cursor = page_info.get("endCursor")
            if not cursor:
                raise GitHubError(
                    "GraphQL 分页声明 hasNextPage 但缺少 endCursor",
                    source="github.pagination",
                    details={"page": page, "path": "/".join(connection_path)},
                )
            if page >= self.max_pages:
                raise BudgetExceededError(
                    f"GraphQL 分页超过上限 {self.max_pages}",
                    source="github.pagination",
                    details={"path": "/".join(connection_path)},
                )
            page += 1

    # --------------------------------------------------------------- request

    def _rest_page(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None,
        *,
        page: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        reject_sensitive_params(params, source="github.params")
        argv = [self.gh_path, "api", "--method", "GET", "--include", endpoint]
        for key in sorted(validate_headers(headers)):
            argv.extend(["-H", f"{key}: {headers[key]}"])
        for key in sorted(params or {}):
            value = (params or {})[key]
            if value is None:
                continue
            flag = "-F" if isinstance(value, (int, float, bool)) else "-f"
            argv.extend([flag, f"{key}={_gh_value(value)}"])
        return self._execute(
            transport="rest",
            endpoint=endpoint,
            params=params or {},
            page=page,
            argv=argv,
        )

    def _execute(
        self,
        *,
        transport: str,
        endpoint: str,
        params: Mapping[str, Any],
        page: int,
        argv: list[str],
        input_text: str | None = None,
        audit_extra: Mapping[str, Any] | None = None,
    ) -> HttpResponse:
        attempts = 0
        logical_request_id = self._next_request_id()
        while True:
            attempts += 1
            self._preflight()
            request_id = self._next_request_id()
            self.usage["requests"] += 1
            result = self.runner(argv, input_text)
            status, headers, body = parse_include_output(result.stdout)
            if status is None:
                body = body or result.stderr

            body_bytes = body.encode("utf-8")
            response_bytes = len(body_bytes)
            self.usage["response_bytes"] += response_bytes
            self.usage["total_bytes"] += response_bytes
            error_code = None
            retryable = False
            blob_sha = None

            if status is None:
                error_code = "transport_error"
                retryable = True
            elif status == 429:
                error_code = "rate_limited"
                retryable = True
            elif status >= 400:
                error_code = f"http_{status}"
                retryable = status >= 500
                if status == 403 and self._is_rate_limited(headers, body):
                    error_code = "rate_limited"
                    retryable = True

            record_extra = dict(audit_extra or {})
            if transport == "graphql":
                record_extra.update(self._graphql_audit(body))

            max_response_bytes = int(self.budget.get("max_response_bytes", 8 * 1024 * 1024))
            if response_bytes > max_response_bytes:
                self._record_request(
                    request_id=request_id,
                    logical_request_id=logical_request_id,
                    transport=transport,
                    endpoint=endpoint,
                    params=params,
                    page=page,
                    status=status,
                    headers=headers,
                    response_bytes=response_bytes,
                    blob_sha=None,
                    error_code="response_too_large",
                    retryable=False,
                    attempts=attempts,
                    extra=record_extra,
                )
                raise BudgetExceededError(
                    f"单个响应超过 {max_response_bytes} 字节",
                    source="github.response",
                    details={"endpoint": sanitize_url(endpoint), "bytes": response_bytes},
                )
            if body_bytes:
                blob_sha = self.store.save_blob(body_bytes)

            self._record_request(
                request_id=request_id,
                logical_request_id=logical_request_id,
                transport=transport,
                endpoint=endpoint,
                params=params,
                page=page,
                status=status,
                headers=headers,
                response_bytes=response_bytes,
                blob_sha=blob_sha,
                error_code=error_code,
                retryable=retryable,
                attempts=attempts,
                extra=record_extra,
            )

            max_total_bytes = int(self.budget.get("max_total_bytes", 150 * 1024 * 1024))
            total_bytes = self._total_bytes_used()
            if total_bytes > max_total_bytes:
                raise BudgetExceededError(
                    f"累计响应超过 {max_total_bytes} 字节",
                    source="github.response",
                    details={
                        "total_bytes": total_bytes,
                        "initial_bytes": int(self.initial_usage.get("total_bytes", 0) or 0),
                    },
                )

            if status is None:
                if attempts < self.max_attempts:
                    self.sleep(self._backoff(attempts))
                    continue
                raise GitHubUnavailableError(
                    "无法连接 GitHub API",
                    source="github.transport",
                    details={"request_id": request_id, "message": redact_secrets(body[:300])},
                )
            if 200 <= status < 300:
                return HttpResponse(status, headers, body, request_id)
            if status == 429 or (status == 403 and self._is_rate_limited(headers, body)):
                wait = self._rate_limit_wait(headers)
                if attempts < self.max_attempts and wait <= self.max_rate_wait:
                    self.sleep(wait)
                    continue
                raise GitHubRateLimitError(
                    "GitHub API 触发限流",
                    source="github.rate_limit",
                    details={
                        "request_id": request_id,
                        "retry_after": wait,
                        "message": redact_secrets(body[:300]),
                    },
                )
            if status in (401, 403):
                raise GitHubAuthError(
                    "GitHub 认证或权限不足",
                    source="github.auth",
                    details={"request_id": request_id, "status": status},
                )
            if status == 404:
                raise GitHubNotFoundError(
                    "GitHub 对象不存在或不可访问",
                    source="github.not_found",
                    details={"endpoint": sanitize_url(endpoint), "request_id": request_id},
                )
            if status >= 500:
                if attempts < self.max_attempts:
                    self.sleep(self._backoff(attempts))
                    continue
                raise GitHubUnavailableError(
                    "GitHub API 服务端错误",
                    source="github.server",
                    details={"request_id": request_id, "status": status},
                )
            raise GitHubError(
                f"GitHub API 返回 HTTP {status}",
                source="github.http",
                details={"request_id": request_id, "status": status},
            )

    def _preflight(self) -> None:
        max_requests = int(self.budget.get("max_requests", 400))
        initial_requests = int(self.initial_usage.get("requests", 0) or 0)
        total_requests = initial_requests + self.usage["requests"]
        if total_requests >= max_requests:
            raise BudgetExceededError(
                f"请求数预算已用尽（{max_requests}）",
                source="github.budget",
                details={
                    "requests": total_requests,
                    "initial_requests": initial_requests,
                },
            )
        max_minutes = int(self.budget.get("max_runtime_minutes", 35))
        initial_runtime = float(self.initial_usage.get("runtime_seconds", 0) or 0)
        elapsed_seconds = initial_runtime + (time.monotonic() - self.started_monotonic)
        if elapsed_seconds / 60 > max_minutes:
            raise BudgetExceededError(
                f"运行时间超过 {max_minutes} 分钟",
                source="github.budget",
                details={
                    "elapsed_minutes": round(elapsed_seconds / 60, 3),
                    "initial_runtime_seconds": round(initial_runtime, 3),
                },
            )
        max_total_bytes = int(self.budget.get("max_total_bytes", 150 * 1024 * 1024))
        total_bytes = self._total_bytes_used()
        if total_bytes >= max_total_bytes:
            raise BudgetExceededError(
                f"累计响应预算已用尽（{max_total_bytes}）",
                source="github.budget",
                details={
                    "total_bytes": total_bytes,
                    "initial_bytes": int(self.initial_usage.get("total_bytes", 0) or 0),
                },
            )

    def _total_bytes_used(self) -> int:
        initial = int(self.initial_usage.get("total_bytes", 0) or 0)
        return initial + int(self.usage["total_bytes"])

    @staticmethod
    def _graphql_audit(body: str) -> dict[str, Any]:
        try:
            document = json.loads(body or "{}")
        except json.JSONDecodeError:
            return {}
        if not isinstance(document, dict):
            return {}
        extra: dict[str, Any] = {}
        data = document.get("data")
        rate_limit = data.get("rateLimit") if isinstance(data, dict) else None
        if isinstance(rate_limit, dict):
            for source, target in (
                ("cost", "graphql_cost"),
                ("remaining", "graphql_remaining"),
                ("resetAt", "graphql_reset_at"),
                ("used", "graphql_used"),
                ("limit", "graphql_limit"),
            ):
                if source in rate_limit:
                    extra[target] = rate_limit[source]
        extensions = document.get("extensions")
        cost = extensions.get("cost") if isinstance(extensions, dict) else None
        if isinstance(cost, dict):
            for source, target in (
                ("actualQueryCost", "graphql_actual_cost"),
                ("requestedQueryCost", "graphql_requested_cost"),
                ("throttleStatus", "graphql_throttle_status"),
            ):
                if source in cost:
                    extra[target] = cost[source]
        return extra

    def _record_request(
        self,
        *,
        request_id: str,
        logical_request_id: str,
        transport: str,
        endpoint: str,
        params: Mapping[str, Any],
        page: int,
        status: int | None,
        headers: Mapping[str, str],
        response_bytes: int,
        blob_sha: str | None,
        error_code: str | None,
        retryable: bool,
        attempts: int,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        record = dict(extra or {})
        record.update(
            {
                "request_id": request_id,
                "logical_request_id": logical_request_id,
                "transport": transport,
                "method": "GET" if transport == "rest" else "GRAPHQL",
                "endpoint": sanitize_url(endpoint),
                "params": sanitize_params(params),
                "page": page,
                "status": status,
                "observed_at": to_iso_z(self.now()),
                "response_bytes": response_bytes,
                "blob_sha256": blob_sha,
                "rate_limit_remaining": headers.get("x-ratelimit-remaining"),
                "rate_limit_reset": headers.get("x-ratelimit-reset"),
                "retry_after": headers.get("retry-after"),
                "error": error_code,
                "retryable": retryable,
                "attempt": attempts,
            }
        )
        self.store.append_jsonl("requests.jsonl", record, key="request_id")
        index_record = {
            "request_id": request_id,
            "logical_request_id": logical_request_id,
            "transport": transport,
            "endpoint": record["endpoint"],
            "status": status,
            "blob_sha256": blob_sha,
            "observed_at": record["observed_at"],
        }
        if "query_sha256" in record:
            index_record["query_sha256"] = record["query_sha256"]
        self.store.append_jsonl("raw/index.jsonl", index_record, key="request_id")

    def _next_request_id(self) -> str:
        self._counter += 1
        return f"req-{self._counter:06d}-{secrets.token_hex(3)}"

    def _is_rate_limited(self, headers: Mapping[str, str], body: str) -> bool:
        if headers.get("x-ratelimit-remaining") == "0":
            return True
        if headers.get("retry-after"):
            return True
        lowered = body.lower()
        return "rate limit" in lowered or "secondary rate limit" in lowered

    def _rate_limit_wait(self, headers: Mapping[str, str]) -> int:
        retry_after = headers.get("retry-after", "").strip()
        if retry_after.isdigit():
            return int(retry_after)
        reset = headers.get("x-ratelimit-reset", "").strip()
        if reset.isdigit():
            return max(0, int(reset) - int(self.now().timestamp()))
        return 1

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2 ** (attempt - 1), 8)

    @staticmethod
    def _decode_json(body: str, endpoint: str) -> Any:
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitHubError(
                "REST 响应不是 JSON",
                source="github.rest",
                details={"endpoint": sanitize_url(endpoint)},
            ) from exc


def _gh_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def _get_path(document: Any, path: tuple[str, ...]) -> Any:
    current = document
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current
