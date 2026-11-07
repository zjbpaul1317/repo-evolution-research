"""Collect repository metadata, tree and capability discovery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..discovery import (
    build_source_profile,
    classify_paths,
    fetch_default_branch_sha,
    fetch_discussions,
    fetch_labels,
    fetch_repository_metadata,
    fetch_tree,
    read_text_file,
    select_key_files,
)
from ..errors import BudgetExceededError, GitHubError, GitHubRateLimitError, ToolError
from ..github import GitHubClient
from ..models import make_entity, repository_id, to_iso_z, validate_document
from ..store import SnapshotStore
from .progress import CollectProgress, coverage_entry


@dataclass
class RepositoryCollection:
    entity: dict[str, Any]
    source_profile: dict[str, Any]
    coverage: list[dict[str, Any]]
    default_branch: str
    default_branch_sha: str


def collect_repository(
    client: GitHubClient,
    store: SnapshotStore,
    repository: dict[str, Any],
    observed_at: str,
    *,
    include_docs: bool = True,
    include_discussions: bool = True,
    progress: CollectProgress | None = None,
    clock: Callable[[], str] | None = None,
) -> RepositoryCollection:
    owner = repository["owner"]
    name = repository["name"]
    coverage: list[dict[str, Any]] = []

    def now() -> str:
        return clock() if clock is not None else to_iso_z(datetime.now(UTC))

    def record(entry: dict[str, Any]) -> None:
        coverage.append(entry)
        if progress is not None:
            progress.record(entry)

    started = now()
    repo = fetch_repository_metadata(client, owner, name)
    finished = now()
    if not isinstance(repo, dict):
        raise GitHubError(
            "repository 响应不是 JSON 对象",
            source="repository.metadata",
        )
    record(
        coverage_entry(
            channel="repository",
            query_scope="metadata",
            query=f"GET repos/{owner}/{name}",
            expected=1,
            fetched=1,
            pages=1,
            observed_start=started,
            observed_end=finished,
        )
    )

    default_branch = repo.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise ToolError(
            "仓库没有默认分支，可能为空仓库或初始化尚未完成",
            source="repository.default_branch",
            recovery="确认仓库非空后重新运行 collect",
        )

    started = now()
    default_branch_sha = fetch_default_branch_sha(client, owner, name, default_branch)
    finished = now()
    if progress is not None:
        progress.set_state("default_branch", default_branch)
        progress.set_state("default_branch_sha", default_branch_sha)
    record(
        coverage_entry(
            channel="default_branch",
            query_scope="default_branch",
            query=f"GET repos/{owner}/{name}/git/ref/heads/{default_branch}",
            expected=1,
            fetched=1,
            pages=1,
            observed_start=started,
            observed_end=finished,
        )
    )

    started = now()
    tree = fetch_tree(client, owner, name, default_branch_sha)
    finished = now()
    entries = tree.get("tree") if isinstance(tree.get("tree"), list) else []
    tree_truncated = bool(tree.get("truncated"))
    paths = [
        entry["path"]
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("type") == "blob"
        and isinstance(entry.get("path"), str)
    ]
    classification = classify_paths(paths)
    record(
        coverage_entry(
            channel="tree",
            query_scope="default_branch",
            query=f"GET repos/{owner}/{name}/git/trees/{default_branch_sha}?recursive=1",
            expected=None if tree_truncated else len(entries),
            fetched=len(entries),
            pages=1,
            exhausted=not tree_truncated,
            truncation_reason="tree_truncated" if tree_truncated else None,
            coverage_status="partial" if tree_truncated else "complete",
            observed_start=started,
            observed_end=finished,
        )
    )

    truncations = classification.get("truncations", {})
    capped_discovery = [
        group
        for group in ("rfc_paths", "design_paths", "docs_paths")
        if isinstance(truncations.get(group), dict)
        and truncations[group].get("truncated")
    ]
    sampled_auxiliary = [
        group
        for group, info in truncations.items()
        if group not in capped_discovery
        and isinstance(info, dict)
        and info.get("truncated")
    ]
    if tree_truncated or capped_discovery:
        discovery_status = "partial"
    elif sampled_auxiliary:
        discovery_status = "sampled"
    else:
        discovery_status = "complete"
    discovery_reasons = []
    if tree_truncated:
        discovery_reasons.append("tree_truncated")
    if capped_discovery:
        discovery_reasons.append("capped:" + ",".join(capped_discovery))
    if sampled_auxiliary:
        discovery_reasons.append("sampled:" + ",".join(sampled_auxiliary))
    record(
        coverage_entry(
            channel="discovery_paths",
            query_scope="default_branch",
            query="classify tree paths",
            expected=len(paths),
            fetched=len(paths),
            pages=1,
            exhausted=not tree_truncated,
            truncation_reason="; ".join(discovery_reasons) or None,
            coverage_status=discovery_status,
            observed_start=started,
            observed_end=finished,
        )
    )

    labels: list[str] = []
    labels_truncated = False
    label_cursor: str | None = None
    started = now()
    try:
        labels, labels_truncated, label_pages, label_cursor = fetch_labels(
            client,
            owner,
            name,
        )
        if progress is not None and label_cursor:
            progress.set_cursor("labels", {"next": label_cursor})
    except BudgetExceededError:
        raise
    except GitHubError as exc:
        finished = now()
        record(
            coverage_entry(
                channel="labels",
                query_scope="repository",
                query=f"GET repos/{owner}/{name}/labels",
                expected=None,
                fetched=0,
                pages=0,
                exhausted=False,
                truncation_reason=exc.code,
                coverage_status="unavailable",
                observed_start=started,
                observed_end=finished,
            )
        )
    else:
        finished = now()
        record(
            coverage_entry(
                channel="labels",
                query_scope="repository",
                query=f"GET repos/{owner}/{name}/labels",
                expected=None,
                fetched=len(labels),
                pages=label_pages,
                exhausted=not labels_truncated,
                truncation_reason="labels_limit" if labels_truncated else None,
                coverage_status="sampled" if labels_truncated else "complete",
                remaining_cursor=label_cursor,
                observed_start=started,
                observed_end=finished,
            )
        )

    started = now()
    if include_discussions:
        try:
            discussions = fetch_discussions(client, owner, name)
        except BudgetExceededError:
            raise
        except GitHubError as exc:
            discussions = {
                "status": "unavailable",
                "enabled": None,
                "total_count": None,
                "categories": [],
                "categories_truncated": False,
                "errors": [exc.code],
            }
        finished = now()
        discussion_status = str(discussions.get("status") or "unavailable")
        capability_status = (
            discussion_status
            if discussion_status in ("complete", "partial", "unavailable", "not_applicable")
            else "unavailable"
        )
        coverage_status = capability_status
        truncation_reason = (
            None
            if capability_status in ("complete", "not_applicable")
            else capability_status
        )
        exhausted = capability_status in ("complete", "not_applicable")
        if discussions.get("categories_truncated"):
            exhausted = False
            if coverage_status == "complete":
                coverage_status = "sampled"
                truncation_reason = "categories_limit"
            elif truncation_reason:
                truncation_reason = f"{truncation_reason}+categories_limit"
            else:
                truncation_reason = "categories_limit"
        record(
            coverage_entry(
                channel="discussions_capability",
                query_scope="repository",
                query="GraphQL repository.discussions(capability)",
                expected=1,
                fetched=1 if capability_status in ("complete", "partial", "not_applicable") else 0,
                pages=1 if capability_status != "unavailable" else 0,
                exhausted=exhausted,
                truncation_reason=truncation_reason,
                coverage_status=coverage_status,
                detail_status="not_requested",
                observed_start=started,
                observed_end=finished,
            )
        )
    else:
        discussions = {
            "status": "not_requested",
            "enabled": None,
            "total_count": None,
            "categories": [],
            "categories_truncated": False,
            "errors": [],
        }
        finished = now()
        record(
            coverage_entry(
                channel="discussions_capability",
                query_scope="repository",
                query="GraphQL repository.discussions(capability)",
                expected=None,
                fetched=0,
                pages=0,
                exhausted=False,
                coverage_status="not_requested",
                detail_status="not_requested",
                observed_start=started,
                observed_end=finished,
            )
        )

    record(
        coverage_entry(
            channel="discussions_items",
            query_scope="repository",
            query="GraphQL repository.discussions(items)",
            expected=None,
            fetched=0,
            pages=0,
            exhausted=False,
            coverage_status="not_requested",
            detail_status="not_requested",
            observed_start=started,
            observed_end=finished,
        )
    )

    files: list[dict[str, Any]] = []
    selected = select_key_files(classification, include_docs=include_docs)
    started = now()
    for item in selected:
        try:
            result = read_text_file(
                client,
                store,
                owner,
                name,
                item["path"],
                default_branch_sha,
            )
        except BudgetExceededError:
            raise
        except GitHubError as exc:
            files.append(
                {
                    "path": item["path"],
                    "kind": item["kind"],
                    "status": "unavailable",
                    "reason": exc.code,
                }
            )
            if isinstance(exc, GitHubRateLimitError):
                break
            continue
        result["kind"] = item["kind"]
        files.append(result)
    finished = now()
    read_count = sum(1 for item in files if item.get("status") == "read")
    if not selected:
        files_status = "not_applicable"
    elif read_count == len(selected):
        files_status = "complete"
    else:
        files_status = "partial"
    record(
        coverage_entry(
            channel="files",
            query_scope="default_branch",
            query="GET repos/{owner}/{name}/contents/<selected paths>",
            expected=len(selected),
            fetched=read_count,
            pages=len(files),
            exhausted=len(files) == len(selected),
            truncation_reason=(
                None
                if files_status in ("complete", "not_applicable")
                else "some_selected_files_unreadable"
            ),
            coverage_status=files_status,
            detail_status=files_status,
            observed_start=started,
            observed_end=finished,
        )
    )

    entity = make_entity(
        entity_id=repository_id("github.com", owner, name),
        kind="repository",
        title=repo.get("full_name") or f"{owner}/{name}",
        url=repo.get("html_url") or f"https://github.com/{owner}/{name}",
        observed_at=observed_at,
        raw_state=repo,
        normalized_state={
            "default_branch": default_branch,
            "default_branch_sha": default_branch_sha,
            "archived": repo.get("archived"),
            "language": repo.get("language"),
        },
        node_id=repo.get("node_id"),
        created_at=repo.get("created_at"),
        updated_at=repo.get("updated_at"),
        external=False,
    )
    validate_document(entity, "entity")
    store.upsert_jsonl(
        "normalized/entities.jsonl",
        [entity],
        key="id",
        schema_name="entity",
    )

    profile = build_source_profile(
        repo,
        owner=owner,
        name=name,
        default_branch=default_branch,
        default_branch_sha=default_branch_sha,
        tree=tree,
        classification=classification,
        labels=labels,
        labels_truncated=labels_truncated,
        discussions=discussions,
        files=files,
        observed_at=observed_at,
    )
    store.write_json(
        "analysis/source-profile.json",
        profile,
        schema_name="source_profile",
    )

    return RepositoryCollection(
        entity=entity,
        source_profile=profile,
        coverage=coverage,
        default_branch=default_branch,
        default_branch_sha=default_branch_sha,
    )
