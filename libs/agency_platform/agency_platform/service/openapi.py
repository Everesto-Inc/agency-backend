"""OpenAPI conventions shared by every service, so all API docs look and behave the same.

  * Endpoints are grouped by tags (declared per service, in display order).
  * operationIds are the handler names (`register`, `run`, ...): stable and
    readable for generated clients, instead of FastAPI's path-derived ids.
  * Errors are documented with the real envelope the services return,
    ``{"error": {"code", "message", "request_id", "details"}}``, including
    422 validation errors (FastAPI's default HTTPValidationError schema is
    replaced, because our handler doesn't return that shape).
  * Bearer auth is a declared security scheme, so Swagger UI has one
    "Authorize" button instead of an Authorization field on every endpoint.
"""
from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(..., description="Stable machine-readable code, e.g. QUOTA_EXCEEDED", examples=["QUOTA_EXCEEDED"])
    message: str = Field(..., description="Human-readable explanation (safe to show to users)")
    request_id: str | None = Field(None, description="Echoed X-Request-ID; quote it when reporting issues")
    details: dict[str, Any] | None = Field(None, description="Extra context, e.g. the plan or the field errors")


class ErrorResponse(BaseModel):
    """Every non-2xx response from every service has this shape."""

    error: ErrorDetail


_DESCRIPTIONS = {
    400: "Bad request (e.g. unknown topic, malformed input)",
    401: "Missing, invalid or expired access token",
    403: "Authenticated but not allowed (code SUBSCRIPTION_REQUIRED: the plan doesn't include this topic; FORBIDDEN: admin only)",
    404: "Not found",
    409: "Conflict (e.g. email already registered)",
    413: "Request body too large",
    422: "Validation error: details.errors lists the offending fields",
    429: "Rate limit or daily quota reached (codes RATE_LIMITED / QUOTA_EXCEEDED); see the Retry-After header",
    502: "An upstream dependency (web source, model provider) failed",
    503: "Temporarily unavailable (topic disabled, model gateway down, ...)",
}


def errors(*codes: int) -> dict[int | str, dict[str, Any]]:
    """`responses=` entries for the given status codes, all using ErrorResponse."""
    return {c: {"model": ErrorResponse, "description": _DESCRIPTIONS.get(c, "Error")} for c in codes}


AUTHED = errors(401, 429)          # any endpoint behind a bearer token (rate limits apply)
ADMIN = errors(401, 403)           # admin-only endpoints


def sse_and_json(json_model: type[BaseModel], description: str, example: str) -> dict[int | str, dict[str, Any]]:
    """A 200 that is either an SSE stream (stream=true) or one JSON document (stream=false)."""
    return {
        200: {
            "model": json_model,
            "description": description,
            "content": {"text/event-stream": {"schema": {"type": "string"}, "example": example}},
        }
    }


def operation_id(route: APIRoute) -> str:
    return route.name


def install(app: FastAPI, tags: Iterable[dict[str, str]]) -> None:
    """Generate the schema once with our conventions applied."""
    tag_list = list(tags)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes,
            tags=tag_list or None, servers=app.servers or None,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.setdefault("ErrorResponse", ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}"))
        for name, sub in ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}").get("$defs", {}).items():
            components.setdefault(name, sub)
        components["ErrorResponse"].pop("$defs", None)
        error_ref = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}}}
        for ops in schema.get("paths", {}).values():
            for op in ops.values():
                responses = op.get("responses", {})
                if "422" in responses:
                    responses["422"] = {"description": _DESCRIPTIONS[422], **error_ref}
        components.pop("HTTPValidationError", None)
        components.pop("ValidationError", None)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


SWAGGER_UI = {
    "persistAuthorization": True,       # keep the token across page reloads
    "displayRequestDuration": True,
    "docExpansion": "list",
    "filter": True,
    "tryItOutEnabled": True,
    "defaultModelsExpandDepth": 0,      # schemas collapsed at the bottom
}
