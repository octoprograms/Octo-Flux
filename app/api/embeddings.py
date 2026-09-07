from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import get_state, new_request_id, require_client_auth
from app.core.app_state import AppState
from app.core.retry import ClientFacingError
from app.models.request import EmbeddingsRequest
from app.observability.logging import log_event

router = APIRouter()


@router.post("/v1/embeddings", dependencies=[Depends(require_client_auth)])
async def create_embeddings(body: EmbeddingsRequest, state: AppState = Depends(get_state)):
    request_id = new_request_id()
    payload = body.model_dump(exclude={"model"}, exclude_none=True)
    log_event("request_received", request_id=request_id, operation="embeddings", model=body.model)

    try:
        result = await state.scheduler.execute_embeddings(
            requested_model=body.model, payload=payload, request_id=request_id
        )
    except ClientFacingError as err:
        return JSONResponse(
            status_code=err.status_code,
            content={"error": {"message": err.message, "type": err.error_type, "tried": err.tried}},
        )

    response = dict(result.json_body)
    response.setdefault("model", result.model_id)
    response.setdefault("object", "list")
    response.setdefault("data", [])
    response.setdefault("usage", {})
    return JSONResponse(status_code=200, content=response)