"""OpenAI-compatible request/response schemas.

We deliberately keep this permissive: unknown/extra fields are passed through
to the upstream provider untouched rather than stripped, since providers vary
in what they accept. We only validate the fields OctoProxy itself needs to
make routing decisions (model, stream).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    response_format: dict[str, Any] | None = None


class ErrorBody(BaseModel):
    message: str
    type: str
    code: str | None = None
    param: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = "octoproxy"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]
