from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import get_state, new_request_id, require_client_auth
from app.core.app_state import AppState
from app.core.retry import ClientFacingError
from app.models.request import ChatCompletionRequest
from app.observability.logging import log_event

router = APIRouter()


def _error_response(err: ClientFacingError) -> JSONResponse:
    return JSONResponse(
        status_code=err.status_code,
        content={"error": {"message": err.message, "type": err.error_type, "tried": err.tried}},
    )


@router.post("/v1/chat/completions", dependencies=[Depends(require_client_auth)])
async def create_chat_completion(body: ChatCompletionRequest, state: AppState = Depends(get_state)):
    request_id = new_request_id()
    payload = body.model_dump(exclude={"model", "stream"}, exclude_none=True)

    log_event("request_received", request_id=request_id, model=body.model, stream=body.stream)

    if body.stream:
        return await _handle_streaming(state, body.model, payload, request_id)

    try:
        result = await state.scheduler.execute_chat_completion(
            requested_model=body.model, payload=payload, request_id=request_id
        )
    except ClientFacingError as err:
        return _error_response(err)

    result.json_body.setdefault("id", f"OctoFlux-{request_id}")
    return JSONResponse(status_code=200, content=result.json_body)


async def _handle_streaming(state: AppState, requested_model: str, payload: dict, request_id: str):
    # Failover before the first byte goes through the same scheduler as
    # non-streaming requests (limits, usage, backoff, safe error messages).
    try:
        result = await state.scheduler.execute_chat_stream(
            requested_model=requested_model, payload=payload, request_id=request_id
        )
    except ClientFacingError as err:
        return _error_response(err)

    log_event(
        "upstream_stream_started", request_id=request_id, provider=result.provider_id,
        model=result.model_id, key=result.key_name, attempt=len(result.trace),
        requested_model=requested_model, upstream_model=result.model_id,
    )

    # From the first chunk on we are committed to this candidate: a failure
    # terminates the stream instead of failing over, because the client may
    # already have partial output (never replay tokens onto another provider).
    async def body_iterator(generator: AsyncIterator[bytes] = result.stream) -> AsyncIterator[bytes]:
        try:
            async for chunk in generator:
                yield chunk
        except Exception as exc:  # mid-stream failure: terminate, do not fail over
            log_event(
                "upstream_stream_interrupted", level="error", request_id=request_id,
                error=str(exc)[:200],
            )

    return StreamingResponse(body_iterator(), media_type="text/event-stream")
