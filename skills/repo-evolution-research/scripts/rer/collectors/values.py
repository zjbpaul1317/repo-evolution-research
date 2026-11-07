"""Shared value normalization for collector payloads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from ..models import to_iso_z


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def parse_datetime_strict(value: Any) -> datetime:
    moment = parse_datetime(value)
    if moment is None:
        raise ValueError(f"invalid timestamp: {value!r}")
    return moment


def iso_or_none(value: Any) -> str | None:
    moment = parse_datetime(value)
    return to_iso_z(moment) if moment is not None else None


def now_iso() -> str:
    return to_iso_z(datetime.now(UTC))


def as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def as_optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(0, value)


def as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def get_in(raw: Any, *path: str) -> Any:
    current = raw
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def labels(raw: Mapping[str, Any]) -> list[str]:
    values = raw.get("labels")
    if not isinstance(values, list):
        return []
    names: list[str] = []
    for item in values:
        if isinstance(item, str) and item:
            names.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return sorted(set(names))


def reaction_count(
    raw: Mapping[str, Any],
    *,
    allow_upvote: bool = True,
) -> int | None:
    value = raw.get("reactions")
    if isinstance(value, Mapping):
        value = value.get("total_count")
    if value is None and allow_upvote:
        value = raw.get("upvoteCount")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def comment_count(raw: Mapping[str, Any]) -> int | None:
    value = raw.get("comments")
    if isinstance(value, Mapping):
        value = value.get("totalCount")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def user_login(raw: Mapping[str, Any]) -> str | None:
    for key in ("user", "author"):
        value = raw.get(key)
        if isinstance(value, Mapping) and isinstance(value.get("login"), str):
            return value["login"]
    return None


def merged_at(raw: Mapping[str, Any]) -> Any:
    pull_request = raw.get("pull_request")
    if isinstance(pull_request, Mapping) and pull_request.get("merged_at"):
        return pull_request.get("merged_at")
    return raw.get("merged_at")


def window_bounds(window: Mapping[str, Any]) -> tuple[datetime, datetime]:
    start = parse_datetime(window.get("start"))
    end = parse_datetime(window.get("end"))
    if start is None or end is None:
        raise ValueError("activity window start/end is missing or invalid")
    if end <= start:
        raise ValueError("activity window end must be after start")
    return start, end


__all__ = [
    "as_int",
    "as_optional_int",
    "as_optional_str",
    "comment_count",
    "get_in",
    "iso_or_none",
    "labels",
    "merged_at",
    "now_iso",
    "parse_datetime",
    "parse_datetime_strict",
    "reaction_count",
    "user_login",
    "window_bounds",
]
