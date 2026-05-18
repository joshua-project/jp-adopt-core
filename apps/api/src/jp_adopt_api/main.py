from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jp_adopt_api.config import get_settings
from jp_adopt_api.db import get_engine
from jp_adopt_api.routers import auth_magic_link, contacts, health, intake


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

@app.exception_handler(RequestValidationError)
async def sanitized_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """A2 / sec-3: FastAPI's default 422 handler serializes ``input`` and
    ``ctx`` into the response body. For endpoints that accept arbitrary
    body fields (e.g. PATCH /v1/contacts/{id} with ``extra='forbid'``), the
    offending field's raw value is echoed back — which then lands in proxy
    and CDN access logs. A caller who posts a secret in the wrong place
    would see it logged downstream.

    Strip ``input`` and ``ctx`` from every error entry; keep ``type``,
    ``loc``, ``msg`` which are derived from the schema and do not contain
    user-controlled data.
    """
    sanitized: list[dict[str, object]] = []
    for e in exc.errors():
        sanitized.append(
            {
                "type": e.get("type"),
                "loc": e.get("loc"),
                "msg": e.get("msg"),
                # explicitly drop "input" and "ctx"
            }
        )
    return JSONResponse(status_code=422, content={"detail": sanitized})


app.include_router(health.router)
app.include_router(contacts.router)
app.include_router(auth_magic_link.router)
app.include_router(intake.router)
