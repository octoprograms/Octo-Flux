from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import get_state, new_request_id, require_client_auth
from app.core.app_state import AppState
from app.core.errors import classify_exception, classify_http_status, decision_for
from app.core.retry import ClientFacingError
from app.models.request import ChatCompletionRequest
from app.observability.logging import log_event
from app.providers.openai_compatible import UpstreamStreamError

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

    result.json_body.setdefault("id", f"octoproxy-{request_id}")
    return JSONResponse(status_code=200, content=result.json_body)


async def _handle_streaming(state: AppState, requested_model: str, payload: dict, request_id: str):
    resolution = state.scheduler.router.resolve(requested_model)
    if not resolution.model_found:
        raise HTTPException(status_code=404, detail={"error": {"message": f"The model '{requested_model}' does not exist.", "type": "model_not_found"}})
    if not resolution.candidates:
        raise HTTPException(status_code=503, detail={"error": {"message": "No healthy provider/key available.", "type": "service_unavailable", "tried": resolution.excluded}})

    max_attempts = state.config.routing.max_total_attempts
    attempted: set[tuple[str, str, str]] = set()

    last_error: ClientFacingError | None = None
    for attempt_number, candidate in enumerate(resolution.candidates[:max_attempts], start=1):
        target_key = (candidate.provider_id, candidate.model_id, candidate.key.name)
        if target_key in attempted:
            continue
        attempted.add(target_key)

        adapter = state.providers.adapter(candidate.provider_id)
        gen = adapter.chat_completion_stream(
            model=candidate.model_id, payload=payload, api_key=candidate.key.value, request_id=request_id
        )

        try:
            first_chunk = await gen.__anext__()
        except StopAsyncIteration:
            first_chunk = None
        except UpstreamStreamError as exc:
            category = classify_http_status(exc.status_code, exc.body_text)
            state.scheduler.apply_failure(candidate, category, None)
            decision = decision_for(category)
            log_event(
                "upstream_stream_failure_pre_byte", level="warning", request_id=request_id,
                provider=candidate.provider_id, model=candidate.model_id, key=candidate.key.name,
                category=category.value, attempt=attempt_number,
            )
            last_error = ClientFacingError(exc.status_code, exc.body_text[:200] or category.value, category.value)
            if not decision.retryable:
                return _error_response(last_error)
            continue  # try next candidate — no bytes were ever sent to the client
        except Exception as exc:  # connection-level failure before first byte
            category = classify_exception(exc)
            state.scheduler.apply_failure(candidate, category, None)
            last_error = ClientFacingError(502, "Could not connect to upstream provider.", category.value)
            log_event(
                "upstream_stream_connect_failure", level="warning", request_id=request_id,
                provider=candidate.provider_id, category=category.value, attempt=attempt_number,
            )
            continue

        # First chunk received successfully — we are committed to this
        # candidate. From here on, a failure terminates the stream instead
        # of failing over, because the client may already have partial
        # output (never duplicate/replay tokens onto a different provider).
        state.health.provider(candidate.provider_id).record_success()
        state.health.key(candidate.provider_id, candidate.key.name).record_success()
        log_event(
            "upstream_stream_started", request_id=request_id, provider=candidate.provider_id,
            model=candidate.model_id, key=candidate.key.name, attempt=attempt_number,
        )

        async def body_iterator(first: bytes | None = first_chunk, generator=gen) -> AsyncIterator[bytes]:
            if first is not None:
                yield first
            try:
                async for chunk in generator:
                    yield chunk
            except Exception as exc:  # mid-stream failure: terminate, do not fail over
                log_event(
                    "upstream_stream_interrupted", level="error", request_id=request_id,
                    error=str(exc)[:200],
                )
                return

        return StreamingResponse(body_iterator(), media_type="text/event-stream")

    if last_error is not None:
        return _error_response(last_error)
    raise HTTPException(status_code=503, detail={"error": {"message": "No candidates succeeded.", "type": "service_unavailable"}})
