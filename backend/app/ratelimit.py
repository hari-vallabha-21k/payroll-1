"""Small in-process rate limiter for the unauthenticated kiosk endpoints.

Good enough for a single-process prototype; a real deployment should put this
in Redis or at the edge.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware

WINDOW_SECONDS = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, limit: int = 30, prefixes: tuple[str, ...] = ("/api/webauthn",)):
        super().__init__(app)
        self.limit = limit
        self.prefixes = prefixes
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        if not request.url.path.startswith(self.prefixes):
            return await call_next(request)

        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= self.limit:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; please wait a moment"
            )
        hits.append(now)
        return await call_next(request)
