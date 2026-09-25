"""Security-hardening regression tests (Issue #8).

Two production-safety defects are pinned down here:

1. SA request actors used to live in a process-global dictionary, so two
   concurrent administrator requests overwrote each other's identity: one
   administrator's SA turn could be served, and audited, under another
   administrator. The actor is now request-local (a ContextVar), so every
   request, thread and asyncio task resolves its own administrator and nothing
   leaks between requests.
2. A configured PostgreSQL DATABASE_URL silently fell back to the local SQLite
   file whenever the connection or the psycopg2 driver was unavailable. In
   production that pointed every request at a database the operator never
   configured. Production now fails closed at startup and at request
   initialisation; intentional SQLite (no DATABASE_URL) keeps working in
   development and production.
"""
import asyncio
import contextvars
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import sa_agent

ROOT = Path(__file__).resolve().parents[1]

# The synthetic non-identity SA uses when no actor is bound. It carries no
# administrator id or email, so it can never widen authorization to a real
# account the way the leaked process-global actor could.
SA_FALLBACK = {"id": None, "role": "ADMIN", "full_name": "SA"}

# A PostgreSQL URL that cannot accept connections: port 1 never listens, so
# the connection is refused immediately instead of timing out.
UNREACHABLE_POSTGRES = "postgresql://docscreen:test-password@127.0.0.1:1/landrecords"


def _admin(full_name: str) -> dict:
    slug = full_name.lower().replace(" ", "-")
    return {
        "id": "admin-" + uuid.uuid4().hex[:8],
        "full_name": full_name,
        "email": f"{slug}-{uuid.uuid4().hex[:6]}@example.test",
        "role": "ADMIN",
    }


@pytest.fixture(autouse=True)
def _isolate_request_actor():
    """Never leave a bound actor behind for later tests in this process."""
    _reset_actor_state()
    yield
    _reset_actor_state()


def _reset_actor_state():
    """Clear the request actor whatever representation it uses, so a future
    regression back to shared module state cannot silently poison later
    tests (and fails them loudly instead)."""
    current = sa_agent._request_actor
    if hasattr(current, "set"):  # request-local ContextVar
        current.set(None)
    else:  # legacy process-global dictionary
        sa_agent._request_actor = {}


# ==========================================================================
# 1. SA actor state is request-local
# ==========================================================================

def test_unset_actor_is_the_synthetic_sa_identity():
    """Without a bound actor SA must not impersonate any administrator."""
    actor = sa_agent._actor()
    assert actor == SA_FALLBACK
    assert actor["id"] is None
    assert "email" not in actor


def test_actor_binds_only_the_current_execution_context():
    admin = _admin("Bound Admin")
    sa_agent._set_actor(admin)
    assert sa_agent._actor() == admin

    # The actor is handed out as a copy: mutating it must not rebind the
    # request's identity.
    handed_out = sa_agent._actor()
    handed_out["id"] = "someone-else"
    assert sa_agent._actor() == admin

    # A new execution context (a fresh thread is one) does not inherit the
    # binding made in this one.
    seen = {}
    thread = threading.Thread(target=lambda: seen.update(actor=sa_agent._actor()))
    thread.start()
    thread.join()
    assert seen["actor"] == SA_FALLBACK


def test_concurrent_requests_never_observe_each_others_actor():
    """Two SA requests in flight at once must each keep their own identity."""
    alice, bob = _admin("Alice Admin"), _admin("Bob Admin")
    observed = {"alice": set(), "bob": set()}
    barrier = threading.Barrier(2)

    def request(admin, key):
        # What run() does when a turn starts.
        sa_agent._set_actor(admin)
        barrier.wait()  # both requests are now live at the same time
        for _ in range(150):
            observed[key].add(json.dumps(sa_agent._actor(), sort_keys=True))
            time.sleep(0.001)

    threads = [
        threading.Thread(target=request, args=(alice, "alice")),
        threading.Thread(target=request, args=(bob, "bob")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert observed["alice"] == {json.dumps(alice, sort_keys=True)}
    assert observed["bob"] == {json.dumps(bob, sort_keys=True)}


def test_actor_does_not_leak_between_requests_on_a_reused_thread():
    """FastAPI serves sync endpoints on reused worker threads, each request
    inside its own copy of the context (anyio). The next request on the same
    thread must not see the previous request's administrator."""
    admin = _admin("First Request")

    def sa_turn():
        sa_agent._set_actor(admin)
        return sa_agent._actor()

    with ThreadPoolExecutor(max_workers=1) as pool:
        context = contextvars.copy_context()
        first = pool.submit(context.run, sa_turn).result()
        # The next request reuses the same worker thread with a fresh context.
        second = pool.submit(sa_agent._actor).result()

    assert first == admin
    assert second == SA_FALLBACK


def test_read_tools_resolves_the_request_local_actor(monkeypatch):
    """_read_tools() without an explicit actor must resolve the actor bound
    for the current request, not whichever administrator wrote module state
    most recently."""
    captured = []

    def fake_land_search(args, actor=None):
        captured.append(actor or sa_agent._actor())
        return {"land_records": [], "total": 0}

    monkeypatch.setattr(sa_agent, "_land_search", fake_land_search)

    alice, bob = _admin("Alice Admin"), _admin("Bob Admin")
    plans = {
        "alice": {"steps": [{"id": "1", "tool": "land_search", "args": {"query": "alice-parcel"}}]},
        "bob": {"steps": [{"id": "1", "tool": "land_search", "args": {"query": "bob-parcel"}}]},
    }
    barrier = threading.Barrier(2)

    def run_turn(admin, key):
        context = contextvars.copy_context()
        context.run(sa_agent._set_actor, admin)
        barrier.wait()  # both identities are bound before either turn reads
        # asyncio.run + to_thread is exactly how SA executes read tools.
        return key, context.run(asyncio.run, sa_agent._execute_reads(plans[key]))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_turn, alice, "alice"), pool.submit(run_turn, bob, "bob")]
        evidence = {key: result for key, result in (future.result() for future in futures)}

    assert [item["ok"] for item in evidence["alice"]] == [True]
    assert [item["ok"] for item in evidence["bob"]] == [True]
    assert captured == [alice, bob] or captured == [bob, alice]
    # Each request's visibility actor was its own administrator.
    for actor in captured:
        assert actor in (alice, bob)


@pytest.fixture
def sa_admin_sessions():
    """Two live SA sessions for two distinct administrators."""
    import sa_conversation
    import server

    sa_agent._ensure_tables()
    sa_conversation.ensure_memory_table()

    sessions = []
    for name in ("Alice Admin", "Bob Admin"):
        admin = _admin(name)
        session_id = "SA-" + uuid.uuid4().hex[:10].upper()
        now = time.time()
        with server.get_db() as db:
            db.execute(
                """INSERT INTO sa_sessions(session_id,admin_id,admin_name,admin_email,activated_at,expires_at,active,last_used_at)
                   VALUES(?,?,?,?,?,?,1,?)""",
                (session_id, admin["id"], name, admin["email"], now, now + 3600, now),
            )
        sessions.append((admin, session_id))

    yield sessions

    for admin, session_id in sessions:
        with server.get_db() as db:
            db.execute("DELETE FROM sa_activity WHERE session_id=?", (session_id,))
            db.execute("DELETE FROM sa_sessions WHERE session_id=?", (session_id,))
        sa_conversation.clear_memory(session_id)


def test_concurrent_sa_turns_keep_each_administrators_identity(monkeypatch, sa_admin_sessions):
    """End to end: two SA turns running at the same time each bind, and pass
    to the visibility layer, their own authenticated administrator."""
    (alice, alice_session), (bob, bob_session) = sa_admin_sessions

    captured = []
    lock = threading.Lock()

    def fake_land_search(args, actor=None):
        with lock:
            captured.append(actor or sa_agent._actor())
        return {"land_records": [], "total": 0}

    monkeypatch.setattr(sa_agent, "_land_search", fake_land_search)

    barrier = threading.Barrier(2)
    outcomes = {}

    def turn(admin, session_id, key):
        barrier.wait()  # both SA turns are in flight together
        outcomes[key] = sa_agent.run(
            "what land is there in Sundarpur village", admin, session_id
        )

    threads = [
        threading.Thread(target=turn, args=(alice, alice_session, "alice")),
        threading.Thread(target=turn, args=(bob, bob_session, "bob")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Both turns completed as real SA responses for their own administrator.
    assert outcomes["alice"]["mode"] == "SA"
    assert outcomes["bob"]["mode"] == "SA"
    assert outcomes["alice"]["admin"] == "admin.alice_admin"
    assert outcomes["bob"]["admin"] == "admin.bob_admin"

    # Each turn's reads ran under its own administrator's identity.
    assert len(captured) == 2
    assert {actor["id"] for actor in captured} == {alice["id"], bob["id"]}
    for actor in captured:
        assert actor["email"] in (alice["email"], bob["email"])


def test_sa_actor_state_does_not_persist_after_a_turn(sa_admin_sessions):
    """After a turn finishes, a fresh request sees no leftover identity."""
    (admin, session_id), _ = sa_admin_sessions
    sa_agent.run("what land is there in Sundarpur village", admin, session_id)

    seen = {}
    thread = threading.Thread(target=lambda: seen.update(actor=sa_agent._actor()))
    thread.start()
    thread.join()
    assert seen["actor"] == SA_FALLBACK


def test_concurrent_http_sa_requests_isolate_their_administrators(
    monkeypatch, make_user_client
):
    """The real serving path: two administrators' SA turns in flight at the
    same time through FastAPI's reused worker threads must never observe or
    inherit each other's identity, and the turn must run as the authenticated
    account rather than as the selected SA identity."""
    import server

    activation_code = "test-only-sa-activation-code"
    identity_password = "Test SA Identity Password 123!"
    monkeypatch.setenv("SA_ACTIVATION_CODE", activation_code)
    monkeypatch.setenv("SA_PASSWORD_HASH_GAUTAM", server.hash_password(identity_password))
    monkeypatch.setenv("SA_PASSWORD_HASH_ADARSH", server.hash_password(identity_password))

    captured = []
    lock = threading.Lock()

    def fake_land_search(args, actor=None):
        with lock:
            captured.append(actor or sa_agent._actor())
        return {"land_records": [], "total": 0}

    monkeypatch.setattr(sa_agent, "_land_search", fake_land_search)

    # Two real administrator accounts, each activating a different SA identity.
    prepared = []
    for key, identity in (("alice", "Gautam"), ("bob", "Adarsh")):
        client, headers, email = make_user_client("ADMIN", prefix=f"sa{key}")
        activation = client.post(
            "/api/admin/assistant/sa/activate",
            json={"code": activation_code, "administrator": identity, "password": identity_password},
            headers=headers,
        )
        assert activation.status_code == 200, activation.text
        session_id = activation.json()["session_id"]
        prepared.append((key, email, identity, client, headers, session_id))

    barrier = threading.Barrier(2)
    outcomes = {}

    def turn(key, email, identity, client, headers, session_id):
        barrier.wait()  # both administrators' turns are in flight together
        response = client.post(
            "/api/admin/assistant/sa/query",
            json={"session_id": session_id, "query": "what land is there in Sundarpur village"},
            headers=headers,
        )
        outcomes[key] = (email, identity, response)

    threads = [
        threading.Thread(target=turn, args=entry)
        for entry in prepared
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        for key, (email, identity, response) in outcomes.items():
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["mode"] == "SA"
            assert payload["session_id"]

        # Every read ran under its own authenticated administrator's account:
        # the actor is the logged-in user, never the other administrator and
        # never the selected SA identity.
        assert len(captured) == 2
        expected_emails = {entry[1] for entry in prepared}
        assert {actor.get("email") for actor in captured} == expected_emails
        for actor in captured:
            assert actor.get("role") == "ADMIN"
            # The visibility actor is the authenticated account, not the SA
            # identity that was activated.
            assert actor.get("full_name") not in {"Gautam", "Adarsh"}
    finally:
        for key, email, identity, client, headers, session_id in prepared:
            try:
                client.post(
                    "/api/admin/assistant/sa/end",
                    json={"session_id": session_id, "query": ""},
                    headers=headers,
                )
            except Exception:
                pass
        with server.get_db() as db:
            for entry in prepared:
                db.execute("DELETE FROM sa_activity WHERE session_id=?", (entry[5],))
                db.execute("DELETE FROM sa_sessions WHERE session_id=?", (entry[5],))


# ==========================================================================
# 2. Production fails closed on a configured but unusable PostgreSQL
# ==========================================================================

def _production_env(**overrides):
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env["JWT_SECRET"] = "test-only-random-secret"
    env["ALLOWED_ORIGINS"] = "https://records.example.gov"
    env["ADMIN_ACTION_SECRET"] = "test-only-admin-secret"
    env.pop("DATABASE_URL", None)
    env.update(overrides)
    return env


def _import_server(env):
    return subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_production_postgres_connection_failure_never_instantiates_sqlite(tmp_path):
    """Production with a configured DATABASE_URL must fail loudly when
    PostgreSQL is unreachable instead of silently serving SQLite."""
    db_path = tmp_path / "must-not-exist.db"
    result = _import_server(_production_env(
        DATABASE_URL=UNREACHABLE_POSTGRES,
        DB_PATH=str(db_path),
    ))

    assert result.returncode != 0
    assert "refusing to fall back to SQLite in production" in result.stderr
    assert "RuntimeError" in result.stderr
    # Nothing was ever written: SQLite (including its -wal/-shm side files)
    # was never instantiated.
    assert list(tmp_path.iterdir()) == []


def test_production_missing_psycopg2_driver_fails_closed(tmp_path):
    """Production with a configured PostgreSQL URL must fail at startup when
    the psycopg2 driver is missing, with the same fail-closed behaviour."""
    db_path = tmp_path / "must-not-exist.db"
    result = subprocess.run(
        # Poisoning sys.modules makes `import psycopg2` raise ImportError,
        # which is exactly what a deployment without the driver sees.
        [sys.executable, "-c", "import sys; sys.modules['psycopg2'] = None; import server"],
        cwd=ROOT,
        env=_production_env(
            DATABASE_URL="postgresql://docscreen:test-password@127.0.0.1:5432/landrecords",
            DB_PATH=str(db_path),
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert "psycopg2" in result.stderr
    assert "refusing to fall back to SQLite in production" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_production_rejects_database_url_that_is_not_postgresql(tmp_path):
    """A configured DATABASE_URL the server cannot honour must not be
    silently ignored in production."""
    db_path = tmp_path / "must-not-exist.db"
    result = _import_server(_production_env(
        DATABASE_URL="mysql://docscreen:test-password@127.0.0.1:3306/landrecords",
        DB_PATH=str(db_path),
    ))

    assert result.returncode != 0
    assert "not a PostgreSQL URL" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_production_without_database_url_still_uses_sqlite(tmp_path):
    """Intentional SQLite (PostgreSQL not configured) keeps working in
    production, matching the documented deployment mode."""
    db_path = tmp_path / "production.db"
    result = _import_server(_production_env(DB_PATH=str(db_path)))

    assert result.returncode == 0, result.stderr
    assert db_path.exists()


def test_development_without_database_url_uses_sqlite(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server, "IS_PRODUCTION", False)
    monkeypatch.setattr(server, "DATABASE_URL", "")
    monkeypatch.setattr(server, "POSTGRES_CONFIGURED", False)
    monkeypatch.setattr(server, "HAS_PSYCOPG2", False)
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "development.db"))

    with server.get_db() as db:
        db.execute("SELECT 1").fetchall()

    assert (tmp_path / "development.db").exists()


def _configure_production_postgres(monkeypatch, *, has_driver=True, connect_raises=None):
    import server

    monkeypatch.setattr(server, "IS_PRODUCTION", True)
    monkeypatch.setattr(server, "DATABASE_URL", "postgresql://docscreen:test-password@127.0.0.1:5432/landrecords")
    monkeypatch.setattr(server, "POSTGRES_CONFIGURED", True)
    monkeypatch.setattr(server, "HAS_PSYCOPG2", has_driver)

    class _FakePsycopg2:
        @staticmethod
        def connect(*args, **kwargs):
            raise connect_raises

    monkeypatch.setattr(server, "psycopg2", _FakePsycopg2, raising=False)
    monkeypatch.setattr(server, "RealDictCursor", object(), raising=False)


def test_production_dbconnection_fails_closed_when_postgres_unreachable(monkeypatch, tmp_path):
    """Request initialisation in production must never instantiate SQLite
    when the configured PostgreSQL cannot be reached."""
    import server

    _configure_production_postgres(
        monkeypatch, has_driver=True,
        connect_raises=RuntimeError("simulated PostgreSQL outage"),
    )
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "must-not-exist.db"))

    sqlite_calls = []
    real_connect = sqlite3.connect

    def sqlite_spy(*args, **kwargs):
        sqlite_calls.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", sqlite_spy)

    with pytest.raises(RuntimeError, match="refusing to fall back to SQLite in production"):
        server.DBConnection()

    assert sqlite_calls == []
    assert list(tmp_path.iterdir()) == []


def test_production_dbconnection_fails_closed_without_postgres_driver(monkeypatch, tmp_path):
    """Even if module state claims no driver at request time, production with
    a configured DATABASE_URL must not serve SQLite."""
    import server

    _configure_production_postgres(monkeypatch, has_driver=False)
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "must-not-exist.db"))

    sqlite_calls = []
    real_connect = sqlite3.connect

    def sqlite_spy(*args, **kwargs):
        sqlite_calls.append(args)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", sqlite_spy)

    with pytest.raises(RuntimeError, match="no PostgreSQL connection is available"):
        server.DBConnection()

    assert sqlite_calls == []
    assert list(tmp_path.iterdir()) == []


def test_development_keeps_the_postgres_to_sqlite_fallback(monkeypatch, tmp_path):
    """Development keeps the historical behaviour: an unreachable PostgreSQL
    logs a warning and continues on SQLite."""
    import server

    monkeypatch.setattr(server, "IS_PRODUCTION", False)
    monkeypatch.setattr(server, "DATABASE_URL", "postgresql://docscreen:test-password@127.0.0.1:5432/landrecords")
    monkeypatch.setattr(server, "POSTGRES_CONFIGURED", True)
    monkeypatch.setattr(server, "HAS_PSYCOPG2", True)

    class _FakePsycopg2:
        @staticmethod
        def connect(*args, **kwargs):
            raise RuntimeError("simulated PostgreSQL outage")

    monkeypatch.setattr(server, "psycopg2", _FakePsycopg2, raising=False)
    monkeypatch.setattr(server, "RealDictCursor", object(), raising=False)
    monkeypatch.setattr(server, "DB_PATH", str(tmp_path / "development.db"))

    connection = server.DBConnection()
    try:
        assert connection.is_pg is False
        assert isinstance(connection.conn, sqlite3.Connection)
    finally:
        connection.conn.close()
    assert (tmp_path / "development.db").exists()
