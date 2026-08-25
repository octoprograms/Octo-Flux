from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_state, require_client_auth
from app.core.app_state import AppState
from app.models.request import ModelInfo, ModelListResponse

router = APIRouter()


@router.get("/v1/models", response_model=ModelListResponse, dependencies=[Depends(require_client_auth)])
async def list_models(state: AppState = Depends(get_state)) -> ModelListResponse:
    seen: set[str] = set()
    models: list[ModelInfo] = [ModelInfo(id="auto", owned_by="octoproxy")]
    seen.add("auto")

    for alias in state.config.aliases:
        if alias not in seen:
            models.append(ModelInfo(id=alias, owned_by="octoproxy-alias"))
            seen.add(alias)

    for provider in state.config.enabled_providers().values():
        for m in provider.models:
            if m.enabled and m.id not in seen:
                models.append(ModelInfo(id=m.id, owned_by=provider.id))
                seen.add(m.id)

    return ModelListResponse(data=models)
