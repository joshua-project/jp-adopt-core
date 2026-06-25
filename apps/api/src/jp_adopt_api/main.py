from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from jp_adopt_api.config import get_settings
from jp_adopt_api.db import get_engine
from jp_adopt_api.routers import (
    admin,
    auth_magic_link,
    contacts,
    drips,
    etl_admin,
    fpgs,
    health,
    intake,
    manual_contacts,
    matches,
    suppression,
    workflow,
)


def _cors_params() -> dict[str, object]:
    settings = get_settings()
    if settings.is_production:
        origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
        return {"allow_origins": origins}
    # Dev: accept localhost (any port — Next sometimes binds 3001+), plus
    # private RFC 1918 IPs and the Tailscale CGNAT range so a peer browser
    # on the tailnet can hit the API. Production CORS uses the explicit
    # allow-list above; this widened regex never runs in prod.
    return {
        "allow_origin_regex": (
            r"https?://("
            r"localhost|127\.0\.0\.1|0\.0\.0\.0|"
            r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}|"  # Tailscale CGNAT
            r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"  # private 10.x
            r"172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}|"  # private 172.16-31.x
            r"192\.168\.\d{1,3}\.\d{1,3}|"  # private 192.168.x
            r"[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)*\.ts\.net|"  # Tailscale MagicDNS
            r"\[::1\]"
            r")(:\d+)?"
        ),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    yield


# Disable interactive docs + OpenAPI schema in production: with the web
# Container App's /api proxy, /docs would otherwise be publicly reachable
# without auth. `get_settings()` is cached (lru_cache); calling it here is
# the same instance the rest of the app uses.
_docs_disabled = get_settings().is_production
app = FastAPI(
    title="JP ADOPT API",
    version="0.1.0",
    openapi_url=None if _docs_disabled else "/openapi.json",
    docs_url=None if _docs_disabled else "/docs",
    redoc_url=None if _docs_disabled else "/redoc",
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
app.include_router(matches.router)
app.include_router(workflow.router)
app.include_router(admin.router)
app.include_router(etl_admin.router)
app.include_router(drips.router)
app.include_router(suppression.router)
app.include_router(manual_contacts.router)
app.include_router(fpgs.router)


def _custom_openapi() -> dict[str, object]:
    """AC-13: declare ``IntakeBearerKey`` in ``components.securitySchemes`` so
    generated clients (``pnpm contracts:generate``) know to auto-inject the
    Authorization header on intake operations. Per-operation ``security``
    declarations live on the route decorators via ``openapi_extra``.

    AC-10: force ``required: true`` on the ``Idempotency-Key`` header for the
    intake operations. The handler types the param as ``str | None`` so we
    can return a custom 400 error (rather than FastAPI's auto-422 with a
    leaky body, especially after the sanitized validation handler strips
    ``input``/``ctx``), which makes the auto-generated OpenAPI mark the
    header optional. The spec should reflect server reality, so flip the
    flag here so generated clients send the header.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["IntakeBearerKey"] = {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "Intake API key, sent as ``Authorization: Bearer <key>``. The key "
            "must match one of the comma-separated entries in the server's "
            "``INTAKE_API_KEYS`` setting."
        ),
    }
    # AC-10: walk the intake operations and flip the Idempotency-Key header
    # parameter to required.
    paths = schema.get("paths", {})
    for path, item in paths.items():
        if not isinstance(path, str) or not path.startswith("/v1/intake/"):
            continue
        if not isinstance(item, dict):
            continue
        for op in item.values():
            if not isinstance(op, dict):
                continue
            for param in op.get("parameters", []):
                if (
                    isinstance(param, dict)
                    and param.get("name") == "Idempotency-Key"
                    and param.get("in") == "header"
                ):
                    param["required"] = True
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]
