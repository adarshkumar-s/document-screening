"""Administration → Data Management: full backup and safe restore.

Backup is a ZIP archive containing:
  * ``manifest.json`` — format version, generator, timestamps, per-table row
    counts and file counts, so a restore can validate before touching data.
  * ``db/tables/<table>.json`` — a logical, portable export of every
    application table (documents with OCR/extraction state, verification and
    AI-governance state, land records/map data, mutations, encumbrances, the
    audit trail, users, corrections, comparisons, …).
  * ``uploads/<stored document scans>`` — the uploaded documents themselves.

Restore (administrator only) follows a strict order:
  1. validate the archive (zip structure, manifest version, member safety);
  2. validate the database payload parses;
  3. write an automatic SAFETY COPY of the current data first
     (``data/backup_before_restore_<timestamp>/``) so a wrong restore is
     always undoable;
  4. only then restore (single transaction);
  5. verify restored row counts against the manifest;
  6. record every step in the existing audit trail.

Security: admin-only endpoints, member-name sanitisation (no absolute paths,
no ``..`` traversal), size limits, and table whitelisting by introspected
application schema (never ``sqlite_*`` internals).
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from server import (
    BASE_DIR,
    DATA_DIR,
    ROLE_ADMIN,
    UPLOADS_DIR,
    get_db,
    require_roles,
    SQLITE_PATH,
    HAS_PSYCOPG2,
    DATABASE_URL,
)
from land_intel import ensure_land_tables, _audit

backup_router = APIRouter(prefix="/api/admin/data-management", tags=["Data Management"])

BACKUP_FORMAT_VERSION = 1
MAX_BACKUP_UPLOAD_BYTES = 512 * 1024 * 1024  # 512 MB restore ceiling
MAX_SINGLE_UPLOAD_MEMBER_BYTES = 256 * 1024 * 1024
UPLOAD_MEMBER_PATTERN = re.compile(r"^[\w.\- ]+$")
# Introspected tables are exported only when they belong to the application
# schema (never SQLite internals or unknown postgreSQL system tables).
TABLE_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")
EXPORT_EXCLUDED_TABLES = {"sqlite_sequence", "geocode_cache"}  # cache is rebuildable
UPLOAD_ALLOWED_SUFFIXES = (".png", ".pdf", ".jpg", ".jpeg", ".tif", ".tiff")


def _is_pg() -> bool:
    return HAS_PSYCOPG2 and bool(DATABASE_URL)


def _application_tables(db: Any) -> List[str]:
    if db.is_pg:
        rows = db.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
        ).fetchall()
        names = [row["table_name"] for row in rows]
    else:
        rows = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        names = [row["name"] for row in rows]
    return [name for name in names if TABLE_NAME_PATTERN.match(name)]


def _table_rows(db: Any, table: str) -> List[Dict[str, Any]]:
    if not TABLE_NAME_PATTERN.match(table):
        raise ValueError(f"Refusing to export table with unexpected name: {table!r}")
    rows = db.execute(f"SELECT * FROM {table}").fetchall()
    return [dict(row) for row in rows]


def _collect_backup() -> Tuple[bytes, Dict[str, Any]]:
    """Build the entire backup ZIP in memory and return (bytes, manifest)."""
    ensure_land_tables()
    buffer = io.BytesIO()
    counts: Dict[str, int] = {}
    upload_files: List[str] = []
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with get_db() as db:
            tables = [name for name in _application_tables(db) if name not in EXPORT_EXCLUDED_TABLES]
            for table in tables:
                rows = _table_rows(db, table)
                counts[table] = len(rows)
                payload = json.dumps(rows, ensure_ascii=False, default=str)
                archive.writestr(f"db/tables/{table}.json", payload)
        if os.path.isdir(UPLOADS_DIR):
            for name in sorted(os.listdir(UPLOADS_DIR)):
                path = os.path.join(UPLOADS_DIR, name)
                if not os.path.isfile(path):
                    continue
                if not UPLOAD_MEMBER_PATTERN.match(name) or not name.lower().endswith(UPLOAD_ALLOWED_SUFFIXES):
                    continue
                with open(path, "rb") as handle:
                    archive.writestr(f"uploads/{name}", handle.read())
                upload_files.append(name)
    manifest = {
        "format_version": BACKUP_FORMAT_VERSION,
        "application": "document-screening",
        "kind": "full-backup",
        "database_mode": "postgresql-logical" if _is_pg() else "sqlite-logical",
        "created_at": time.time(),
        "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "includes": [
            "database (documents, OCR/extraction state, verification state, AI governance, land records, map data)",
            "mutations", "encumbrances", "risk registers", "audit trail", "users", "uploaded documents",
        ],
        "table_counts": counts,
        "upload_count": len(upload_files),
        "uploads": upload_files,
    }
    with zipfile.ZipFile(buffer, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return buffer.getvalue(), manifest


@backup_router.get("/backup")
def download_backup(user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    """Download the full backup ZIP (administrator only, audited)."""
    content, manifest = _collect_backup()
    filename = f"document_screening_backup_{time.strftime('%Y%m%d_%H%M')}.zip"
    _audit(user, "BACKUP_EXPORTED",
           f"Full backup {filename} exported ({manifest['table_counts'].get('documents', 0)} documents, "
           f"{manifest['upload_count']} uploads, {sum(manifest['table_counts'].values())} total rows)")
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@backup_router.get("/backup/manifest")
def backup_manifest_preview(user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    _, manifest = _collect_backup()
    return {"manifest": manifest}


def _safe_members(archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    """Reject path traversal and unexpected members before extraction."""
    safe: List[zipfile.ZipInfo] = []
    for member in archive.infolist():
        name = member.filename
        normalised = name.replace("\\", "/")
        if normalised.startswith("/") or ":" in normalised.split("/")[0]:
            raise HTTPException(status_code=400, detail=f"Unsafe backup member (absolute path): {name}")
        parts = [part for part in normalised.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise HTTPException(status_code=400, detail=f"Unsafe backup member (path traversal): {name}")
        if member.file_size > MAX_SINGLE_UPLOAD_MEMBER_BYTES:
            raise HTTPException(status_code=400, detail=f"Backup member too large: {name}")
        safe.append(member)
    return safe


def _validate_archive(content: bytes) -> Tuple[zipfile.ZipFile, Dict[str, Any], Dict[str, List[Dict[str, Any]]]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except Exception:
        raise HTTPException(status_code=400, detail="Not a valid ZIP archive.")
    _safe_members(archive)
    names = set(archive.namelist())
    if "manifest.json" not in names:
        raise HTTPException(status_code=400, detail="Missing manifest.json — not a document-screening backup.")
    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="manifest.json is not readable JSON.")
    if not isinstance(manifest, dict) or manifest.get("application") != "document-screening":
        raise HTTPException(status_code=400, detail="This archive is not a document-screening backup.")
    if int(manifest.get("format_version", 0)) > BACKUP_FORMAT_VERSION:
        raise HTTPException(status_code=400, detail="Backup format version is newer than this application supports.")
    table_payloads: Dict[str, List[Dict[str, Any]]] = {}
    for name in names:
        match = re.match(r"^db/tables/([a-z_][a-z0-9_]*)\.json$", name)
        if not match:
            continue
        table = match.group(1)
        try:
            rows = json.loads(archive.read(name).decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail=f"Table payload {name} is not readable JSON.")
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail=f"Table payload {name} is not a JSON array.")
        table_payloads[table] = rows
    if not table_payloads:
        raise HTTPException(status_code=400, detail="Backup contains no database payload.")
    if "documents" not in table_payloads:
        raise HTTPException(status_code=400, detail="Backup database payload is missing the documents table.")
    return archive, manifest, table_payloads


def _write_safety_copy(content_builder) -> str:
    """Automatic pre-restore backup under data/ so a wrong restore is undoable."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = os.path.join(DATA_DIR, f"backup_before_restore_{stamp}")
    os.makedirs(target, exist_ok=True)
    data, _ = content_builder()
    with open(os.path.join(target, "data_backup.zip"), "wb") as handle:
        handle.write(data)
    if not _is_pg() and os.path.isfile(SQLITE_PATH):
        # byte-level copy of the live SQLite database for maximum fidelity
        try:
            import shutil
            shutil.copy2(SQLITE_PATH, os.path.join(target, "land_records.db"))
        except Exception as exc:  # pragma: no cover
            print(f"[RESTORE SAFETY COPY WARNING] {exc}")
    return target


def _restore_tables(table_payloads: Dict[str, List[Dict[str, Any]]], manifest: Dict[str, Any]) -> Dict[str, int]:
    restored_counts: Dict[str, int] = {}
    with get_db() as db:
        if db.is_pg:
            db.execute("SAVEPOINT restore_sp")
        try:
            existing = set(_application_tables(db))
            # Import order: identity first, then dependent rows.
            ordered = sorted(table_payloads.keys())
            for preferred in ("users", "documents", "corrections", "audit"):
                if preferred in ordered:
                    ordered.remove(preferred)
                    ordered.insert(0, preferred)
            for table in ordered:
                if table not in existing:
                    continue  # never invent tables during restore
                rows = table_payloads[table]
                db.execute(f"DELETE FROM {table}")
                for row in rows:
                    columns = [key for key in row.keys() if TABLE_NAME_PATTERN.match(str(key))]
                    if not columns:
                        continue
                    placeholders = ", ".join("?" for _ in columns)
                    column_list = ", ".join(columns)
                    values = [row[column] for column in columns]
                    db.execute(f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})", tuple(values))
            if db.is_pg:
                db.execute("RELEASE SAVEPOINT restore_sp")
        except Exception:
            if db.is_pg:
                try:
                    db.execute("ROLLBACK TO SAVEPOINT restore_sp")
                except Exception:
                    pass
            db.conn.rollback()
            raise
    for table, rows in table_payloads.items():
        restored_counts[table] = len(rows)
    return restored_counts


def _restore_uploads(archive: zipfile.ZipFile, manifest: Dict[str, Any]) -> int:
    restored = 0
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    for member in archive.infolist():
        normalised = member.filename.replace("\\", "/")
        if not normalised.startswith("uploads/"):
            continue
        name = os.path.basename(normalised)
        if not name or not UPLOAD_MEMBER_PATTERN.match(name) or not name.lower().endswith(UPLOAD_ALLOWED_SUFFIXES):
            continue
        target = os.path.join(UPLOADS_DIR, name)
        if os.path.abspath(target) != os.path.join(os.path.abspath(UPLOADS_DIR), name):
            raise HTTPException(status_code=400, detail=f"Unsafe upload member: {member.filename}")
        with open(target, "wb") as handle:
            handle.write(archive.read(member))
        restored += 1
    return restored


def _verify_restore(restored_counts: Dict[str, int], manifest: Dict[str, Any]) -> Dict[str, Any]:
    mismatches: List[str] = []
    with get_db() as db:
        for table, expected in (manifest.get("table_counts") or {}).items():
            if table not in restored_counts or not TABLE_NAME_PATTERN.match(table):
                continue
            row = db.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            actual = int(row["c"]) if row else 0
            if actual != expected:
                mismatches.append(f"{table}: expected {expected}, found {actual}")
    return {"ok": not mismatches, "mismatches": mismatches}


@backup_router.post("/restore")
async def restore_backup(file: UploadFile, user: Dict[str, Any] = Depends(require_roles(ROLE_ADMIN))):
    """Restore from a backup ZIP (administrator only).

    Order: validate → safety copy → restore → verify → audit. Never overwrites
    production data blindly."""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty upload — nothing to restore.")
    if len(content) > MAX_BACKUP_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Backup exceeds the maximum allowed restore size.")
    _audit(user, "BACKUP_RESTORE_STARTED", f"Restore attempted from upload {file.filename!r} ({len(content)} bytes)")

    # 1 + 2. validate archive, manifest and payloads BEFORE touching anything
    archive, manifest, table_payloads = _validate_archive(content)
    for table, rows in table_payloads.items():
        for row in rows[:5]:
            if not isinstance(row, dict):
                raise HTTPException(status_code=400, detail=f"Table {table} contains malformed rows.")

    # 3. automatic safety copy of the CURRENT data
    safety_dir = _write_safety_copy(_collect_backup)
    _audit(user, "BACKUP_SAFETY_COPY_CREATED", f"Pre-restore safety backup written to {os.path.relpath(safety_dir, BASE_DIR)}")

    # 4. restore database + uploads
    restored_counts = _restore_tables(table_payloads, manifest)
    restored_uploads = _restore_uploads(archive, manifest)

    # 5. verify
    verification = _verify_restore(restored_counts, manifest)

    # 6. audit
    _audit(user, "BACKUP_RESTORED",
           f"Backup {file.filename!r} restored: {sum(restored_counts.values())} rows across {len(restored_counts)} tables, "
           f"{restored_uploads} uploads. Verification {'passed' if verification['ok'] else 'FLAGGED: ' + '; '.join(verification['mismatches'])}. "
           f"Safety copy: {os.path.relpath(safety_dir, BASE_DIR)}")
    return {
        "status": "restored",
        "safety_copy": os.path.relpath(safety_dir, BASE_DIR),
        "restored_counts": restored_counts,
        "uploads_restored": restored_uploads,
        "verification": verification,
    }
