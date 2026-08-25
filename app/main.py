from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin import status as admin_status
from app.api import chat, models, responses
from app.core.app_state import AppState
from app.core.config import ConfigError, load_config_from_env
from app.observability.logging import configure_logging, log_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(os.environ.get("OCTOPROXY_LOG_LEVEL", "INFO"))
    try:
        config = load_config_from_env()
    except ConfigError as exc:
        # Fail fast and loud — do not start with broken configuration.
        log_event("startup_config_error", level="error", error=str(exc))
        raise SystemExit(str(exc)) from exc

    app.state.octoproxy = AppState.build(config)
    log_event("startup", providers=list(config.enabled_providers().keys()))
    yield
    await app.state.octoproxy.providers.aclose()
    log_event("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(title="OctoProxy", version="0.1.0", lifespan=lifespan)
    app.include_router(models.router)
    app.include_router(chat.router)
    app.include_router(responses.router)
    app.include_router(admin_status.router)
    return app


app = create_app()
