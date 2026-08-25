"""Error classification.

Normalizes upstream HTTP responses / exceptions into a closed set of
categories, independent of which provider produced them, and attaches a
`RetryDecision` describing what the router/retry engine should do. See
ARCHITECTURE.md's "Error classification" table for the rationale.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import httpx


class ErrorCategory(str, Enum):
    AUTHENTICATION_ERROR = "authentication_error"
    AUTHORIZATION_ERROR = "authorization_error"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    MODEL_NOT_FOUND = "model_not_found"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LENGTH_EXCEEDED = "context_length_exceeded"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    OVERLOADED = "overloaded"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryDecision:
    category: ErrorCategory
    retryable: bool  # can this request be retried against *some* candidate at all
    retry_same_key: bool  # is it worth retrying the identical (provider, model, key)
    retry_other_key: bool  # try another key on the same provider
    retry_other_provider: bool  # try another provider entirely
    mark_unhealthy: str | None  # None | "key" | "provider" (soft, uses failure_threshold)
    cooldown: bool  # apply cooldown to the failing target
    long_cooldown: bool  # use health.long_cooldown_seconds instead of cooldown_seconds
    return_to_client: bool  # if all retries are exhausted or this is decisive, this is what caller sees
    retry_after_seconds: float | None = None  # honored from upstream Retry-After header


_DECISIONS: dict[ErrorCategory, RetryDecision] = {
    ErrorCategory.AUTHENTICATION_ERROR: RetryDecision(
        ErrorCategory.AUTHENTICATION_ERROR, True, False, False, True, "key", True, False, True
    ),
    ErrorCategory.AUTHORIZATION_ERROR: RetryDecision(
        ErrorCategory.AUTHORIZATION_ERROR, True, False, False, True, "key", True, False, True
    ),
    ErrorCategory.RATE_LIMITED: RetryDecision(
        ErrorCategory.RATE_LIMITED, True, False, True, True, None, True, False, True
    ),
    ErrorCategory.QUOTA_EXCEEDED: RetryDecision(
        ErrorCategory.QUOTA_EXCEEDED, True, False, True, True, None, True, True, True
    ),
    ErrorCategory.MODEL_NOT_FOUND: RetryDecision(
        ErrorCategory.MODEL_NOT_FOUND, True, False, False, True, None, False, False, True
    ),
    ErrorCategory.INVALID_REQUEST: RetryDecision(
        ErrorCategory.INVALID_REQUEST, False, False, False, False, None, False, False, True
    ),
    ErrorCategory.CONTEXT_LENGTH_EXCEEDED: RetryDecision(
        ErrorCategory.CONTEXT_LENGTH_EXCEEDED, False, False, False, False, None, False, False, True
    ),
    ErrorCategory.SERVER_ERROR: RetryDecision(
        ErrorCategory.SERVER_ERROR, True, True, True, True, "provider", True, False, True
    ),
    ErrorCategory.TIMEOUT: RetryDecision(
        ErrorCategory.TIMEOUT, True, True, True, True, "provider", True, False, True
    ),
    ErrorCategory.CONNECTION_ERROR: RetryDecision(
        ErrorCategory.CONNECTION_ERROR, True, True, True, True, "provider", True, False, True
    ),
    ErrorCategory.SERVICE_UNAVAILABLE: RetryDecision(
        ErrorCategory.SERVICE_UNAVAILABLE, True, True, True, True, "provider", True, False, True
    ),
    ErrorCategory.OVERLOADED: RetryDecision(
        ErrorCategory.OVERLOADED, True, True, True, True, "provider", True, False, True
    ),
    ErrorCategory.UNKNOWN: RetryDecision(
        ErrorCategory.UNKNOWN, True, False, True, True, None, True, False, True
    ),
}


def decision_for(category: ErrorCategory) -> RetryDecision:
    return _DECISIONS[category]


_CONTEXT_LENGTH_RE = re.compile(r"context.{0,20}length|maximum context|too many tokens", re.I)
_MODEL_NOT_FOUND_RE = re.compile(r"model.{0,20}(not found|does not exist|unknown)", re.I)
_QUOTA_RE = re.compile(r"quota|insufficient.?(credit|balance|funds)|billing", re.I)


def classify_http_status(status_code: int, body_text: str = "") -> ErrorCategory:
    """Classify based on HTTP status code plus a light look at the body text
    for the ambiguous cases (400 vs context-length, 429 vs quota)."""
    if status_code == 401:
        return ErrorCategory.AUTHENTICATION_ERROR
    if status_code == 403:
        return ErrorCategory.AUTHORIZATION_ERROR
    if status_code == 404:
        return ErrorCategory.MODEL_NOT_FOUND
    if status_code == 408:
        return ErrorCategory.TIMEOUT
    if status_code == 429:
        if _QUOTA_RE.search(body_text):
            return ErrorCategory.QUOTA_EXCEEDED
        return ErrorCategory.RATE_LIMITED
    if status_code == 400 or status_code == 422:
        if _CONTEXT_LENGTH_RE.search(body_text):
            return ErrorCategory.CONTEXT_LENGTH_EXCEEDED
        if _MODEL_NOT_FOUND_RE.search(body_text):
            return ErrorCategory.MODEL_NOT_FOUND
        return ErrorCategory.INVALID_REQUEST
    if status_code == 409:
        return ErrorCategory.INVALID_REQUEST
    if status_code == 503:
        return ErrorCategory.SERVICE_UNAVAILABLE
    if status_code == 529:  # some providers use this for "overloaded"
        return ErrorCategory.OVERLOADED
    if 500 <= status_code < 600:
        return ErrorCategory.SERVER_ERROR
    return ErrorCategory.UNKNOWN


def classify_exception(exc: Exception) -> ErrorCategory:
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return ErrorCategory.TIMEOUT
    if isinstance(exc, httpx.ConnectError | httpx.NetworkError | httpx.RemoteProtocolError):
        return ErrorCategory.CONNECTION_ERROR
    if isinstance(exc, httpx.TimeoutException):
        return ErrorCategory.TIMEOUT
    return ErrorCategory.UNKNOWN


def parse_retry_after(headers: httpx.Headers) -> float | None:
    val = headers.get("retry-after")
    if val is None:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None
