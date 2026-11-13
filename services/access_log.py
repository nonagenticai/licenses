"""One access-log line per request, keyed on the CALLER (azp / realm / owner).

This service had no access log: an incident question like "which client revoked
that licence" or "who is still calling with the retired client" could only be
answered from the gateway, which sees the token but not the resolved owner.

Pure ASGI on purpose (not ``BaseHTTPMiddleware``): it wraps ``send`` to read the
FINAL status, including a 401/403 the ``AuthMiddleware`` returns itself, and it
reads the user ``AuthMiddleware`` stored in ``scope["state"]`` after the inner
app has run. It must therefore be registered OUTSIDE ``AuthMiddleware`` (added
after it — Starlette's last ``add_middleware`` is outermost).

⛔ Never logs headers, tokens or the query string: the path is ``scope["path"]``,
which carries no query, and the caller fields are the verified ones.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

access_logger = logging.getLogger("licenses.access")

#: Probe / scrape paths: one line every few seconds per pod would drown the log.
SKIP_PATHS = frozenset({"/health", "/healthz", "/ready", "/metrics"})
SKIP_PREFIXES = ("/health/",)


def _skip(path: str) -> bool:
    return path in SKIP_PATHS or path.startswith(SKIP_PREFIXES)


def _field(value: Any) -> str:
    return str(value) if value else "-"


class AccessLogMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _skip(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        status_code = 500
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            user = (scope.get("state") or {}).get("user")
            access_logger.info(
                "access method=%s path=%s status=%d duration_ms=%.1f azp=%s realm=%s owner=%s",
                scope.get("method", "-"),
                scope.get("path", "-"),
                status_code,
                duration_ms,
                _field(getattr(user, "azp", None)),
                _field(getattr(user, "realm", None)),
                _field(getattr(user, "owner_key", None)),
            )
