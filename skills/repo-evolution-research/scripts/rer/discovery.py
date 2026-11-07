"""Discover repository structure, documentation and collaboration capabilities."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

from .errors import GitHubAuthError, GitHubNotFoundError
from .github import GitHubClient, parse_link_header, redact_secrets
from .models import SCHEMA_VERSION
from .store import SnapshotStore

_RFC_PATTERNS = (
    re.compile(r"^(?:rfcs?|proposals?|enhancements?)/.+\.(?:md|markdown|rst|txt)$", re.I),
    re.compile(r"^docs?/rfcs?/.+", re.I),
)
_DESIGN_PATTERNS = (
    re.compile(r"^docs?/design/.+", re.I),
    re.compile(r"^design/.+", re.I),
)
_DOC_PREFIXES = ("docs/", "doc/", "documentation/")
_EXCLUDED_MODULE_ROOTS = {
    ".github",
    ".git",
    "docs",
    "doc",
    "documentation",
    "tests",
    "test",
    "examples",
    "scripts",
    "benchmarks",
    "benchmark",
}


def fetch_repository_metadata(client: GitHubClient, owner: str, name: str) -> dict[str, Any]:
    return client.rest(f"repos/{owner}/{name}")


def fetch_default_branch_sha(
    client: GitHubClient,
    owner: str,
    name: str,
    branch: str,
) -> str:
    encoded = quote(branch, safe="/")
    data = client.rest(f"repos/{owner}/{name}/git/ref/heads/{encoded}")
    obj = data.get("object") if isinstance(data, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not sha:
        raise ValueError(f"默认分支 {branch!r} 无法解析 SHA")
    return sha


def fetch_tree(client: GitHubClient, owner: str, name: str, sha: str) -> dict[str, Any]:
    data = client.rest(
        f"repos/{owner}/{name}/git/trees/{sha}",
        params={"recursive": 1},
    )
    if not isinstance(data, dict):
        raise ValueError("tree 响应结构异常")
    return data


def fetch_labels(
    client: GitHubClient,
    owner: str,
    name: str,
    *,
    limit: int = 500,
) -> tuple[list[str], bool, int, str | None]:
    names: list[str] = []
    pages = 0
    truncated = False
    next_url: str | None = None
    for page in client.iter_rest_pages(
        f"repos/{owner}/{name}/labels",
        params={"per_page": 100},
    ):
        pages += 1
        if not isinstance(page.data, list):
            continue
        for label in page.data:
            if isinstance(label, dict) and isinstance(label.get("name"), str):
                names.append(label["name"])
        if len(names) >= limit:
            next_url = parse_link_header(page.headers.get("link")).get("next")
            truncated = bool(next_url)
            break
    return sorted(set(names)), truncated, pages, next_url


def fetch_discussions(client: GitHubClient, owner: str, name: str) -> dict[str, Any]:
    query = """
query($owner: String!, $name: String!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    hasDiscussionsEnabled
    discussions(first: 1) { totalCount }
    discussionCategories(first: 20) { totalCount nodes { name } }
  }
}
""".strip()
    result = client.graphql(query, variables={"owner": owner, "name": name})
    redacted_errors = [
        redact_secrets(str(error.get("message", error))) for error in result.errors[:5]
    ]
    repository = result.data.get("repository")
    if not isinstance(repository, dict):
        return {
            "status": "unavailable",
            "enabled": None,
            "total_count": None,
            "categories": [],
            "categories_truncated": False,
            "errors": redacted_errors or ["repository 不在 GraphQL 响应中"],
        }
    discussions = repository.get("discussions")
    categories = repository.get("discussionCategories")
    enabled = repository.get("hasDiscussionsEnabled")
    category_nodes = categories.get("nodes") if isinstance(categories, dict) else []
    category_names = [
        node["name"]
        for node in (category_nodes or [])
        if isinstance(node, dict) and isinstance(node.get("name"), str)
    ]
    category_total = categories.get("totalCount") if isinstance(categories, dict) else None
    if result.errors:
        status = "partial"
    elif enabled is False:
        status = "not_applicable"
    else:
        status = "complete"
    return {
        "status": status,
        "enabled": enabled,
        "total_count": discussions.get("totalCount") if isinstance(discussions, dict) else None,
        "categories": category_names,
        "categories_truncated": (
            isinstance(category_total, int) and category_total > len(category_names)
        ),
        "errors": redacted_errors,
    }


def classify_paths(paths: Iterable[str]) -> dict[str, Any]:
    blobs = list(paths)

    def basename(path: str) -> str:
        return path.rsplit("/", 1)[-1].lower()

    def limited(
        items: Iterable[str],
        limit: int,
    ) -> tuple[list[str], dict[str, Any]]:
        ordered = sorted(set(items))
        return ordered[:limit], {
            "total": len(ordered),
            "returned": min(len(ordered), limit),
            "limit": limit,
            "truncated": len(ordered) > limit,
        }

    rfc_paths, rfc_truncation = limited(
        (path for path in blobs if any(pattern.match(path) for pattern in _RFC_PATTERNS)),
        200,
    )
    design_paths, design_truncation = limited(
        (path for path in blobs if any(pattern.match(path) for pattern in _DESIGN_PATTERNS)),
        100,
    )
    docs_paths, docs_truncation = limited(
        (path for path in blobs if path.lower().startswith(_DOC_PREFIXES)),
        200,
    )
    module_roots, module_truncation = limited(
        (
            path.split("/", 1)[0]
            for path in blobs
            if "/" in path and path.split("/", 1)[0] not in _EXCLUDED_MODULE_ROOTS
        ),
        50,
    )
    roadmap_files, roadmap_truncation = limited(
        (path for path in blobs if "roadmap" in basename(path)),
        20,
    )
    architecture_files, architecture_truncation = limited(
        (
            path
            for path in blobs
            if "architecture" in basename(path) or "/architecture/" in path.lower()
        ),
        20,
    )
    contributing_files, contributing_truncation = limited(
        (path for path in blobs if basename(path).startswith("contributing")),
        10,
    )
    changelog_files, changelog_truncation = limited(
        (path for path in blobs if basename(path).startswith("changelog")),
        10,
    )
    issue_templates, issue_template_truncation = limited(
        (
            path
            for path in blobs
            if path.lower().startswith((".github/issue_template/", ".github/issue_templates/"))
        ),
        50,
    )
    pull_request_templates, pull_request_template_truncation = limited(
        (path for path in blobs if basename(path).startswith("pull_request_template")),
        10,
    )
    readme_files, readme_truncation = limited(
        (path for path in blobs if "/" not in path and basename(path).startswith("readme")),
        10,
    )
    truncations = {
        "rfc_paths": rfc_truncation,
        "design_paths": design_truncation,
        "docs_paths": docs_truncation,
        "module_roots": module_truncation,
        "roadmap_files": roadmap_truncation,
        "architecture_files": architecture_truncation,
        "contributing_files": contributing_truncation,
        "changelog_files": changelog_truncation,
        "issue_templates": issue_template_truncation,
        "pull_request_templates": pull_request_template_truncation,
        "readme_files": readme_truncation,
    }
    return {
        "rfc_paths": rfc_paths,
        "design_paths": design_paths,
        "docs_paths": docs_paths,
        "roadmap_files": roadmap_files,
        "architecture_files": architecture_files,
        "contributing_files": contributing_files,
        "changelog_files": changelog_files,
        "issue_templates": issue_templates,
        "pull_request_templates": pull_request_templates,
        "readme_files": readme_files,
        "module_roots": module_roots,
        "truncations": truncations,
    }


def select_key_files(
    classification: Mapping[str, Any],
    *,
    include_docs: bool,
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    candidates = [
        ("readme", classification.get("readme_files", [])[:1]),
        ("contributing", classification.get("contributing_files", [])[:1]),
        ("architecture", classification.get("architecture_files", [])[:1]),
        ("roadmap", classification.get("roadmap_files", [])[:1]),
        ("changelog", classification.get("changelog_files", [])[:1]),
    ]
    if include_docs:
        candidates.extend(
            [
                ("rfc", classification.get("rfc_paths", [])[:2]),
                ("design", classification.get("design_paths", [])[:1]),
            ]
        )
    seen: set[str] = set()
    for kind, paths in candidates:
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            selected.append({"kind": kind, "path": path})
    return selected[:8]


def read_text_file(
    client: GitHubClient,
    store: SnapshotStore,
    owner: str,
    name: str,
    path: str,
    ref: str,
    *,
    max_bytes: int = 512 * 1024,
    auth_fatal: bool = False,
) -> dict[str, Any]:
    encoded_path = quote(path, safe="/")
    try:
        data = client.rest(
            f"repos/{owner}/{name}/contents/{encoded_path}",
            params={"ref": ref},
        )
    except GitHubNotFoundError:
        return {"path": path, "status": "not_found"}
    except GitHubAuthError:
        if auth_fatal:
            raise
        return {"path": path, "status": "unavailable"}

    if not isinstance(data, dict):
        return {"path": path, "status": "unsupported"}
    size = data.get("size")
    if isinstance(size, int) and size > max_bytes:
        return {"path": path, "status": "too_large", "size": size}

    content = data.get("content")
    if not isinstance(content, str) or data.get("encoding") != "base64":
        return {"path": path, "status": "unsupported", "size": size}

    try:
        raw = base64.b64decode(content)
    except (binascii.Error, ValueError):
        return {"path": path, "status": "decode_error", "size": size}
    if len(raw) > max_bytes:
        return {"path": path, "status": "too_large", "size": len(raw)}

    blob_sha = store.save_blob(raw)
    fallback_url = (
        f"https://github.com/{owner}/{name}/blob/"
        f"{quote(ref, safe='')}/{quote(path, safe='/')}"
    )
    url = data.get("html_url") or fallback_url
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "path": path,
            "status": "unsupported_encoding",
            "size": len(raw),
            "blob_sha256": blob_sha,
            "sha": data.get("sha"),
            "url": url,
        }
    return {
        "path": path,
        "status": "read",
        "size": len(raw),
        "chars": len(text),
        "blob_sha256": blob_sha,
        "sha": data.get("sha"),
        "url": url,
    }


def build_source_profile(
    repository: Mapping[str, Any],
    *,
    owner: str,
    name: str,
    default_branch: str,
    default_branch_sha: str,
    tree: Mapping[str, Any],
    classification: Mapping[str, Any],
    labels: list[str],
    labels_truncated: bool,
    discussions: Mapping[str, Any],
    files: list[dict[str, Any]],
    observed_at: str,
) -> dict[str, Any]:
    entries = tree.get("tree")
    total_entries = len(entries) if isinstance(entries, list) else 0
    return {
        "schema_version": SCHEMA_VERSION,
        "repository": {
            "id": repository.get("id"),
            "node_id": repository.get("node_id"),
            "owner": owner,
            "name": name,
            "full_name": repository.get("full_name") or None,
            "url": repository.get("html_url") or None,
            "description": repository.get("description"),
            "homepage": repository.get("homepage"),
            "topics": repository.get("topics") or [],
            "default_branch": default_branch,
            "default_branch_sha": default_branch_sha,
            "archived": repository.get("archived"),
            "language": repository.get("language"),
            "created_at": repository.get("created_at"),
            "updated_at": repository.get("updated_at"),
            "pushed_at": repository.get("pushed_at"),
        },
        "discovery": {
            "tree_truncated": bool(tree.get("truncated")),
            "tree_total_entries": total_entries,
            "rfc_paths": classification.get("rfc_paths", []),
            "design_paths": classification.get("design_paths", []),
            "docs_paths": classification.get("docs_paths", []),
            "roadmap_files": classification.get("roadmap_files", []),
            "architecture_files": classification.get("architecture_files", []),
            "contributing_files": classification.get("contributing_files", []),
            "changelog_files": classification.get("changelog_files", []),
            "issue_templates": classification.get("issue_templates", []),
            "pull_request_templates": classification.get("pull_request_templates", []),
            "module_roots": classification.get("module_roots", []),
            "truncations": classification.get("truncations", {}),
            "labels": labels,
            "labels_truncated": labels_truncated,
            "discussions": {
                **discussions,
                "capability_status": discussions.get("status"),
                "items_status": "not_requested",
            },
            "deferred": ["releases", "docs_site", "release_habits"],
            "files": files,
        },
        "observed_at": observed_at,
    }
