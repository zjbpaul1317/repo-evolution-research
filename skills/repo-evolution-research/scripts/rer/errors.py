"""Structured errors and a single error-category -> exit-code mapping."""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3
EXIT_PARTIAL = 4
EXIT_VALIDATION = 5
EXIT_INTERNAL = 6

EXIT_CODES: dict[str, int] = {
    "usage_error": EXIT_USAGE,
    "schema_error": EXIT_USAGE,
    "config_hash_mismatch": EXIT_USAGE,
    "environment_error": EXIT_ENVIRONMENT,
    "environment_degraded": EXIT_ENVIRONMENT,
    "partial_result": EXIT_PARTIAL,
    "run_locked": EXIT_PARTIAL,
    "validation_failed": EXIT_VALIDATION,
    "github_auth": EXIT_ENVIRONMENT,
    "github_not_found": EXIT_ENVIRONMENT,
    "github_rate_limited": EXIT_PARTIAL,
    "github_unavailable": EXIT_PARTIAL,
    "budget_exceeded": EXIT_PARTIAL,
    "github_error": EXIT_INTERNAL,
    "artifact_error": EXIT_INTERNAL,
    "store_error": EXIT_INTERNAL,
    "internal_error": EXIT_INTERNAL,
}


class RERError(Exception):
    """Base error carrying a stable code, exit status and recovery hint."""

    code = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        source: str | None = None,
        retryable: bool = False,
        recovery: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.source = source
        self.retryable = retryable
        self.recovery = recovery
        self.details = details or {}

    @property
    def exit_code(self) -> int:
        code = EXIT_CODES.get(self.code, EXIT_INTERNAL)
        if self.retryable and code == EXIT_INTERNAL:
            return EXIT_PARTIAL
        return code

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "retryable": self.retryable,
            "recovery": self.recovery,
            "details": self.details,
        }


class UsageError(RERError):
    code = "usage_error"


class ToolError(RERError):
    code = "environment_error"


class EnvironmentDegraded(ToolError):
    code = "environment_degraded"


class PartialError(RERError):
    code = "partial_result"


class ValidationFailure(RERError):
    """Evidence/state validation failed; the report must not be published."""

    code = "validation_failed"


class SchemaError(ValidationFailure):
    """Invalid document shape for an input or persisted artifact."""

    code = "schema_error"


class ConfigHashError(SchemaError):
    code = "config_hash_mismatch"


class StoreError(RERError):
    code = "store_error"


class ArtifactError(StoreError):
    """A persisted snapshot artifact is corrupt or incompatible."""

    code = "artifact_error"


class LockError(StoreError):
    code = "run_locked"

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class GitHubError(RERError):
    code = "github_error"


class GitHubAuthError(GitHubError):
    code = "github_auth"


class GitHubNotFoundError(GitHubError):
    code = "github_not_found"


class GitHubRateLimitError(GitHubError):
    code = "github_rate_limited"

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class GitHubUnavailableError(GitHubError):
    code = "github_unavailable"

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class BudgetExceededError(GitHubError):
    code = "budget_exceeded"

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class InternalError(RERError):
    code = "internal_error"
