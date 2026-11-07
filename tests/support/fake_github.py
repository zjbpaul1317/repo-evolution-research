from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, unquote

from rer.github import CommandResult

OWNER = "acme"
NAME = "demo"
REVISION = "b" * 40
PR_SHA = "c" * 40
TAG_COMMIT = "e" * 40
ISSUE_NUMBER = 40
PR_NUMBER = 1
RELEASE_ID = 900
RELEASE_TAG = "v1.0.0"
WINDOW_CREATED = "2026-09-15T10:00:00Z"
WINDOW_UPDATED = "2026-10-20T10:00:00Z"
RELEASE_TIME = "2026-10-01T00:00:00Z"

README = "# Demo\n\nA small repository for offline research tests.\n"
DESIGN = (
    "# Cache design\n\n"
    "The project needs an explicit cleanup API so callers can release cache entries.\n"
    "The implementation keeps eviction behind a small interface.\n"
)
PR_BODY = (
    "This PR implements the cache cleanup API skeleton described in the design doc.\n\n"
    "It adds a cleanup entry point and keeps eviction policy unchanged.\n"
)
ISSUE_BODY = (
    "RFC: add an explicit cache cleanup API.\n\n"
    "Callers need a deterministic way to release cache entries before shutdown.\n"
)
RELEASE_BODY = "v1.0.0 adds the cache cleanup API and documentation.\n"


def _response(
    body: Any,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> CommandResult:
    reason = {200: "OK", 404: "Not Found", 403: "Forbidden"}.get(status, "OK")
    lines = [f"HTTP/2.0 {status} {reason}"]
    lines.extend(f"{key}: {value}" for key, value in (headers or {}).items())
    lines.append("")
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    lines.append(text)
    return CommandResult(0 if status < 400 else 1, "\n".join(lines), "")


def _endpoint(argv: list[str]) -> str:
    if len(argv) > 2 and argv[2] == "graphql":
        return "graphql"
    try:
        return argv[argv.index("--include") + 1]
    except (ValueError, IndexError):
        return "unknown"


def _params(argv: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for index, token in enumerate(argv[:-1]):
        if token in ("-f", "-F"):
            key, _, value = argv[index + 1].partition("=")
            params[key] = value
    return params


class FakeGitHub:
    """Deterministic gh runner for one small repository."""

    def __init__(self) -> None:
        self.unrouted: list[str] = []
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, argv: list[str], input_text: str | None) -> CommandResult:
        endpoint = _endpoint(argv)
        params = _params(argv)
        self.requests.append((endpoint, params))
        if endpoint == "graphql":
            self.unrouted.append(endpoint)
            return _response({"message": "graphql not configured"}, 404)

        repo = f"repos/{OWNER}/{NAME}"
        if endpoint == repo:
            return _response(self._repository())
        if endpoint == f"{repo}/git/ref/heads/main":
            return _response({"object": {"sha": REVISION, "type": "commit"}})
        if endpoint.startswith(f"{repo}/git/trees/"):
            return _response(self._tree())
        if endpoint == f"{repo}/labels":
            return _response([{"name": "rfc"}, {"name": "enhancement"}])
        if endpoint == "search/issues":
            return _response(self._search(params.get("q", "")))
        if endpoint == f"{repo}/pulls/{PR_NUMBER}":
            return _response(self._pull_request())
        if endpoint == f"{repo}/pulls/{PR_NUMBER}/files":
            return _response(self._files())
        if endpoint == f"{repo}/pulls/{PR_NUMBER}/commits":
            return _response(self._commits())
        if endpoint == f"{repo}/issues/{PR_NUMBER}/comments":
            return _response([])
        if endpoint == f"{repo}/pulls/{PR_NUMBER}/reviews":
            return _response([])
        if endpoint == f"{repo}/pulls/{PR_NUMBER}/comments":
            return _response([])
        if endpoint == f"{repo}/issues/{ISSUE_NUMBER}":
            return _response(self._issue())
        if endpoint == f"{repo}/issues/{ISSUE_NUMBER}/comments":
            return _response([])
        if endpoint == f"{repo}/issues/{ISSUE_NUMBER}/timeline":
            return _response([])
        if endpoint == f"{repo}/releases":
            return _response([self._release()])
        if endpoint == f"{repo}/git/ref/tags/{quote(RELEASE_TAG, safe='')}":
            return _response({"object": {"sha": TAG_COMMIT, "type": "commit"}})
        if endpoint.startswith(f"{repo}/contents/"):
            return self._content(endpoint[len(f"{repo}/contents/") :])
        self.unrouted.append(endpoint)
        return _response({"message": "unrouted"}, 404)

    def _repository(self) -> dict[str, Any]:
        return {
            "id": 1,
            "node_id": "R_demo",
            "name": NAME,
            "full_name": f"{OWNER}/{NAME}",
            "owner": {"login": OWNER},
            "html_url": f"https://github.com/{OWNER}/{NAME}",
            "default_branch": "main",
            "archived": False,
            "language": "Python",
            "created_at": "2025-01-01T00:00:00Z",
            "updated_at": WINDOW_UPDATED,
        }

    def _tree(self) -> dict[str, Any]:
        return {
            "sha": REVISION,
            "truncated": False,
            "tree": [
                {"path": "README.md", "type": "blob", "sha": "1" * 40, "size": len(README)},
                {
                    "path": "docs/design.md",
                    "type": "blob",
                    "sha": "2" * 40,
                    "size": len(DESIGN),
                },
                {"path": "src/core.py", "type": "blob", "sha": "3" * 40, "size": 100},
            ],
        }

    def _search(self, query: str) -> dict[str, Any]:
        if "is:merged" in query:
            items = [self._pr_item()]
        elif "is:pr" in query:
            items = [self._pr_item()]
        elif "is:issue" in query:
            items = [self._issue_item()]
        else:
            items = []
        return {"total_count": len(items), "incomplete_results": False, "items": items}

    def _pr_item(self) -> dict[str, Any]:
        return {
            "number": PR_NUMBER,
            "title": "Add cache cleanup API",
            "state": "closed",
            "created_at": WINDOW_CREATED,
            "updated_at": WINDOW_UPDATED,
            "closed_at": WINDOW_UPDATED,
            "merged_at": WINDOW_UPDATED,
            "html_url": f"https://github.com/{OWNER}/{NAME}/pull/{PR_NUMBER}",
            "user": {"login": "alice"},
            "labels": [{"name": "enhancement"}],
            "pull_request": {"merged_at": WINDOW_UPDATED},
        }

    def _issue_item(self) -> dict[str, Any]:
        return {
            "number": ISSUE_NUMBER,
            "title": "RFC: cache cleanup API",
            "state": "open",
            "created_at": WINDOW_CREATED,
            "updated_at": WINDOW_UPDATED,
            "html_url": f"https://github.com/{OWNER}/{NAME}/issues/{ISSUE_NUMBER}",
            "user": {"login": "bob"},
            "labels": [{"name": "rfc"}],
            "comments": 0,
        }

    def _pull_request(self) -> dict[str, Any]:
        return {
            "number": PR_NUMBER,
            "title": "Add cache cleanup API",
            "body": PR_BODY,
            "state": "closed",
            "merged": True,
            "merged_at": WINDOW_UPDATED,
            "draft": False,
            "created_at": WINDOW_CREATED,
            "updated_at": WINDOW_UPDATED,
            "html_url": f"https://github.com/{OWNER}/{NAME}/pull/{PR_NUMBER}",
            "user": {"login": "alice"},
            "labels": [{"name": "enhancement"}],
            "base": {"ref": "main", "sha": REVISION},
            "head": {"ref": "cache-cleanup", "sha": PR_SHA},
            "merge_commit_sha": "d" * 40,
            "additions": 10,
            "deletions": 2,
            "changed_files": 1,
            "commits": 1,
            "comments": 0,
            "review_comments": 0,
        }

    def _files(self) -> list[dict[str, Any]]:
        return [
            {
                "filename": "src/core.py",
                "status": "modified",
                "additions": 10,
                "deletions": 2,
                "changes": 12,
                "sha": "4" * 40,
                "patch": "@@ -1,2 +1,3 @@\n-old\n+new\n+cleanup\n",
            }
        ]

    def _commits(self) -> list[dict[str, Any]]:
        return [
            {
                "sha": PR_SHA,
                "html_url": f"https://github.com/{OWNER}/{NAME}/commit/{PR_SHA}",
                "commit": {
                    "message": "Add cache cleanup API",
                    "author": {"name": "Alice", "date": WINDOW_UPDATED},
                },
                "author": {"login": "alice"},
            }
        ]

    def _issue(self) -> dict[str, Any]:
        return {
            "number": ISSUE_NUMBER,
            "title": "RFC: cache cleanup API",
            "body": ISSUE_BODY,
            "state": "open",
            "state_reason": None,
            "created_at": WINDOW_CREATED,
            "updated_at": WINDOW_UPDATED,
            "html_url": f"https://github.com/{OWNER}/{NAME}/issues/{ISSUE_NUMBER}",
            "user": {"login": "bob"},
            "labels": [{"name": "rfc"}],
            "comments": 0,
            "reactions": {"total_count": 0},
        }

    def _release(self) -> dict[str, Any]:
        return {
            "id": RELEASE_ID,
            "tag_name": RELEASE_TAG,
            "name": RELEASE_TAG,
            "body": RELEASE_BODY,
            "draft": False,
            "prerelease": False,
            "created_at": RELEASE_TIME,
            "published_at": RELEASE_TIME,
            "html_url": f"https://github.com/{OWNER}/{NAME}/releases/tag/{RELEASE_TAG}",
            "target_commitish": "main",
        }

    def _content(self, encoded_path: str) -> CommandResult:
        path = unquote(encoded_path)
        contents = {
            "README.md": README,
            "docs/design.md": DESIGN,
        }
        text = contents.get(path)
        if text is None:
            return _response({"message": "not found"}, 404)
        raw = text.encode("utf-8")
        return _response(
            {
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "sha": "5" * 40,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode("ascii"),
                "html_url": f"https://github.com/{OWNER}/{NAME}/blob/{REVISION}/{path}",
            }
        )
