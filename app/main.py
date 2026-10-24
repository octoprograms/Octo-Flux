from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse

from app.admin import config_api, insights
from app.admin import status as admin_status
from app.admin import ui as admin_ui
from app.api import chat, embeddings, models, responses
from app.core.app_state import AppState
from app.core.config import ConfigError, load_config_from_env
from app.observability.logging import configure_logging, log_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("OctoFlux_LOG_LEVEL", "INFO"))
    try:
        config = load_config_from_env()
    except ConfigError as exc:
        # Fail fast and loud — do not start with broken configuration.
        log_event("startup_config_error", level="error", error=str(exc))
        raise SystemExit(str(exc)) from exc

    app.state.OctoFlux = AppState.build(config)
    log_event("startup", providers=list(config.enabled_providers().keys()))
    app.state.OctoFlux.health_monitor.start()
    app.state.OctoFlux.start_flusher()
    yield
    await app.state.OctoFlux.stop_flusher()
    await app.state.OctoFlux.health_monitor.stop()
    app.state.OctoFlux.usage.close()
    await app.state.OctoFlux.providers.aclose()
    log_event("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="OctoFlux", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(HTTPException)
    async def _openai_shaped_errors(request: Request, exc: HTTPException):
        # Auth and admin errors carry {"error": {...}} as detail; return it unwrapped.
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(exc.detail, status_code=exc.status_code, headers=exc.headers)
        return await http_exception_handler(request, exc)

    app.include_router(admin_ui.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    app.include_router(embeddings.router)
    app.include_router(responses.router)
    app.include_router(admin_status.router)
    app.include_router(config_api.router)
    app.include_router(insights.router)
    return app


app = create_app()
