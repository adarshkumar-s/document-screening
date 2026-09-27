"""QR-backed certified-copy and public verification flow.

Inspired by the reference land-record portal's certified-copy workflow: a
verified record gets a tamper-evident SHA-256 fingerprint, a QR code points to
an unauthenticated verification page, and the generated PDF carries the same
QR. The verification page recomputes the fingerprint from the live record.
"""
from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from typing import Any, Dict

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

import server

router = APIRouter()


def ensure_columns() -> None:
    with server.get_db() as db:
        statements = (
            "ALTER TABLE documents ADD COLUMN cert_hash TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE documents ADD COLUMN cert_ref TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE documents ADD COLUMN cert_by TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE documents ADD COLUMN cert_at REAL NOT NULL DEFAULT 0",
        )
        for stmt in statements:
            try:
                db.execute(stmt if not db.is_pg else stmt.replace(" TEXT", " TEXT"))
            except Exception:
                # Column already exists on upgraded installations.
                pass


def _fields(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        value = row.get("fields") or "{}"
        return json.loads(value) if isinstance(value, str) else (value or {})
    except Exception:
        return {}


def _canonical(row: Dict[str, Any]) -> bytes:
    payload = {
        "id": row.get("id"),
        "filename": row.get("filename"),
        "doc_type": row.get("doc_type"),
        "status": row.get("status"),
        "fields": _fields(row),
        "ocr_text": row.get("ocr_text") or "",
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(row: Dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(row)).hexdigest()


def _qr_png(value: str) -> bytes:
    qr = qrcode.QRCode(version=None, box_size=8, border=3, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _get_doc(doc_id: str):
    with server.get_db() as db:
        row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    return dict(row) if row else None


def _certify(row: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    ref = row.get("cert_ref") or f"LVR-{time.strftime('%Y')}-{uuid.uuid4().hex[:8].upper()}"
    # Calculate after forcing the current verified status into the canonical
    # record. The hash intentionally includes OCR evidence and structured fields.
    current = dict(row)
    current["status"] = row.get("status")
    digest = fingerprint(current)
    now = time.time()
    with server.get_db() as db:
        db.execute(
            "UPDATE documents SET cert_hash=?, cert_ref=?, cert_by=?, cert_at=?, updated_at=? WHERE id=?",
            (digest, ref, user.get("email") or user.get("full_name") or "SYSTEM", now, now, row["id"]),
        )
    server.log_audit(user.get("full_name") or user.get("email") or "SYSTEM", "CERTIFIED_COPY_ISSUED", ref, row["id"])
    return {"cert_hash": digest, "cert_ref": ref, "cert_by": user.get("email") or "SYSTEM", "cert_at": now}


def _public_verification(row: Dict[str, Any]) -> Dict[str, Any]:
    stored = str(row.get("cert_hash") or "")
    recomputed = fingerprint(row)
    valid = bool(stored) and stored == recomputed
    fields = _fields(row)
    def value(key: str) -> str:
        v = fields.get(key)
        return str(v.get("value") or "") if isinstance(v, dict) else str(v or "")
    return {
        "verified": valid,
        "record_id": row.get("id"),
        "reference": row.get("cert_ref") or "",
        "filename": row.get("filename") or "",
        "status": row.get("status") or "",
        "certified_by": row.get("cert_by") or "",
        "certified_at": row.get("cert_at") or 0,
        "cert_hash": stored,
        "owner": value("owner_name"),
        "survey_number": value("survey_number"),
        "khasra_number": value("khasra_number"),
        "khata_number": value("khata_number"),
        "area": value("area"),
        "village": value("village"),
        "tehsil": value("tehsil"),
        "district": value("district"),
        "state": value("state"),
    }


@router.get("/api/documents/{doc_id}/certified-pdf")
def certified_pdf(doc_id: str, request: Request, user: dict = Depends(server.get_current_user)):
    row = _get_doc(doc_id)
    if not row:
        raise HTTPException(404, "Document not found")
    if row.get("status") not in (server.STATUS_APPROVED, "APPROVED", "verified", "auto_approved"):
        raise HTTPException(403, "Certified copies are available only after verification/approval")
    if not row.get("cert_hash") or not row.get("cert_ref"):
        _certify(row, user)
        row = _get_doc(doc_id)
    host = request.headers.get("host") or "localhost:8000"
    scheme = "https" if request.url.scheme == "https" else "http"
    verify_url = f"{scheme}://{host}/verify/{doc_id}"
    fields = _fields(row)
    report = {
        "reference_no": row.get("cert_ref"), "document_id": doc_id,
        "land_record_id": row.get("metadata") or doc_id,
        "owner": (fields.get("owner_name") or {}).get("value", ""),
        "father": (fields.get("father_name") or {}).get("value", ""),
        "survey": (fields.get("survey_number") or {}).get("value", ""),
        "khasra": (fields.get("khasra_number") or {}).get("value", ""),
        "area": (fields.get("area") or {}).get("value", ""),
        "village": (fields.get("village") or {}).get("value", ""),
        "tehsil": (fields.get("tehsil") or {}).get("value", ""),
        "district": (fields.get("district") or {}).get("value", ""),
        "verification_status": str(row.get("status") or "").upper(),
        "risk_status": "REVIEW", "encumbrance_status": "UNKNOWN",
        "mutation_status": "UNKNOWN", "litigation_status": "UNKNOWN",
        "generated_at": row.get("cert_at") or time.time(),
        "reviewer": row.get("cert_by") or "SYSTEM",
        "disclaimer": "Internal verification workflow certified copy. Verify authenticity by scanning the QR code.",
    }
    try:
        from report_pdf import render_verification_report_pdf
        pdf = render_verification_report_pdf(report, _qr_png(verify_url))
    except Exception as exc:
        raise HTTPException(500, f"Could not generate certified PDF: {type(exc).__name__}")
    server.log_audit(user.get("full_name") or user.get("email") or "SYSTEM", "CERTIFIED_COPY_DOWNLOADED", row.get("cert_ref") or "", doc_id)
    return Response(content=pdf, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="certified_record_{doc_id}.pdf"'})


@router.get("/verify/{doc_id}", response_class=HTMLResponse)
def verify_page(doc_id: str):
    row = _get_doc(doc_id)
    if not row or not row.get("cert_hash"):
        return HTMLResponse("<h2>NO CERTIFIED RECORD FOUND</h2><p>This record has not been certified.</p>", status_code=404)
    data = _public_verification(row)
    status_class = "ok" if data["verified"] else "bad"
    title = "VERIFIED — GENUINE & UNALTERED" if data["verified"] else "TAMPER CHECK FAILED"
    rows = "".join(
        f"<div class='row'><span>{label}</span><b>{data.get(key) or '—'}</b></div>"
        for label, key in (("Owner", "owner"), ("Survey Number", "survey_number"), ("Khasra", "khasra_number"), ("Khata", "khata_number"), ("Area", "area"), ("Village", "village"), ("Tehsil", "tehsil"), ("District", "district"), ("State", "state"))
    )
    html = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Land Record Verification</title><style>body{{font-family:system-ui,Arial;background:#eef2f7;padding:24px;color:#0f172a}}.card{{max-width:680px;margin:auto;background:white;border:1px solid #cbd5e1;border-radius:14px;padding:24px;box-shadow:0 4px 20px #0001}}h1{{font-size:20px}}.badge{{padding:14px;border-radius:10px;background:{'#dcfce7' if status_class=='ok' else '#fee2e2'};color:{'#166534' if status_class=='ok' else '#991b1b'};font-weight:800}}.row{{display:flex;gap:16px;padding:9px 0;border-bottom:1px dashed #ddd}}.row span{{width:150px;color:#64748b}}small{{color:#64748b;word-break:break-all}}</style></head><body><div class='card'><h1>🏛️ Land Record Verification</h1><div class='badge'>{title}</div><p>Reference: <b>{data['reference'] or '—'}</b></p>{rows}<p><small>Certification hash: {data['cert_hash']}</small></p><p><small>Certified by: {data['certified_by'] or '—'} · The hash is recomputed from the live record when this page is opened.</small></p></div></body></html>"""
    return HTMLResponse(html)


def install() -> None:
    ensure_columns()
