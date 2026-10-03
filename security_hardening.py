"""Small, dependency-free production security guardrails."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

import os

MAX_REQUEST_BYTES = 25 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    """Read a positive integer limit from the environment.

    Anything unparseable or non-positive falls back to the documented default
    rather than silently disabling the limit.
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Limits are configuration, not code: a deployment behind a corporate NAT or a
# load-balanced proxy legitimately needs different numbers than a laptop.
AUTH_WINDOW_SECONDS = _env_int("RATE_LIMIT_AUTH_WINDOW_SECONDS", 15 * 60)
AUTH_MAX_ATTEMPTS = _env_int("RATE_LIMIT_AUTH_MAX_ATTEMPTS", 10)
SIGNUP_WINDOW_SECONDS = _env_int("RATE_LIMIT_SIGNUP_WINDOW_SECONDS", 60 * 60)
SIGNUP_MAX_ATTEMPTS = _env_int("RATE_LIMIT_SIGNUP_MAX_ATTEMPTS", 5)
MULTIPART_WINDOW_SECONDS = _env_int("RATE_LIMIT_MULTIPART_WINDOW_SECONDS", 60 * 60)
MULTIPART_MAX_ATTEMPTS = _env_int("RATE_LIMIT_MULTIPART_MAX_ATTEMPTS", 20)
TRUSTED_PROXY_IPS = {x.strip() for x in os.getenv('TRUSTED_PROXY_IPS', '').split(',') if x.strip()}


def reset_buckets() -> None:
    """Drop all rate-limit state.

    Used by the test suite so the in-process sliding windows cannot leak
    between tests: buckets are module-global, so without this a whole pytest
    process shares one budget and every test after the fifth signup sees 429.
    """
    with _lock:
        _buckets.clear()

_lock = threading.Lock()
_buckets: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)
_MAX_BUCKETS = 20_000


def _client_key(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in TRUSTED_PROXY_IPS:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:64]
    return peer[:64]


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
                    raise RuntimeError("Request body exceeds the 25 MiB limit")
            return message

        request._receive = limited_receive
        try:
            return await call_next(request)
        except RuntimeError as exc:
            if str(exc) == "Request body exceeds the 25 MiB limit":
                return JSONResponse({"detail": "Request body exceeds the 25 MiB limit."}, status_code=413)
            raise


__all__ = ["install", "reset_buckets", "MAX_REQUEST_BYTES"]
