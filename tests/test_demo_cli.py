"""Command-line demo loader tests (scripts/seed_demo_data.py).

Every write mode is exercised against a throwaway database through a real
subprocess, exactly as an operator would run it, including the production
refusal and the missing-confirmation refusal.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "seed_demo_data.py"


@pytest.fixture
def scratch_db(tmp_path):
    path = tmp_path / "cli-demo.db"
    env = os.environ.copy()
    env["DB_PATH"] = str(path)
    env.pop("APP_ENV", None)
    return path, env


def _run(env, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
    )


def _seeded_rows(db_path):
    import sqlite3

    connection = sqlite3.connect(db_path)
    try:
        return {
            "documents": connection.execute("SELECT COUNT(*) FROM documents WHERE id LIKE 'DEMO-%'").fetchone()[0],
            "mutations": connection.execute("SELECT COUNT(*) FROM land_mutations WHERE id LIKE 'DEMO-%'").fetchone()[0],
            "encumbrances": connection.execute("SELECT COUNT(*) FROM land_encumbrances WHERE id LIKE 'DEMO-%'").fetchone()[0],
            "court_cases": connection.execute("SELECT COUNT(*) FROM land_court_cases WHERE id LIKE 'DEMO-%'").fetchone()[0],
        }
    finally:
        connection.close()


def test_check_only_reports_and_never_writes(scratch_db):
    path, env = scratch_db
    result = _run(env, "--check-only")
    assert result.returncode == 1, result.stdout + result.stderr  # incomplete on a fresh database
    assert "16" in result.stdout and "court cases" in result.stdout
    assert not path.exists() or _seeded_rows(path)["documents"] == 0


def test_writes_are_refused_without_yes(scratch_db):
    path, env = scratch_db
    result = _run(env)
    assert "nothing written" in result.stdout.lower()
    assert not path.exists() or _seeded_rows(path)["documents"] == 0


def test_seed_is_idempotent_and_reports_counts(scratch_db):
    path, env = scratch_db
    first = _run(env, "--yes", "--json")
    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(first.stdout)["status"] == "seeded"
    created = json.loads(first.stdout)["created"]
    assert created["documents"] == 29
    assert created["mutations"] == 7
    assert created["encumbrances"] == 4
    assert created["court_cases"] == 6
    rows = _seeded_rows(path)
    assert rows == {"documents": 29, "mutations": 7, "encumbrances": 4, "court_cases": 6}

    second = _run(env, "--yes", "--json")
    assert second.returncode == 0, second.stdout + second.stderr
    second_body = json.loads(second.stdout)
    assert second_body["created"]["total"] == 0
    assert second_body["already_present"] == 46
    assert _seeded_rows(path)["documents"] == 29  # nothing duplicated

    check = _run(env, "--check-only")
    assert check.returncode == 0
    assert "COMPLETE" in check.stdout

    text_run = _run(env, "--yes")
    assert text_run.returncode == 0
    assert "DEMO DATA ALREADY LOADED" in text_run.stdout and "idempotent" in text_run.stdout


def test_clear_removes_only_demo_rows(scratch_db):
    path, env = scratch_db
    assert _run(env, "--yes").returncode == 0

    # a "real" record seeded before the demo data must survive the demo wipe
    import sqlite3
    import json
    import time

    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO documents (id, filename, doc_type, mean_conf, verdict, status, languages, pages,"
        " fields, validation, ai_decision_support, ocr_text, cleaned_ocr_text, detected_language,"
        " original_fields, metadata, uploaded_by, reviewer_comments, created_at, updated_at)"
        " VALUES ('REAL-DOC-1','real.pdf','Land Record',95,'review','APPROVED','[\"eng\"]',1,'{}','{}','{}',"
        " '','','eng','{}','{}','real-officer@example.test','',?,?)",
        (time.time(), time.time()),
    )
    connection.commit()
    connection.close()

    cleared = _run(env, "--clear", "--yes", "--json")
    assert cleared.returncode == 0, cleared.stdout + cleared.stderr
    cleared_body = json.loads(cleared.stdout)
    removed = cleared_body["removed"]
    assert removed["documents"] == 29
    assert removed["court_cases"] == 6
    assert cleared_body["real_data_untouched"] is True

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM documents WHERE id='REAL-DOC-1'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM documents WHERE id LIKE 'DEMO-%'").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM land_court_cases").fetchone()[0] == 0
    finally:
        connection.close()

    # clearing again is a clean no-op
    again = _run(env, "--clear", "--yes", "--json")
    assert again.returncode == 0
    assert json.loads(again.stdout)["removed"]["documents"] == 0


def test_production_environment_refuses_every_write(scratch_db):
    path, env = scratch_db
    env["APP_ENV"] = "production"
    env["JWT_SECRET"] = "test-only-random-secret"
    env["ALLOWED_ORIGINS"] = "https://records.example.gov"
    env["ADMIN_ACTION_SECRET"] = "test-only-admin-secret"

    seed = _run(env, "--yes")
    assert seed.returncode == 2, seed.stdout + seed.stderr
    assert "REFUSED" in seed.stdout and "production" in seed.stdout

    clear = _run(env, "--clear", "--yes")
    assert clear.returncode == 2

    # and the sneaky path: a bare --yes with --clear
    both = _run(env, "--clear", "--yes", "--check-only")
    assert both.returncode in {0, 2}  # read-only may proceed, writes may not

    assert not path.exists() or _seeded_rows(path)["documents"] == 0


def test_unknown_scenario_is_an_error(scratch_db):
    path, env = scratch_db
    result = _run(env, "--yes", "--scenario", "S999")
    assert result.returncode == 4
    assert "unknown scenario" in result.stderr.lower()
