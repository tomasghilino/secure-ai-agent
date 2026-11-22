"""HTTP hardening for every response: request IDs, security headers, safe errors, access log.

- **Request ID**: taken from ``X-Request-ID`` when it is a short safe token, otherwise
  generated; bound to every log line of the request and returned in the response, so a
  user's error report can be matched to the server logs.
- **Security headers** (OWASP Secure Headers Project, API profile): no MIME sniffing, no
  framing, no referrer, no caching of tenant data, a CSP that loads nothing. HSTS only
  where TLS terminates in front of the API (staging).
- **Safe errors**: an unhandled exception becomes a generic 500 with the request ID; the
  stack trace goes to the log, never to the client. Validation errors keep the field and
  the reason but not the submitted value (which may be a secret or another user's data).
- **Access log**: method, route, status, duration and the caller's tenant/user; never
  bodies, query strings or headers.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import structlog
from fastapi import Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.core.logging import get_logger

log = get_logger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
# Includes "=": API Gateway request IDs are base64-like ("HREdzhlCIAMEVHQ="), and staging
# overwrites the client value with the gateway's, so both log layers share one ID.
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._=-]{8,64}$")
# The interactive API docs load scripts and styles from a CDN; everything else loads nothing.
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
HSTS = "max-age=31536000; includeSubDomains"


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("http.unhandled_error", method=request.method, path=request.url.path)
            response = JSONResponse(
                {"detail": "internal error", "request_id": request_id},
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        self._secure(response, request.url.path)
        response.headers[REQUEST_ID_HEADER] = request_id
        principal = getattr(request.state, "principal", None)
        route = request.scope.get("route")
        log.info(
            "http.request",
            method=request.method,
            route=getattr(route, "path", request.url.path),
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            tenant_id=getattr(principal, "tenant_id", None),
            user_id=getattr(principal, "user_id", None),
        )
        return response

    def _secure(self, response: Response, path: str) -> None:
        for name, value in SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and path in DOCS_PATHS:
                continue
            response.headers.setdefault(name, value)
        if self._hsts:
            response.headers["Strict-Transport-Security"] = HSTS


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """422 with where and why, without echoing the submitted values."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101 (registered for this type)
    errors: list[dict[str, Any]] = [
        {"loc": list(error.get("loc", ())), "msg": error.get("msg"), "type": error.get("type")}
        for error in exc.errors()
    ]
    return JSONResponse({"detail": errors}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
