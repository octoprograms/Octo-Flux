from __future__ import annotations

import hmac
import uuid

from fastapi import Depends, HTTPException, Request

from app.core.app_state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.OctoFlux


def _bearer_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    return auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else None


def _matches_any(token: str, values: list[str]) -> int | None:
    """Constant-time match against every value (no early exit); returns the matching index."""
    found = None
    for i, value in enumerate(values):
        if hmac.compare_digest(token.encode(), value.encode()) and found is None:
            found = i
    return found


def _auth_error(status: int, type_: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": {"message": message, "type": type_}})


def require_client_auth(request: Request, state: AppState = Depends(get_state)) -> None:
    if not state.config.server.require_auth:
        return
    token = _bearer_token(request)
    if not token or _matches_any(token, state.config.server.client_keys) is None:
        raise _auth_error(401, "authentication_error", "Invalid or missing OctoFlux API key.")


def require_admin_auth(request: Request, state: AppState = Depends(get_state)) -> None:
    admin_keys = state.config.server.admin_keys
    if not admin_keys:
        raise _auth_error(403, "admin_disabled", "No admin keys configured (server.admin_keys).")
    token = _bearer_token(request)
    index = _matches_any(token, [k.value for k in admin_keys]) if token else None
    if index is None:
        raise _auth_error(401, "authentication_error", "Invalid or missing OctoFlux admin key.")
    request.state.admin_name = admin_keys[index].name


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
