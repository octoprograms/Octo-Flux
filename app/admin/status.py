from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_state, require_client_auth
from app.core.app_state import AppState
from app.core.config import ConfigError, load_config_from_env
from app.core.provider_checks import mask_api_key
from app.observability.logging import log_event
from app.providers.base import UpstreamSuccess

router = APIRouter()


@router.get("/health")
async def health(state: AppState = Depends(get_state)) -> dict:
    providers = {}
    for pid, provider in state.config.enabled_providers().items():
        keys = {}
        for key in provider.keys:
            if not key.enabled:
                continue
            check = state.health.key(pid, key.name)
            keys[key.name] = {
                "key_hint": mask_api_key(key.value),
                "status": "working" if check.last_check_ok else "unhealthy" if check.last_check_ok is False else "unknown",
                "last_check_at": check.last_check_at,
                "last_check_latency_ms": check.last_check_latency_ms,
                "reason": check.last_check_reason,
            }
        providers[pid] = {
            "status": "working" if any(item["status"] == "working" for item in keys.values()) else "unhealthy" if keys and all(item["status"] == "unhealthy" for item in keys.values()) else "unknown",
            "keys": keys,
        }
    return {"status": "ok", "providers_configured": len(providers), "providers": providers}


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
                "health_check": {
                    "status": "working" if kh.last_check_ok else "unhealthy" if kh.last_check_ok is False else "unknown",
                    "key_hint": mask_api_key(key.value),
                    "last_check_at": kh.last_check_at,
                    "last_check_latency_ms": kh.last_check_latency_ms,
                    "reason": kh.last_check_reason,
                },
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


@router.post("/admin/providers/{provider_id}/keys/{key_name}/test", dependencies=[Depends(require_client_auth)])
async def test_provider_key(provider_id: str, key_name: str, state: AppState = Depends(get_state)) -> dict:
    provider = state.config.providers.get(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider_id}'.")

    key = next((item for item in provider.keys if item.name == key_name), None)
    if key is None:
        raise HTTPException(status_code=404, detail=f"Unknown key '{key_name}' for provider '{provider_id}'.")
    if not provider.enabled or not key.enabled:
        raise HTTPException(status_code=409, detail="The provider and key must both be enabled to test them.")

    result = await state.health_monitor.check_key(provider_id, key.name, key.value)
    working = isinstance(result, UpstreamSuccess)
    return {
        "provider": provider_id,
        "key": key.name,
        "key_hint": mask_api_key(key.value),
        "status": "working" if working else "unhealthy",
        "status_code": result.status_code,
        "latency_ms": round(result.latency_ms, 1),
        "reason": None if working else result.body_text[:200] or "connection failed",
    }


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
    new_state.health_monitor.start()
    await state.health_monitor.stop()
    await old_providers.aclose()

    log_event("config_reloaded", providers=list(new_config.providers.keys()))
    return {"status": "ok", "providers": list(new_config.providers.keys())}
