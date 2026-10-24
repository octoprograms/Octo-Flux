"""In-memory ring of recent requests for the dashboard. Never stores message content."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field


@dataclass
class RequestRecord:
    id: str
    started_at: float
    requested_model: str
    upstream_model: str | None
    provider: str | None
    key: str | None
    status: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    stream: bool
    attempts: list[dict] = field(default_factory=list)  # provider, model, key, outcome, latency_ms
    operation: str = "chat.completions"


class RequestLog:
    def __init__(self, maxlen: int = 200) -> None:
        self._records: deque[RequestRecord] = deque(maxlen=maxlen)

    def add(self, record: RequestRecord) -> None:
        self._records.append(record)

    def list(self, limit: int = 200) -> list[dict]:
        return [asdict(r) for r in list(reversed(self._records))[:limit]]

    def get(self, request_id: str) -> dict | None:
        return next((asdict(r) for r in reversed(self._records) if r.id == request_id), None)
