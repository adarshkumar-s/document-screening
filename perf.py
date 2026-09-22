"""Development-only request timing instrumentation for the hot data endpoints.

When the process runs in development, instrumented endpoints emit one
``[PERF]`` log line carrying wall-clock total, database time, query count,
flagged DDL statements and any named stage timings (e.g. risk calculation,
payload build). In production (``APP_ENV=production``) the module is inert:
nothing is logged, and the only residual cost is a context-variable lookup per
instrumented stage.

No sensitive information is recorded — only endpoint names, durations and
counts. Query text is never logged.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
from typing import Any, Dict, Iterator, Optional

try:
    import server
except Exception:  # pragma: no cover - tooling used only inside the app
    server = None  # type: ignore[assignment]

_ENABLED = server is not None and not getattr(server, "IS_PRODUCTION", False)

_stats: contextvars.ContextVar = contextvars.ContextVar("perf_query_stats", default=None)


def _install_query_counter() -> None:
    """Wrap the application's DB connection so instrumented requests can count
    and time their queries. Installed once, in development only."""
    if server is None:
        return
    original = server.DBConnection.execute

    def counted_execute(self, query: Any, params: Any = ()):  # noqa: ANN001
        stats = _stats.get()
        if stats is None:
            return original(self, query, params)
        statement = " ".join(str(query).split())
        if statement[:6].upper() in {"CREATE", "ALTER "} or statement[:5].upper() == "DROP ":
            stats["ddl"] += 1
        started = time.perf_counter()
        try:
            return original(self, query, params)
        finally:
            stats["queries"] += 1
            stats["db_seconds"] += time.perf_counter() - started

    server.DBConnection.execute = counted_execute


if _ENABLED:
    _install_query_counter()


@contextlib.contextmanager
def request_timer(endpoint: str) -> Iterator[Dict[str, float]]:
    """Time one request; emit a single [PERF] line in development.

    Usage inside an endpoint::

        with perf.request_timer("GET /api/land-records") as stages:
            with perf.stage(stages, "risk"):
                ...
    """
    stats: Dict[str, Any] = {"queries": 0, "db_seconds": 0.0, "ddl": 0}
    stages: Dict[str, float] = {}
    token = _stats.set(stats)
    started = time.perf_counter()
    try:
        yield stages
    finally:
        _stats.reset(token)
        if _ENABLED:
            total_ms = (time.perf_counter() - started) * 1000
            stage_text = " ".join(f"{name}={(seconds) * 1000:.0f}ms" for name, seconds in stages.items())
            print(
                f"[PERF] {endpoint} total={total_ms:.0f}ms db={stats['db_seconds'] * 1000:.0f}ms "
                f"queries={stats['queries']}"
                + (f" ddl={stats['ddl']}" if stats["ddl"] else "")
                + (f" {stage_text}" if stage_text else ""),
                flush=True,
            )


@contextlib.contextmanager
def stage(stages: Dict[str, float], name: str) -> Iterator[None]:
    """Accumulate a named stage duration into the request's stage map."""
    started = time.perf_counter()
    try:
        yield
    finally:
        stages[name] = stages.get(name, 0.0) + (time.perf_counter() - started)
