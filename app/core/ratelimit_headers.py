"""Parse upstream rate-limit headers (Groq/OpenAI and OpenRouter styles)."""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

MAX_RESET_SECONDS = 86400.0
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


@dataclass
class RateLimitInfo:
    remaining_requests: int | None
    remaining_tokens: int | None
    resets_at: float | None  # wall-clock epoch seconds


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value is not None else None
    except ValueError:
        return None


def _duration_seconds(value: str) -> float | None:
    """'2m59.5s' / '7.66s' / '120ms' -> seconds; anything else -> None."""
    value = value.strip()
    if not value or _DURATION_RE.sub("", value):
        return None
    return sum(float(n) * _UNIT_SECONDS[u] for n, u in _DURATION_RE.findall(value))


def _epoch_seconds(value: str, now: float) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    # OpenRouter sends epoch milliseconds; accept epoch seconds and plain deltas too.
    if number > 1e11:
        return number / 1000.0 - now
    if number > 1e9:
        return number - now
    return number


def _resets_at(delta: float | None, now: float) -> float | None:
    if delta is None or delta <= 0:
        return None
    return now + min(delta, MAX_RESET_SECONDS)


def parse_rate_limit_headers(headers: httpx.Headers, now: float) -> RateLimitInfo | None:
    remaining_requests = _int(headers.get("x-ratelimit-remaining-requests"))
    remaining_tokens = _int(headers.get("x-ratelimit-remaining-tokens"))
    reset_requests = headers.get("x-ratelimit-reset-requests")
    reset_tokens = headers.get("x-ratelimit-reset-tokens")
    request_reset = _resets_at(_duration_seconds(reset_requests), now) if reset_requests else None
    token_reset = _resets_at(_duration_seconds(reset_tokens), now) if reset_tokens else None

    if remaining_requests is None and headers.get("x-ratelimit-remaining") is not None:
        remaining_requests = _int(headers.get("x-ratelimit-remaining"))
        reset = headers.get("x-ratelimit-reset")
        request_reset = _resets_at(_epoch_seconds(reset, now), now) if reset else None

    if remaining_requests is None and remaining_tokens is None:
        return None
    # The reset that matters is the one for the exhausted budget; otherwise the later one.
    if remaining_requests == 0 and request_reset is not None:
        resets_at = request_reset
    elif remaining_tokens == 0 and token_reset is not None:
        resets_at = token_reset
    else:
        known = [r for r in (request_reset, token_reset) if r is not None]
        resets_at = max(known) if known else None
    return RateLimitInfo(remaining_requests, remaining_tokens, resets_at)
