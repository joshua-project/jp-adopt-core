from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jp_adopt_api.config import get_settings
from jp_adopt_api.db import get_engine
from jp_adopt_api.routers import contacts, health


def _cors_params() -> dict[str, object]:
    settings = get_settings()
    if settings.is_production:
        origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
        return {"allow_origins": origins}
    # Next.js dev may bind 3001+ if 3000 is taken; match any localhost port.
    return {
        "allow_origin_regex": r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    yield


app = FastAPI(
    title="JP ADOPT API",
    version="0.1.0",
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Local dev: Next.js on another port/origin. Production: set CORS_ALLOW_ORIGINS (comma-separated) on Settings.
app.add_middleware(
    CORSMiddleware,
    **_cors_params(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(contacts.router)
