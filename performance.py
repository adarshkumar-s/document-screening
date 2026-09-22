"""Low-risk performance helpers for the production ASGI app.

Keeps the existing business endpoints intact while preventing large audit/document
list responses from blocking the first paint. Detail endpoints are untouched.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from fastapi import Request
from fastapi.responses import JSONResponse

import server


DEFAULT_AUDIT_LIMIT = 100
DEFAULT_DOCUMENT_LIMIT = 200
MAX_LIST_LIMIT = 500


def _user_from_request(request: Request):
    """Use the application's canonical authentication function; never duplicate JWT logic."""
    try:
        authorization = request.headers.get("authorization", "")
        return server.get_current_user(request, authorization)
    except Exception:
        return None


def _json_obj(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _field_value(fields: Dict[str, Any], key: str) -> str:
    value = fields.get(key, "")
    if isinstance(value, dict):
        value = value.get("value", "")
    return str(value or "")


def _doc_summary(row: Any) -> Dict[str, Any]:
    """List-view projection: deliberately excludes OCR payloads."""
    fields = _json_obj(row["fields"])
    validation = _json_obj(row["validation"])
    metadata = _json_obj(row["metadata"])
    return {
        "id": row["id"],
        "filename": row["filename"],
        "doc_type": row["doc_type"],
        "mean_conf": row["mean_conf"],
        "verdict": row["verdict"],
        "status": row["status"],
        "languages": _json_obj(row["languages"]),
        "pages": row["pages"],
        "fields": fields,
        "validation": validation,
        "ai_decision_support": _json_obj(row["ai_decision_support"]),
        "detected_language": row["detected_language"],
        "original_fields": _json_obj(row["original_fields"]),
        "metadata": metadata,
        "uploaded_by": row["uploaded_by"],
        "reviewer_comments": row["reviewer_comments"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _install_indexes() -> None:
    """Prepare cheap list-view indexes once at startup, never during a request."""
    try:
        with server.get_db() as db:
            for statement in (
                "CREATE INDEX IF NOT EXISTS idx_perf_documents_created ON documents(created_at DESC, id)",
                "CREATE INDEX IF NOT EXISTS idx_perf_documents_status_created ON documents(status, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_perf_documents_uploader_created ON documents(uploaded_by, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_perf_audit_ts ON audit(ts DESC, id DESC)",
            ):
                try:
                    db.execute(statement)
                except Exception:
                    # PostgreSQL/SQLite schema differences should never prevent app startup.
                    pass
    except Exception:
        pass


def _audit_response(request: Request, user: Dict[str, Any]):
    if user.get("role") != server.ROLE_ADMIN:
        return None

    try:
        limit = min(max(int(request.query_params.get("limit", DEFAULT_AUDIT_LIMIT)), 1), MAX_LIST_LIMIT)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        limit, offset = DEFAULT_AUDIT_LIMIT, 0

    action = request.query_params.get("action", "").strip()
    username = request.query_params.get("username", "").strip()
    search = request.query_params.get("q", "").strip().casefold()

    where: List[str] = []
    params: List[Any] = []
    if action:
        where.append("action = ?")
        params.append(action)
    if username:
        where.append("username = ?")
        params.append(username)
    if search:
        where.append("(LOWER(action) LIKE ? OR LOWER(username) LIKE ? OR LOWER(detail) LIKE ?)")
        needle = f"%{search}%"
        params.extend([needle, needle, needle])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with server.get_db() as db:
        total = db.execute(f"SELECT COUNT(*) AS n FROM audit{where_sql}", tuple(params)).fetchone()["n"]
        rows = db.execute(
            f"SELECT id, ts, username, action, detail, doc_id FROM audit{where_sql} "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params + [limit, offset]),
        ).fetchall()

    payload = {"audit": [dict(row) for row in rows], "total": total,
               "limit": limit, "offset": offset, "has_more": offset + len(rows) < total}
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _documents_response(request: Request, user: Dict[str, Any]):
    try:
        limit = min(max(int(request.query_params.get("limit", DEFAULT_DOCUMENT_LIMIT)), 1), MAX_LIST_LIMIT)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        limit, offset = DEFAULT_DOCUMENT_LIMIT, 0

    status_filter = request.query_params.get("status", "").strip().upper()
    doc_type = request.query_params.get("doc_type", "").strip().casefold()
    search = request.query_params.get("q", "").strip().casefold()

    role = user.get("role")
    conditions: List[str] = []
    params: List[Any] = []
    if role == server.ROLE_VIEWER:
        conditions.append("status = ?")
        params.append(server.STATUS_APPROVED)
    elif role == server.ROLE_DATA_OFFICER:
        conditions.append("uploaded_by = ?")
        params.append(user.get("email", ""))
    if status_filter:
        conditions.append("status = ?")
        params.append(status_filter)
    if doc_type:
        conditions.append("LOWER(doc_type) = ?")
        params.append(doc_type)

    where_sql = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    # Never select ocr_text/cleaned_ocr_text for a list. Those payloads can be the
    # dominant source of transfer time; the existing detail endpoint remains the
    # source for opening a document.
    columns = (
        "id, filename, doc_type, mean_conf, verdict, status, languages, pages, fields, validation, "
        "ai_decision_support, detected_language, original_fields, metadata, uploaded_by, "
        "reviewer_comments, created_at, updated_at"
    )
    with server.get_db() as db:
        rows = db.execute(
            f"SELECT {columns} FROM documents{where_sql} ORDER BY created_at DESC, id DESC",
            tuple(params),
        ).fetchall()

    items = []
    for row in rows:
        item = _doc_summary(row)
        fields = item["fields"]
        searchable = " ".join([
            str(item.get("id") or ""), str(item.get("filename") or ""),
            str(item.get("doc_type") or ""), _field_value(fields, "owner_name"),
            _field_value(fields, "survey_number"), _field_value(fields, "khasra_number"),
            _field_value(fields, "village"), _field_value(fields, "district"),
        ]).casefold()
        if search and search not in searchable:
            continue
        items.append(item)

    total = len(items)
    page = items[offset:offset + limit]
    response = JSONResponse({"documents": page, "total": total, "limit": limit,
                             "offset": offset, "has_more": offset + len(page) < total})
    response.headers["Cache-Control"] = "private, no-store"
    return response


async def performance_middleware(request: Request, call_next):
    """Fast-path only the two heavy list endpoints; all other routes are untouched."""
    path = request.url.path.rstrip("/") or "/"
    if path not in {"/api/audit", "/api/documents"}:
        return await call_next(request)

    user = _user_from_request(request)
    if not user:
        return await call_next(request)

    try:
        if path == "/api/audit":
            response = _audit_response(request, user)
            if response is not None:
                return response
        elif path == "/api/documents":
            return _documents_response(request, user)
    except Exception:
        # Preserve the canonical endpoint as a safe fallback if a deployment has
        # an unexpected schema mismatch. Performance code must never break auth/UI.
        return await call_next(request)

    return await call_next(request)


_install_indexes()
