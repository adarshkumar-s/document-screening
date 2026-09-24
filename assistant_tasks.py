"""Idempotent assistant request lifecycle (SA reliability upgrade).

This module makes an assistant request a *logical operation* with a durable,
server-side lifecycle instead of a fire-and-forget HTTP call.

Guarantees (the CRITICAL no-duplicate-mutation requirement):

1. Every logical request carries a client-generated ``request_id``. The
   original request payload is preserved server-side automatically, so a
   retry never has to re-type anything and can never silently become a
   different request.
2. A request is executed **at most once at a time**. A retry while the work
   is still running (for example after a frontend timeout) only re-attaches
   to the in-flight execution and waits for its result. It never starts a
   second execution.
3. Once an execution finished, a retry **replays the stored result** and
   never re-executes.
4. If a worker was lost mid-flight (process restart), a stale RUNNING row is
   only re-claimed after a generous stale window AND the runner is expected
   to be idempotent through ``idempotency_key`` (see ``ai_governance``:
   proposals are keyed so a re-run can never create a second proposal).
5. Errors are classified: recoverable (``TIMEOUT``, ``PROVIDER``) are
   retryable; permanent authorization/validation errors are never retried.

Nothing in this module interprets model output as authorization and nothing
in it performs application mutations itself - it only schedules the runner
supplied by the caller.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Dict, Optional

TASKS_TABLE_READY = False

# Public task states
STATE_PENDING = "PENDING"
STATE_RUNNING = "RUNNING"
STATE_SUCCEEDED = "SUCCEEDED"
STATE_FAILED_RETRYABLE = "FAILED_RETRYABLE"
STATE_FAILED_PERMANENT = "FAILED_PERMANENT"
STATE_CANCELLED = "CANCELLED"

TERMINAL_STATES = {STATE_SUCCEEDED, STATE_FAILED_PERMANENT, STATE_CANCELLED}

# Error codes surfaced to the client. Only TIMEOUT/PROVIDER are recoverable.
ERROR_TIMEOUT = "TIMEOUT"
ERROR_PROVIDER = "PROVIDER"
ERROR_VALIDATION = "VALIDATION"
ERROR_AUTH = "AUTH"
ERROR_INTERNAL = "INTERNAL"
RETRYABLE_ERRORS = {ERROR_TIMEOUT, ERROR_PROVIDER}

# A RUNNING row older than this is considered abandoned (worker lost) and may
# be re-claimed. Re-claimed runners MUST be idempotent via idempotency keys.
STALE_RUNNING_SECONDS = 300.0

_REQUEST_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")

_WORKER_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="assistant-task")


class AssistantProviderError(RuntimeError):
    """Recoverable external AI-provider failure (safe to retry)."""


class AssistantTimeoutError(RuntimeError):
    """The operation timed out (safe to retry)."""


class AssistantPermanentError(RuntimeError):
    """Permanent failure that must NOT offer a retry."""

    def __init__(self, detail: str, code: str = ERROR_INTERNAL):
        super().__init__(detail)
        self.code = code


class RequestConflict(Exception):
    """The request_id was already used for a different logical request."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _get_db():
    import server

    return server.get_db()


def ensure_tasks_table() -> None:
    """Create the request registry without touching existing schemas."""
    global TASKS_TABLE_READY
    if TASKS_TABLE_READY:
        return
    with _get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_requests (
                request_id TEXT PRIMARY KEY,
                surface TEXT NOT NULL,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                result TEXT NOT NULL DEFAULT '{}',
                error_code TEXT,
                error_detail TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_requests_user ON assistant_requests(user_id, created_at)"
        )
    TASKS_TABLE_READY = True


def normalize_request_id(raw: Optional[str]) -> Optional[str]:
    """Validate a client-supplied request id. Returns None when absent."""
    if raw is None:
        return None
    value = str(raw).strip()
    if not (8 <= len(value) <= 64) or any(ch not in _REQUEST_ID_ALLOWED for ch in value):
        raise AssistantPermanentError(
            "Invalid request identifier.", code=ERROR_VALIDATION
        )
    return value


def new_request_id() -> str:
    return uuid.uuid4().hex


def _payload_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_loads(raw: Any, default: Any) -> Any:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return parsed if parsed is not None else default
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _api_state(db_state: str, row: Optional[Dict[str, Any]] = None) -> str:
    return {
        STATE_PENDING: "pending",
        STATE_RUNNING: "running",
        STATE_SUCCEEDED: "succeeded",
        STATE_FAILED_RETRYABLE: "failed_retryable",
        STATE_FAILED_PERMANENT: "failed_permanent",
        STATE_CANCELLED: "cancelled",
    }.get(db_state, "failed_permanent")


def _envelope(
    row: Dict[str, Any],
    *,
    replayed: bool = False,
    error_override: Optional[Dict[str, Any]] = None,
    state_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the client-facing envelope for a request row.

    ``state_override`` supports the ``timeout`` surface state: the HTTP call
    ran out of budget while the worker keeps running server-side.
    """
    error = error_override
    if error is None and row.get("error_code"):
        error = {
            "code": row.get("error_code"),
            "detail": row.get("error_detail") or "The request failed.",
            "retryable": row.get("error_code") in RETRYABLE_ERRORS,
        }
    result = _json_loads(row.get("result"), None)
    if result == {}:
        result = None
    payload = _json_loads(row.get("payload"), {}) or {}
    return {
        "request_id": row.get("request_id"),
        "kind": row.get("kind"),
        "state": state_override or _api_state(row.get("state", "")),
        "attempts": int(row.get("attempts") or 0),
        "replayed": bool(replayed),
        "result": result,
        "error": error,
        # The original request is preserved server-side and returned so the UI
        # can offer a real "Try again" without re-typing anything.
        "request": {"kind": row.get("kind"), "query": payload.get("query", "")},
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "finished_at": row.get("finished_at"),
    }


def _fetch_row(request_id: str) -> Optional[Dict[str, Any]]:
    with _get_db() as db:
        row = db.execute(
            "SELECT * FROM assistant_requests WHERE request_id=?", (request_id,)
        ).fetchone()
    return dict(row) if row else None


def _classify_exception(exc: BaseException) -> tuple:
    if isinstance(exc, AssistantProviderError):
        return ERROR_PROVIDER, str(exc) or "The AI provider is temporarily unavailable.", True
    if isinstance(exc, AssistantTimeoutError):
        return ERROR_TIMEOUT, str(exc) or "The assistant timed out.", True
    if isinstance(exc, AssistantPermanentError):
        return exc.code, str(exc) or "The request failed.", False
    # Authorization and validation mistakes are permanent; never retry them.
    return ERROR_INTERNAL, "The request failed.", False


def _mark_finished(request_id: str, state: str, result: Dict[str, Any], error_code, error_detail) -> None:
    """Persist a worker outcome. Never runs inside another open transaction."""
    now = time.time()
    with _get_db() as db:
        # The result of work that already ran is ALWAYS preserved - even if the
        # row was cancelled while the worker was in flight - so a retry can
        # replay it instead of ever executing again.
        db.execute(
            "UPDATE assistant_requests SET result=?, error_code=?, error_detail=?, finished_at=?, updated_at=?, "
            "state = CASE WHEN state=? THEN ? ELSE state END WHERE request_id=?",
            (
                json.dumps(result or {}, ensure_ascii=False, default=str),
                error_code,
                error_detail,
                now,
                now,
                STATE_RUNNING,
                state,
                request_id,
            ),
        )


def get_request(request_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a request envelope. Actor isolation: only the owner sees it."""
    ensure_tasks_table()
    row = _fetch_row(request_id)
    if not row or row.get("user_id") != user_id:
        return None
    return _envelope(row)


def _claim(request_id: str, allowed_states: tuple) -> Optional[Dict[str, Any]]:
    """Atomically claim a request for execution (compare-and-swap).

    Returns the updated row when this caller won the claim, otherwise None.
    """
    now = time.time()
    placeholders = ",".join("?" for _ in allowed_states)
    with _get_db() as db:
        cur = db.execute(
            f"UPDATE assistant_requests SET state=?, attempts=attempts+1, started_at=?, updated_at=?, "
            f"error_code=NULL, error_detail=NULL WHERE request_id=? AND state IN ({placeholders})",
            (STATE_RUNNING, now, now, request_id, *allowed_states),
        )
        won = getattr(cur, "rowcount", 1) == 1
        if not won:
            return None
        row = db.execute(
            "SELECT * FROM assistant_requests WHERE request_id=?", (request_id,)
        ).fetchone()
    return dict(row) if row else None


def _execute_claimed(row: Dict[str, Any], runner: Callable[[Dict[str, Any], str], Dict[str, Any]], wait_seconds: float) -> Dict[str, Any]:
    """Run the claimed work in a worker thread and wait up to ``wait_seconds``.

    On response timeout the worker CONTINUES server-side (row stays RUNNING).
    The frontend gets a retryable timeout envelope; "Try again" re-attaches to
    this very execution instead of starting a second one.
    """
    request_id = row["request_id"]
    holder: Dict[str, Any] = {}

    payload = _json_loads(row.get("payload"), {}) or {}

    def _worker():
        # Completion bookkeeping runs in the worker itself: the HTTP call may
        # time out and return first, but the outcome of work that ran is
        # ALWAYS persisted exactly once (never lost, never duplicated).
        try:
            result = runner(payload, request_id)
        except BaseException as exc:  # noqa: BLE001 - classified below
            code, detail, retryable = _classify_exception(exc)
            state = STATE_FAILED_RETRYABLE if retryable else STATE_FAILED_PERMANENT
            _mark_finished(request_id, state, {}, code, detail)
            holder["error"] = exc
            return
        _mark_finished(request_id, STATE_SUCCEEDED, result or {}, None, None)
        holder["result"] = result

    future = _WORKER_POOL.submit(_worker)
    try:
        future.result(timeout=max(0.05, float(wait_seconds)))
    except FutureTimeout:
        # Response timeout: the logical operation may still complete. Leave the
        # row RUNNING so a retry attaches to it. Never execute again here.
        fresh = _fetch_row(request_id) or row
        return _envelope(
            fresh,
            state_override="timeout",
            error_override={
                "code": ERROR_TIMEOUT,
                "detail": "The assistant timed out.",
                "retryable": True,
            },
        )

    fresh = _fetch_row(request_id) or row
    return _envelope(fresh)


def submit_or_replay(
    *,
    request_id: Optional[str],
    surface: str,
    user_id: str,
    kind: str,
    payload: Dict[str, Any],
    runner: Callable[[Dict[str, Any], str], Dict[str, Any]],
    wait_seconds: float = 25.0,
) -> Dict[str, Any]:
    """Create-or-attach to the logical request identified by ``request_id``.

    See the module docstring for the at-most-once execution guarantees.
    """
    ensure_tasks_table()
    rid = normalize_request_id(request_id) or new_request_id()
    payload = dict(payload or {})
    payload_hash = _payload_hash(payload)
    now = time.time()

    # 1) Try to register the logical request exactly once.
    try:
        with _get_db() as db:
            db.execute(
                "INSERT INTO assistant_requests (request_id, surface, user_id, kind, payload, payload_hash, "
                "state, attempts, result, cancel_requested, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    surface,
                    user_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    payload_hash,
                    STATE_PENDING,
                    0,
                    "{}",
                    0,
                    now,
                    now,
                ),
            )
    except Exception:
        # Already registered: attach/replay according to the current state.
        row = _fetch_row(rid)
        if not row:
            raise
        if row.get("user_id") != user_id:
            # Actor isolation: a request id cannot be probed or hijacked.
            raise AssistantPermanentError("The request was not found.", code=ERROR_VALIDATION)
        if row.get("payload_hash") != payload_hash:
            raise RequestConflict(
                "This request identifier was already used for a different request."
            )

        state = row.get("state")
        if state == STATE_SUCCEEDED:
            return _envelope(row, replayed=True)

        if state == STATE_RUNNING:
            # Somebody (possibly this client before a timeout) already has an
            # execution in flight. Stale rows may be re-claimed; the runner is
            # idempotent through its idempotency key so the logical operation
            # can still only take effect once.
            started = float(row.get("started_at") or row.get("updated_at") or 0)
            if now - started > STALE_RUNNING_SECONDS:
                claimed = _claim(rid, (STATE_RUNNING,))
                if claimed:
                    return _execute_claimed(claimed, runner, wait_seconds)
            return _envelope(row, state_override="running")

        if state == STATE_FAILED_PERMANENT:
            return _envelope(row)

        if state == STATE_CANCELLED:
            # If the work already finished before the cancellation landed, its
            # preserved result wins - never run it again.
            if row.get("result") not in (None, "", "{}") and row.get("finished_at"):
                return _envelope(row, replayed=True)
            claimed = _claim(rid, (STATE_CANCELLED,))
            if claimed:
                return _execute_claimed(claimed, runner, wait_seconds)
            fresh = _fetch_row(rid) or row
            return _envelope(fresh)

        # PENDING or FAILED_RETRYABLE: retry is expected and safe (nothing is
        # currently executing for this logical request).
        claimed = _claim(rid, (STATE_PENDING, STATE_FAILED_RETRYABLE))
        if claimed:
            return _execute_claimed(claimed, runner, wait_seconds)
        fresh = _fetch_row(rid) or row
        return _envelope(fresh)

    # 2) Fresh request: claim and execute it exactly once.
    claimed = _claim(rid, (STATE_PENDING,))
    if not claimed:
        fresh = _fetch_row(rid)
        return _envelope(fresh or {}, state_override="running")
    return _execute_claimed(claimed, runner, wait_seconds)


def retry_request(request_id: str, user_id: str, runner: Callable[[Dict[str, Any], str], Dict[str, Any]], wait_seconds: float = 25.0) -> Dict[str, Any]:
    """Explicit "Try again" against the preserved original request.

    The stored payload is authoritative - the caller cannot smuggle a new
    payload under an old request id. At-most-once execution is enforced by the
    same state machine as :func:`submit_or_replay`.
    """
    ensure_tasks_table()
    row = _fetch_row(request_id)
    if not row or row.get("user_id") != user_id:
        raise AssistantPermanentError("The request was not found.", code=ERROR_VALIDATION)

    state = row.get("state")
    if state == STATE_SUCCEEDED:
        return _envelope(row, replayed=True)
    if state == STATE_FAILED_PERMANENT:
        return _envelope(row)
    if state == STATE_RUNNING:
        started = float(row.get("started_at") or row.get("updated_at") or 0)
        if time.time() - started > STALE_RUNNING_SECONDS:
            claimed = _claim(request_id, (STATE_RUNNING,))
            if claimed:
                return _execute_claimed(claimed, runner, wait_seconds)
        return _envelope(row, state_override="running")
    if state == STATE_CANCELLED:
        if row.get("result") not in (None, "", "{}") and row.get("finished_at"):
            return _envelope(row, replayed=True)
        claimed = _claim(request_id, (STATE_CANCELLED,))
    else:
        claimed = _claim(request_id, (STATE_PENDING, STATE_FAILED_RETRYABLE))
    if claimed:
        return _execute_claimed(claimed, runner, wait_seconds)
    fresh = _fetch_row(request_id) or row
    return _envelope(fresh)


def cancel_request(request_id: str, user_id: str) -> Dict[str, Any]:
    """Cancel/dismiss path. Never stops work that already ran; its result is
    preserved so a later retry replays instead of duplicating."""
    ensure_tasks_table()
    row = _fetch_row(request_id)
    if not row or row.get("user_id") != user_id:
        raise AssistantPermanentError("The request was not found.", code=ERROR_VALIDATION)

    state = row.get("state")
    if state == STATE_RUNNING:
        with _get_db() as db:
            db.execute(
                "UPDATE assistant_requests SET cancel_requested=1, updated_at=? WHERE request_id=?",
                (time.time(), request_id),
            )
        fresh = _fetch_row(request_id) or row
        return _envelope(fresh, state_override="running")
    if state in (STATE_PENDING, STATE_FAILED_RETRYABLE, STATE_FAILED_PERMANENT):
        now = time.time()
        with _get_db() as db:
            db.execute(
                "UPDATE assistant_requests SET state=?, cancel_requested=1, updated_at=? WHERE request_id=? AND state=?",
                (STATE_CANCELLED, now, request_id, state),
            )
    fresh = _fetch_row(request_id) or row
    return _envelope(fresh)
