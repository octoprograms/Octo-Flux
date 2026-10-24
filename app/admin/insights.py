"""Dashboard read endpoints: recent requests, per-minute series, latency, and the playground."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.api.deps import get_state, new_request_id, require_admin_auth
from app.core.app_state import AppState
from app.core.retry import ClientFacingError
from app.models.request import ChatCompletionRequest
from app.observability.logging import log_event

router = APIRouter(dependencies=[Depends(require_admin_auth)])


@router.get("/admin/requests")
async def list_requests(limit: int = Query(200, ge=1, le=200), state: AppState = Depends(get_state)) -> dict:
    return {"requests": state.request_log.list(limit)}


@router.get("/admin/requests/{request_id}")
async def get_request(request_id: str, state: AppState = Depends(get_state)):
    record = state.request_log.get(request_id)
    if record is None:
        return JSONResponse({"error": {"type": "not_found", "message": f"No request '{request_id}' in the log."}}, status_code=404)
    return record


@router.get("/admin/timeseries")
async def timeseries(minutes: int = Query(1440, ge=1, le=1440), state: AppState = Depends(get_state)) -> dict:
    """Per provider: [requests, errors, tokens] for each minute, oldest first."""
    return {
        "minutes": minutes,
        "providers": {p: state.series.series(p, minutes) for p in state.series.providers()},
    }


@router.get("/admin/latency")
async def latency(state: AppState = Depends(get_state)) -> dict:
    return {
        "providers": {p: state.latency.percentiles(p) for p in state.latency.providers()},
        "all": state.latency.percentiles_all(),
    }


@router.post("/admin/playground")
async def playground(body: ChatCompletionRequest, state: AppState = Depends(get_state)):
    """Runs a chat request through the gateway (always non-streaming) and returns the reply plus its log record."""
    request_id = new_request_id()
    payload = body.model_dump(exclude={"model", "stream"}, exclude_none=True)
    log_event("playground_request", request_id=request_id, model=body.model)
    try:
        result = await state.scheduler.execute_chat_completion(
            requested_model=body.model, payload=payload, request_id=request_id
        )
    except ClientFacingError as err:
        return JSONResponse(
            {"error": {"type": err.error_type, "message": err.message, "tried": err.tried},
             "record": state.request_log.get(request_id)},
            status_code=err.status_code,
        )
    return {"response": result.json_body, "record": state.request_log.get(request_id)}
