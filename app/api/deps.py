from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request

from app.core.app_state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.octoproxy


def require_client_auth(request: Request, state: AppState = Depends(get_state)) -> None:
    if not state.config.server.require_auth:
        return
    auth_header = request.headers.get("authorization", "")
    token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else None
    if not token or token not in state.config.server.client_keys:
        raise HTTPException(status_code=401, detail={"error": {"message": "Invalid or missing OctoProxy API key.", "type": "authentication_error"}})


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
