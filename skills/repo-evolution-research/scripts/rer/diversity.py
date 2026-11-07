"""Small deterministic exploration reserves over explicit title/area hints."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any, TypeVar

T = TypeVar("T")


def title_area(title: str) -> str | None:
    """Return an explicit bracketed area; never pretend this is a source path."""
    match = re.match(r"^\[(`?)([\w.-]+)\1\]", title.strip())
    if match:
        return f"title:{match[2].casefold()}"
    # Common technical areas provide a fallback for repositories without tags.
    # These remain visible title hints, never asserted source directories.
    for area, pattern in (
        ("wasm", r"\bwasm\b|\bwebassembly\b"),
        ("formatter", r"\bformat(?:ter|ting)?\b|\bcomments? placement\b"),
        ("parser", r"\blex(?:er|ing)?\b|\bpars(?:er|ing|e)\b|\btokeniz\w*"),
        ("linter", r"\blint(?:er|ing)?\b|\blint rules?\b"),
        ("server", r"\blsp\b|\bserver\b"),
        ("packaging", r"\brelease\b|\bwheels?\b|\bpackag(?:e|ing)\b"),
    ):
        if re.search(pattern, title, re.IGNORECASE):
            return f"title:{area}"
    return None


def record_area(record: Mapping[str, Any]) -> str:
    return title_area(str(record.get("title") or "")) or "untagged"


def diverse_order(
    items: Iterable[T], *, key: Callable[[T], str], reserve: int,
) -> list[T]:
    """Promote at most ``reserve`` area representatives, keeping score order otherwise.

    Input must already be ranked. Unknown/untagged material forms one group,
    rather than many invented modules. This is a bounded exploration heuristic.
    """
    ranked = list(items)
    seen: set[str] = set()
    representatives: list[int] = []
    for index, item in enumerate(ranked):
        area = key(item)
        if area not in seen:
            seen.add(area)
            representatives.append(index)
        if len(representatives) >= max(1, reserve):
            break
    promoted = set(representatives)
    return [ranked[i] for i in representatives] + [
        item for i, item in enumerate(ranked) if i not in promoted
    ]
