from __future__ import annotations

import httpx
import pytest

from app.core.ratelimit_headers import parse_rate_limit_headers

NOW = 1_700_000_000.0


def test_groq_headers_parse():
    info = parse_rate_limit_headers(
        httpx.Headers({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "2m59.5s"}), NOW
    )
    assert info.remaining_requests == 0
    assert info.resets_at == pytest.approx(NOW + 179.5)


def test_groq_token_headers_parse():
    info = parse_rate_limit_headers(
        httpx.Headers({"X-RateLimit-Remaining-Tokens": "1200", "x-ratelimit-reset-tokens": "120ms"}), NOW
    )
    assert info.remaining_tokens == 1200
    assert info.remaining_requests is None
    assert info.resets_at == pytest.approx(NOW + 0.12)


def test_openrouter_headers_parse():
    info = parse_rate_limit_headers(
        httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int((NOW + 30) * 1000))}), NOW
    )
    assert info.remaining_requests == 0
    assert info.resets_at == pytest.approx(NOW + 30, abs=1)


def test_no_headers_returns_none():
    assert parse_rate_limit_headers(httpx.Headers({"content-type": "application/json"}), NOW) is None


def test_absurd_reset_headers_clamped():
    def reset(value: str):
        return parse_rate_limit_headers(
            httpx.Headers({"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": value}), NOW
        ).resets_at

    assert reset("-5s") is None
    assert reset("abc") is None
    assert reset("72h") == pytest.approx(NOW + 86400)
    far = parse_rate_limit_headers(
        httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int((NOW + 3 * 86400) * 1000))}), NOW
    )
    assert far.resets_at == pytest.approx(NOW + 86400)
    past = parse_rate_limit_headers(
        httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int((NOW - 60) * 1000))}), NOW
    )
    assert past.resets_at is None
    assert parse_rate_limit_headers(httpx.Headers({"x-ratelimit-remaining-requests": "lots"}), NOW) is None
