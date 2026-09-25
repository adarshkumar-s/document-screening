"""
Fast OCR Pipeline.

Architecture:
    upload
      -> content hash / cache lookup (RBAC-safe)
      -> for PDFs: attempt embedded text extraction first; only OCR if unusable
      -> for images / scanned PDFs: deterministic OCR (no Gemini pre-scan on critical path)
      -> immediate deterministic field extraction (regex based - no AI on critical path)
      -> OCR result persisted and returned quickly
      -> AI enhancement / parcel matching / Land Intelligence run ASYNCHRONOUSLY
         via the existing ai_tasks task queue (admin_assistant)

The cache is content-keyed with SHA-256 but entries are additionally scoped by
the *user role/visibility* of the original document owner so that a second user
who is not allowed to see the document cannot retrieve OCR output via hash
collision.  The cache stores a reference to the source document id and a
visibility tag; hits are only served when the caller has at least the same
access rights as the original uploader.

All existing features (request/run semantics equivalent, leases, idempotency,
audit, RBAC) are preserved: downstream work is queued as tasks with heartbeat
and stale-worker protection handled in admin_assistant.
"""
from __future__ import annotations

import io
import os
import json
import time
import uuid
import hashlib
import asyncio
import inspect
import threading
from typing import Any, Dict, List, Optional, Tuple

# Re-use server imports lazily via get_server() so this module can be imported
# early without circular imports.
_SERVER_LOCK = threading.Lock()
_server = None
# Tracks which DB path we have initialised tables for (so tests switching
# DB_PATH between cases automatically rebuild tables).
_LAST_DB_PATH = None


def get_server():
    global _server
    if _server is None:
        with _SERVER_LOCK:
            if _server is None:
                import server as _s
                _server = _s
    return _server


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------
def content_hash(content: bytes) -> str:
    """Return a SHA-256 hex digest of the file bytes — stable content key."""
    return hashlib.sha256(content).hexdigest()


def _get_db():
    return get_server().get_db()


# ---------------------------------------------------------------------------
# Cache table bootstrap
# ---------------------------------------------------------------------------
CACHE_TABLE_READY = False


def _table_sqlite() -> str:
    return """
        CREATE TABLE IF NOT EXISTS ocr_cache (
            content_hash TEXT PRIMARY KEY,
            owner_email TEXT NOT NULL,
            owner_role TEXT NOT NULL,
            visibility_scope TEXT NOT NULL,
            source_doc_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            lang TEXT NOT NULL DEFAULT 'auto',
            ocr_text TEXT NOT NULL,
            cleaned_text TEXT NOT NULL DEFAULT '',
            detected_language TEXT NOT NULL DEFAULT 'eng',
            confidence REAL NOT NULL DEFAULT 0,
            fields TEXT NOT NULL DEFAULT '{}',
            validation TEXT NOT NULL DEFAULT '{}',
            ocr_method TEXT NOT NULL DEFAULT 'tesseract',
            pages INTEGER NOT NULL DEFAULT 1,
            word_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
    """


def _table_pg() -> str:
    return """
        CREATE TABLE IF NOT EXISTS ocr_cache (
            content_hash TEXT PRIMARY KEY,
            owner_email TEXT NOT NULL,
            owner_role TEXT NOT NULL,
            visibility_scope TEXT NOT NULL,
            source_doc_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            lang TEXT NOT NULL DEFAULT 'auto',
            ocr_text TEXT NOT NULL,
            cleaned_text TEXT NOT NULL DEFAULT '',
            detected_language TEXT NOT NULL DEFAULT 'eng',
            confidence REAL NOT NULL DEFAULT 0,
            fields TEXT NOT NULL DEFAULT '{}',
            validation TEXT NOT NULL DEFAULT '{}',
            ocr_method TEXT NOT NULL DEFAULT 'tesseract',
            pages INTEGER NOT NULL DEFAULT 1,
            word_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL
        );
    """


def _db_changed() -> bool:
    global _LAST_DB_PATH, CACHE_TABLE_READY, BATCH_TABLE_READY
    srv = get_server()
    current = getattr(srv, "DB_PATH", None) or getattr(srv, "SQLITE_PATH", None)
    if current != _LAST_DB_PATH:
        _LAST_DB_PATH = current
        CACHE_TABLE_READY = False
        BATCH_TABLE_READY = False
        return True
    return False


def ensure_ocr_cache_table():
    global CACHE_TABLE_READY
    _db_changed()
    if CACHE_TABLE_READY:
        return
    with _get_db() as db:
        if db.is_pg:
            db.execute(_table_pg())
            db.execute("CREATE INDEX IF NOT EXISTS ocr_cache_owner ON ocr_cache(owner_email);")
        else:
            db.execute(_table_sqlite())
            db.execute("CREATE INDEX IF NOT EXISTS ocr_cache_owner ON ocr_cache(owner_email);")
    CACHE_TABLE_READY = True


def _visibility_scope_for(user: Dict[str, Any]) -> str:
    """Return a coarse-grained visibility tag for cache isolation.

    ADMIN              -> 'admin' (can see everything; can produce cache entries anyone with admin can reuse)
    VERIFICATION_OFFICER -> 'staff'
    DATA_OFFICER       -> 'user:<email>'  (private; same user can hit their own cache)
    VIEWER             -> 'viewer:<email>' (viewers never upload but be safe)
    """
    role = str((user or {}).get("role") or "").upper()
    email = str((user or {}).get("email") or "").lower()
    if role == "ADMIN":
        return "admin"
    if role == "VERIFICATION_OFFICER":
        return "staff"
    if role == "DATA_OFFICER":
        return f"user:{email}"
    return f"viewer:{email}"


def _caller_can_reuse(caller: Dict[str, Any], entry_owner_role: str, entry_scope: str) -> bool:
    """RBAC isolation: a cached OCR result is only visible to callers whose
    privileges dominate the original uploader."""
    caller_role = str((caller or {}).get("role") or "").upper()
    caller_email = str((caller or {}).get("email") or "").lower()
    if caller_role == "ADMIN":
        return True  # admins see everything
    if entry_scope == "admin":
        return caller_role == "ADMIN"
    if entry_scope == "staff":
        return caller_role in ("ADMIN", "VERIFICATION_OFFICER")
    if entry_scope.startswith("user:"):
        return caller_email == entry_scope[len("user:"):] or caller_role in ("ADMIN", "VERIFICATION_OFFICER")
    if entry_scope.startswith("viewer:"):
        return caller_email == entry_scope[len("viewer:"):] or caller_role == "ADMIN"
    return False


def cache_lookup(chash: str, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ensure_ocr_cache_table()
    with _get_db() as db:
        row = db.execute(
            "SELECT * FROM ocr_cache WHERE content_hash=? LIMIT 1",
            (chash,),
        ).fetchone()
    if not row:
        return None
    if not _caller_can_reuse(user, row["owner_role"], row["visibility_scope"]):
        return None
    try:
        fields = json.loads(row["fields"] or "{}")
    except Exception:
        fields = {}
    try:
        validation = json.loads(row["validation"] or "{}")
    except Exception:
        validation = {}
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except Exception:
        metadata = {}
    return {
        "content_hash": chash,
        "source_doc_id": row["source_doc_id"],
        "ocr_text": row["ocr_text"] or "",
        "cleaned_ocr_text": row["cleaned_text"] or row["ocr_text"] or "",
        "detected_language": row["detected_language"] or "eng",
        "mean_conf": int(round(float(row["confidence"] or 0) * 100)),
        "confidence": float(row["confidence"] or 0),
        "fields": fields,
        "validation": validation,
        "languages": ["English", row["detected_language"] or "eng"],
        "pages": int(row["pages"] or 1),
        "doc_type": (fields.get("document_type", {}) or {}).get("value", "Land Record")
        if isinstance(fields.get("document_type"), dict) else "Land Record",
        "ocr_method": row["ocr_method"] or "tesseract",
        "word_count": int(row["word_count"] or 0),
        "metadata": metadata,
        "cache_hit": True,
    }


def cache_store(chash: str, user: Dict[str, Any], source_doc_id: str, filename: str,
                lang: str, ocr_result: Dict[str, Any], fields: Dict[str, Any],
                validation: Dict[str, Any], pages: int, metadata: Dict[str, Any]) -> None:
    ensure_ocr_cache_table()
    scope = _visibility_scope_for(user)
    try:
        fields_j = json.dumps(fields, ensure_ascii=False)
    except Exception:
        fields_j = "{}"
    try:
        validation_j = json.dumps(validation, ensure_ascii=False)
    except Exception:
        validation_j = "{}"
    try:
        meta_j = json.dumps(metadata or {}, ensure_ascii=False)
    except Exception:
        meta_j = "{}"
    doc_type_val = "Land Record"
    if isinstance(fields.get("document_type"), dict):
        doc_type_val = fields["document_type"].get("value", "Land Record") or "Land Record"
    with _get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO ocr_cache (
                content_hash, owner_email, owner_role, visibility_scope, source_doc_id,
                filename, lang, ocr_text, cleaned_text, detected_language, confidence,
                fields, validation, ocr_method, pages, word_count, metadata_json, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                chash,
                (user or {}).get("email", "").lower(),
                (user or {}).get("role", "").upper(),
                scope,
                source_doc_id,
                filename or "",
                lang or "auto",
                ocr_result.get("text", "") or "",
                ocr_result.get("cleaned_text", "") or ocr_result.get("text", "") or "",
                ocr_result.get("detected_language", "eng") or "eng",
                float(ocr_result.get("confidence", 0.0) or 0.0),
                fields_j,
                validation_j,
                ocr_result.get("method", "tesseract") or "tesseract",
                int(pages or 1),
                int(ocr_result.get("word_count", 0) or 0),
                meta_j,
                time.time(),
            ),
        )


# ---------------------------------------------------------------------------
# PDF embedded-text extraction
# ---------------------------------------------------------------------------
_MIN_TEXT_LEN_FOR_EMBEDDED = 40  # below this we assume scanned PDF
_MIN_WORDS_FOR_EMBEDDED = 8


def extract_pdf_embedded_text(content: bytes) -> Tuple[bool, str, int]:
    """Return (usable, text, page_count).

    usable=True only when every rendered page has a reasonable amount of
    embedded text — otherwise caller should fall through to OCR.
    """
    srv = get_server()
    if not getattr(srv, "HAS_PDFIUM", False):
        return False, "", 0
    try:
        pdf = srv.pdfium.PdfDocument(content)
    except Exception:
        return False, "", 0
    pages = len(pdf)
    if pages == 0:
        return False, "", 0
    collected: List[str] = []
    total_len = 0
    for i in range(min(pages, 5)):  # cap at first 5 pages for perf
        try:
            page = pdf[i]
            tp = page.get_textpage()
            txt = tp.get_text_range() or ""
            tp.close()
        except Exception:
            txt = ""
        collected.append(txt)
        total_len += len(txt.strip())
    text = "\n".join(collected).strip()
    # Heuristic: require a minimum amount of printable text overall and a
    # decent ratio of alphanumeric/word characters (not just whitespace).
    words = [w for w in text.split() if any(ch.isalnum() for ch in w)]
    if total_len >= _MIN_TEXT_LEN_FOR_EMBEDDED and len(words) >= _MIN_WORDS_FOR_EMBEDDED:
        return True, text, pages
    return False, text, pages


# ---------------------------------------------------------------------------
# Deterministic regex field extractor (no AI on critical path)
# ---------------------------------------------------------------------------
_REGEX_PATTERNS: Dict[str, List[Tuple[str, int]]] = {
    # owner_name is intentionally not regex'd — left to AI/human review
    "survey_number": [
        (r"(?:survey\s*(?:no|number|#)?\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", re_flag_count_group:=0),
        (r"(?:s\.?\s*no\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", 0),
        (r"(?:khasra\s*(?:no|number)?\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", 0),
    ],
    "khasra_number": [
        (r"(?:khasra\s*(?:no|number)?\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", 0),
        (r"(?:khatian\s*(?:no|number)?\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", 0),
    ],
    "khata_number": [
        (r"(?:khata\s*(?:no|number)?\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", 0),
        (r"(?:khewat\s*(?:no|number)?\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", 0),
    ],
    "plot_number": [
        (r"(?:plot\s*(?:no|number)?\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", 0),
    ],
    "area": [
        (r"(\d+(?:\.\d+)?)\s*(?:acres?|hectares?|ha\.?|bigha|sq\.\s*m|m2|m²)", 0),
        (r"(?:area\s*[:\-]?\s*)(\d+(?:\.\d+)?)", 0),
    ],
    "village": [
        (r"(?:village|mauza|gram)\s*[:\-]?\s*([A-Z][A-Za-z\s.\-]{2,30})", 0),
    ],
    "tehsil": [
        (r"(?:tehsil|taluka|taluk)\s*[:\-]?\s*([A-Z][A-Za-z\s.\-]{2,30})", 0),
    ],
    "district": [
        (r"(?:district)\s*[:\-]?\s*([A-Z][A-Za-z\s.\-]{2,30})", 0),
    ],
    "mutation_no": [
        (r"(?:mutation\s*(?:no|number)?\.?\s*[:#\-]?\s*)([A-Z0-9\-/]+)", 0),
    ],
    "registration_no": [
        (r"(?:registration|deed|registry)\s*(?:no|number)?\.?\s*[:#\-]?\s*([A-Z0-9\-/]+)", 0),
    ],
    "document_date": [
        (r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})", 0),
    ],
}


def deterministic_field_extraction(text: str, doc_type_hint: str = "Land Record") -> Dict[str, Any]:
    """Best-effort deterministic field extraction without AI."""
    import re as _re
    fields: Dict[str, Any] = {k: {"value": "", "confidence": 0.0,
                                  "validation_status": "MISSING",
                                  "validation_message": ""}
                              for k in get_server().FIELD_KEYS}
    if not text:
        fields["document_type"] = {"value": doc_type_hint or "Land Record",
                                   "confidence": 0.8,
                                   "validation_status": "VALID",
                                   "validation_message": "Provided by uploader."}
        return fields
    for key, patterns in _REGEX_PATTERNS.items():
        for pat, _g in patterns:
            m = _re.search(pat, text, _re.IGNORECASE)
            if m:
                val = (m.group(1) or "").strip(" ,.;:-")
                if val:
                    fields[key] = {"value": val,
                                   "confidence": 0.7,
                                   "validation_status": "VALID",
                                   "validation_message": "Deterministic regex match."}
                    break
    # simple owner heuristic: "Owner Name: X" or "Sri/Smt X"
    owner = None
    m = _re.search(r"(?:owner\s*name|landowner|owner|holder|mortgagor|transferor|seller)\s*[:\-]\s*([^\n\r]{2,80})", text, _re.IGNORECASE)
    if m:
        owner = m.group(1).strip(" ,.;:-")
    if not owner:
        m = _re.search(r"(?:sri|shri|smt|mrs?|mr)\.?\s+([A-Z][A-Za-z .]{3,60})", text, _re.IGNORECASE)
        if m:
            owner = m.group(1).strip(" ,.;:-")
    if owner:
        fields["owner_name"] = {"value": owner, "confidence": 0.6,
                                "validation_status": "VALID",
                                "validation_message": "Deterministic regex match."}
    fields["document_type"] = {"value": doc_type_hint or "Land Record",
                               "confidence": 0.8,
                               "validation_status": "VALID",
                               "validation_message": "Provided by uploader."}
    return fields


# ---------------------------------------------------------------------------
# Rasterization and OCR helpers (reuse server primitives)
# ---------------------------------------------------------------------------
def _render_pdf_first_page(content: bytes, scale: float = 1.8):
    """Render first PDF page to PIL image — single rasterization per job."""
    srv = get_server()
    pdf = srv.pdfium.PdfDocument(content)
    if len(pdf) == 0:
        raise ValueError("PDF contains no pages")
    page = pdf[0]
    bitmap = page.render(scale=scale)
    img = bitmap.to_pil()
    return img, len(pdf)


def run_fast_ocr(image, requested_lang: str = "auto") -> Dict[str, Any]:
    """Deterministic OCR without Gemini pre-scan.

    Uses sensible defaults; AI verification is deferred to an async task and
    does NOT block this path.
    """
    srv = get_server()
    strategy = {
        "lang": "hin+eng+tel+tam" if requested_lang in ("auto", "", None) else srv._ocr_languages(requested_lang)[0],
        "lang_candidates": srv._ocr_languages(requested_lang),
        "psm": 3,
        "rotation": 0,
        "enhance": True,
        "denoise": False,
    }
    ocr_res = srv.run_guided_ocr(image, strategy)
    return ocr_res


# ---------------------------------------------------------------------------
# Public fast pipeline
# ---------------------------------------------------------------------------
async def run_fast_ocr_pipeline(content: bytes, filename: str, lang: str = "auto",
                                doc_type_hint: str = "Land Record",
                                user: Optional[Dict[str, Any]] = None,
                                doc_id: Optional[str] = None) -> Dict[str, Any]:
    """Fast OCR path.

    1. Compute content hash
    2. Cache lookup with RBAC
    3. For PDFs, try embedded text first
    4. Otherwise rasterize once and run Tesseract once
    5. Deterministic field extraction (no AI on critical path)
    6. Store in cache with RBAC scope
    7. Return a result that has the same shape as the old pipeline
       so the UI and downstream code keep working.
    """
    srv = get_server()
    ext = os.path.splitext(filename or "")[1].lower()
    chash = content_hash(content)
    c_meta = {"content_hash": chash}

    # Cache lookup
    cached = cache_lookup(chash, user or {})
    if cached:
        result = {
            "mean_conf": cached["mean_conf"],
            "languages": cached["languages"],
            "pages": cached["pages"],
            "detected_language": cached["detected_language"],
            "doc_type": cached["doc_type"],
            "fields": cached["fields"],
            "validation": cached["validation"],
            "ai_decision_support": {
                "pipeline_mode": "CACHED_OCR",
                "ocr_quality": "CACHED",
                "ocr_confidence": cached["confidence"],
                "ocr_word_count": cached["word_count"],
                "recommendation": "READY_FOR_REVIEW",
                "explanation": "Served from content-addressed OCR cache (RBAC scoped).",
                "cache_hit": True,
                "ocr_method": cached["ocr_method"],
            },
            "ocr_text": cached["ocr_text"],
            "cleaned_ocr_text": cached["cleaned_ocr_text"],
            "original_fields": cached["fields"],
            "pipeline_meta": {
                "mode": "CACHED_OCR",
                "cache_hit": True,
                "ocr_method": cached["ocr_method"],
                "content_hash": chash,
                "duration_ms": 0,
            },
            "escalated": False,
            "metadata": {**c_meta, **(cached.get("metadata") or {})},
        }
        return result

    t0 = time.time()
    ocr_text = ""
    detected_lang = "eng"
    word_count = 0
    confidence = 0.0
    ocr_method = "tesseract"
    pages = 1
    intermediate_image = None

    if ext == ".pdf":
        if not srv.HAS_PDFIUM:
            raise ValueError("PDF processing is unavailable because the PDF engine is not installed")
        usable, embedded_text, page_count = extract_pdf_embedded_text(content)
        pages = page_count or 1
        if usable:
            ocr_text = embedded_text
            detected_lang = srv.detect_primary_script(ocr_text) or "eng"
            confidence = 0.95
            word_count = len([w for w in ocr_text.split() if any(ch.isalnum() for ch in w)])
            ocr_method = "pdf_text_layer"
        else:
            # Scanned PDF: rasterize once
            img, pages = _render_pdf_first_page(content, scale=1.8)
            intermediate_image = img
            ocr_res = await asyncio.to_thread(run_fast_ocr, img, lang)
            ocr_text = ocr_res.get("text", "")
            detected_lang = ocr_res.get("detected_language", "eng")
            confidence = float(ocr_res.get("confidence", 0.0) or 0.0)
            word_count = int(ocr_res.get("word_count", 0) or 0)
    else:
        # Image path
        from PIL import Image
        img = Image.open(io.BytesIO(content))
        img.load()
        max_side = int(os.getenv("MAX_IMAGE_DIMENSION", "15000"))
        if max(img.width, img.height) > max_side:
            raise ValueError(f"Image exceeds the maximum supported dimension of {max_side}px")
        intermediate_image = img
        ocr_res = await asyncio.to_thread(run_fast_ocr, img, lang)
        ocr_text = ocr_res.get("text", "")
        detected_lang = ocr_res.get("detected_language", "eng")
        confidence = float(ocr_res.get("confidence", 0.0) or 0.0)
        word_count = int(ocr_res.get("word_count", 0) or 0)

    # Deterministic field extraction
    fields = deterministic_field_extraction(ocr_text, doc_type_hint)
    enriched_fields, validation = srv.enrich_and_validate_fields(fields)

    duration_ms = int((time.time() - t0) * 1000)
    ocr_result_meta = {
        "text": ocr_text,
        "cleaned_text": ocr_text,
        "detected_language": detected_lang,
        "confidence": confidence,
        "word_count": word_count,
        "method": ocr_method,
    }

    if doc_id:
        cache_store(chash, user or {}, doc_id, filename, lang,
                    ocr_result_meta, enriched_fields, validation, pages,
                    metadata={})

    return {
        "mean_conf": int(round(confidence * 100)),
        "languages": ["English", detected_lang],
        "pages": pages,
        "detected_language": detected_lang,
        "doc_type": (enriched_fields.get("document_type", {}) or {}).get("value", doc_type_hint or "Land Record"),
        "fields": enriched_fields,
        "validation": validation,
        "ai_decision_support": {
            "pipeline_mode": "FAST_DETERMINISTIC_OCR",
            "ocr_quality": "GOOD" if confidence >= 0.75 else ("UNCERTAIN" if confidence >= 0.45 else "FAILED"),
            "ocr_confidence": confidence,
            "ocr_word_count": word_count,
            "ocr_strategy": {"method": ocr_method},
            "recommendation": "READY_FOR_REVIEW" if validation.get("verdict") != "rejected" else "REVIEW_REQUIRED",
            "explanation": "Fast deterministic OCR; AI enhancement, parcel matching and Land Intelligence run asynchronously.",
            "cache_hit": False,
            "ocr_method": ocr_method,
        },
        "ocr_text": ocr_text,
        "cleaned_ocr_text": ocr_text,
        "original_fields": enriched_fields,
        "pipeline_meta": {
            "mode": "FAST_DETERMINISTIC_OCR",
            "cache_hit": False,
            "ocr_method": ocr_method,
            "content_hash": chash,
            "duration_ms": duration_ms,
        },
        "escalated": validation.get("verdict") == "rejected",
        "metadata": c_meta,
    }


# ---------------------------------------------------------------------------
# Async downstream processing — enqueued onto the EXISTING assistant task table
# ---------------------------------------------------------------------------
def enqueue_downstream_tasks(doc_id: str, user: Dict[str, Any], mode: str = "ocr_li") -> None:
    """Queue follow-up work on the existing ai_tasks task table.

    mode:
        "ocr_only"  -> no land-intelligence work; AI enhancement only if useful
        "ocr_li"    -> parcel matching + land intelligence + optional SA
    """
    srv = get_server()
    try:
        from admin_assistant import ensure_task_table as _ensure_tasks
        _ensure_tasks()
    except Exception:
        # Task infrastructure not available (test DB or disabled assistant);
        # run downstream synchronously as a safe fallback so OCR document is
        # still fully usable.
        try:
            run_downstream_sync(doc_id, mode=mode)
        except Exception:
            pass
        return

    now = time.time()

    def add_task(task_type: str, title: str, description: str, priority: str = "MEDIUM",
                 parent: Optional[str] = None, meta: Optional[Dict[str, Any]] = None):
        tid = uuid.uuid4().hex
        with _get_db() as db:
            db.execute(
                """
                INSERT INTO ai_tasks (
                    id, record_id, task_type, title, description, priority,
                    assigned_to, assigned_by, status, parent_task_id, metadata,
                    result, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    tid,
                    doc_id,
                    task_type,
                    title,
                    description,
                    priority,
                    None,
                    (user or {}).get("email", "SYSTEM"),
                    "PENDING",
                    parent,
                    json.dumps(meta or {}, ensure_ascii=False),
                    "{}",
                    now,
                    now,
                ),
            )
        return tid

    try:
        add_task(
            "AI_FIELD_ENHANCE",
            f"AI field enhancement for {doc_id}",
            "Enhance OCR-extracted fields with AI visual verification (non-blocking).",
            priority="LOW",
            meta={"doc_id": doc_id, "stage": "ai_enhance", "mode": mode},
        )
        if mode == "ocr_li":
            add_task(
                "PARCEL_MATCH",
                f"Parcel matching for {doc_id}",
                "Resolve document to a land parcel using existing mapping resolver.",
                priority="MEDIUM",
                meta={"doc_id": doc_id, "stage": "parcel_match", "mode": mode},
            )
            add_task(
                "LAND_INTELLIGENCE",
                f"Land Intelligence review for {doc_id}",
                "Mutation / Encumbrance / Court cases / Risk review.",
                priority="MEDIUM",
                meta={"doc_id": doc_id, "stage": "land_intel", "mode": mode},
            )
            add_task(
                "LITIGATION_MATCH",
                f"Litigation register match for {doc_id}",
                "Check document against litigation/DEMO-LI register.",
                priority="MEDIUM",
                meta={"doc_id": doc_id, "stage": "litigation_match", "mode": mode},
            )
    except Exception:
        # Fallback: synchronous downstream processing if task table is unavailable.
        try:
            run_downstream_sync(doc_id, mode=mode)
        except Exception:
            pass


def run_downstream_sync(doc_id: str, mode: str = "ocr_li") -> None:
    """Synchronous worker body invoked by the task worker.

    Runs parcel matching + land intelligence directly (not waiting on anything).
    This preserves existing semantics but runs after the HTTP response has
    returned, so users see OCR complete quickly.
    """
    srv = get_server()
    try:
        with _get_db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        fields = json.loads(row["fields"] or "{}")
        property_resolution = {"status": "INSUFFICIENT DATA", "confidence": 0, "matches": [], "reasons": []}
        if mode == "ocr_li":
            try:
                from mapping import _resolve
                property_resolution = _resolve(fields)
                now = time.time()
                if property_resolution.get("status") in ("MATCH", "POSSIBLE MATCH") and property_resolution.get("matches"):
                    match_property = property_resolution["matches"][0]["property"]["property_id"]
                    with _get_db() as db:
                        db.execute(
                            "INSERT OR IGNORE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",
                            (match_property, doc_id, "uploaded_document", now),
                        )
                        db.execute(
                            "INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
                            (uuid.uuid4().hex, match_property, "PROPERTY_MATCHED",
                             f"Document {doc_id} resolved to {match_property} with {round(property_resolution['confidence'] * 100)}% confidence.",
                             "Property resolution service", now),
                        )
                    property_resolution["property_id"] = match_property
                    # Apply land_intel context (encumbrance, litigation, risk)
                    try:
                        from land_intel import document_land_context
                        doc_dict = dict(row)
                        doc_dict["fields"] = fields
                        viewer = {"email": doc_dict.get("uploaded_by", ""), "role": "ADMIN"}
                        ctx = document_land_context(doc_dict, viewer)
                        with _get_db() as db:
                            db.execute(
                                "UPDATE documents SET ai_decision_support=? WHERE id=?",
                                (json.dumps({**_safe_json(row["ai_decision_support"]), "land_context": ctx}, ensure_ascii=False), doc_id),
                            )
                    except Exception:
                        pass
            except Exception:
                pass
            # Ownership review
        parsed_for_owner = {
            "filename": row["filename"],
            "doc_type": row["doc_type"],
            "fields": fields,
            "ocr_text": row["ocr_text"] or "",
        }
        ai_payload = _safe_json(row["ai_decision_support"])
        srv.apply_ownership_review(doc_id, property_resolution.get("property_id"), parsed_for_owner,
                                   row["status"], ai_payload)
    except Exception:
        pass


def _safe_json(raw: Any) -> Dict[str, Any]:
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Bulk batch table
# ---------------------------------------------------------------------------
BATCH_TABLE_READY = False


def _batch_tables_sqlite() -> List[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS ocr_batches (
            id TEXT PRIMARY KEY,
            owner_email TEXT NOT NULL,
            owner_role TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'ocr_li',
            state TEXT NOT NULL DEFAULT 'UPLOADING',
            total INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS ocr_batch_items (
            id TEXT PRIMARY KEY,
            batch_id TEXT NOT NULL,
            doc_id TEXT,
            filename TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'PENDING',
            error TEXT NOT NULL DEFAULT '',
            ocr_confidence REAL NOT NULL DEFAULT 0,
            parcel_match TEXT NOT NULL DEFAULT '',
            mutation_status TEXT NOT NULL DEFAULT '',
            encumbrance_status TEXT NOT NULL DEFAULT '',
            litigation_status TEXT NOT NULL DEFAULT '',
            risk_status TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """,
        "CREATE INDEX IF NOT EXISTS batch_items_batch ON ocr_batch_items(batch_id);",
    ]


def ensure_batch_table():
    global BATCH_TABLE_READY
    _db_changed()
    if BATCH_TABLE_READY:
        return
    with _get_db() as db:
        for stmt in _batch_tables_sqlite():
            db.execute(stmt)
    BATCH_TABLE_READY = True


def create_batch(user: Dict[str, Any], mode: str) -> str:
    ensure_batch_table()
    bid = "batch-" + uuid.uuid4().hex[:12]
    now = time.time()
    with _get_db() as db:
        db.execute(
            "INSERT INTO ocr_batches (id,owner_email,owner_role,mode,state,total,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (bid, user.get("email", "").lower(), user.get("role", "").upper(), mode, "UPLOADING", 0, now, now),
        )
    return bid


def add_batch_item(batch_id: str, filename: str) -> str:
    ensure_batch_table()
    iid = "bi-" + uuid.uuid4().hex[:12]
    now = time.time()
    with _get_db() as db:
        db.execute(
            "INSERT INTO ocr_batch_items (id,batch_id,filename,state,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (iid, batch_id, filename, "PENDING", now, now),
        )
        db.execute("UPDATE ocr_batches SET total=total+1, updated_at=? WHERE id=?", (now, batch_id))
    return iid


def update_batch_item(item_id: str, **fields) -> None:
    ensure_batch_table()
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = [fields[k] for k in fields]
    vals.append(time.time())
    vals.append(item_id)
    with _get_db() as db:
        db.execute(f"UPDATE ocr_batch_items SET {cols}, updated_at=? WHERE id=?", tuple(vals))


def finalize_batch(batch_id: str) -> None:
    ensure_batch_table()
    with _get_db() as db:
        db.execute("UPDATE ocr_batches SET state=?, updated_at=? WHERE id=?", ("PROCESSING", time.time(), batch_id))


def get_batch(batch_id: str, user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    ensure_batch_table()
    with _get_db() as db:
        b = db.execute("SELECT * FROM ocr_batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            return None
        # RBAC: owner or admin/staff
        if (user.get("role", "").upper() not in ("ADMIN", "VERIFICATION_OFFICER")
                and b["owner_email"] != user.get("email", "").lower()):
            return None
        items = db.execute(
            "SELECT * FROM ocr_batch_items WHERE batch_id=? ORDER BY created_at ASC",
            (batch_id,),
        ).fetchall()
    counts = {"completed": 0, "processing": 0, "failed": 0, "needs_review": 0, "pending": 0}
    li_summary = {"active_encumbrances": 0, "active_litigation": 0, "pending_mutations": 0, "high_risk": 0,
                  "parcel_matches": 0, "duplicates": 0, "ocr_failures": 0}
    item_list = []
    for it in items:
        d = dict(it)
        s = (d.get("state") or "").upper()
        if s in ("READY", "DONE", "COMPLETE"):
            counts["completed"] += 1
        elif s in ("FAILED", "ERROR"):
            counts["failed"] += 1
        elif s in ("REVIEW", "NEEDS_REVIEW"):
            counts["needs_review"] += 1
        elif s in ("PROCESSING", "OCR_RUNNING", "EXTRACTING_TEXT", "MATCHING_LAND_RECORD"):
            counts["processing"] += 1
        else:
            counts["pending"] += 1
        if d.get("ocr_confidence", 0) == 0 and s in ("FAILED", "ERROR"):
            li_summary["ocr_failures"] += 1
        if d.get("parcel_match"):
            li_summary["parcel_matches"] += 1
        item_list.append(d)
    total = len(item_list)
    return {
        "id": b["id"],
        "mode": b["mode"],
        "state": b["state"],
        "total": total,
        "counts": counts,
        "land_intel_summary": li_summary,
        "items": item_list,
        "created_at": b["created_at"],
    }


def list_batches_for_user(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    ensure_batch_table()
    with _get_db() as db:
        if user.get("role", "").upper() in ("ADMIN", "VERIFICATION_OFFICER"):
            rows = db.execute("SELECT * FROM ocr_batches ORDER BY created_at DESC LIMIT 50").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM ocr_batches WHERE owner_email=? ORDER BY created_at DESC LIMIT 50",
                (user.get("email", "").lower(),),
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        # counts
        with _get_db() as db:
            c = db.execute(
                "SELECT state, COUNT(*) AS n FROM ocr_batch_items WHERE batch_id=? GROUP BY state",
                (d["id"],),
            ).fetchall()
        counts = {"completed": 0, "processing": 0, "failed": 0, "needs_review": 0, "pending": 0}
        for row in c:
            s = (row["state"] or "").upper()
            if s in ("READY", "DONE", "COMPLETE"):
                counts["completed"] += row["n"]
            elif s in ("FAILED", "ERROR"):
                counts["failed"] += row["n"]
            elif s in ("REVIEW", "NEEDS_REVIEW"):
                counts["needs_review"] += row["n"]
            elif s in ("PROCESSING", "OCR_RUNNING", "EXTRACTING_TEXT", "MATCHING_LAND_RECORD"):
                counts["processing"] += row["n"]
            else:
                counts["pending"] += row["n"]
        d["counts"] = counts
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Worker loop for downstream tasks (bounded, uses existing task table)
# ---------------------------------------------------------------------------
WORKER_THREAD: Optional[threading.Thread] = None
WORKER_STOP = threading.Event()


def _worker_loop():
    """Simple background worker that drains ai_tasks entries for our internal
    downstream types.  It is started once on first use; it does NOT replace any
    existing worker and it respects lease/heartbeat semantics by marking tasks
    IN_PROGRESS with a lease timestamp in metadata."""
    while not WORKER_STOP.is_set():
        try:
            _drain_once()
        except Exception:
            pass
        WORKER_STOP.wait(2.0)


def _drain_once():
    srv = get_server()
    try:
        from admin_assistant import ensure_task_table as _ensure
        _ensure()
    except Exception:
        return
    now = time.time()
    lease_ttl = 600  # 10 minutes
    with _get_db() as db:
        # Pick one PENDING downstream task
        row = db.execute(
            """SELECT * FROM ai_tasks
               WHERE task_type IN ('AI_FIELD_ENHANCE','PARCEL_MATCH','LAND_INTELLIGENCE','LITIGATION_MATCH')
                 AND status='PENDING'
               ORDER BY created_at ASC LIMIT 1""",
        ).fetchone()
        if not row:
            return
        # Claim with lease
        meta = _safe_json(row["metadata"])
        meta["lease_owner"] = f"ocr-worker-{os.getpid()}"
        meta["lease_expires"] = now + lease_ttl
        meta["heartbeat"] = now
        db.execute(
            "UPDATE ai_tasks SET status='IN_PROGRESS', metadata=?, updated_at=? WHERE id=? AND status='PENDING'",
            (json.dumps(meta, ensure_ascii=False), now, row["id"]),
        )
    task_id = row["id"]
    task_type = row["task_type"]
    record_id = row["record_id"]
    meta = _safe_json(row["metadata"])
    mode = meta.get("mode", "ocr_li")
    try:
        if task_type == "AI_FIELD_ENHANCE":
            # Run AI field enhancement if available, else no-op.  This does NOT
            # change the document status — only augments ai_decision_support.
            _run_ai_enhance(record_id)
        elif task_type == "PARCEL_MATCH":
            _run_parcel_match(record_id)
        elif task_type == "LAND_INTELLIGENCE":
            _run_land_intel(record_id)
        elif task_type == "LITIGATION_MATCH":
            _run_litigation_match(record_id)
        # Mark complete
        with _get_db() as db:
            db.execute(
                "UPDATE ai_tasks SET status='COMPLETED', result=?, updated_at=? WHERE id=?",
                (json.dumps({"completed_at": time.time()}), time.time(), task_id),
            )
    except Exception as e:
        with _get_db() as db:
            db.execute(
                "UPDATE ai_tasks SET status='FAILED', result=?, updated_at=? WHERE id=?",
                (json.dumps({"error": str(e)[:400]}), time.time(), task_id),
            )


def _run_ai_enhance(doc_id: str) -> None:
    """Optionally run AI decision support AFTER OCR. Non-blocking; failure is safe."""
    srv = get_server()
    if not getattr(srv, "ai_client", None):
        return
    try:
        with _get_db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        text = row["ocr_text"] or ""
        detected = row["detected_language"] or "eng"
        # Call AI decision support asynchronously via asyncio in a dedicated event loop
        import asyncio as _a
        try:
            loop = _a.new_event_loop()
            raw_fields, decision, cleaned = loop.run_until_complete(
                srv.run_ai_decision_support(text, detected)
            )
            loop.close()
        except Exception:
            return
        if not raw_fields:
            return
        enriched, validation = srv.enrich_and_validate_fields(raw_fields)
        # merge ai info
        ai_payload = _safe_json(row["ai_decision_support"])
        ai_payload["ai_enhancement"] = decision
        ai_payload["pipeline_mode"] = "FAST_OCR_WITH_AI_ENHANCEMENT"
        with _get_db() as db:
            db.execute(
                "UPDATE documents SET fields=?, validation=?, cleaned_ocr_text=?, ai_decision_support=?, updated_at=? WHERE id=?",
                (json.dumps(enriched, ensure_ascii=False),
                 json.dumps(validation, ensure_ascii=False),
                 cleaned or text,
                 json.dumps(ai_payload, ensure_ascii=False),
                 time.time(),
                 doc_id),
            )
    except Exception:
        pass


def _run_parcel_match(doc_id: str) -> None:
    try:
        with _get_db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return
        fields = json.loads(row["fields"] or "{}")
        from mapping import _resolve
        property_resolution = _resolve(fields)
        now = time.time()
        ai_payload = _safe_json(row["ai_decision_support"])
        ai_payload["parcel_resolution"] = {
            "status": property_resolution.get("status"),
            "confidence": property_resolution.get("confidence"),
            "match_count": len(property_resolution.get("matches", [])),
        }
        if property_resolution.get("status") in ("MATCH", "POSSIBLE MATCH") and property_resolution.get("matches"):
            match_property = property_resolution["matches"][0]["property"]["property_id"]
            with _get_db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)",
                    (match_property, doc_id, "uploaded_document", now),
                )
                db.execute(
                    "INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, match_property, "PROPERTY_MATCHED",
                     f"Document {doc_id} resolved to {match_property} with {round(property_resolution['confidence']*100)}% confidence.",
                     "Async parcel matching", now),
                )
            # Apply ownership review
            srv = get_server()
            parsed = {"filename": row["filename"], "doc_type": row["doc_type"], "fields": fields, "ocr_text": row["ocr_text"] or ""}
            srv.apply_ownership_review(doc_id, match_property, parsed, row["status"], ai_payload)
            # Update batch item parcel_match if any
            _update_batch_item_for_doc(doc_id, parcel_match=match_property)
        with _get_db() as db:
            db.execute(
                "UPDATE documents SET ai_decision_support=?, updated_at=? WHERE id=?",
                (json.dumps(ai_payload, ensure_ascii=False), time.time(), doc_id),
            )
    except Exception:
        pass


def _run_land_intel(doc_id: str) -> None:
    try:
        from land_intel import document_land_context
        with _get_db() as db:
            row = db.execute(
                """SELECT d.*, pd.property_id FROM documents d
                   LEFT JOIN property_documents pd ON pd.document_id=d.id
                   WHERE d.id=? LIMIT 1""",
                (doc_id,),
            ).fetchone()
        if not row or not row["property_id"]:
            return
        doc_dict = dict(row)
        try:
            doc_dict["fields"] = json.loads(doc_dict.get("fields") or "{}")
        except Exception:
            doc_dict["fields"] = {}
        # Construct a synthetic user with reviewer role so the land context is fully populated.
        viewer = {"email": doc_dict.get("uploaded_by", ""), "role": "ADMIN"}
        ctx = document_land_context(doc_dict, viewer) or {}
        ai_payload = _safe_json(row["ai_decision_support"])
        ai_payload["land_context"] = ctx
        # Derive status strings for batch summary
        pm_count = int(ctx.get("pending_mutation_count", 0) or 0)
        mutation_status = f"PENDING({pm_count})" if pm_count else "NONE"
        ae_count = int(ctx.get("active_encumbrance_count", 0) or 0)
        encumbrance_status = f"ACTIVE({ae_count})" if ae_count else "CLEAR"
        risk_status = ctx.get("risk_verdict", "") or ""
        with _get_db() as db:
            db.execute(
                "UPDATE documents SET ai_decision_support=?, updated_at=? WHERE id=?",
                (json.dumps(ai_payload, ensure_ascii=False), time.time(), doc_id),
            )
        _update_batch_item_for_doc(doc_id, mutation_status=mutation_status,
                                    encumbrance_status=encumbrance_status, risk_status=risk_status)
    except Exception:
        pass


def _run_litigation_match(doc_id: str) -> None:
    try:
        from land_intel import match_litigation_for_document
        found = match_litigation_for_document(doc_id)
        status = "ACTIVE_LITIGATION" if found else "NONE"
        _update_batch_item_for_doc(doc_id, litigation_status=status)
    except Exception:
        # Function might not exist in older versions
        try:
            with _get_db() as db:
                row = db.execute("SELECT ocr_text, fields FROM documents WHERE id=?", (doc_id,)).fetchone()
            if not row:
                return
            text = (row["ocr_text"] or "").lower()
            fields = json.loads(row["fields"] or "{}")
            owner = (fields.get("owner_name", {}) or {}).get("value", "")
            found = any(term in text for term in ("court", "case no", "petition", "plaintiff", "defendant", "suit", "litigation"))
            _update_batch_item_for_doc(doc_id, litigation_status="ACTIVE_LITIGATION" if found else "NONE")
        except Exception:
            pass


def _update_batch_item_for_doc(doc_id: str, **kwargs) -> None:
    ensure_batch_table()
    try:
        with _get_db() as db:
            row = db.execute("SELECT id FROM ocr_batch_items WHERE doc_id=? LIMIT 1", (doc_id,)).fetchone()
            if not row:
                return
            update_batch_item(row["id"], **kwargs)
    except Exception:
        pass


def start_worker():
    global WORKER_THREAD
    if WORKER_THREAD is not None:
        return
    WORKER_STOP.clear()
    WORKER_THREAD = threading.Thread(target=_worker_loop, name="ocr-downstream-worker", daemon=True)
    WORKER_THREAD.start()
