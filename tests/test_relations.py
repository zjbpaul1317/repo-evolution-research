from __future__ import annotations

from typing import Any

from rer.models import entity_id, make_entity
from rer.normalize import Block, NormalizeIndex, TextSource, split_blocks
from rer.relation_policy import collapse_relations_by_identity, merge_relation_scope
from rer.relations import Graph, RelationBuilder, mask_markdown

HOST = "github.com"
OWNER = "acme"
NAME = "demo"
NOW = "2026-11-01T08:00:00Z"
BLOB = "0" * 64


def issue(number: int = 12, *, host: str = HOST) -> dict[str, Any]:
    return make_entity(
        entity_id=entity_id(host, OWNER, NAME, "issue", str(number)),
        kind="issue",
        title=f"#{number}",
        url=f"https://{host}/{OWNER}/{NAME}/issues/{number}",
        observed_at=NOW,
        number=number,
    )


def commit(sha: str) -> dict[str, Any]:
    return make_entity(
        entity_id=entity_id(HOST, OWNER, NAME, "commit", sha),
        kind="commit",
        title=sha[:7],
        url=f"https://github.com/{OWNER}/{NAME}/commit/{sha}",
        observed_at=NOW,
        raw_state={"sha": sha},
    )


def source(
    text: str,
    *,
    source_kind: str = "pull_request_body",
    source_entity: str | None = None,
) -> TextSource:
    return TextSource(
        artifact="raw/pr-details/1.json",
        document_key="raw/pr-details/1.json",
        json_pointer="/body",
        entity_id=source_entity or entity_id(HOST, OWNER, NAME, "pull_request", "1"),
        source_kind=source_kind,
        source_url=f"https://github.com/{OWNER}/{NAME}/pull/1",
        observed_at=NOW,
        source_event_at=NOW,
        source_revision=None,
        blob_sha256=BLOB,
        text=text,
    )


def blocks(text: str) -> list[Block]:
    return split_blocks(text)


def run(
    text: str,
    *,
    index: NormalizeIndex | None = None,
    source_kind: str = "pull_request_body",
    max_external: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, int]]:
    builder = RelationBuilder(
        index or NormalizeIndex.build([]),
        host=HOST,
        owner=OWNER,
        name=NAME,
        max_external_entities=max_external,
    )
    text_source = source(text, source_kind=source_kind)
    for position, block in enumerate(blocks(text), start=1):
        builder.add_text(text_source, block, f"ev-{position}")
    return builder.result()


def test_fixes_becomes_candidate_closing_reference() -> None:
    relations, external, _counts = run(
        "Fixes #12.",
        index=NormalizeIndex.build([issue(12)]),
    )

    assert external == {}
    assert len(relations) == 1
    relation = relations[0]
    assert relation["type"] == "closing_reference"
    assert relation["basis"] == "text_explicit"
    assert relation["verification"] == "candidate"
    assert relation["to_id"] == entity_id(HOST, OWNER, NAME, "issue", "12")


def test_code_block_reference_is_not_verified() -> None:
    relations, _external, counts = run(
        "```text\nFixes #12\n```\n",
        index=NormalizeIndex.build([issue(12)]),
    )

    assert relations == []
    assert counts["ignored_code_refs"] >= 1


def test_doc_anchor_is_not_a_relation() -> None:
    relations, external, _counts = run(
        "See #section and #123",
        index=NormalizeIndex.build([issue(123)]),
        source_kind="doc_content",
    )

    assert relations == []
    assert external == {}


def test_external_url_creates_stub_and_is_budgeted() -> None:
    text = "See https://github.com/other/project/issues/7"
    relations, external, counts = run(text, max_external=5)
    assert len(relations) == 1
    assert relations[0]["type"] == "mentions"
    assert relations[0]["verification"] == "candidate"
    assert counts["external_entities"] == 1
    stub = next(iter(external.values()))
    assert stub["external"] is True
    assert stub["id"] == "github.com/other/project/issue/7"

    limited, external_limited, limited_counts = run(text, max_external=0)
    assert limited == []
    assert external_limited == {}
    assert limited_counts["external_entity_limit"] == 1


def test_ambiguous_external_repo_ref_creates_no_entity() -> None:
    relations, external, counts = run("See other/project#7")
    assert relations == []
    assert external == {}
    assert counts["ambiguous_external_refs"] == 1


def test_unknown_local_number_is_unresolved() -> None:
    relations, external, counts = run("See #999")
    assert relations == []
    assert external == {}
    assert counts["unresolved_refs"] == 1


def test_short_sha_unique_and_ambiguous() -> None:
    unique = commit("abc1234" + "0" * 33)
    relations, _external, _counts = run(
        "See abc1234",
        index=NormalizeIndex.build([unique]),
    )
    assert len(relations) == 1
    assert relations[0]["type"] == "mentions"
    assert relations[0]["verification"] == "verified"
    assert relations[0]["to_id"] == unique["id"]

    ambiguous = NormalizeIndex.build(
        [
            commit("abc1234" + "0" * 33),
            commit("abc1234" + "1" * 33),
        ]
    )
    relations, _external, counts = run("See abc1234", index=ambiguous)
    assert relations == []
    assert counts["ambiguous_short_sha"] == 1


def test_same_reference_three_blocks_merges_relation_evidence() -> None:
    builder = RelationBuilder(
        NormalizeIndex.build([issue(12)]),
        host=HOST,
        owner=OWNER,
        name=NAME,
        max_external_entities=0,
    )
    text_source = source("")
    for position in range(1, 4):
        text = "Fixes #12"
        builder.add_text(
            text_source,
            Block(0, len(text), 1, 1, text),
            f"ev-{position}",
        )
    relations, _external, _counts = builder.result()
    assert len(relations) == 1
    assert relations[0]["evidence_ids"] == ["ev-1", "ev-2", "ev-3"]


def test_mentions_never_becomes_implements() -> None:
    relations, _external, _counts = run("See #12", index=NormalizeIndex.build([issue(12)]))
    assert len(relations) == 1
    assert relations[0]["type"] == "mentions"
    assert all(item["type"] != "implements_part_of" for item in relations)


def test_contains_commit_has_api_basis_and_no_quote() -> None:
    builder = RelationBuilder(
        NormalizeIndex.build([]),
        host=HOST,
        owner=OWNER,
        name=NAME,
        max_external_entities=0,
    )
    pull_id = entity_id(HOST, OWNER, NAME, "pull_request", "1")
    commit_id = entity_id(HOST, OWNER, NAME, "commit", "a" * 40)
    builder.add_contains_commit(
        pull_id,
        commit_id,
        artifact="raw/pr-details/1.json",
        observed_at=NOW,
    )
    relations, _external, _counts = builder.result()
    assert len(relations) == 1
    relation = relations[0]
    assert relation["type"] == "contains_commit"
    assert relation["basis"] == "api_explicit"
    assert relation["verification"] == "verified"
    assert relation["evidence_ids"] == []
    assert relation["scope"]["section"] == "commits"


def test_graph_traverse_respects_hops_and_budget() -> None:
    relations = [
        {
            "id": "rel-1",
            "from_id": "a",
            "to_id": "b",
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "verified",
            "evidence_ids": [],
            "scope": None,
        },
        {
            "id": "rel-2",
            "from_id": "b",
            "to_id": "c",
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "verified",
            "evidence_ids": [],
            "scope": None,
        },
        {
            "id": "rel-3",
            "from_id": "c",
            "to_id": "d",
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "verified",
            "evidence_ids": [],
            "scope": None,
        },
    ]
    graph = Graph.build(relations)

    one_hop = graph.traverse("a", max_hops=1)
    assert [item["entity_id"] for item in one_hop["nodes"]] == ["a", "b"]
    assert len(one_hop["edges"]) == 1

    all_hops = graph.traverse("a", max_hops=3)
    assert [item["entity_id"] for item in all_hops["nodes"]] == ["a", "b", "c", "d"]

    bounded = graph.traverse("a", max_hops=3, budget=2)
    assert [item["entity_id"] for item in bounded["nodes"]] == ["a", "b"]
    assert bounded["truncated"] is True


def test_fenced_code_with_blank_line_stays_ignored_across_blocks() -> None:
    text = "```python\nprint(1)\n\nprint(2)  #123\n```\n\nOutside."
    index = NormalizeIndex.build([issue(123)])
    builder = RelationBuilder(index, host=HOST, owner=OWNER, name=NAME, max_external_entities=0)
    text_source = source(text)
    mask = mask_markdown(text)
    for position, block in enumerate(split_blocks(text), start=1):
        builder.add_text(text_source, block, f"ev-{position}", mask=mask)
    relations, _external, counts = builder.result()
    assert relations == []
    assert counts["ignored_code_refs"] >= 1


def test_long_fenced_code_block_stays_ignored_after_slicing() -> None:
    body = ("#123 " + "x" * 4100) + "\n"
    text = "```\n" + body + "```"
    index = NormalizeIndex.build([issue(123)])
    builder = RelationBuilder(index, host=HOST, owner=OWNER, name=NAME, max_external_entities=0)
    text_source = source(text)
    mask = mask_markdown(text)
    blocks = split_blocks(text)
    assert len(blocks) >= 2
    for position, block in enumerate(blocks, start=1):
        builder.add_text(text_source, block, f"ev-{position}", mask=mask)
    relations, _external, counts = builder.result()
    assert relations == []
    assert counts["ignored_code_refs"] >= 1


def test_closing_keyword_requires_adjacent_reference() -> None:
    relations, _external, _counts = run(
        "This fixes the bug. See #12",
        index=NormalizeIndex.build([issue(12)]),
    )
    assert all(item["type"] != "closing_reference" for item in relations)
    assert any(item["type"] == "mentions" for item in relations)


def test_closing_keyword_accepts_multiple_targets() -> None:
    relations, _external, _counts = run(
        "Fixes #12, #13",
        index=NormalizeIndex.build([issue(12), issue(13)]),
    )
    closing = [item for item in relations if item["type"] == "closing_reference"]
    assert len(closing) == 2
    assert {item["to_id"] for item in closing} == {
        entity_id(HOST, OWNER, NAME, "issue", "12"),
        entity_id(HOST, OWNER, NAME, "issue", "13"),
    }


def test_closing_keyword_rejects_commit_and_release_targets() -> None:
    commit_id = "abc1234" + "0" * 33
    relations, external, counts = run(
        "Fixes abc1234",
        index=NormalizeIndex.build([commit(commit_id)]),
    )
    assert all(item["type"] != "closing_reference" for item in relations)
    assert external == {}
    assert counts["invalid_closing_targets"] == 1

    release_text = "Fixes https://github.com/other/project/releases/tag/v1"
    relations, external, counts = run(release_text)
    assert all(item["type"] != "closing_reference" for item in relations)
    assert external == {}
    assert counts["invalid_closing_targets"] == 1


def test_release_body_produces_release_note_mentions() -> None:
    relations, _external, _counts = run(
        "See #12",
        index=NormalizeIndex.build([issue(12)]),
        source_kind="release_body",
    )
    assert len(relations) == 1
    assert relations[0]["type"] == "release_note_mentions"


def test_review_comment_does_not_produce_closing_reference() -> None:
    relations, _external, _counts = run(
        "Fixes #12",
        index=NormalizeIndex.build([issue(12)]),
        source_kind="pull_request_review_comment",
    )
    assert len(relations) == 1
    assert relations[0]["type"] == "mentions"


def test_enterprise_host_resolves_local_repo_reference() -> None:
    host = "git.example.com"
    builder = RelationBuilder(
        NormalizeIndex.build([issue(12, host=host)]),
        host=host,
        owner=OWNER,
        name=NAME,
        max_external_entities=0,
    )
    text = "See acme/demo#12"
    builder.add_text(source(text), blocks(text)[0], "ev-1")
    relations, external, counts = builder.result()
    assert external == {}
    assert len(relations) == 1
    assert relations[0]["to_id"] == entity_id(host, OWNER, NAME, "issue", "12")
    assert counts["ambiguous_external_refs"] == 0


def test_graph_budget_does_not_emit_dangling_edges() -> None:
    relations = [
        {
            "id": "rel-1",
            "from_id": "a",
            "to_id": "b",
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "verified",
            "evidence_ids": [],
            "scope": None,
        },
        {
            "id": "rel-2",
            "from_id": "b",
            "to_id": "c",
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "verified",
            "evidence_ids": [],
            "scope": None,
        },
        {
            "id": "rel-3",
            "from_id": "c",
            "to_id": "a",
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "verified",
            "evidence_ids": [],
            "scope": None,
        },
    ]
    graph = Graph.build(relations)
    bounded = graph.traverse("a", max_hops=3, budget=2)
    node_ids = {item["entity_id"] for item in bounded["nodes"]}
    assert bounded["truncated"] is True
    assert all(
        edge["from_id"] in node_ids and edge["to_id"] in node_ids
        for edge in bounded["edges"]
    )

    cycle = graph.traverse("a", max_hops=5)
    assert {item["entity_id"] for item in cycle["nodes"]} == {"a", "b", "c"}
    assert len(cycle["edges"]) == 3


def test_fence_with_info_string_does_not_close_a_code_block() -> None:
    text = "```\n#123\n```python\n#456\n```\nOutside #789"
    index = NormalizeIndex.build([issue(123), issue(456), issue(789)])
    builder = RelationBuilder(
        index,
        host=HOST,
        owner=OWNER,
        name=NAME,
        max_external_entities=0,
    )
    mask = mask_markdown(text)
    for position, block in enumerate(split_blocks(text), start=1):
        builder.add_text(source(text), block, f"ev-{position}", mask=mask)
    relations, _external, _counts = builder.result()

    assert [item["to_id"] for item in relations] == [
        "github.com/acme/demo/issue/789"
    ]


def test_collapse_relations_by_identity_keeps_disputed() -> None:
    relations = [
        {
            "id": "rel-a",
            "from_id": "a",
            "to_id": "b",
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "verified",
            "evidence_ids": ["ev-a"],
            "scope": None,
        },
        {
            "id": "rel-b",
            "from_id": "a",
            "to_id": "b",
            "type": "mentions",
            "basis": "text_explicit",
            "verification": "disputed",
            "evidence_ids": ["ev-b"],
            "scope": None,
        },
    ]

    collapsed = collapse_relations_by_identity(relations)

    assert len(collapsed) == 1
    assert collapsed[0]["id"] == "rel-a"
    assert collapsed[0]["verification"] == "disputed"
    assert collapsed[0]["evidence_ids"] == ["ev-a", "ev-b"]


def test_merge_relation_scope_unions_existing_aliases() -> None:
    merged = merge_relation_scope(
        {
            "generator": "import-source",
            "aliases": ["rel-x"],
        },
        {
            "generator": "import-source",
            "aliases": ["rel-y"],
        },
        [],
        alias="rel-b",
    )

    assert merged["aliases"] == ["rel-b", "rel-x", "rel-y"]


def test_offline_reference_index_retains_more_than_twenty_external_issues():
    text = "\n\n".join(f"See https://github.com/other/lib/issues/{i}" for i in range(1, 51))
    relations, external, counts = run(text, max_external=None)
    assert len(external) == 50
    assert len(relations) == 50
    assert counts["external_entity_limit"] == 0
    assert all(r["verification"] == "candidate" for r in relations)
