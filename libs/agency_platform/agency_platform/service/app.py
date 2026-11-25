"""Service app factory: every service in the agency boots the same way.

Gives each service, for free: structured logging, request ids, security
headers, body-size limits, CORS, uniform error envelopes, LangSmith wiring,
DB/Redis lifecycles, a shared outbound HTTP client, liveness/readiness probes
and Prometheus metrics. Services only add their own routers and hooks.
"""
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from agency_platform.cache import close_redis, get_redis, init_redis
from agency_platform.config.settings import PlatformSettings
from agency_platform.db import close_db, get_db, init_db
from agency_platform.errors import AppError, RateLimitedError
from agency_platform.observability.logging import configure_logging, request_id_var
from agency_platform.observability.tracing import init_tracing
from agency_platform.service import openapi
from agency_platform.service.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

logger = logging.getLogger(__name__)

Hook = Callable[[FastAPI], Awaitable[None]]
ReadinessCheck = Callable[[], Awaitable[None]]


@dataclass
class ServiceSpec:
    title: str
    description: str
    version: str = "1.0.0"
    routers: Sequence[tuple[APIRouter, str]] = ()
    on_startup: Sequence[Hook] = ()
    on_shutdown: Sequence[Hook] = ()
    # OpenAPI tag groups in display order: [{"name": ..., "description": ...}]
    tags: Sequence[dict[str, str]] = ()
    # Serve /docs and /openapi.json under this prefix (topic services: /api/v1/<topic>),
    # so the public ingress, which routes by path prefix, reaches each service's docs.
    docs_prefix: str = ""
    use_db: bool = True
    use_redis: bool = True
    extra_readiness: dict[str, ReadinessCheck] = field(default_factory=dict)
    # Bigger request bodies for some paths only (path prefix → bytes), e.g. picture uploads.
    body_limits: dict[str, int] = field(default_factory=dict)


def error_envelope(code: str, message: str, details: dict | None = None) -> dict:
    body: dict = {"error": {"code": code, "message": message, "request_id": request_id_var.get()}}
    if details:
        body["error"]["details"] = details
    return body


def create_service_app(settings: PlatformSettings, spec: ServiceSpec) -> FastAPI:
    configure_logging(settings.service_name, settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_tracing(settings)
        if spec.use_db:
            await init_db(settings)
        if spec.use_redis:
            init_redis(settings)
        app.state.http = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        for hook in spec.on_startup:
            await hook(app)
        logger.info("service started", extra={"environment": settings.environment})
        try:
            yield
        finally:
            for hook in reversed(spec.on_shutdown):
                try:
                    await hook(app)
                except Exception:
                    logger.exception("shutdown hook failed")
            await app.state.http.aclose()
            if spec.use_redis:
                await close_redis()
            if spec.use_db:
                await close_db()

    docs = settings.expose_docs
    app = FastAPI(
        title=spec.title,
        description=spec.description,
        version=spec.version,
        lifespan=lifespan,
        docs_url=f"{spec.docs_prefix}/docs" if docs else None,
        redoc_url=None,
        openapi_url=f"{spec.docs_prefix}/openapi.json" if docs else None,
        swagger_ui_parameters=openapi.SWAGGER_UI,
        generate_unique_id_function=openapi.operation_id,
    )

    # Order: outermost first in execution = last added.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes, overrides=spec.body_limits)
    app.add_middleware(SecurityHeadersMiddleware, hsts=not settings.is_dev)
    app.add_middleware(RequestContextMiddleware, service=settings.service_name)

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimitedError) and exc.retry_after_s:
            headers["Retry-After"] = str(exc.retry_after_s)
        if exc.status_code >= 500:
            logger.error("app error", extra={"code": exc.code, "error": exc.message})
        return JSONResponse(
            error_envelope(exc.code, exc.message, exc.details or None), status_code=exc.status_code, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations + messages only: never echo the submitted values back.
        errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse(error_envelope("VALIDATION_ERROR", "Invalid request", {"errors": errors}), status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return JSONResponse(error_envelope("INTERNAL_ERROR", "Internal server error"), status_code=500)

    for router, prefix in spec.routers:
        app.include_router(router, prefix=prefix)

    checks: dict[str, ReadinessCheck] = dict(spec.extra_readiness)
    if spec.use_db:
        async def _db() -> None:
            await get_db().fetchval("SELECT 1 AS ok")
        checks["mysql"] = _db
    if spec.use_redis:
        async def _redis() -> None:
            await get_redis().ping()
        checks["redis"] = _redis

    @app.get("/health/live", include_in_schema=False)
    async def live() -> dict:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/health/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        results: dict[str, str] = {}
        for name, check in checks.items():
            try:
                await check()
                results[name] = "ok"
            except Exception as exc:  # report, don't raise: this is a probe
                results[name] = f"fail: {type(exc).__name__}"
        ok = all(v == "ok" for v in results.values())
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": results}, status_code=200 if ok else 503)

    openapi.install(app, spec.tags)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        # Not routed by the public ingress; scraped in-cluster only.
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def client_ip(request: Request, trusted_proxy_count: int, header: str = "") -> str:
    """Client IP: the edge proxy's header when configured (e.g. CF-Connecting-IP), else exactly N trusted proxies
    of X-Forwarded-For (prevents spoofing)."""
    if header and (value := request.headers.get(header, "").strip()):
        return value
    xff = request.headers.get("x-forwarded-for")
    if xff and trusted_proxy_count > 0:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        if len(hops) >= trusted_proxy_count:
            return hops[-trusted_proxy_count]
    return request.client.host if request.client else "unknown"
