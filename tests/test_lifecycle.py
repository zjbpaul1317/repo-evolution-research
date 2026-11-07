from __future__ import annotations

import copy

from rer.lifecycle import check_state_assertions, verify_capability

NOW = "2026-11-01T08:00:00Z"
HEAD = "a" * 40
MERGE = "b" * 40
REVERT_HEAD = "c" * 40
REVERT_MERGE = "d" * 40


def issue_entity(
    number: int = 40,
    *,
    state: str = "open",
    state_reason: str | None = None,
    labels: list[str] | None = None,
    comments: int = 0,
    proposal_state: str | None = None,
) -> dict:
    normalized = {"role_hints": ["rfc", "design"]}
    if proposal_state is not None:
        normalized["proposal_state"] = proposal_state
    return {
        "id": f"github.com/acme/demo/issue/{number}",
        "kind": "issue",
        "title": f"Issue {number}",
        "number": number,
        "roles": ["rfc", "design"],
        "raw_state": {
            "state": state,
            "state_reason": state_reason,
            "labels": labels or ["rfc"],
            "comments": comments,
        },
        "normalized_state": normalized,
    }


def pr_entity(
    number: int = 101,
    *,
    merged: bool = True,
    merged_at: str | None = "2026-10-20T10:00:00Z",
    state: str = "closed",
    head_sha: str = HEAD,
) -> dict:
    return {
        "id": f"github.com/acme/demo/pull/{number}",
        "kind": "pull_request",
        "title": f"PR {number}",
        "number": number,
        "roles": ["implementation"],
        "raw_state": {
            "state": state,
            "merged": merged,
            "merged_at": merged_at,
            "base": {"ref": "main", "sha": "e" * 40},
            "head": {"sha": head_sha},
        },
        "normalized_state": {"merged": merged, "merged_at": merged_at},
    }


def release_entity(tag: str = "v1.0.0") -> dict:
    return {
        "id": f"github.com/acme/demo/release/{tag}",
        "kind": "release",
        "title": tag,
        "raw_state": {"tag_name": tag, "release_id": 1},
        "normalized_state": {},
    }


def commit_entity(sha: str = HEAD) -> dict:
    return {
        "id": f"github.com/acme/demo/{sha}",
        "kind": "commit",
        "title": "commit",
        "raw_state": {"sha": sha},
        "normalized_state": {},
    }


def doc_entity(path: str = "docs/config.md") -> dict:
    revision = "f" * 40
    return {
        "id": f"github.com/acme/demo/{revision}/{path}",
        "kind": "doc",
        "title": path,
        "raw_state": {"revision": revision, "path": path},
        "normalized_state": {"revision": revision, "path": path},
    }


def evidence(identifier: str, entity_id: str, source_kind: str = "pull_request_body") -> dict:
    return {
        "id": identifier,
        "entity_id": entity_id,
        "source_kind": source_kind,
        "observed_at": NOW,
        "verification": "verified",
    }


def relation(
    identifier: str,
    relation_type: str,
    from_id: str,
    to_id: str,
    *,
    evidence_ids: list[str] | None = None,
    verification: str = "verified",
) -> dict:
    return {
        "id": identifier,
        "from_id": from_id,
        "to_id": to_id,
        "type": relation_type,
        "basis": "text_explicit",
        "verification": verification,
        "evidence_ids": evidence_ids or [],
        "scope": None,
    }


def pr_document(
    number: int = 101,
    *,
    merged: bool = True,
    merged_at: str | None = "2026-10-20T10:00:00Z",
    files_status: str = "complete",
    files_truncated: bool = False,
    diff_status: str = "not_requested",
    draft: bool = False,
    state: str = "closed",
    head_sha: str = HEAD,
    merge_sha: str = MERGE,
    commit_shas: list[str] | None = None,
) -> dict:
    return {
        "number": number,
        "merged": merged,
        "merged_at": merged_at,
        "draft": draft,
        "state": state,
        "head_sha": head_sha,
        "merge_commit_sha": merge_sha,
        "base": {"ref": "main", "sha": "e" * 40},
        "files": {
            "status": files_status,
            "truncated": files_truncated,
            "items": [],
        },
        "diff": {"status": diff_status, "truncated": False},
        "commits": {
            "status": "complete",
            "items": [{"sha": sha} for sha in commit_shas or []],
        },
    }


def release_document(
    tag: str = "v1.0.0",
    *,
    prerelease: bool = False,
    draft: bool = False,
    published_at: str | None = "2026-10-25T00:00:00Z",
) -> dict:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "published_at": published_at,
    }


def capability(**overrides) -> dict:
    value = {
        "id": "cap-001",
        "topic_id": "topic-001",
        "title": "Explicit cache cleanup",
        "scope": "",
        "proposal_state": "unknown",
        "implementation_state": "unknown",
        "delivery_state": "unknown",
        "availability": "unknown",
        "target_branch": None,
        "observed_revision": None,
        "verified_versions": [],
        "requirements": {"done": [], "remaining": []},
        "evidence_ids": ["ev-issue", "ev-pr"],
    }
    value.update(overrides)
    return value


def base_inputs(*, include_release: bool = False):
    issue = issue_entity()
    pr = pr_entity()
    commit = commit_entity()
    entities = [issue, pr, commit]
    evidence_records = [
        evidence("ev-issue", issue["id"], "issue_body"),
        evidence("ev-pr", pr["id"]),
        evidence("ev-commit", commit["id"], "commit_message"),
    ]
    relations = [
        relation("rel-part", "implements_part_of", pr["id"], issue["id"]),
        relation("rel-commit", "contains_commit", pr["id"], commit["id"]),
    ]
    if include_release:
        release = release_entity()
        entities.append(release)
        evidence_records.append(evidence("ev-release", release["id"], "release_body"))
        relations.append(
            relation(
                "rel-note",
                "release_note_mentions",
                release["id"],
                pr["id"],
                evidence_ids=["ev-release"],
            )
        )
    return entities, relations, evidence_records


def test_open_pr_is_in_progress() -> None:
    entities, relations, evidence_records = base_inputs()
    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(merged=False, merged_at=None, state="open")],
    )

    assert result["implementation_state"] == "in_progress"
    assert result["proposal_state"] == "proposed"
    assert result["delivery_state"] == "unknown"
    assert result["supporting_entities"]["proposal"] == [
        "github.com/acme/demo/issue/40"
    ]
    assert result["supporting_entities"]["commit"] == [
        f"github.com/acme/demo/{HEAD}"
    ]


def test_closed_unmerged_never_becomes_merged() -> None:
    entities, relations, evidence_records = base_inputs()
    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[
            pr_document(merged=False, merged_at=None, state="closed")
        ],
    )

    assert result["implementation_state"] == "not_started"
    assert "closed_unmerged_prs" in result["gaps"]
    assert result["implementation_state"] != "merged"


def test_merged_with_remaining_requirements_is_partial() -> None:
    entities, relations, evidence_records = base_inputs()
    cap = capability(requirements={"done": ["skeleton"], "remaining": ["cleanup"]})

    result = verify_capability(
        cap,
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )

    assert result["implementation_state"] == "partially_merged"
    issues = check_state_assertions(
        [],
        [capability(implementation_state="merged", requirements=cap["requirements"])],
        [result],
    )
    assert any(item["code"] == "merged_with_remaining_requirements" for item in issues)


def test_merged_with_requirements_done_is_merged() -> None:
    entities, relations, evidence_records = base_inputs()
    result = verify_capability(
        capability(requirements={"done": ["skeleton"], "remaining": []}),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )

    assert result["implementation_state"] == "merged"
    assert result["target_branch"] == "main"
    assert result["observed_revision"] == MERGE
    assert "diff_not_requested" in result["gaps"]


def test_revert_relation_sets_reverted() -> None:
    entities, relations, evidence_records = base_inputs()
    revert_pr = pr_entity(
        102,
        head_sha=REVERT_HEAD,
    )
    entities.append(revert_pr)
    relations.append(
        relation("rel-revert", "reverts", revert_pr["id"], "github.com/acme/demo/pull/101")
    )
    cap = capability(implementation_state="merged")

    result = verify_capability(
        cap,
        entities,
        relations,
        evidence_records,
        pr_documents=[
            pr_document(),
            pr_document(
                102,
                head_sha=REVERT_HEAD,
                merge_sha=REVERT_MERGE,
            ),
        ],
    )

    assert result["implementation_state"] == "reverted"
    issues = check_state_assertions([], [cap], [result])
    assert any(item["code"] == "reverted_merged_conflict" for item in issues)


def test_unverified_revert_relation_does_not_set_reverted() -> None:
    entities, relations, evidence_records = base_inputs()
    revert_pr = pr_entity(
        102,
        head_sha=REVERT_HEAD,
    )
    entities.append(revert_pr)
    relations.append(
        relation(
            "rel-revert",
            "reverts",
            revert_pr["id"],
            "github.com/acme/demo/pull/101",
            verification="candidate",
        )
    )

    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[
            pr_document(),
            pr_document(
                102,
                head_sha=REVERT_HEAD,
                merge_sha=REVERT_MERGE,
            ),
        ],
    )

    assert result["implementation_state"] == "merged"


def test_release_note_only_is_note_only() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document()],
    )

    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "note_only"
    assert "release_note_only" in result["gaps"]
    assert result["verified_versions"] == []


def test_verified_release_stable_and_prerelease() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    release_id = "github.com/acme/demo/release/v1.0.0"
    relations.append(
        relation("rel-verified", "verified_in_release", release_id, "github.com/acme/demo/pull/101")
    )

    stable = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document(prerelease=False)],
    )
    prerelease = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document(prerelease=True)],
    )

    assert stable["delivery_state"] == "released"
    assert stable["delivery_verification"] == "verified"
    assert stable["verified_versions"] == ["v1.0.0"]
    assert prerelease["delivery_state"] == "prerelease"
    assert prerelease["delivery_verification"] == "verified"


def test_disputed_duplicate_identity_blocks_verified_delivery() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    release_id = "github.com/acme/demo/release/v1.0.0"
    relations = [
        item
        for item in relations
        if item["type"] != "release_note_mentions"
    ]
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            release_id,
            "github.com/acme/demo/pull/101",
        )
    )
    relations.append(
        relation(
            "rel-disputed",
            "verified_in_release",
            release_id,
            "github.com/acme/demo/pull/101",
            verification="disputed",
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document()],
    )

    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "unknown"
    assert result["verified_versions"] == []


def test_unverified_release_relation_does_not_verify_delivery() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    release_id = "github.com/acme/demo/release/v1.0.0"
    relations = [
        item
        for item in relations
        if item["type"] != "release_note_mentions"
    ]
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            release_id,
            "github.com/acme/demo/pull/101",
            verification="candidate",
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document()],
    )

    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "unknown"
    assert result["verified_versions"] == []


def test_release_document_missing_is_not_verified() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    release_id = "github.com/acme/demo/release/v1.0.0"
    relations = [
        item
        for item in relations
        if item["type"] != "release_note_mentions"
    ]
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            release_id,
            "github.com/acme/demo/pull/101",
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[],
    )

    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "unknown"
    assert result["verified_versions"] == []
    assert "release_document_missing:v1.0.0" in result["gaps"]


def test_draft_release_is_not_verified() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    release_id = "github.com/acme/demo/release/v1.0.0"
    relations = [
        item
        for item in relations
        if item["type"] != "release_note_mentions"
    ]
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            release_id,
            "github.com/acme/demo/pull/101",
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document(draft=True)],
    )

    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "unknown"
    assert result["verified_versions"] == []
    assert "release_draft:v1.0.0" in result["gaps"]


def test_mixed_stable_and_prerelease_tags_prefer_released() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    entities.append(release_entity("v2.0.0"))
    relations.append(
        relation(
            "rel-prerelease",
            "verified_in_release",
            "github.com/acme/demo/release/v2.0.0",
            "github.com/acme/demo/pull/101",
        )
    )
    relations.append(
        relation(
            "rel-stable",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            "github.com/acme/demo/pull/101",
        )
    )

    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[
            release_document(prerelease=False),
            release_document("v2.0.0", prerelease=True),
        ],
    )

    assert result["delivery_state"] == "released"
    assert result["verified_versions"] == ["v1.0.0", "v2.0.0"]


def test_unverified_document_evidence_does_not_support_availability() -> None:
    entities, relations, evidence_records = base_inputs()
    doc = doc_entity()
    entities.append(doc)
    doc_evidence = evidence("ev-doc", doc["id"], "doc_content")
    doc_evidence["verification"] = "unverifiable"
    evidence_records.append(doc_evidence)

    result = verify_capability(
        capability(
            availability="default_on",
            evidence_ids=["ev-issue", "ev-pr", "ev-doc"],
        ),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )

    assert result["availability"] == "unknown"
    assert "availability_without_document_evidence" in result["gaps"]


def test_claim_version_must_be_in_verified_versions() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    release_id = "github.com/acme/demo/release/v1.0.0"
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            release_id,
            "github.com/acme/demo/pull/101",
        )
    )
    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document()],
    )
    claims = [
        {
            "id": "claim-version",
            "kind": "observed",
            "scope": {"capability_id": "cap-001", "version": "v9.9.9"},
        }
    ]

    issues = check_state_assertions(
        claims,
        [capability(delivery_state="released")],
        [result],
    )

    assert result["delivery_verification"] == "verified"
    assert "claim_version_not_verified" in {item["code"] for item in issues}


def test_claim_versions_are_checked_item_by_item() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    release_id = "github.com/acme/demo/release/v1.0.0"
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            release_id,
            "github.com/acme/demo/pull/101",
        )
    )
    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document()],
    )
    claims = [
        {
            "id": "claim-versions",
            "kind": "observed",
            "scope": {
                "capability_id": "cap-001",
                "versions": ["v1.0.0", "v9.9.9"],
            },
        }
    ]

    issues = check_state_assertions(
        claims,
        [capability(delivery_state="released")],
        [result],
    )

    version_issues = [
        item for item in issues if item["code"] == "claim_version_not_verified"
    ]
    assert [item["version"] for item in version_issues] == ["v9.9.9"]


def test_merge_after_release_stays_unknown() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(merged_at="2026-10-20T10:00:00Z")],
        release_documents=[release_document()],
    )

    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "note_only"


def test_verified_release_before_its_merge_is_not_delivery() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    release_id = "github.com/acme/demo/release/v1.0.0"
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            release_id,
            "github.com/acme/demo/pull/101",
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(merged_at="2026-10-20T10:00:00Z")],
        release_documents=[release_document(published_at="2026-10-01T00:00:00Z")],
    )

    assert result["delivery_state"] == "unknown"
    assert result["verified_versions"] == []
    assert "release_before_implementation:v1.0.0:github.com/acme/demo/pull/101" in (
        result["gaps"]
    )

    issues = check_state_assertions([], [capability(delivery_state="released")], [result])
    codes = {item["code"] for item in issues}
    assert "release_before_implementation" in codes
    assert "released_without_verified_release" not in codes


def test_older_release_does_not_block_newer_delivery() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    entities.append(release_entity("v2.0.0"))
    entities.append(pr_entity(102, merged_at="2026-05-10T00:00:00Z"))
    relations.append(
        relation(
            "rel-verified-old",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            "github.com/acme/demo/pull/101",
        )
    )
    relations.append(
        relation(
            "rel-part-new",
            "implements_part_of",
            "github.com/acme/demo/pull/102",
            "github.com/acme/demo/issue/40",
        )
    )
    relations.append(
        relation(
            "rel-verified-new",
            "verified_in_release",
            "github.com/acme/demo/release/v2.0.0",
            "github.com/acme/demo/pull/102",
        )
    )
    documents = [
        pr_document(merged_at="2026-10-20T10:00:00Z"),
        pr_document(102, merged_at="2026-05-10T00:00:00Z"),
    ]
    releases = [
        release_document("v1.0.0", published_at="2026-10-01T00:00:00Z"),
        release_document("v2.0.0", published_at="2026-11-15T00:00:00Z"),
    ]

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=documents,
        release_documents=releases,
    )

    assert result["delivery_state"] == "released"
    assert result["verified_versions"] == ["v2.0.0"]
    assert "release_before_implementation:v1.0.0:github.com/acme/demo/pull/101" in (
        result["gaps"]
    )
    issues = check_state_assertions([], [capability(delivery_state="released")], [result])
    assert "release_before_implementation" not in {item["code"] for item in issues}


def test_release_without_published_at_is_not_verified() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    relations = [
        item for item in relations if item["type"] != "release_note_mentions"
    ]
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            "github.com/acme/demo/pull/101",
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document(published_at=None)],
    )

    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "unknown"
    assert result["verified_versions"] == []
    assert "release_published_at_missing:v1.0.0" in result["gaps"]
    assert not any(
        gap.startswith("release_before_implementation") for gap in result["gaps"]
    )


def test_proposal_closed_duplicate_and_not_planned() -> None:
    entities, relations, evidence_records = base_inputs()
    duplicate = verify_capability(
        capability(),
        [issue_entity(state="closed", state_reason="duplicate"), pr_entity()],
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )
    not_planned = verify_capability(
        capability(),
        [issue_entity(state="closed", state_reason="not_planned"), pr_entity()],
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )
    completed = verify_capability(
        capability(),
        [issue_entity(state="closed", state_reason="completed"), pr_entity()],
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )

    assert duplicate["proposal_state"] == "superseded"
    assert "duplicate_is_not_implementation" in duplicate["gaps"]
    assert not_planned["proposal_state"] == "rejected"
    assert "not_planned_is_not_implementation" in not_planned["gaps"]
    assert completed["proposal_state"] == "proposed"
    assert "closed_without_acceptance_evidence" in completed["gaps"]


def test_missing_files_coverage_is_partial() -> None:
    entities, relations, evidence_records = base_inputs()
    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(files_status="partial", files_truncated=True)],
    )

    assert result["implementation_state"] == "partially_merged"
    assert "pr_files_incomplete" in result["gaps"]


def test_missing_release_evidence_is_unknown() -> None:
    entities, relations, evidence_records = base_inputs()
    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )

    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "unknown"
    assert "no_release_evidence" in result["gaps"]
    assert result["delivery_state"] != "not_found"


def test_availability_requires_document_evidence() -> None:
    entities, relations, evidence_records = base_inputs()
    no_doc = verify_capability(
        capability(availability="default_on"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )
    doc = doc_entity()
    entities.append(doc)
    evidence_records.append(evidence("ev-doc", doc["id"], "doc_content"))
    with_doc = verify_capability(
        capability(
            availability="default_on",
            evidence_ids=["ev-issue", "ev-pr", "ev-doc"],
        ),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
    )

    assert no_doc["availability"] == "unknown"
    assert "availability_without_document_evidence" in no_doc["gaps"]
    assert with_doc["availability"] == "default_on"


def test_check_state_assertions_counterexamples() -> None:
    cap_merged = capability(
        id="cap-merged",
        implementation_state="merged",
        requirements={"done": [], "remaining": ["cleanup"]},
    )
    cap_released = capability(id="cap-released", delivery_state="released")
    cap_default = capability(id="cap-default", availability="default_on")
    results = [
        {
            "capability_id": "cap-merged",
            "implementation_state": "partially_merged",
            "delivery_state": "unknown",
            "delivery_verification": "unknown",
            "availability": "unknown",
            "verified_versions": [],
        },
        {
            "capability_id": "cap-released",
            "implementation_state": "merged",
            "delivery_state": "unknown",
            "delivery_verification": "note_only",
            "availability": "unknown",
            "verified_versions": [],
        },
        {
            "capability_id": "cap-default",
            "implementation_state": "merged",
            "delivery_state": "unknown",
            "delivery_verification": "unknown",
            "availability": "unknown",
            "verified_versions": [],
        },
    ]
    claims = [
        {
            "id": "claim-missing",
            "kind": "observed",
            "scope": {"capability_id": "cap-missing", "version": None},
        },
        {
            "id": "claim-unknown",
            "kind": "unknown",
            "scope": {"capability_id": "cap-merged", "version": None},
            "reason": None,
            "missing_evidence": [],
        },
        {
            "id": "claim-version",
            "kind": "observed",
            "scope": {"capability_id": "cap-released", "version": "v9.9.9"},
        },
    ]

    issues = check_state_assertions(
        claims,
        [cap_merged, cap_released, cap_default],
        results,
    )
    codes = {item["code"] for item in issues}

    assert "merged_with_remaining_requirements" in codes
    assert "released_without_verified_release" in codes
    assert "availability_without_document_evidence" in codes
    assert "missing_claim_capability" in codes
    assert "unknown_claim_missing_reason" in codes
    assert "unknown_claim_missing_evidence" in codes
    assert "claim_version_not_verified" in codes


def test_inputs_are_not_mutated_and_result_is_deterministic() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    cap = capability()
    pr_documents = [pr_document()]
    release_documents = [release_document()]
    snapshot = copy.deepcopy(
        (cap, entities, relations, evidence_records, pr_documents, release_documents)
    )

    first = verify_capability(
        cap,
        entities,
        relations,
        evidence_records,
        pr_documents=pr_documents,
        release_documents=release_documents,
    )
    second = verify_capability(
        cap,
        entities,
        relations,
        evidence_records,
        pr_documents=pr_documents,
        release_documents=release_documents,
    )

    assert first == second
    assert (
        cap,
        entities,
        relations,
        evidence_records,
        pr_documents,
        release_documents,
    ) == snapshot


def test_claims_and_relations_expand_supporting_entities() -> None:
    issue = issue_entity()
    pr = pr_entity()
    commit = commit_entity()
    entities = [issue, pr, commit]
    evidence_records = [
        evidence("ev-issue", issue["id"], "issue_body"),
        evidence("ev-claim", pr["id"]),
    ]
    relations = [
        relation("rel-part", "implements_part_of", pr["id"], issue["id"]),
        relation("rel-commit", "contains_commit", pr["id"], commit["id"]),
    ]
    claim = {
        "id": "claim-001",
        "kind": "observed",
        "scope": {
            "capability_id": "cap-001",
            "entity_ids": [pr["id"]],
            "version": None,
        },
        "evidence_ids": ["ev-claim"],
        "state_time_basis": "historical_reconstructed",
    }

    result = verify_capability(
        capability(evidence_ids=["ev-issue"]),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        claims=[claim],
    )

    assert result["supporting_entities"]["proposal"] == [issue["id"]]
    assert result["supporting_entities"]["pull_request"] == [pr["id"]]
    assert result["supporting_entities"]["commit"] == [commit["id"]]
    assert result["state_time_basis"] == "historical_reconstructed"


def test_unmerged_pr_cannot_satisfy_verified_release() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    relations = [
        item for item in relations if item["type"] != "release_note_mentions"
    ]
    for entity in entities:
        if entity["kind"] == "pull_request":
            entity["raw_state"].update(
                merged=False,
                merged_at=None,
                state="open",
            )
            entity["normalized_state"].update(merged=False, merged_at=None)
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            "github.com/acme/demo/pull/101",
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(merged=False, merged_at=None, state="open")],
        release_documents=[release_document()],
    )

    assert result["implementation_state"] == "in_progress"
    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "unknown"
    assert "verified_target_not_merged:github.com/acme/demo/pull/101" in result["gaps"]


def test_unverified_claim_scope_does_not_support_availability() -> None:
    entities, relations, evidence_records = base_inputs()
    doc = doc_entity()
    entities.append(doc)
    doc_evidence = evidence("ev-doc", doc["id"], "doc_content")
    doc_evidence["verification"] = "unverifiable"
    evidence_records.append(doc_evidence)
    claim = {
        "id": "claim-doc",
        "kind": "observed",
        "scope": {
            "capability_id": "cap-001",
            "entity_ids": [doc["id"]],
            "version": None,
        },
        "evidence_ids": ["ev-doc"],
        "state_time_basis": "observed_current",
    }

    result = verify_capability(
        capability(availability="default_on"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        claims=[claim],
    )

    assert result["availability"] == "unknown"
    assert "availability_without_document_evidence" in result["gaps"]


def test_mixed_claim_evidence_skips_scope() -> None:
    entities, relations, evidence_records = base_inputs()
    doc = doc_entity()
    entities.append(doc)
    verified = evidence("ev-doc", doc["id"], "doc_content")
    unverified = evidence("ev-doc-2", doc["id"], "doc_content")
    unverified["verification"] = "unverifiable"
    evidence_records.extend([verified, unverified])
    claim = {
        "id": "claim-doc",
        "kind": "observed",
        "scope": {
            "capability_id": "cap-001",
            "entity_ids": [doc["id"]],
            "version": None,
        },
        "evidence_ids": ["ev-doc", "ev-doc-2"],
        "state_time_basis": "observed_current",
    }

    result = verify_capability(
        capability(availability="default_on"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        claims=[claim],
    )

    assert result["availability"] == "unknown"


def test_unknown_claim_without_evidence_skips_scope() -> None:
    entities, relations, evidence_records = base_inputs()
    doc = doc_entity()
    entities.append(doc)
    claim = {
        "id": "claim-unknown",
        "kind": "unknown",
        "scope": {
            "capability_id": "cap-001",
            "entity_ids": [doc["id"]],
            "version": None,
        },
        "evidence_ids": [],
        "state_time_basis": "observed_current",
    }

    result = verify_capability(
        capability(availability="default_on"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        claims=[claim],
    )

    assert result["availability"] == "unknown"


def test_missing_pr_document_downgrades_merged_to_partial() -> None:
    entities, relations, evidence_records = base_inputs()

    result = verify_capability(
        capability(),
        entities,
        relations,
        evidence_records,
        pr_documents=[],
    )

    assert result["implementation_state"] == "partially_merged"
    assert "pr_document_missing" in result["gaps"]


def test_commit_target_requires_a_merged_pr_document() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    relations = [
        item for item in relations if item["type"] != "release_note_mentions"
    ]
    for entity in entities:
        if entity["kind"] == "pull_request":
            entity["raw_state"].update(merged=False, merged_at=None, state="open")
            entity["normalized_state"].update(merged=False, merged_at=None)
    commit_id = f"github.com/acme/demo/{HEAD}"
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            commit_id,
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[
            pr_document(merged=False, merged_at=None, state="open", commit_shas=[HEAD])
        ],
        release_documents=[release_document()],
    )

    assert result["implementation_state"] == "in_progress"
    assert result["delivery_state"] == "unknown"
    assert f"verified_target_not_merged:{commit_id}" in result["gaps"]


def test_commit_target_from_merged_pr_is_delivery() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    relations = [
        item for item in relations if item["type"] != "release_note_mentions"
    ]
    commit_id = f"github.com/acme/demo/{HEAD}"
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            commit_id,
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(commit_shas=[HEAD])],
        release_documents=[release_document()],
    )

    assert result["delivery_state"] == "released"
    assert result["delivery_verification"] == "verified"
    assert result["verified_versions"] == ["v1.0.0"]


def test_commit_target_rejects_release_before_containing_pr_merge() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    relations = [
        item for item in relations if item["type"] != "release_note_mentions"
    ]
    commit_id = f"github.com/acme/demo/{HEAD}"
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            commit_id,
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(commit_shas=[HEAD])],
        release_documents=[
            release_document(published_at="2026-10-01T00:00:00Z")
        ],
    )

    assert result["delivery_state"] == "unknown"
    assert result["verified_versions"] == []
    assert (
        f"release_before_implementation:v1.0.0:{commit_id}"
        in result["gaps"]
    )


def test_commit_target_uses_earliest_containing_pr_merge() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    relations = [
        item for item in relations if item["type"] != "release_note_mentions"
    ]
    commit_id = f"github.com/acme/demo/{HEAD}"
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            commit_id,
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[
            pr_document(
                number=101,
                merged_at="2026-10-10T00:00:00Z",
                commit_shas=[HEAD],
            ),
            pr_document(
                number=102,
                merged_at="2026-10-30T00:00:00Z",
                commit_shas=[HEAD],
            ),
        ],
        release_documents=[release_document()],
    )

    assert result["delivery_state"] == "released"
    assert result["verified_versions"] == ["v1.0.0"]


def test_commit_target_unresolvable_timeline_is_excluded() -> None:
    entities, relations, evidence_records = base_inputs(include_release=True)
    relations = [
        item for item in relations if item["type"] != "release_note_mentions"
    ]
    commit_id = f"github.com/acme/demo/{HEAD}"
    relations.append(
        relation(
            "rel-verified",
            "verified_in_release",
            "github.com/acme/demo/release/v1.0.0",
            commit_id,
        )
    )

    result = verify_capability(
        capability(delivery_state="released"),
        entities,
        relations,
        evidence_records,
        pr_documents=[
            pr_document(merged_at="not-a-date", commit_shas=[HEAD])
        ],
        release_documents=[release_document()],
    )

    assert result["delivery_state"] == "unknown"
    assert result["verified_versions"] == []
    assert (
        f"release_timeline_unresolved:v1.0.0:{commit_id}"
        in result["gaps"]
    )


def test_release_verified_in_release_does_not_verify_other_pr() -> None:
    issue = issue_entity()
    pr = pr_entity(101)
    other = pr_entity(102)
    release = release_entity()
    entities = [issue, pr, other, release]
    evidence_records = [
        evidence("ev-issue", issue["id"], "issue_body"),
        evidence("ev-pr-a", pr["id"]),
    ]
    relations = [
        relation("rel-note", "release_note_mentions", release["id"], pr["id"]),
        relation("rel-verified-b", "verified_in_release", release["id"], other["id"]),
    ]

    result = verify_capability(
        capability(evidence_ids=["ev-issue", "ev-pr-a"]),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(101), pr_document(102)],
        release_documents=[release_document()],
    )

    assert result["implementation_state"] == "merged"
    assert result["delivery_state"] == "unknown"
    assert result["delivery_verification"] == "note_only"
    assert result["verified_versions"] == []
    assert result["supporting_entities"]["pull_request"] == [pr["id"]]
    assert result["supporting_entities"]["release"] == [release["id"]]


def test_release_is_a_terminal_entity_not_an_expansion_hub() -> None:
    release = release_entity()
    pr = pr_entity(101)
    other = pr_entity(102)
    entities = [release, pr, other]
    evidence_records = [
        evidence("ev-release", release["id"], "release_body"),
        evidence("ev-pr", pr["id"]),
    ]
    relations = [
        relation("rel-note", "release_note_mentions", release["id"], pr["id"]),
        relation("rel-verified", "verified_in_release", release["id"], other["id"]),
    ]

    result = verify_capability(
        capability(evidence_ids=["ev-release"]),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document(101), pr_document(102)],
        release_documents=[release_document()],
    )

    assert result["supporting_entities"]["release"] == [release["id"]]
    assert result["supporting_entities"]["pull_request"] == []


def test_release_note_mentions_does_not_expand_to_other_prs() -> None:
    issue = issue_entity()
    pr = pr_entity()
    release = release_entity()
    other = [pr_entity(201), pr_entity(202)]
    entities = [issue, pr, release, *other]
    evidence_records = [
        evidence("ev-issue", issue["id"], "issue_body"),
        evidence("ev-pr", pr["id"]),
    ]
    relations = [
        relation("rel-note", "release_note_mentions", release["id"], pr["id"]),
        relation("rel-other-1", "release_note_mentions", release["id"], other[0]["id"]),
        relation("rel-other-2", "release_note_mentions", release["id"], other[1]["id"]),
    ]

    result = verify_capability(
        capability(evidence_ids=["ev-issue", "ev-pr"]),
        entities,
        relations,
        evidence_records,
        pr_documents=[pr_document()],
        release_documents=[release_document()],
    )

    assert result["implementation_state"] == "merged"
    assert "release_note_only" in result["gaps"]
    assert result["supporting_entities"]["pull_request"] == [pr["id"]]
    assert result["supporting_entities"]["release"] == [release["id"]]
