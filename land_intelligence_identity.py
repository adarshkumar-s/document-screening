"""Fast indexed identity projection for saved land documents.

The source of truth remains ``documents.fields``. This table is a derived,
rebuildable projection used only for indexed Land Intelligence lookup.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Dict, Optional

from server import get_db

IDENTITY_KEYS = (
    "survey_number", "gat_number", "khasra_number", "sub_division",
    "village", "taluka", "district",
)
INDEXES = {
    "survey_number": "idx_doc_land_identity_survey",
    "gat_number": "idx_doc_land_identity_gat",
    "khasra_number": "idx_doc_land_identity_khasra",
    "sub_division": "idx_doc_land_identity_subdivision",
    "village": "idx_doc_land_identity_village",
    "taluka": "idx_doc_land_identity_taluka",
    "district": "idx_doc_land_identity_district",
}
_lock = threading.Lock()
_ready = False


def _norm(value: Any) -> str:
    value = str(value or "")
    value = re.sub(r"\s+", " ", value.strip().casefold())
    return value


def _value(fields: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = fields.get(key)
        if isinstance(value, dict):
            value = value.get("value") or value.get("text")
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def extract_identity(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "survey_number": _value(fields, "survey_number"),
        "gat_number": _value(fields, "gat_number"),
        "khasra_number": _value(fields, "khasra_number"),
        "sub_division": _value(fields, "sub_division", "subdivision"),
        "village": _value(fields, "village"),
        "taluka": _value(fields, "taluka", "tehsil"),
        "district": _value(fields, "district"),
        "area": _value(fields, "area"),
        "owner_name": _value(fields, "owner_name", "owner"),
    }


def ensure_store() -> None:
    """Create the derived store and indexes once during application startup."""
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        with get_db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS document_land_identity (
                document_id TEXT PRIMARY KEY,
                survey_number TEXT, survey_number_norm TEXT,
                gat_number TEXT, gat_number_norm TEXT,
                khasra_number TEXT, khasra_number_norm TEXT,
                sub_division TEXT, sub_division_norm TEXT,
                village TEXT, village_norm TEXT,
                taluka TEXT, taluka_norm TEXT,
                district TEXT, district_norm TEXT,
                area TEXT, owner_name TEXT,
                identity_version INTEGER NOT NULL DEFAULT 1,
                source_updated_at REAL
            )""")
            for key, index_name in INDEXES.items():
                db.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON document_land_identity ({key}_norm)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_doc_land_identity_owner ON document_land_identity (owner_name)")
        _ready = True


def upsert_document(document_id: str, fields: Dict[str, Any], source_updated_at: Optional[float] = None) -> None:
    ensure_store()
    identity = extract_identity(fields or {})
    columns = ["document_id"]
    values = [document_id]
    for key in IDENTITY_KEYS:
        columns.extend([key, f"{key}_norm"])
        value = identity.get(key)
        values.extend([value, _norm(value) or None])
    columns.extend(["area", "owner_name", "identity_version", "source_updated_at"])
    values.extend([identity.get("area"), identity.get("owner_name"), 1, source_updated_at])
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{c}=excluded.{c}" for c in columns[1:])
    with get_db() as db:
        db.execute(
            f"INSERT INTO document_land_identity ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(document_id) DO UPDATE SET {updates}",
            tuple(values),
        )


def backfill(limit: int = 500) -> int:
    """Backfill a bounded batch; safe to call repeatedly from a background worker."""
    ensure_store()
    with get_db() as db:
        rows = db.execute("""SELECT d.id,d.fields,d.updated_at
                            FROM documents d
                            LEFT JOIN document_land_identity i ON i.document_id=d.id
                            WHERE i.document_id IS NULL
                            ORDER BY d.created_at ASC LIMIT ?""", (limit,)).fetchall()
    count = 0
    for row in rows:
        try:
            fields = json.loads(row["fields"] or "{}")
            upsert_document(str(row["id"]), fields, row["updated_at"])
            count += 1
        except Exception:
            continue
    return count


def get_identity(document_id: str) -> Optional[Dict[str, Any]]:
    ensure_store()
    with get_db() as db:
        row = db.execute("SELECT * FROM document_land_identity WHERE document_id=?", (document_id,)).fetchone()
    return dict(row) if row else None
