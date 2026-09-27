"""Small, dependency-free production security guardrails."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

MAX_REQUEST_BYTES = 25 * 1024 * 1024
AUTH_WINDOW_SECONDS = 15 * 60
AUTH_MAX_ATTEMPTS = 10
SIGNUP_WINDOW_SECONDS = 60 * 60
SIGNUP_MAX_ATTEMPTS = 5
MULTIPART_WINDOW_SECONDS = 60 * 60
MULTIPART_MAX_ATTEMPTS = 20

_lock = threading.Lock()
_buckets: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_MAX_BUCKETS = 20_000


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _allow(bucket: str, key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    with _lock:
        if len(_buckets) > _MAX_BUCKETS:
            stale = [k for k, q in _buckets.items() if not q or now - q[-1] > window]
            for k in stale[: max(1, len(stale) // 2)]:
                _buckets.pop(k, None)
        q = _buckets[(bucket, key)]
        cutoff = now - window
        while q and q[0] <= cutoff:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def _rate_limit_for(request: Request):
    if request.method != "POST":
        return None
    path = request.url.path.rstrip("/") or "/"
    ip = _client_key(request)
    if path == "/api/auth/login" and not _allow("login", ip, AUTH_MAX_ATTEMPTS, AUTH_WINDOW_SECONDS):
        return JSONResponse({"detail": "Too many login attempts. Please try again later."}, status_code=429,
                            headers={"Retry-After": str(AUTH_WINDOW_SECONDS)})
    if path == "/api/auth/signup" and not _allow("signup", ip, SIGNUP_MAX_ATTEMPTS, SIGNUP_WINDOW_SECONDS):
        return JSONResponse({"detail": "Too many signup attempts. Please try again later."}, status_code=429,
                            headers={"Retry-After": str(SIGNUP_WINDOW_SECONDS)})
    if request.headers.get("content-type", "").lower().startswith("multipart/") and not _allow("multipart", ip, MULTIPART_MAX_ATTEMPTS, MULTIPART_WINDOW_SECONDS):
        return JSONResponse({"detail": "Upload rate limit exceeded. Please try again later."}, status_code=429,
                            headers={"Retry-After": str(MULTIPART_WINDOW_SECONDS)})
    return None


def install(app) -> None:
    if getattr(app.state, "security_hardening_installed", False):
        return
    app.state.security_hardening_installed = True

    @app.middleware("http")
    async def security_guard(request: Request, call_next):
        limited = _rate_limit_for(request)
        if limited is not None:
            return limited

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    return JSONResponse({"detail": "Request body exceeds the 25 MiB limit."}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)

        received = 0
        original_receive = request._receive

        async def limited_receive():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"") or b""
                received += len(chunk)
                if received > MAX_REQUEST_BYTES:
                    # ASGI receive cannot safely return a normal HTTP response
                    # once the downstream parser is consuming the body. Stop
                    # the stream; Content-Length handles normal oversized uploads.
                    raise RuntimeError("Request body exceeds the 25 MiB limit")
            return message

        request._receive = limited_receive
        return await call_next(request)


__all__ = ["install", "MAX_REQUEST_BYTES"]
