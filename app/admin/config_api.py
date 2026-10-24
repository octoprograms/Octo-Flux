from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.admin.status import apply_config
from app.api.deps import require_admin_auth
from app.core import config_store
from app.core.config_store import ConfigStoreError
from app.observability.logging import log_event

router = APIRouter(dependencies=[Depends(require_admin_auth)])
_save_lock = asyncio.Lock()


class ConfigUpdate(BaseModel):
    version: str
    config: dict


def _error(exc: ConfigStoreError) -> JSONResponse:
    error: dict = {"type": exc.type, "message": exc.message}
    if exc.errors is not None:
        error["errors"] = exc.errors
    return JSONResponse({"error": error}, status_code=exc.status)


async def save_raw(request: Request, raw: dict, expected_version: str | None, data: bytes | None = None) -> str:
    """Validate, back up, write and hot-reload. expected_version=None skips the conflict check;
    `data` writes those exact bytes (a restored backup) instead of re-dumping `raw`."""
    path = config_store.config_path()
    async with _save_lock:
        current, version = config_store.read_raw(path)
        if expected_version is not None and version != expected_version:
            raise ConfigStoreError(409, "config_conflict", "The config changed since it was loaded — reload and retry.")
        raw = config_store.unmask_secrets(raw, current)
        new_config = config_store.validate_raw(raw)
        new_version = config_store.write_with_backup(path, raw, data)
        await apply_config(request.app, new_config)
    log_event("config_saved", admin=request.state.admin_name, version=new_version[:12])
    return new_version


@router.get("/admin/config")
async def get_config() -> dict:
    try:
        raw, version = config_store.read_raw(config_store.config_path())
    except ConfigStoreError as exc:
        return _error(exc)
    return {"version": version, "config": config_store.mask_secrets(raw)}


@router.put("/admin/config")
async def put_config(body: ConfigUpdate, request: Request):
    try:
        return {"version": await save_raw(request, body.config, body.version)}
    except ConfigStoreError as exc:
        return _error(exc)


@router.get("/admin/config/backups")
async def get_backups() -> dict:
    return {"backups": config_store.list_backups(config_store.config_path())}


@router.post("/admin/config/backups/{name}/restore")
async def restore_backup(name: str, request: Request):
    try:
        raw, data = config_store.read_backup(config_store.config_path(), name)
        return {"version": await save_raw(request, raw, None, data)}
    except ConfigStoreError as exc:
        return _error(exc)
