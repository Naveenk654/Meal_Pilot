"""FastAPI entrypoint. Mounts routers, applies CORS, exposes /health.

Kept intentionally thin. Business logic lives in tools/, models/, and routers/.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_settings
from backend.crons import weekly_learning
from backend.routers import admin, auth_callback, health, hitl, meals, onboarding, planner


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="mess-agent backend",
        version="0.1.0",
        docs_url="/docs" if settings.app_env == "dev" else None,
        redoc_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(auth_callback.router)
    app.include_router(onboarding.router)
    app.include_router(admin.router)
    app.include_router(planner.router)
    app.include_router(meals.router)
    app.include_router(hitl.router)
    app.include_router(weekly_learning.router)
    return app


app = create_app()
