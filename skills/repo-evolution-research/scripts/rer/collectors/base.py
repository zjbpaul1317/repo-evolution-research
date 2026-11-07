"""Shared collector context and error classification."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import (
    BudgetExceededError,
    GitHubAuthError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)
from .values import now_iso

if TYPE_CHECKING:
    from ..github import GitHubClient
    from ..store import SnapshotStore
    from .progress import CollectProgress


@dataclass
class CollectContext:
    client: GitHubClient
    store: SnapshotStore
    repository: Mapping[str, Any]
    clock: Callable[[], str] = now_iso
    progress: CollectProgress | None = None
    budget_exhausted: bool = False

    def now(self) -> str:
        return self.clock()


FATAL_ERRORS = (
    BudgetExceededError,
    GitHubRateLimitError,
    GitHubAuthError,
    GitHubUnavailableError,
)


def error_code(error: BaseException, default: str | None = None) -> str:
    return str(getattr(error, "code", default or type(error).__name__))


def is_run_budget_error(error: BaseException) -> bool:
    """True when the failing budget is run-level, not a channel-local limit."""
    if not isinstance(error, BudgetExceededError):
        return False
    if error.source == "github.budget":
        return True
    details = error.details if isinstance(error.details, Mapping) else {}
    return error.source == "github.response" and "total_bytes" in details


def mark_run_budget_exhausted(context: CollectContext) -> None:
    context.budget_exhausted = True
    if context.progress is not None:
        context.progress.budget_exhausted = True


__all__ = [
    "CollectContext",
    "FATAL_ERRORS",
    "error_code",
    "is_run_budget_error",
    "mark_run_budget_exhausted",
]
