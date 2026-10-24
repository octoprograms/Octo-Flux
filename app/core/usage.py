"""Usage tracking with optional SQLite persistence."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Counters:
    requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_latency_ms: float = 0.0
    rate_limit_events: int = 0
    fallback_count: int = 0
    timeout_count: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def average_latency_ms(self) -> float:
        if self.successful_requests == 0:
            return 0.0
        return self.total_latency_ms / self.successful_requests

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "average_latency_ms": round(self.average_latency_ms, 2),
            "rate_limit_events": self.rate_limit_events,
            "fallback_count": self.fallback_count,
            "timeout_count": self.timeout_count,
        }


class UsageStore(Protocol):
    def record_request(self, provider: str, model: str, key: str, success: bool) -> None: ...
    def record_tokens(self, provider: str, model: str, key: str, input_tokens: int, output_tokens: int) -> None: ...
    def record_latency(self, provider: str, model: str, ms: float) -> None: ...


@dataclass
class UsageRegistry:
    """UsageStore implementation with durable counters and per-target breakdown."""

    global_counters: Counters = field(default_factory=Counters)
    _provider_counters: dict[str, Counters] = field(default_factory=dict)
    _model_counters: dict[tuple[str, str], Counters] = field(default_factory=dict)
    _key_counters: dict[tuple[str, str], Counters] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    db_path: str | None = None
    _connection: sqlite3.Connection | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.db_path is None:
            return
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS usage_state (
                scope TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                key_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (scope, provider, model, key_name)
            )"""
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS usage_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS limit_counters (
                scope TEXT NOT NULL,
                provider TEXT NOT NULL,
                key_name TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (scope, provider, key_name)
            )"""
        )
        self._load()

    def _load(self) -> None:
        assert self._connection is not None
        import json

        row = self._connection.execute(
            "SELECT value FROM usage_meta WHERE name = 'started_at'"
        ).fetchone()
        if row:
            self.started_at = float(row[0])
        for scope, provider, model, key_name, payload in self._connection.execute(
            "SELECT scope, provider, model, key_name, payload FROM usage_state"
        ):
            counters = Counters(**json.loads(payload))
            if scope == "global":
                self.global_counters = counters
            elif scope == "provider":
                self._provider_counters[provider] = counters
            elif scope == "model":
                self._model_counters[(provider, model)] = counters
            elif scope == "key":
                self._key_counters[(provider, key_name)] = counters

    def _persist(self) -> None:
        if self._connection is None:
            return
        import json

        rows = [("global", "", "", "", self.global_counters)]
        rows.extend(("provider", provider, "", "", counters) for provider, counters in self._provider_counters.items())
        rows.extend(("model", provider, model, "", counters) for (provider, model), counters in self._model_counters.items())
        rows.extend(("key", provider, "", key, counters) for (provider, key), counters in self._key_counters.items())
        with self._connection:
            self._connection.execute("DELETE FROM usage_state")
            self._connection.executemany(
                "INSERT INTO usage_state(scope, provider, model, key_name, payload) VALUES (?, ?, ?, ?, ?)",
                [(scope, provider, model, key, json.dumps(counters.__dict__)) for scope, provider, model, key, counters in rows],
            )
            self._connection.execute(
                "INSERT INTO usage_meta(name, value) VALUES ('started_at', ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (str(self.started_at),),
            )

    def flush(self) -> None:
        """Write counters to SQLite (called every 60 s and on shutdown, not per request)."""
        with self._lock:
            self._persist()

    def save_limits(self, rows: list[tuple[str, str, str, dict]]) -> None:
        if self._connection is None:
            return
        import json

        with self._lock, self._connection:
            self._connection.execute("DELETE FROM limit_counters")
            self._connection.executemany(
                "INSERT INTO limit_counters(scope, provider, key_name, payload) VALUES (?, ?, ?, ?)",
                [(scope, provider, key, json.dumps(payload)) for scope, provider, key, payload in rows],
            )

    def load_limits(self) -> list[tuple[str, str, str, dict]]:
        if self._connection is None:
            return []
        import json

        with self._lock:
            return [
                (scope, provider, key, json.loads(payload))
                for scope, provider, key, payload in self._connection.execute(
                    "SELECT scope, provider, key_name, payload FROM limit_counters"
                )
            ]

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._persist()
                self._connection.close()
                self._connection = None

    def _get(self, store: dict, key) -> Counters:
        if key not in store:
            store[key] = Counters()
        return store[key]

    def record_attempt(self, provider: str, model: str, key: str, success: bool, latency_ms: float) -> None:
        with self._lock:
            for c in (
                self.global_counters,
                self._get(self._provider_counters, provider),
                self._get(self._model_counters, (provider, model)),
                self._get(self._key_counters, (provider, key)),
            ):
                c.requests += 1
                if success:
                    c.successful_requests += 1
                    c.total_latency_ms += latency_ms
                else:
                    c.failed_requests += 1

    def record_tokens(self, provider: str, model: str, key: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            for c in (
                self.global_counters,
                self._get(self._provider_counters, provider),
                self._get(self._model_counters, (provider, model)),
                self._get(self._key_counters, (provider, key)),
            ):
                c.input_tokens += input_tokens
                c.output_tokens += output_tokens

    def record_rate_limited(self, provider: str) -> None:
        with self._lock:
            self.global_counters.rate_limit_events += 1
            self._get(self._provider_counters, provider).rate_limit_events += 1

    def record_timeout(self, provider: str) -> None:
        with self._lock:
            self.global_counters.timeout_count += 1
            self._get(self._provider_counters, provider).timeout_count += 1

    def record_fallback(self) -> None:
        with self._lock:
            self.global_counters.fallback_count += 1

    def snapshot(self) -> dict:
        return {
            "persistence": {"enabled": self._connection is not None},
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "global": self.global_counters.as_dict(),
            "providers": {k: v.as_dict() for k, v in self._provider_counters.items()},
            "models": {f"{k[0]}/{k[1]}": v.as_dict() for k, v in self._model_counters.items()},
            "keys": {f"{k[0]}:{k[1]}": v.as_dict() for k, v in self._key_counters.items()},
        }
