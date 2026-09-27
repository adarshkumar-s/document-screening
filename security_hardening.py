"""Small, dependency-free production security guardrails.

This module is installed by main.py after the FastAPI app is constructed. It
keeps request-size and abuse controls out of the large legacy server module and
makes them easy to test independently.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import HTTPException, Request

MAX_REQUEST_BYTES = 25 * 1024 * 1024  # 25 MiB hard ceiling for uploads/requests
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
    # Render terminates TLS at the proxy and supplies X-Forwarded-For. Only the
    # first address is used; rate limiting is advisory and never an auth factor.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _allow(bucket: str, key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    with _lock:
        if len(_buckets) > _MAX_BUCKETS:
            # Remove the oldest/emptiest buckets before admitting more keys.
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


def _rate_limit_for(request: Request) -> None:
    if request.method != "POST":
        return
    path = request.url.path.rstrip("/") or "/"
    ip = _client_key(request)
    if path == "/api/auth/login":
        if not _allow("login", ip, AUTH_MAX_ATTEMPTS, AUTH_WINDOW_SECONDS):
            raise HTTPException(429, "Too many login attempts. Please try again later.", headers={"Retry-After": str(AUTH_WINDOW_SECONDS)})
    elif path == "/api/auth/signup":
        if not _allow("signup", ip, SIGNUP_MAX_ATTEMPTS, SIGNUP_WINDOW_SECONDS):
            raise HTTPException(429, "Too many signup attempts. Please try again later.", headers={"Retry-After": str(SIGNUP_WINDOW_SECONDS)})
    elif request.headers.get("content-type", "").lower().startswith("multipart/"):
        if not _allow("multipart", ip, MULTIPART_MAX_ATTEMPTS, MULTIPART_WINDOW_SECONDS):
            raise HTTPException(429, "Upload rate limit exceeded. Please try again later.", headers={"Retry-After": str(MULTIPART_WINDOW_SECONDS)})


def install(app) -> None:
    """Install request-size and abuse controls exactly once."""
    if getattr(app.state, "security_hardening_installed", False):
        return
    app.state.security_hardening_installed = True

    @app.middleware("http")
    async def security_guard(request: Request, call_next):
        _rate_limit_for(request)

        # Fast rejection for normal requests and a streaming guard for chunked
        # uploads where Content-Length is absent or forged.
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "Request body exceeds the 25 MiB limit.")
            except ValueError:
                raise HTTPException(400, "Invalid Content-Length header.")

        received = 0
        original_receive = request._receive

        async def limited_receive():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"") or b""
                received += len(chunk)
                if received > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "Request body exceeds the 25 MiB limit.")
            return message

        request._receive = limited_receive
        return await call_next(request)


__all__ = ["install", "MAX_REQUEST_BYTES"]
