from __future__ import annotations

import httpx
import pytest

from app.core.errors import ErrorCategory, classify_exception, classify_http_status, decision_for, parse_retry_after


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, "", ErrorCategory.AUTHENTICATION_ERROR),
        (403, "", ErrorCategory.AUTHORIZATION_ERROR),
        (404, "", ErrorCategory.MODEL_NOT_FOUND),
        (408, "", ErrorCategory.TIMEOUT),
        (429, "rate limit exceeded", ErrorCategory.RATE_LIMITED),
        (429, "insufficient quota, please check your billing", ErrorCategory.QUOTA_EXCEEDED),
        (400, "invalid request: missing field", ErrorCategory.INVALID_REQUEST),
        (400, "This model's maximum context length is 4096 tokens", ErrorCategory.CONTEXT_LENGTH_EXCEEDED),
        (409, "", ErrorCategory.INVALID_REQUEST),
        (500, "", ErrorCategory.SERVER_ERROR),
        (502, "", ErrorCategory.SERVER_ERROR),
        (503, "", ErrorCategory.SERVICE_UNAVAILABLE),
        (529, "", ErrorCategory.OVERLOADED),
        (418, "", ErrorCategory.UNKNOWN),
    ],
)
def test_classify_http_status(status, body, expected):
    assert classify_http_status(status, body) == expected


def test_classify_connect_error():
    exc = httpx.ConnectError("boom")
    assert classify_exception(exc) == ErrorCategory.CONNECTION_ERROR


def test_classify_timeout():
    exc = httpx.ReadTimeout("boom")
    assert classify_exception(exc) == ErrorCategory.TIMEOUT


@pytest.mark.parametrize(
    "category,retryable",
    [
        (ErrorCategory.INVALID_REQUEST, False),
        (ErrorCategory.CONTEXT_LENGTH_EXCEEDED, False),
        (ErrorCategory.RATE_LIMITED, True),
        (ErrorCategory.SERVER_ERROR, True),
        (ErrorCategory.TIMEOUT, True),
        (ErrorCategory.AUTHENTICATION_ERROR, True),  # retryable against a *different* key
    ],
)
def test_retry_decision_matrix(category, retryable):
    assert decision_for(category).retryable is retryable


def test_authentication_error_does_not_retry_same_key():
    d = decision_for(ErrorCategory.AUTHENTICATION_ERROR)
    assert d.retry_same_key is False
    assert d.retry_other_key is False
    assert d.retry_other_provider is True


def test_invalid_request_never_triggers_provider_failover():
    d = decision_for(ErrorCategory.INVALID_REQUEST)
    assert d.retry_other_provider is False
    assert d.retryable is False


def test_parse_retry_after_present():
    headers = httpx.Headers({"retry-after": "5"})
    assert parse_retry_after(headers) == 5.0


def test_parse_retry_after_missing():
    headers = httpx.Headers({})
    assert parse_retry_after(headers) is None


def test_parse_retry_after_malformed():
    headers = httpx.Headers({"retry-after": "not-a-number"})
    assert parse_retry_after(headers) is None
