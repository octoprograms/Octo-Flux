from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_state, require_client_auth
from app.core.app_state import AppState
from app.core.config import ConfigError, load_config_from_env
from app.observability.logging import log_event

router = APIRouter()


@router.get("/health")
async def health(state: AppState = Depends(get_state)) -> dict:
    return {"status": "ok", "providers_configured": len(state.config.enabled_providers())}


@router.get("/admin/status", dependencies=[Depends(require_client_auth)])
async def admin_status(state: AppState = Depends(get_state)) -> dict:
    providers_status = {}
    for pid, provider in state.config.providers.items():
        provider_health = state.health.provider(pid)
        keys_status = {}
        for key in provider.keys:
            kh = state.health.key(pid, key.name)
            keys_status[key.name] = {
                "status": kh.status(provider.health.failure_threshold).value,
                "consecutive_failures": kh.consecutive_failures,
                "seconds_until_available": round(kh.seconds_until_available(), 1),
            }
        providers_status[pid] = {
            "enabled": provider.enabled,
            "status": provider_health.status(provider.health.failure_threshold).value if provider.enabled else "disabled",
            "models": [{"id": m.id, "enabled": m.enabled, "priority": m.priority} for m in provider.models],
            "keys": keys_status,
        }
    return {"providers": providers_status}


@router.get("/admin/providers", dependencies=[Depends(require_client_auth)])
async def admin_providers(state: AppState = Depends(get_state)) -> dict:
    # Never include key values — only names and non-secret shape.
    return {
        pid: {
            "type": p.type,
            "base_url": p.base_url,
            "enabled": p.enabled,
            "priority": p.priority,
            "key_selection": p.key_selection,
            "key_names": [k.name for k in p.keys],
            "model_ids": [m.id for m in p.models],
            "limits": p.limits.model_dump(exclude_none=True),
        }
        for pid, p in state.config.providers.items()
    }


@router.get("/admin/usage", dependencies=[Depends(require_client_auth)])
async def admin_usage(state: AppState = Depends(get_state)) -> dict:
    return state.usage.snapshot()


@router.get("/metrics", dependencies=[Depends(require_client_auth)])
async def metrics(state: AppState = Depends(get_state)) -> dict:
    return {
        "usage": state.usage.snapshot(),
        "health": state.health.snapshot(),
        "limits": state.limits.snapshot(),
    }


@router.post("/admin/reload", dependencies=[Depends(require_client_auth)])
async def admin_reload(request: Request, state: AppState = Depends(get_state)) -> dict:
    """Reloads configuration from disk without restarting the process.
    Runtime state (health, cooldowns, usage counters) is preserved for
    providers/keys that still exist after reload. Old provider HTTP clients
    are closed after the swap so pooled connections don't leak."""
    try:
        new_config = load_config_from_env()
    except ConfigError as exc:
        log_event("config_reload_failed", level="error", error=str(exc))
        return {"status": "error", "message": str(exc)}

    old_providers = state.providers
    new_state = state.replace_config(new_config)
    request.app.state.OctoFlux = new_state
    await old_providers.aclose()

    log_event("config_reloaded", providers=list(new_config.providers.keys()))
    return {"status": "ok", "providers": list(new_config.providers.keys())}
