"""POST /v1/responses.

This is a thin pass-through mirror of /v1/chat/completions, not a full
implementation of OpenAI's Responses API (no server-side conversation state,
no built-in tools). It accepts either `messages` (chat-style) or a plain
string/list `input` (responses-style) and routes through the same
scheduler, so simple clients pointed at `/v1/responses` still get
provider/key failover. This limitation is documented in the README.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from app.api.deps import get_state, new_request_id, require_client_auth
from app.core.app_state import AppState
from app.core.retry import ClientFacingError
from app.observability.logging import log_event

router = APIRouter()


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    input: str | list[dict[str, Any]] | None = None
    messages: list[dict[str, Any]] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None


def _to_messages(body: ResponsesRequest) -> list[dict[str, Any]]:
    if body.messages:
        return body.messages
    if isinstance(body.input, str):
        return [{"role": "user", "content": body.input}]
    if isinstance(body.input, list):
        return body.input
    return []


@router.post("/v1/responses", dependencies=[Depends(require_client_auth)])
async def create_response(body: ResponsesRequest, state: AppState = Depends(get_state)):
    request_id = new_request_id()
    messages = _to_messages(body)
    payload: dict[str, Any] = {"messages": messages}
    if body.temperature is not None:
        payload["temperature"] = body.temperature
    if body.max_output_tokens is not None:
        payload["max_tokens"] = body.max_output_tokens

    log_event("request_received", request_id=request_id, model=body.model, endpoint="/v1/responses")

    try:
        result = await state.scheduler.execute_chat_completion(
            requested_model=body.model, payload=payload, request_id=request_id
        )
    except ClientFacingError as err:
        return JSONResponse(
            status_code=err.status_code,
            content={"error": {"message": err.message, "type": err.error_type, "tried": err.tried}},
        )

    choice = (result.json_body.get("choices") or [{}])[0]
    output_text = (choice.get("message") or {}).get("content", "")
    return JSONResponse(
        status_code=200,
        content={
            "id": f"octoproxy-{request_id}",
            "object": "response",
            "model": result.model_id,
            "output_text": output_text,
            "output": result.json_body.get("choices"),
            "usage": result.json_body.get("usage"),
        },
    )
