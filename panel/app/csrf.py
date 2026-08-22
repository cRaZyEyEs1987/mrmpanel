"""Session CSRF protection for HTML forms and same-origin fetch POSTs."""

from __future__ import annotations

import secrets
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
SESSION_KEY = "csrf_token"
FORM_FIELD = "csrf_token"
HEADER_NAME = "x-csrf-token"


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(SESSION_KEY)
    if not isinstance(token, str) or len(token) < 16:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


def _tokens_match(expected: str, provided: str | None) -> bool:
    if not provided or not expected:
        return False
    return secrets.compare_digest(str(provided), str(expected))


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        ensure_csrf_token(request)
        if request.method.upper() in SAFE_METHODS:
            return await call_next(request)

        expected = str(request.session.get(SESSION_KEY) or "")
        header_token = request.headers.get(HEADER_NAME)
        form_token = None
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            form = await request.form()
            form_token = form.get(FORM_FIELD)
            if hasattr(form_token, "__iter__") and not isinstance(form_token, str):
                # Multi-value: take first
                try:
                    form_token = next(iter(form_token), None)
                except TypeError:
                    pass

        if not _tokens_match(expected, header_token) and not _tokens_match(
            expected, str(form_token) if form_token is not None else None
        ):
            accept = (request.headers.get("accept") or "").lower()
            wants_json = "application/json" in accept or request.url.path.startswith("/api/")
            if wants_json:
                return JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)
            return HTMLResponse(
                "<!DOCTYPE html><html><body><h1>Forbidden</h1>"
                "<p>CSRF token missing or invalid. Reload the page and try again.</p>"
                "</body></html>",
                status_code=403,
            )
        return await call_next(request)
