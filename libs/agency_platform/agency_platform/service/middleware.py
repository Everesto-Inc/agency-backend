"""Pure-ASGI middlewares (safe with streaming responses, unlike BaseHTTPMiddleware)."""
import re
import time
import uuid

from prometheus_client import Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agency_platform.observability.logging import request_id_var, user_id_var
from agency_platform.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9\-_]{8,64}$")


class RequestContextMiddleware:
    """Request-id propagation (accept a well-formed inbound id, else mint one) + HTTP metrics."""

    def __init__(self, app: ASGIApp, service: str) -> None:
        self.app = app
        self.service = service
        self._requests: Counter = HTTP_REQUESTS
        self._latency: Histogram = HTTP_LATENCY

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        inbound = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = inbound if _REQUEST_ID_RE.match(inbound) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        rid_token = request_id_var.set(request_id)
        uid_token = user_id_var.set(None)
        status = {"code": 500}
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope["type"] == "http":
                route = scope.get("route")
                path = getattr(route, "path", "unmatched")
                method = scope.get("method", "GET")
                self._requests.labels(self.service, method, path, str(status["code"])).inc()
                self._latency.labels(self.service, method, path).observe(time.perf_counter() - start)
            request_id_var.reset(rid_token)
            user_id_var.reset(uid_token)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Swagger UI needs its CDN assets; docs may live under a prefix (/api/v1/<topic>/docs).
        is_docs = scope["path"].endswith(("/docs", "/openapi.json", "/docs/oauth2-redirect"))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                present = {name.lower() for name, _ in h}

                def default(name: bytes, value: bytes) -> None:   # a route's own header wins (e.g. images: cacheable)
                    if name not in present:
                        h.append((name, value))
                default(b"x-content-type-options", b"nosniff")
                default(b"x-frame-options", b"DENY")
                default(b"referrer-policy", b"no-referrer")
                default(b"permissions-policy", b"camera=(), microphone=(), geolocation=()")
                if not is_docs:
                    default(b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
                    default(b"cache-control", b"no-store")
                if self.hsts:
                    default(b"strict-transport-security", b"max-age=63072000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodySizeLimitMiddleware:
    """Reject oversized bodies (declared or streamed) before handlers buffer them."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await _send_413(send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _send_413(send)


class _BodyTooLarge(Exception):
    pass


async def _send_413(send: Send) -> None:
    body = b'{"error":{"code":"PAYLOAD_TOO_LARGE","message":"Request body too large"}}'
    await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})
