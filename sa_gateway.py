"""SA gateway: secure secondary-password (security-lock) access to SA.

Design (the credential is verified SERVER-SIDE and resolves to an already
configured administrator identity):

    valid credential -> administrator identity -> short-lived SA session

What is never trusted from the browser:

* username / administrator name  (identity comes from ``sa_credentials``
  bound to a real ``users`` row; the browser cannot submit ``name=Adarsh``)
* ``isAdmin`` / ``unlocked=true`` / hidden form fields / JS variables
  (SA endpoints validate a server-side SA session on EVERY request)

Security properties:

* The AI access password is stored only as an argon2id hash (never plaintext,
  never logged, never returned to any client).
* The SA session is an opaque random token stored server-side as a SHA-256
  hash in an httpOnly cookie. It is bound to the logged-in administrator
  (user id + session version) and to the verified administrator identity.
* Sessions are short-lived, revoked on logout / password change / role change
  (version bump), and require re-authentication after expiry.
* Unlock attempts are throttled per account with a database-backed limit
  shared by every application worker/process; every attempt is audited WITHOUT
  the secret.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

SA_SESSION_COOKIE = "sa_session"
SA_COOKIE_PREFIX = "sas_"

DEFAULT_SA_SESSION_TTL = 15 * 60  # short-lived SA authorization state
_MIN_TTL, _MAX_TTL = 60, 60 * 60

_MAX_FAILED_ATTEMPTS = 5
_FAILED_WINDOW_SECONDS = 5 * 60

_TABLES_READY = False

router = APIRouter(prefix="/api/sa", tags=["SA Gateway"])


def _get_server():
    import server

    return server


def _get_db():
    return _get_server().get_db()


def _now() -> float:
    return time.time()


def sa_session_ttl_seconds() -> int:
    raw = os.getenv("SA_SESSION_TTL_SECONDS", "").strip()
    try:
        ttl = int(raw) if raw else DEFAULT_SA_SESSION_TTL
    except ValueError:
        ttl = DEFAULT_SA_SESSION_TTL
    return max(_MIN_TTL, min(_MAX_TTL, ttl))


def ensure_sa_tables() -> None:
    """Create SA credential/session tables (additive; existing schema untouched)."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    with _get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sa_credentials (
                id TEXT PRIMARY KEY,
                admin_user_id TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                secret_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                last_used_at REAL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sa_credentials_admin ON sa_credentials(admin_user_id)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sa_sessions (
                session_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                credential_id TEXT NOT NULL,
                admin_user_id TEXT NOT NULL,
                bound_user_id TEXT NOT NULL,
                bound_user_version INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                revoked_at REAL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_sa_sessions_bound ON sa_sessions(bound_user_id)"
        )
        # Shared, database-backed unlock throttle: every application
        # worker/process enforces the SAME limit (no process-local state).
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS sa_auth_throttle (
                key TEXT PRIMARY KEY,
                window_start REAL NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                last_admit_token TEXT
            )
            """
        )
    _TABLES_READY = True


# ------------------------------------------------------------------
# Credentials (server-side configuration)
# ------------------------------------------------------------------
# Inactive credentials never retain a usable secret: rotation overwrites the
# stored hash with this non-verifying placeholder immediately.
_RETIRED_SECRET_HASH = "RETIRED"


def _revoke_identity_sessions_in(db, admin_user_id: str) -> int:
    """Revoke every SA session established for OR by an administrator identity,
    on an EXISTING transaction (so rotation can be one atomic unit)."""
    cur = db.execute(
        "UPDATE sa_sessions SET revoked_at=? WHERE revoked_at IS NULL AND (admin_user_id=? OR bound_user_id=?)",
        (_now(), admin_user_id, admin_user_id),
    )
    return int(getattr(cur, "rowcount", 0) or 0)


def revoke_sa_sessions_for_identity(admin_user_id: str) -> int:
    """Revoke every SA session established for OR by an administrator identity.

    Used on credential rotation so an old credential's sessions cannot outlive
    it (defense in depth alongside the per-account logout revocation).
    """
    try:
        ensure_sa_tables()
    except Exception:
        return 0
    try:
        with _get_db() as db:
            return _revoke_identity_sessions_in(db, admin_user_id)
    except Exception:
        return 0


def create_sa_credential(admin_user_id: str, password: str, label: str = "", rotate: bool = False) -> Dict[str, Any]:
    """Register — or, with ``rotate=True``, rotate — the AI access password.

    The password is hashed immediately with argon2id and never persisted or
    logged in plaintext.

    Rotation semantics (``rotate=True``):
    * every previous credential of that administrator is invalidated AND its
      stored hash is scrubbed (the old password stops working immediately);
    * every existing SA session established for/by that administrator is
      revoked server-side (re-authentication with the new password is
      required).

    This function is server-side configuration (deployment seeding). The HTTP
    endpoint only ever calls it for the CALLING administrator's own identity,
    so one administrator can never create a credential belonging to another.
    """
    if not password or len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="AI access password must be at least 8 characters.",
        )
    server = _get_server()
    ensure_sa_tables()
    # Hash OUTSIDE the transaction (pure CPU): the write lock is held only for
    # the atomic rotation unit below.
    secret_hash = server.hash_password(password)  # argon2id; never plaintext
    revoked = 0
    cred_id = "SAC-" + uuid.uuid4().hex[:12]
    with _get_db() as db:
        user = db.execute(
            "SELECT id, full_name, role, is_active FROM users WHERE id=?",
            (admin_user_id,),
        ).fetchone()
        if not user or not user["is_active"] or server.normalize_role(user["role"]) != server.ROLE_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="SA credentials can only be configured for an active administrator identity.",
            )
        if rotate:
            # ATOMIC ROTATION - ONE transaction contains ALL of:
            #   1. deactivate every previous credential of this identity;
            #   2. scrub their stored hashes (old password can never verify);
            #   3. revoke every existing SA session for/by this identity;
            #   4. register the new credential.
            # There is no committed state where the new credential is active
            # while an old captured SA session (or old password) still works:
            # any failure rolls the whole rotation back.
            db.execute(
                "UPDATE sa_credentials SET is_active=0, secret_hash=? WHERE admin_user_id=?",
                (_RETIRED_SECRET_HASH, admin_user_id),
            )
            revoked = _revoke_identity_sessions_in(db, admin_user_id)
        db.execute(
            "INSERT INTO sa_credentials (id, admin_user_id, label, secret_hash, is_active, created_at) VALUES (?,?,?,?,1,?)",
            (cred_id, admin_user_id, (label or "")[:120], secret_hash, _now()),
        )
    server.log_audit(
        user["full_name"],
        "SA_CREDENTIAL_ROTATED" if rotate else "SA_CREDENTIAL_CONFIGURED",
        f"AI access credential {'rotated' if rotate else 'configured'} for administrator {user['full_name']}"
        + (f"; {revoked} existing SA session(s) revoked" if rotate else ""),
        None,
    )
    return {"credential_id": cred_id, "admin_user_id": admin_user_id, "revoked_sessions": revoked}


def seed_sa_credential_from_env() -> None:
    """Seed a credential from SA_ACCESS_PASSWORD for the initial administrator.

    Only runs when no active SA credential exists yet so it can never rotate a
    configured secret on restart.
    """
    password = os.getenv("SA_ACCESS_PASSWORD", "").strip()
    if not password:
        return
    try:
        ensure_sa_tables()
        server = _get_server()
        with _get_db() as db:
            existing = db.execute(
                "SELECT id FROM sa_credentials WHERE is_active=1 LIMIT 1"
            ).fetchone()
            if existing:
                return
            admin = db.execute(
                "SELECT id FROM users WHERE LOWER(email)='admin@landrec.gov.in' AND is_active=1"
            ).fetchone()
            if not admin:
                admin = db.execute(
                    "SELECT id FROM users WHERE role='ADMIN' AND is_active=1 ORDER BY id LIMIT 1"
                ).fetchone()
            admin_id = admin["id"] if admin else None
        if admin_id:
            create_sa_credential(admin_id, password, label="deployment-seeded")
    except Exception as exc:  # pragma: no cover - defensive bootstrap path
        print(f"[SA CREDENTIAL SEED WARNING] {type(exc).__name__}")


def _verify_sa_password(password: str, bound_user_id: str) -> Optional[Dict[str, Any]]:
    """Resolve ``password`` to the CALLING administrator's own identity.

    Ownership rule: an SA credential belongs to exactly one administrator and
    can only ever be USED by that administrator's authenticated session.
    Administrator A presenting administrator B's credential gets nowhere - the
    candidate set is restricted to credentials owned by the logged-in account.

    Returns the credential row (joined with its administrator) or None. The
    secret is compared with the argon2 verifier and never exposed.
    """
    server = _get_server()
    ensure_sa_tables()
    candidates: List[Dict[str, Any]] = []
    with _get_db() as db:
        rows = db.execute(
            "SELECT c.id AS credential_id, c.admin_user_id, u.full_name, u.email, u.role, u.is_active "
            "FROM sa_credentials c JOIN users u ON u.id = c.admin_user_id "
            "WHERE c.is_active=1 AND c.admin_user_id=?",
            (bound_user_id,),
        ).fetchall()
        for row in rows:
            if not row["is_active"]:
                continue
            if server.normalize_role(row["role"]) != server.ROLE_ADMIN:
                continue
            candidates.append(dict(row))

    matched: Optional[Dict[str, Any]] = None
    for candidate in candidates:
        with _get_db() as db:
            secret = db.execute(
                "SELECT secret_hash FROM sa_credentials WHERE id=?",
                (candidate["credential_id"],),
            ).fetchone()
        if not secret:
            continue
        ok, _legacy = server.verify_password(secret["secret_hash"], password)
        if ok and matched is None:
            matched = candidate
    return matched


# ------------------------------------------------------------------
# Throttling (database-backed, shared by EVERY application worker/process)
# ------------------------------------------------------------------
def _throttle_key(bound_user_id: str) -> str:
    return bound_user_id or "anonymous"


def _admit_attempt(bound_user_id: str) -> bool:
    """Atomically consume one unlock-attempt slot (or report throttled).

    Policy: at most ``_MAX_FAILED_ATTEMPTS`` admitted attempts per
    ``_FAILED_WINDOW_SECONDS`` window per account; a successful authentication
    clears the state (see ``_register_success``); expired windows reset and old
    records are purged. No password or secret is ever stored - only a counter,
    timestamps, and a random admission token.

    The whole decision is ONE database UPSERT on a shared counter row: every
    application worker/process sees the same state, and concurrent requests
    are serialized by the row (PostgreSQL) / writer (SQLite) lock, so multiple
    workers cannot bypass the limit by racing separate in-memory dictionaries.
    """
    ensure_sa_tables()
    key = _throttle_key(bound_user_id)
    now = _now()
    cutoff = now - _FAILED_WINDOW_SECONDS
    token = uuid.uuid4().hex
    with _get_db() as db:
        db.execute(
            "INSERT INTO sa_auth_throttle (key, window_start, failures, updated_at, last_admit_token) "
            "VALUES (?,?,1,?,?) "
            "ON CONFLICT (key) DO UPDATE SET "
            " window_start = CASE WHEN sa_auth_throttle.window_start < ? THEN ? ELSE sa_auth_throttle.window_start END, "
            " failures = CASE WHEN sa_auth_throttle.window_start < ? THEN 1 "
            "   WHEN sa_auth_throttle.failures < ? THEN sa_auth_throttle.failures + 1 "
            "   ELSE sa_auth_throttle.failures END, "
            " updated_at = ?, "
            " last_admit_token = CASE WHEN sa_auth_throttle.window_start < ? THEN ? "
            "   WHEN sa_auth_throttle.failures < ? THEN ? "
            "   ELSE sa_auth_throttle.last_admit_token END",
            (
                key, now, now, token,
                cutoff, now,
                cutoff, _MAX_FAILED_ATTEMPTS,
                now,
                cutoff, token,
                _MAX_FAILED_ATTEMPTS, token,
            ),
        )
        row = db.execute(
            "SELECT last_admit_token FROM sa_auth_throttle WHERE key=?", (key,)
        ).fetchone()
        # Expire/clean records untouched for two whole windows.
        db.execute(
            "DELETE FROM sa_auth_throttle WHERE updated_at < ?", (now - 2 * _FAILED_WINDOW_SECONDS,)
        )
    return bool(row) and row["last_admit_token"] == token


def _register_success(bound_user_id: str) -> None:
    """Successful authentication clears the failure state entirely."""
    try:
        ensure_sa_tables()
        with _get_db() as db:
            db.execute("DELETE FROM sa_auth_throttle WHERE key=?", (_throttle_key(bound_user_id),))
    except Exception:
        pass


# ------------------------------------------------------------------
# Sessions
# ------------------------------------------------------------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_sa_session(credential: Dict[str, Any], bound_user: Dict[str, Any]) -> tuple:
    """Create the short-lived SA authorization state.

    Returns ``(cookie_token, session_info)``. The raw token only ever exists
    in the httpOnly cookie; the database stores its SHA-256 hash.
    """
    ensure_sa_tables()
    token = SA_COOKIE_PREFIX + secrets.token_urlsafe(32)
    session_id = uuid.uuid4().hex
    created = _now()
    expires = created + sa_session_ttl_seconds()
    with _get_db() as db:
        db.execute(
            "INSERT INTO sa_sessions (session_id, token_hash, credential_id, admin_user_id, bound_user_id, "
            "bound_user_version, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id,
                _hash_token(token),
                credential["credential_id"],
                credential["admin_user_id"],
                bound_user["id"],
                int(bound_user.get("version") or 0),
                created,
                expires,
            ),
        )
        db.execute(
            "UPDATE sa_credentials SET last_used_at=? WHERE id=?",
            (created, credential["credential_id"]),
        )
    session_info = {
        "session_id": session_id,
        "admin_user_id": credential["admin_user_id"],
        "admin_name": credential["full_name"],
        "created_at": created,
        "expires_at": expires,
    }
    return token, session_info


def revoke_sa_sessions_for_user(bound_user_id: str) -> int:
    """Revoke every SA session bound to a logged-in account (used on logout)."""
    try:
        ensure_sa_tables()
    except Exception:
        return 0
    try:
        with _get_db() as db:
            cur = db.execute(
                "UPDATE sa_sessions SET revoked_at=? WHERE bound_user_id=? AND revoked_at IS NULL",
                (_now(), bound_user_id),
            )
        return int(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        return 0


def get_sa_session(token: str, bound_user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate the SA session cookie server-side. Returns session info or None.

    Checks: token hash, not revoked, not expired, bound to this logged-in
    account AND its current session version (logout/role/password changes
    revoke it), and the verified administrator identity is still an active
    administrator.
    """
    if not token:
        return None
    ensure_sa_tables()
    server = _get_server()
    with _get_db() as db:
        row = db.execute(
            "SELECT s.*, u.full_name AS admin_name, u.role AS admin_role, u.is_active AS admin_active "
            "FROM sa_sessions s JOIN users u ON u.id = s.admin_user_id WHERE s.token_hash=?",
            (_hash_token(token),),
        ).fetchone()
    if not row:
        return None
    session = dict(row)
    if session.get("revoked_at"):
        return None
    if float(session.get("expires_at") or 0) < _now():
        return None
    if session.get("bound_user_id") != bound_user.get("id"):
        return None
    if int(session.get("bound_user_version") or 0) != int(bound_user.get("version") or 0):
        # Password/role change (version bump) or logout invalidates SA access.
        return None
    if not session.get("admin_active"):
        return None
    if server.normalize_role(session.get("admin_role", "")) != server.ROLE_ADMIN:
        return None
    return {
        "session_id": session["session_id"],
        "admin_user_id": session["admin_user_id"],
        "admin_name": session["admin_name"],
        "created_at": session["created_at"],
        "expires_at": session["expires_at"],
    }


def current_admin_user(request: Request) -> Dict[str, Any]:
    """Resolve the logged-in administrator strictly server-side.

    Uses the canonical JWT/session validation from ``server`` at request time
    (never a browser-supplied name, role flag, or hidden field).
    """
    server = _get_server()
    authorization = request.headers.get("authorization")
    user = server.get_current_user(request, authorization=authorization)
    if server.normalize_role(user.get("role", "")) != server.ROLE_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: SA requires an authenticated administrator.",
        )
    return user


def require_sa_session(request: Request) -> Dict[str, Any]:
    """Dependency enforcing BOTH admin RBAC and a live, server-side SA session.

    Returns the *verified administrator identity* resolved from the credential
    and its bound session - never anything supplied by the browser.
    """
    user = current_admin_user(request)
    token = request.cookies.get(SA_SESSION_COOKIE) or ""
    session = get_sa_session(token, user)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SA access requires verification. Enter the AI access password.",
        )
    return {
        "session_id": session["session_id"],
        "admin_user_id": session["admin_user_id"],
        "admin_name": session["admin_name"],
        "expires_at": session["expires_at"],
        # The logged-in account remains part of the actor for isolation/audit.
        "bound_user_id": user.get("id"),
        "bound_user_name": user.get("full_name"),
        "id": session["admin_user_id"],
        "full_name": session["admin_name"],
        "role": _get_server().ROLE_ADMIN,
    }


# ------------------------------------------------------------------
# HTTP routes
# ------------------------------------------------------------------
class SAUnlockReq(BaseModel):
    # ONLY the credential. Identity is resolved server-side; any name or
    # username supplied by the browser is ignored by design.
    password: str


class SACredentialReq(BaseModel):
    # The credential always belongs to the CALLING administrator. Any identity
    # fields a browser might add (admin_email, name, ...) are ignored by
    # design: administrator A can never create a credential for administrator B.
    password: str
    label: str = ""


@router.post("/unlock")
def sa_unlock(req: SAUnlockReq, request: Request, user: dict = Depends(current_admin_user)):
    """Verify the AI access password and open a short-lived SA session.

    ``valid credential -> administrator identity -> SA session``. The welcome
    name comes from the verified identity on the server, never the browser.
    """
    ensure_sa_tables()
    bound_user_id = user.get("id", "")
    if not _admit_attempt(bound_user_id):
        _get_server().log_audit(
            user.get("full_name", ""), "SA_UNLOCK_THROTTLED", "SA unlock throttled after repeated failures", None
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again later.",
        )

    credential = _verify_sa_password(req.password, bound_user_id)
    if not credential:
        # The admitted attempt already counts in the shared failure window.
        _get_server().log_audit(
            user.get("full_name", ""), "SA_UNLOCK_FAILED", "Invalid AI access password", None
        )
        # Generic error: no user/credential enumeration. The secret is never
        # logged, echoed, or returned.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid AI access password.",
        )

    _register_success(bound_user_id)
    token, session = issue_sa_session(credential, user)
    server = _get_server()
    server.log_audit(
        credential["full_name"],
        "SA_UNLOCK",
        f"SA session opened for verified administrator {credential['full_name']}",
        None,
    )
    from fastapi.responses import JSONResponse

    response = JSONResponse(
        {
            "unlocked": True,
            # Verified administrator identity resolved server-side.
            "admin": {"full_name": credential["full_name"], "role": server.ROLE_ADMIN},
            "welcome": f"Welcome, {credential['full_name']}",
            "expires_at": session["expires_at"],
            "ttl_seconds": sa_session_ttl_seconds(),
        }
    )
    response.set_cookie(
        SA_SESSION_COOKIE,
        token,
        httponly=True,
        secure=server.IS_PRODUCTION,
        samesite="lax",
        max_age=sa_session_ttl_seconds(),
        path="/",
    )
    return response


@router.post("/lock")
def sa_lock(request: Request, user: dict = Depends(current_admin_user)):
    """Lock SA now: revoke every SA session bound to this account."""
    revoked = revoke_sa_sessions_for_user(user.get("id", ""))
    _get_server().log_audit(
        user.get("full_name", ""), "SA_LOCK", f"SA session locked ({revoked} session(s) revoked)", None
    )
    from fastapi.responses import JSONResponse

    response = JSONResponse({"unlocked": False})
    response.delete_cookie(SA_SESSION_COOKIE, path="/")
    return response


@router.get("/session")
def sa_status(request: Request, user: dict = Depends(current_admin_user)):
    """Report the server-side SA session state (never trusts a client flag)."""
    token = request.cookies.get(SA_SESSION_COOKIE) or ""
    session = get_sa_session(token, user)
    if not session:
        return {"unlocked": False, "admin": None, "welcome": None, "expires_at": None}
    return {
        "unlocked": True,
        "admin": {"full_name": session["admin_name"], "role": _get_server().ROLE_ADMIN},
        "welcome": f"Welcome, {session['admin_name']}",
        "expires_at": session["expires_at"],
    }


@router.post("/credentials")
def sa_configure_credential(req: SACredentialReq, user: dict = Depends(current_admin_user)):
    """Administrator-only: ROTATE the AI access credential of the calling
    administrator.

    Ownership rule: the credential is always bound to the CALLING
    administrator's own identity - one administrator can never create or
    rotate a credential belonging to another. Rotation invalidates and scrubs
    the previous credential and revokes every existing SA session for/by that
    identity (the new password must be used to re-authenticate). The secret is
    hashed immediately and is never returned.
    """
    created = create_sa_credential(user["id"], req.password, req.label, rotate=True)
    return {
        "status": "ok",
        "credential_id": created["credential_id"],
        "rotated": True,
        "revoked_sessions": created.get("revoked_sessions", 0),
        "admin": {"full_name": user["full_name"]},
    }


# Bootstrap: table creation and optional env seeding happen at import time so
# both the canonical server and the entrypoints see a ready gateway. Seeding is
# idempotent and never overwrites an existing configured credential.
try:
    ensure_sa_tables()
except Exception as exc:  # pragma: no cover - defensive bootstrap path
    print(f"[SA TABLE WARNING] {type(exc).__name__}")
