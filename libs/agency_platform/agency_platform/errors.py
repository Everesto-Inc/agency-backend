"""Typed application errors, rendered uniformly by the service app factory.

Domain code raises these; it never builds HTTP responses itself. Clients get a
stable `code` to branch on (e.g. show an upgrade prompt on SUBSCRIPTION_REQUIRED)
and never see stack traces or internals.
"""


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str = "Internal error", **details: object) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class BadRequestError(AppError):
    status_code = 400
    code = "BAD_REQUEST"


class UnauthorizedError(AppError):
    status_code = 401
    code = "UNAUTHORIZED"


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"


class SubscriptionRequiredError(ForbiddenError):
    code = "SUBSCRIPTION_REQUIRED"


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class RateLimitedError(AppError):
    status_code = 429
    code = "RATE_LIMITED"

    def __init__(self, message: str, retry_after_s: int | None = None, **details: object) -> None:
        super().__init__(message, **details)
        self.retry_after_s = retry_after_s


class QuotaExceededError(RateLimitedError):
    code = "QUOTA_EXCEEDED"


class UpstreamError(AppError):
    status_code = 502
    code = "UPSTREAM_ERROR"


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "SERVICE_UNAVAILABLE"
