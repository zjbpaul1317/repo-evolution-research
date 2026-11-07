from __future__ import annotations

from rer.collectors.base import is_run_budget_error
from rer.errors import (
    EXIT_CODES,
    ArtifactError,
    BudgetExceededError,
    ConfigHashError,
    EnvironmentDegraded,
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    InternalError,
    LockError,
    PartialError,
    RERError,
    SchemaError,
    StoreError,
    ToolError,
    UsageError,
    ValidationFailure,
)


def test_error_category_to_exit_code_mapping() -> None:
    assert UsageError("x").exit_code == 2
    assert SchemaError("x").exit_code == 2
    assert ConfigHashError("x").exit_code == 2
    assert ToolError("x").exit_code == 3
    assert EnvironmentDegraded("x").exit_code == 3
    assert PartialError("x").exit_code == 4
    assert ValidationFailure("x").exit_code == 5
    assert StoreError("x").exit_code == 6
    assert InternalError("x").exit_code == 6
    assert LockError("x").exit_code == 4
    assert StoreError("x", retryable=True).exit_code == 4
    assert GitHubAuthError("x").exit_code == 3
    assert GitHubNotFoundError("x").exit_code == 3
    assert GitHubRateLimitError("x").exit_code == 4
    assert GitHubUnavailableError("x").exit_code == 4
    assert BudgetExceededError("x").exit_code == 4
    assert ArtifactError("x").exit_code == 6
    assert RERError("x").exit_code == 6


def test_run_budget_error_classification() -> None:
    assert is_run_budget_error(
        BudgetExceededError("x", source="github.budget")
    )
    assert is_run_budget_error(
        BudgetExceededError(
            "x",
            source="github.response",
            details={"total_bytes": 1},
        )
    )
    assert not is_run_budget_error(
        BudgetExceededError(
            "x",
            source="github.response",
            details={"bytes": 1},
        )
    )
    assert not is_run_budget_error(
        BudgetExceededError("x", source="github.pagination")
    )
    assert not is_run_budget_error(RuntimeError("x"))


def test_every_error_code_has_an_exit_mapping() -> None:
    classes = [
        UsageError,
        SchemaError,
        ConfigHashError,
        ToolError,
        EnvironmentDegraded,
        PartialError,
        ValidationFailure,
        StoreError,
        LockError,
        InternalError,
        GitHubAuthError,
        GitHubNotFoundError,
        GitHubRateLimitError,
        GitHubUnavailableError,
        BudgetExceededError,
        ArtifactError,
    ]
    for error_class in classes:
        assert error_class.code in EXIT_CODES
