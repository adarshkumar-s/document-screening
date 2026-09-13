import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _production_import_env(**overrides):
    env = os.environ.copy()
    env["APP_ENV"] = "production"
    env.pop("JWT_SECRET", None)
    env.pop("ALLOWED_ORIGINS", None)
    env.pop("ADMIN_ACTION_SECRET", None)
    env.update(overrides)
    return env


def _import_server(env):
    return subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_production_import_requires_jwt_secret():
    result = _import_server(_production_import_env(ALLOWED_ORIGINS="https://example.gov", ADMIN_ACTION_SECRET="test-only-admin-secret"))
    assert result.returncode != 0
    assert "JWT_SECRET must be configured in production." in result.stderr


def test_production_import_requires_allowed_origins():
    result = _import_server(_production_import_env(JWT_SECRET="test-only-random-secret", ADMIN_ACTION_SECRET="test-only-admin-secret"))
    assert result.returncode != 0
    assert "ALLOWED_ORIGINS must be configured in production." in result.stderr


@pytest.mark.parametrize("raw", [
    "*",
    "https://example.gov/path",
    "https://example.gov?x=1",
    "ftp://example.gov",
    "example.gov",
    "https://user:password@example.gov",
])
def test_production_rejects_invalid_allowed_origins(raw):
    import server

    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS contains invalid"):
        server.parse_allowed_origins(raw, production=True)


def test_production_accepts_multiple_http_https_origins():
    import server

    assert server.parse_allowed_origins(
        " https://records.example.gov, http://localhost:8000 ",
        production=True,
    ) == ["https://records.example.gov", "http://localhost:8000"]


def test_production_import_requires_admin_action_secret():
    result = _import_server(_production_import_env(
        JWT_SECRET="test-only-random-secret",
        ALLOWED_ORIGINS="https://records.example.gov",
    ))
    assert result.returncode != 0
    assert "ADMIN_ACTION_SECRET must be configured in production." in result.stderr


def test_production_import_succeeds_with_required_configuration():
    result = _import_server(_production_import_env(
        JWT_SECRET="test-only-random-secret",
        ALLOWED_ORIGINS="https://records.example.gov,https://admin.example.gov",
        ADMIN_ACTION_SECRET="test-only-admin-secret",
        DB_PATH=str(ROOT / "data" / "production-config-test.db"),
    ))
    assert result.returncode == 0, result.stderr
