import os
import io
import time
import json
import sqlite3
import hashlib
import hmac
import base64
import uuid
import re
import asyncio
import inspect
import secrets
import unicodedata
from datetime import datetime, date
from typing import Optional, Dict, Any, List, Tuple
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Query, Request, Depends, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Limit CPU threads
os.environ["OMP_THREAD_LIMIT"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"

# PDF Engine
try:
    import pypdfium2 as pdfium
    HAS_PDFIUM = True
except ImportError:
    HAS_PDFIUM = False

# OCR Engine
try:
    import pytesseract
    tesseract_cmd = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if os.path.isfile(tesseract_cmd):
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

# AI Engine Setup
try:
    from google import genai
    from google.genai import types
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
except ImportError:
    ai_client = None

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

HAS_PSYCOPG2 = False
if DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://"):
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        HAS_PSYCOPG2 = True
    except ImportError:
        HAS_PSYCOPG2 = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
SQLITE_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "land_records.db"))
# Backward-compatible alias used by the existing test suite and local tooling.
DB_PATH = SQLITE_PATH

APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV in {"production", "prod"}
JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
if IS_PRODUCTION and not JWT_SECRET:
    raise RuntimeError("JWT_SECRET must be configured in production.")
if not JWT_SECRET:
    JWT_SECRET = secrets.token_urlsafe(48)

ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv("ALLOWED_ORIGINS", "").split(",") if origin.strip()]
if IS_PRODUCTION and not ALLOWED_ORIGINS:
    raise RuntimeError("ALLOWED_ORIGINS must be configured in production.")
if not ALLOWED_ORIGINS:
    ALLOWED_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8000", "http://127.0.0.1:8000"]

PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)

def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)

def verify_password(stored_hash: str, password: str) -> tuple[bool, bool]:
    if not stored_hash:
        return False, False
    if stored_hash.startswith("$argon2"):
        try:
            return PASSWORD_HASHER.verify(stored_hash, password), False
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False, False
    if re.fullmatch(r"[0-9a-fA-F]{64}", stored_hash):
        legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(stored_hash, legacy), True
    return False, False

def needs_password_rehash(stored_hash: str) -> bool:
    if not stored_hash.startswith("$argon2"):
        return True
    try:
        return PASSWORD_HASHER.check_needs_rehash(stored_hash)
    except (VerificationError, InvalidHashError):
        return True

ROLE_VIEWER = "VIEWER"
ROLE_DATA_OFFICER = "DATA_OFFICER"
ROLE_VERIFICATION_OFFICER = "VERIFICATION_OFFICER"
ROLE_ADMIN = "ADMIN"

VALID_ROLES = {ROLE_VIEWER, ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN}

ROLE_ALIASES = {
    "viewer": ROLE_VIEWER,
    "operator": ROLE_DATA_OFFICER,
    "data_officer": ROLE_DATA_OFFICER,
    "verifier": ROLE_VERIFICATION_OFFICER,
    "verification_officer": ROLE_VERIFICATION_OFFICER,
    "admin": ROLE_ADMIN
}

def normalize_role(role_raw: str) -> str:
    cleaned = (role_raw or "").strip().lower()
    return ROLE_ALIASES.get(cleaned, ROLE_VIEWER)

STATUS_DRAFT = "DRAFT"
STATUS_PROCESSING = "PROCESSING"
STATUS_PENDING_VERIFICATION = "PENDING_VERIFICATION"
STATUS_APPROVED = "APPROVED"
STATUS_RETURNED = "RETURNED_TO_DATA_OFFICER"
STATUS_REJECTED = "REJECTED"

VALID_STATUSES = {
    STATUS_DRAFT, STATUS_PROCESSING, STATUS_PENDING_VERIFICATION,
    STATUS_APPROVED, STATUS_RETURNED, STATUS_REJECTED
}

SUPPORTED_LANGUAGES = [
    {"code": "eng", "name": "English"},
    {"code": "hin", "name": "Hindi"},
    {"code": "tel", "name": "Telugu"},
    {"code": "tam", "name": "Tamil"},
    {"code": "ben", "name": "Bengali"},
    {"code": "mar", "name": "Marathi"},
    {"code": "guj", "name": "Gujarati"},
    {"code": "pan", "name": "Punjabi"},
    {"code": "kan", "name": "Kannada"},
    {"code": "ori", "name": "Odia"},
    {"code": "urd", "name": "Urdu"}
]

app = FastAPI(
    title="DILRMP Land Record Digitization & Validation System",
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    print(f"[UNHANDLED ERROR] {type(exc).__name__}")
    return JSONResponse(status_code=500, content={"detail": "An unexpected server error occurred."})


class DBConnection:
    def __init__(self):
        self.is_pg = False
        self.conn = None

        if HAS_PSYCOPG2 and DATABASE_URL:
            try:
                self.conn = psycopg2.connect(
                    DATABASE_URL,
                    cursor_factory=RealDictCursor,
                    connect_timeout=3
                )
                self.is_pg = True
            except Exception as e:
                print(f"[POSTGRES CONNECT WARNING] {e}. Falling back to SQLite.")
                self.is_pg = False
                self.conn = None

        if not self.is_pg:
            self.conn = sqlite3.connect(DB_PATH, timeout=10.0, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            self.conn.close()

    def execute(self, query: str, params: tuple = ()):
        cur = self.conn.cursor()
        if self.is_pg:
            query = query.replace("?", "%s")
        cur.execute(query, params)
        return cur

def get_db():
    return DBConnection()

def init_db():
    with get_db() as db:
        corrections_sql = (
            """
            CREATE TABLE IF NOT EXISTS corrections (
                id SERIAL PRIMARY KEY,
                field_id TEXT NOT NULL,
                wrong TEXT NOT NULL,
                right_val TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(field_id, wrong, right_val)
            );
            """ if db.is_pg else """
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                field_id TEXT NOT NULL,
                wrong TEXT NOT NULL,
                right_val TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(field_id, wrong, right_val)
            );
            """
        )

        audit_sql = (
            """
            CREATE TABLE IF NOT EXISTS audit (
                id SERIAL PRIMARY KEY,
                ts REAL NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                doc_id TEXT
            );
            """ if db.is_pg else """
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                doc_id TEXT
            );
            """
        )

        statements = [
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'DATA_OFFICER',
                version INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                doc_type TEXT NOT NULL DEFAULT 'Land Record',
                mean_conf INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'DRAFT',
                languages TEXT NOT NULL,
                pages INTEGER NOT NULL,
                fields TEXT NOT NULL,
                validation TEXT NOT NULL,
                ai_decision_support TEXT NOT NULL DEFAULT '{}',
                ocr_text TEXT NOT NULL,
                cleaned_ocr_text TEXT NOT NULL DEFAULT '',
                detected_language TEXT NOT NULL DEFAULT 'unknown',
                original_fields TEXT NOT NULL DEFAULT '{}',
                uploaded_by TEXT NOT NULL DEFAULT 'SYSTEM',
                reviewer_comments TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL DEFAULT 0
            );
            """,
            corrections_sql,
            audit_sql,
            """
            CREATE TABLE IF NOT EXISTS comparisons (
                id TEXT PRIMARY KEY,
                doc_a_id TEXT NOT NULL,
                doc_b_id TEXT NOT NULL,
                officer_name TEXT NOT NULL,
                officer_email TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT 'pending',
                officer_notes TEXT NOT NULL DEFAULT '',
                diff_payload TEXT NOT NULL,
                ai_summary TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS consistency_checks (
                id TEXT PRIMARY KEY,
                document_ids TEXT NOT NULL,
                overall_status TEXT NOT NULL,
                matched_count INTEGER NOT NULL,
                mismatched_count INTEGER NOT NULL,
                missing_count INTEGER NOT NULL,
                uncertain_count INTEGER NOT NULL,
                report_payload TEXT NOT NULL,
                ai_explanation TEXT NOT NULL,
                officer_name TEXT NOT NULL,
                officer_email TEXT NOT NULL,
                decision TEXT NOT NULL DEFAULT 'flagged_for_verification',
                officer_notes TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL
            );
            """
        ]

        for stmt in statements:
            try:
                if db.is_pg:
                    db.execute("SAVEPOINT stmt_sp;")
                db.execute(stmt)
                if db.is_pg:
                    db.execute("RELEASE SAVEPOINT stmt_sp;")
            except Exception as e:
                if db.is_pg:
                    try:
                        db.execute("ROLLBACK TO SAVEPOINT stmt_sp;")
                    except Exception:
                        pass
                print(f"[TABLE INIT WARNING] {e}")

        try:
            if db.is_pg:
                db.execute("SAVEPOINT admin_sp;")
            cur = db.execute("SELECT id, role FROM users WHERE LOWER(email)='admin@landrec.gov.in'")
            row = cur.fetchone()
            initial_admin_password = os.getenv("ADMIN_INITIAL_PASSWORD", "").strip()
            if not row and initial_admin_password:
                admin_id = uuid.uuid4().hex[:12]
                h = hash_password(initial_admin_password)
                db.execute(
                    "INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)",
                    (admin_id, "System Administrator", "admin@landrec.gov.in", h, ROLE_ADMIN)
                )
                db.execute(
                    "INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                    (time.time(), "SYSTEM", "INIT", "Initial administrator account provisioned from deployment configuration", None)
                )
            if db.is_pg:
                db.execute("RELEASE SAVEPOINT admin_sp;")
        except Exception as e:
            if db.is_pg:
                try:
                    db.execute("ROLLBACK TO SAVEPOINT admin_sp;")
                except Exception:
                    pass
            print(f"[INIT ADMIN ERROR] {e}")

init_db()

def log_audit(username: str, action: str, detail: str, doc_id: Optional[str] = None):
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                (time.time(), username or "System", action, detail, doc_id)
            )
    except Exception as e:
        print(f"[AUDIT LOG ERROR] {e}")

# Auth Helpers
def b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def b64_decode(data: str) -> bytes:
    pad = 4 - (len(data) % 4)
    if pad != 4:
        data += "=" * pad
    return base64.urlsafe_b64decode(data.encode())

def create_jwt_token(uid: str, role: str, version: int = 0) -> str:
    payload = {"uid": uid, "role": role, "ver": version, "exp": int(time.time()) + 86400 * 14}
    payload_b64 = b64_encode(json.dumps(payload, separators=(',', ':')).encode())
    sig = hmac.new(JWT_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    return f"{payload_b64}.{b64_encode(sig)}"

def verify_jwt_token(token: str) -> Dict[str, Any]:
    if not token or "." not in token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token structure")
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(JWT_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(b64_encode(expected_sig), sig_b64):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")
        payload = json.loads(b64_decode(payload_b64).decode())
        if payload.get("exp", 0) < time.time():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
        return payload
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")

def get_current_user(request: Request, authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    cookie_token = request.cookies.get("lr_session")
    jwt_token = cookie_token or (authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else "")
    if not jwt_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    
    payload = verify_jwt_token(jwt_token)
    with get_db() as db:
        cur = db.execute("SELECT id, full_name, email, role, version, is_active FROM users WHERE id=?", (payload.get("uid"),))
        user = cur.fetchone()
        if not user or not user["is_active"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User account is inactive or not found")
        if user["version"] != payload.get("ver", 0):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked. Please log in again.")
        user_dict = dict(user)
        user_dict["role"] = normalize_role(user_dict.get("role", ROLE_VIEWER))
        return user_dict

def require_roles(*allowed_roles: str):
    normalized_allowed = {normalize_role(r) for r in allowed_roles}
    def role_checker(user: dict = Depends(get_current_user)) -> dict:
        user_role = user.get("role", ROLE_VIEWER)
        if user_role not in normalized_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied: Requires one of roles {list(normalized_allowed)}. Your role is '{user_role}'."
            )
        return user
    return role_checker

# OCR Engine & Validation
FIELD_KEYS = (
    "owner_name", "father_name", "survey_number", "khasra_number",
    "khata_number", "plot_number", "area", "village", "tehsil",
    "district", "state", "document_date", "land_class", "ownership_type",
    "mutation_no", "registration_no", "khatauni_year"
)

def clean_ocr_image(image: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(image).convert("L")
    if img.width < 1200:
        scale = 1500.0 / float(max(1, img.width))
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
    elif img.width > 2200:
        scale = 1800.0 / float(img.width)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.BILINEAR)

    img = ImageOps.autocontrast(img, cutoff=0.5)
    enhancer = ImageEnhance.Sharpness(img)
    return enhancer.enhance(1.5)

def detect_primary_script(text: str) -> str:
    counts = {
        "hin": sum(1 for c in text if 0x0900 <= ord(c) <= 0x097F),
        "tel": sum(1 for c in text if 0x0C00 <= ord(c) <= 0x0C7F),
        "tam": sum(1 for c in text if 0x0B80 <= ord(c) <= 0x0BFF),
        "ben": sum(1 for c in text if 0x0980 <= ord(c) <= 0x09FF)
    }
    if not counts or max(counts.values()) == 0:
        return "eng"
    return max(counts, key=counts.get)

def _rotate_image(image: Image.Image, rotation: int) -> Image.Image:
    rotation = int(rotation or 0) % 360
    if rotation == 90:
        return image.rotate(90, expand=True)
    if rotation == 180:
        return image.rotate(180, expand=True)
    if rotation == 270:
        return image.rotate(270, expand=True)
    return image


def _ocr_languages(preferred: str = "auto") -> List[str]:
    """Return a small, safe language list for the installed Tesseract setup."""
    requested = (preferred or "auto").lower().strip()
    if requested in {"eng", "hin", "tel", "tam", "ben", "mar", "guj", "pan", "kan", "ori", "urd"}:
        return [requested]
    # Keep the existing multilingual behaviour as the first choice.
    return ["hin+eng+tel+tam", "eng"]


def run_targeted_ocr(image: Image.Image, lang_code: str = "auto", strategy: Optional[Dict[str, Any]] = None) -> tuple[str, str]:
    """Backward-compatible OCR helper used by comparison/text-only paths."""
    result = run_guided_ocr(image, strategy or {"lang": _ocr_languages(lang_code)[0], "psm": 3, "rotation": 0, "enhance": True})
    return result["text"], result["detected_language"]


def run_guided_ocr(image: Image.Image, strategy: Dict[str, Any]) -> Dict[str, Any]:
    """Run Tesseract with AI-selected settings and return genuine word confidence."""
    img = ImageOps.exif_transpose(image).convert("L")
    img = _rotate_image(img, strategy.get("rotation", 0))

    if img.width < 1200:
        scale = 1500.0 / float(max(1, img.width))
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
    elif img.width > 2600:
        scale = 2200.0 / float(img.width)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)

    if strategy.get("enhance", True):
        img = ImageOps.autocontrast(img, cutoff=0.5)
        img = ImageEnhance.Contrast(img).enhance(1.15)
        img = ImageEnhance.Sharpness(img).enhance(1.35)
    if strategy.get("denoise", False):
        img = img.filter(ImageFilter.MedianFilter(size=3))

    psm = int(strategy.get("psm", 3) or 3)
    lang_candidates = strategy.get("lang_candidates") or [strategy.get("lang", "hin+eng+tel+tam"), "eng"]

    data = None
    selected_lang = "eng"
    for candidate in lang_candidates:
        try:
            if not HAS_TESSERACT:
                break
            data = pytesseract.image_to_data(
                img,
                lang=candidate,
                config=f"--oem 3 --psm {psm}",
                output_type=pytesseract.Output.DICT,
            )
            selected_lang = candidate
            break
        except Exception:
            continue

    if data is None:
        return {"text": "", "confidence": 0.0, "tokens": [], "detected_language": "English"}

    words: List[str] = []
    confs: List[float] = []
    tokens: List[Dict[str, Any]] = []
    for i, raw_word in enumerate(data.get("text", [])):
        word = str(raw_word or "").strip()
        try:
            raw_conf = float(data.get("conf", ["-1"])[i])
        except Exception:
            raw_conf = -1.0
        if not word or raw_conf < 0:
            continue
        confidence = round(max(0.0, min(1.0, raw_conf / 100.0)), 3)
        words.append(word)
        confs.append(confidence)
        tokens.append({
            "text": word,
            "confidence": confidence,
            "bbox": [
                int(data.get("left", [0])[i]),
                int(data.get("top", [0])[i]),
                int(data.get("width", [0])[i]),
                int(data.get("height", [0])[i]),
            ],
        })

    text = " ".join(words)
    avg_conf = float(sum(confs) / len(confs)) if confs else 0.0
    detected = detect_primary_script(text)
    return {
        "text": text,
        "confidence": round(avg_conf, 3),
        "tokens": tokens,
        "detected_language": detected,
        "tesseract_language": selected_lang,
        "word_count": len(words),
    }

def validate_single_field(field_name: str, value: str, confidence: float) -> Tuple[str, str]:
    val = (value or "").strip()
    if not val or val == "—":
        if field_name in ("owner_name", "khasra_number", "survey_number", "village", "district"):
            return "MISSING", f"Required field '{field_name}' is missing."
        return "MISSING", "Field is empty."
    if field_name == "area":
        if not re.search(r"\d", val):
            return "INVALID", "Missing numerical area magnitude."
        return "VALID", "Area specification valid."
    if confidence < 0.65:
        return "WARNING", "Low confidence score."
    return "VALID", "Passed validation rule."

def enrich_and_validate_fields(raw_fields: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    enriched_fields = {}
    issues = []
    has_invalid = False
    has_warning = False

    for key in FIELD_KEYS:
        f_obj = raw_fields.get(key, {})
        val = str(f_obj.get("value", "") or "").strip()
        conf = float(f_obj.get("confidence", 0.0))
        v_status, v_msg = validate_single_field(key, val, conf)

        if v_status == "INVALID":
            has_invalid = True
            issues.append({"severity": "error", "field": key, "msg": f"{key}: {v_msg}"})
        elif v_status in ("WARNING", "MISSING"):
            has_warning = True
            issues.append({"severity": "warning", "field": key, "msg": f"{key}: {v_msg}"})

        enriched_fields[key] = {
            "value": val,
            "confidence": conf,
            "validation_status": v_status,
            "validation_message": v_msg
        }

    if "document_type" in raw_fields:
        enriched_fields["document_type"] = raw_fields["document_type"]

    verdict = "rejected" if has_invalid else ("review" if has_warning else "valid")
    validation_report = {
        "verdict": verdict,
        "issues": issues,
        "summary": {
            "valid": sum(1 for f in enriched_fields.values() if isinstance(f, dict) and f.get("validation_status") == "VALID"),
            "warning": sum(1 for f in enriched_fields.values() if isinstance(f, dict) and f.get("validation_status") == "WARNING"),
            "invalid": sum(1 for f in enriched_fields.values() if isinstance(f, dict) and f.get("validation_status") == "INVALID"),
            "missing": sum(1 for f in enriched_fields.values() if isinstance(f, dict) and f.get("validation_status") == "MISSING")
        }
    }
    return enriched_fields, validation_report

# AI Decision Support Schema
class AIFieldAnalysisItem(BaseModel):
    field: str
    extracted_value: str
    interpretation: str
    requires_human_review: bool = False

class AIDecisionSupportSchema(BaseModel):
    document_classification: str = Field(default="Land Record")
    cleaned_ocr_summary: str = Field(default="")
    summary: str = Field(default="")
    flags: List[str] = Field(default=[])
    field_analysis: List[AIFieldAnalysisItem] = Field(default=[])
    recommendation: str = Field(default="REVIEW_REQUIRED")
    explanation: str = Field(default="")

async def run_ai_decision_support(raw_ocr_text: str, detected_lang: str) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    if not ai_client or not raw_ocr_text:
        return {}, {}, raw_ocr_text

    prompt = f"""
    You are an AI Decision Support Assistant for Land Record Verification Officers (DILRMP).
    Analyze this OCR text detected in {detected_lang}:
    \"\"\"{raw_ocr_text}\"\"\"
    """
    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIDecisionSupportSchema,
                temperature=0.0
            )
        )
        parsed = json.loads(response.text)
        raw_fields = {}
        for item in parsed.get("field_analysis", []):
            f_key = item.get("field", "")
            f_val = item.get("extracted_value", "").strip()
            if f_key in FIELD_KEYS:
                raw_fields[f_key] = {"value": f_val, "confidence": 0.90}
        for k in FIELD_KEYS:
            if k not in raw_fields:
                raw_fields[k] = {"value": "", "confidence": 0.0}

        doc_cls = parsed.get("document_classification", "Land Record")
        raw_fields["document_type"] = {"value": doc_cls, "confidence": 0.95}
        return raw_fields, parsed, parsed.get("cleaned_ocr_summary", raw_ocr_text)
    except Exception:
        return {}, {}, raw_ocr_text

async def parse_document_content(text: str, detected_lang: str, pages: int = 1) -> Dict[str, Any]:
    raw_fields, decision_support, cleaned_ocr = await run_ai_decision_support(text, detected_lang)
    if not raw_fields:
        raw_fields = {k: {"value": "", "confidence": 0.0} for k in FIELD_KEYS}
        raw_fields["document_type"] = {"value": "Land Record", "confidence": 0.8}
        cleaned_ocr = text
        decision_support = {"summary": "Extracted via fallback mode.", "recommendation": "REVIEW_REQUIRED", "explanation": "Manual check required."}

    enriched, validation = enrich_and_validate_fields(raw_fields)
    return {
        "mean_conf": 90,
        "languages": ["English", detected_lang],
        "pages": pages,
        "detected_language": detected_lang,
        "doc_type": raw_fields.get("document_type", {}).get("value", "Land Record"),
        "fields": enriched,
        "validation": validation,
        "ai_decision_support": decision_support,
        "ocr_text": text,
        "cleaned_ocr_text": cleaned_ocr
    }

# ---------------------------------------------------------------------
# AI-assisted OCR pipeline
# ---------------------------------------------------------------------
OCR_HIGH_CONFIDENCE_THRESHOLD = 0.75
OCR_LOW_CONFIDENCE_THRESHOLD = 0.45
AI_CORRECTION_CONFIDENCE_THRESHOLD = 0.85
MAX_AI_ATTEMPTS = 3


def _image_bytes_for_ai(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def _clean_json(text: str) -> str:
    text = (text or "").strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    return text


def ai_prescan_document(image_bytes: bytes) -> Dict[str, Any]:
    """Let Gemini choose OCR strategy; deterministic defaults remain authoritative."""
    result = {
        "document_type": "Land Record",
        "language": "Mixed",
        "script": "Mixed",
        "quality": "medium",
        "layout": "mixed",
        "rotation": 0,
        "is_handwritten": False,
    }
    if not ai_client or not image_bytes:
        return result

    prompt = """Inspect this land-record document image and return STRICT JSON ONLY with:
{
  "document_type": "Land Record/Sale Deed/Khatauni/Patta/Jamabandi/Other",
  "language": "English/Hindi/Telugu/Tamil/Bengali/Marathi/Gujarati/Punjabi/Kannada/Odia/Urdu/Mixed",
  "script": "Latin/Devanagari/Telugu/Tamil/Bengali/Gujarati/Gurmukhi/Kannada/Odia/Urdu/Mixed",
  "quality": "high/medium/low",
  "layout": "table/dense_text/mixed",
  "rotation": 0,
  "is_handwritten": false
}
Rules: rotation must be 0, 90, 180 or 270. Do not guess unreadable content."""
    try:
        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        parsed = json.loads(_clean_json(response.text))
        if isinstance(parsed, dict):
            result.update(parsed)
    except Exception as exc:
        result["error"] = str(exc)[:300]
    return result


def select_ai_ocr_strategy(prescan: Dict[str, Any], requested_lang: str = "auto") -> Dict[str, Any]:
    language = str(prescan.get("language", "Mixed")).lower()
    script = str(prescan.get("script", "Mixed")).lower()
    layout = str(prescan.get("layout", "mixed")).lower()
    quality = str(prescan.get("quality", "medium")).lower()

    if requested_lang and requested_lang != "auto":
        candidates = _ocr_languages(requested_lang)
    elif "hindi" in language or "devanagari" in script:
        candidates = ["hin+eng", "eng"]
    elif "telugu" in language or "telugu" in script:
        candidates = ["tel+eng", "eng"]
    elif "tamil" in language or "tamil" in script:
        candidates = ["tam+eng", "eng"]
    elif "mixed" in language or "mixed" in script:
        candidates = ["hin+eng+tel+tam", "eng"]
    else:
        candidates = ["eng"]

    return {
        "lang": candidates[0],
        "lang_candidates": candidates,
        "psm": 6 if layout == "table" else (11 if layout == "dense_text" else 3),
        "rotation": int(prescan.get("rotation", 0) or 0) if str(prescan.get("rotation", 0)).isdigit() else 0,
        "enhance": quality != "high",
        "denoise": quality == "low" or bool(prescan.get("is_handwritten", False)),
    }


def evaluate_ai_ocr_quality(ocr_result: Dict[str, Any], extracted_fields: Dict[str, Any]) -> str:
    text = str(ocr_result.get("text") or "").strip()
    confidence = float(ocr_result.get("confidence", 0.0) or 0.0)
    missing = [
        key for key in ("owner_name", "survey_number", "village", "area")
        if not (extracted_fields.get(key, {}) or {}).get("value")
    ]
    if len(text) < 25 or confidence < 0.30:
        return "FAILED"
    if confidence >= OCR_HIGH_CONFIDENCE_THRESHOLD and not missing:
        return "GOOD"
    return "UNCERTAIN"


def ai_verify_ocr_fields(image_bytes: bytes, ocr_fields: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    """Visually verify OCR fields and only auto-correct high-confidence differences."""
    updated = json.loads(json.dumps(ocr_fields))
    corrections: List[Dict[str, Any]] = []
    report = {"verified": [], "uncertain": [], "corrections": []}
    if not ai_client or not image_bytes:
        return updated, corrections, report

    candidate = {k: (v.get("value", "") if isinstance(v, dict) else v) for k, v in ocr_fields.items()}
    prompt = f"""You are assisting a government land-record Verification Officer.
Visually inspect the attached document and verify these OCR candidate fields:
{json.dumps(candidate, ensure_ascii=False, indent=2)}

Return STRICT JSON ARRAY ONLY. Each item:
{{"field":"survey_number","ocr_value":"128","ai_value":"182","decision":"VERIFIED|CORRECT|UNCERTAIN","confidence":0.0,"reason":"..."}}
Rules:
- CORRECT only when the image clearly supports the corrected value.
- UNCERTAIN when text is blurred, obstructed or ambiguous.
- Never invent a value and never declare fraud.
- Keep the original OCR value in ocr_value."""
    try:
        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        items = json.loads(_clean_json(response.text))
        if not isinstance(items, list):
            return updated, corrections, report
        for item in items:
            field = item.get("field")
            decision = str(item.get("decision", "UNCERTAIN")).upper()
            if field not in FIELD_KEYS:
                continue
            try:
                confidence = float(item.get("confidence", 0.0))
            except Exception:
                confidence = 0.0
            ocr_value = str(item.get("ocr_value") or (candidate.get(field) or ""))
            ai_value = str(item.get("ai_value") or "").strip()
            reason = str(item.get("reason") or "Visual verification result")
            if decision == "CORRECT" and ai_value and confidence >= AI_CORRECTION_CONFIDENCE_THRESHOLD:
                old = updated.get(field, {}) if isinstance(updated.get(field), dict) else {"value": updated.get(field, "")}
                updated[field] = {
                    "value": ai_value,
                    "confidence": round(confidence, 3),
                    "validation_status": "VALID",
                    "validation_message": "Corrected by AI after visual verification."
                }
                corr = {
                    "field": field,
                    "original_value": str(old.get("value") or ocr_value),
                    "corrected_value": ai_value,
                    "confidence": round(confidence, 3),
                    "reason": reason,
                    "method": "AI_VISUAL_VERIFICATION",
                }
                corrections.append(corr)
                report["corrections"].append(corr)
            elif decision == "VERIFIED":
                report["verified"].append({"field": field, "confidence": round(confidence, 3), "reason": reason})
            else:
                report["uncertain"].append({"field": field, "confidence": round(confidence, 3), "reason": reason})
    except Exception as exc:
        report["error"] = str(exc)[:300]
    return updated, corrections, report


def direct_ai_extract(image_bytes: bytes, attempt: int, unresolved_fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """Three deliberately different extraction attempts; null is preferred over guessing."""
    if not ai_client or not image_bytes:
        return {}
    all_fields = list(FIELD_KEYS)
    if attempt == 1:
        focus = "Read the document normally and map visible labels to values."
    elif attempt == 2:
        focus = "Use label-anchored extraction. Pay special attention to Hindi/Indic labels, table rows and the value immediately beside or below each label."
    else:
        targets = unresolved_fields or ["owner_name", "survey_number", "village", "area"]
        focus = f"Perform a targeted retry only for these unresolved fields: {json.dumps(targets)}. Inspect nearby table cells, stamps and handwritten marks carefully."
    schema = {field: None for field in all_fields}
    schema["document_type"] = None
    schema["_confidence"] = 0.0
    prompt = f"""Extract structured data from this official land-record image. {focus}
Return STRICT JSON only using this exact shape:
{json.dumps(schema, ensure_ascii=False)}
Rules:
- Return null when a value is genuinely unreadable or absent.
- Never guess, normalize away meaningful digits, or fabricate land numbers.
- Preserve names and numbers as they appear.
- _confidence is your confidence in the overall extraction, from 0 to 1."""
    try:
        response = ai_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"), prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0),
        )
        parsed = json.loads(_clean_json(response.text))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def reconcile_ai_attempts(attempts: List[Dict[str, Any]], fields: List[str]) -> Tuple[Dict[str, Any], Dict[str, float], List[str]]:
    final: Dict[str, Any] = {}
    confidence: Dict[str, float] = {}
    unresolved: List[str] = []
    for field in fields:
        values = []
        for attempt in attempts:
            value = attempt.get(field)
            if value is not None and str(value).strip() and str(value).strip().lower() != "null":
                values.append(str(value).strip())
        if not values:
            final[field] = ""
            confidence[field] = 0.0
            if field in ("owner_name", "survey_number", "village", "area"):
                unresolved.append(field)
            continue
        counts: Dict[str, int] = {}
        for value in values:
            counts[value] = counts.get(value, 0) + 1
        best, count = max(counts.items(), key=lambda x: x[1])
        final[field] = best
        confidence[field] = 0.95 if count >= 3 else (0.85 if count == 2 else 0.70)
        if count == 1 and field in ("owner_name", "survey_number", "village", "area"):
            unresolved.append(field)
    return final, confidence, unresolved


async def run_ai_assisted_pipeline(image: Image.Image, requested_lang: str = "auto") -> Dict[str, Any]:
    """UPLOAD -> AI pre-scan -> guided OCR -> AI verification/correction -> direct AI fallback."""
    image_bytes = _image_bytes_for_ai(image)
    prescan = await asyncio.to_thread(ai_prescan_document, image_bytes)
    strategy = select_ai_ocr_strategy(prescan, requested_lang)
    ocr_result = await asyncio.to_thread(run_guided_ocr, image, strategy)

    # Use the existing text-to-structure extractor first; it is kept for compatibility
    # with the current UI and validation model.
    initial_parsed = await parse_document_content(
        ocr_result.get("text", ""),
        ocr_result.get("detected_language", "eng"),
        pages=1,
    )
    initial_fields = initial_parsed.get("fields", {})
    # Field confidence here reflects the actual Tesseract document confidence;
    # Gemini visual verification can later raise confidence for a specific correction.
    for _field_name, _field_obj in initial_fields.items():
        if isinstance(_field_obj, dict) and _field_obj.get("value"):
            _field_obj["confidence"] = round(float(ocr_result.get("confidence", 0.0) or 0.0), 3)
    ocr_quality = evaluate_ai_ocr_quality(ocr_result, initial_fields)

    attempts: List[Dict[str, Any]] = [{
        "attempt": 1,
        "method": "AI_GUIDED_TESSERACT",
        "ocr_confidence": ocr_result.get("confidence", 0.0),
        "word_count": ocr_result.get("word_count", 0),
    }]
    corrections: List[Dict[str, Any]] = []
    verification_report: Dict[str, Any] = {}
    final_fields = initial_fields
    pipeline_mode = "AI_SUPERVISED_OCR"
    escalation = None

    # AI verifies even good/uncertain OCR. This is where confident OCR errors are fixed.
    if ocr_quality in ("GOOD", "UNCERTAIN") and ai_client:
        verified_fields, corrections, verification_report = await asyncio.to_thread(
            ai_verify_ocr_fields, image_bytes, initial_fields
        )
        final_fields = verified_fields
        for correction in corrections:
            attempts.append({
                "attempt": len(attempts) + 1,
                "method": "AI_VISUAL_VERIFICATION",
                "field": correction["field"],
                "original_value": correction["original_value"],
                "corrected_value": correction["corrected_value"],
                "confidence": correction["confidence"],
                "reason": correction["reason"],
            })

        # If OCR was only uncertain and visual verification could not establish
        # the mandatory fields, fall back to the three direct-AI attempts.
        unresolved_after_verify = [
            key for key in ("owner_name", "survey_number", "village", "area")
            if not (final_fields.get(key, {}) or {}).get("value")
        ]
        if ocr_quality == "UNCERTAIN" and unresolved_after_verify:
            direct_attempts: List[Dict[str, Any]] = []
            unresolved = unresolved_after_verify
            for attempt_no in range(1, MAX_AI_ATTEMPTS + 1):
                result = await asyncio.to_thread(direct_ai_extract, image_bytes, attempt_no, unresolved)
                direct_attempts.append(result)
                attempts.append({
                    "attempt": len(attempts) + 1,
                    "method": f"GEMINI_DIRECT_{attempt_no}",
                    "confidence": result.get("_confidence", 0.0) if isinstance(result, dict) else 0.0,
                })
                _, _, unresolved = reconcile_ai_attempts(direct_attempts, ("owner_name", "survey_number", "village", "area"))
                if not unresolved:
                    break
            if direct_attempts:
                reconciled, field_conf, unresolved_final = reconcile_ai_attempts(direct_attempts, list(FIELD_KEYS))
                for key in FIELD_KEYS:
                    if reconciled.get(key):
                        final_fields[key] = {
                            "value": reconciled[key],
                            "confidence": field_conf.get(key, 0.0),
                            "validation_status": "VALID",
                            "validation_message": "Resolved by AI direct fallback after uncertain OCR."
                        }
                if unresolved_final:
                    escalation = {
                        "reason": "OCR remained uncertain and AI could not reliably resolve all mandatory fields after three attempts.",
                        "unresolved_fields": unresolved_final,
                        "attempts": len(direct_attempts),
                        "route": "Verification Officer",
                    }
    else:
        pipeline_mode = "AI_DIRECT_EXTRACTION"
        direct_attempts: List[Dict[str, Any]] = []
        unresolved = [
            key for key in ("owner_name", "survey_number", "village", "area")
            if not (initial_fields.get(key, {}) or {}).get("value")
        ] or ["owner_name", "survey_number", "village", "area"]

        for attempt_no in range(1, MAX_AI_ATTEMPTS + 1):
            result = await asyncio.to_thread(direct_ai_extract, image_bytes, attempt_no, unresolved)
            direct_attempts.append(result)
            attempts.append({
                "attempt": attempt_no,
                "method": f"GEMINI_DIRECT_{attempt_no}",
                "confidence": result.get("_confidence", 0.0) if isinstance(result, dict) else 0.0,
            })
            _, _, unresolved = reconcile_ai_attempts(direct_attempts, ["owner_name", "survey_number", "village", "area"])
            if not unresolved:
                break

        if direct_attempts:
            reconciled, field_conf, unresolved_final = reconcile_ai_attempts(direct_attempts, list(FIELD_KEYS))
            final_fields = {
                key: {
                    "value": reconciled.get(key, ""),
                    "confidence": field_conf.get(key, 0.0),
                    "validation_status": "VALID" if reconciled.get(key) else "MISSING",
                    "validation_message": "Extracted by AI direct fallback." if reconciled.get(key) else "AI could not establish a reliable value."
                }
                for key in FIELD_KEYS
            }
            final_fields["document_type"] = {
                "value": next((a.get("document_type") for a in direct_attempts if a.get("document_type")), "Land Record"),
                "confidence": max([float(a.get("_confidence", 0.0) or 0.0) for a in direct_attempts] or [0.0]),
                "validation_status": "VALID",
                "validation_message": "Document type identified by AI direct fallback."
            }
            if unresolved_final:
                escalation = {
                    "reason": "AI could not reliably resolve all mandatory land-record fields after three extraction attempts.",
                    "unresolved_fields": unresolved_final,
                    "attempts": len(direct_attempts),
                    "route": "Verification Officer",
                }
        else:
            escalation = {
                "reason": "AI extraction was unavailable or failed.",
                "unresolved_fields": ["owner_name", "survey_number", "village", "area"],
                "attempts": MAX_AI_ATTEMPTS,
                "route": "Verification Officer",
            }

    # Re-run the existing deterministic validator on the final values.
    final_fields, validation = enrich_and_validate_fields(final_fields)
    unresolved_mandatory = [
        key for key in ("owner_name", "survey_number", "village", "area")
        if not (final_fields.get(key, {}) or {}).get("value")
    ]

    if unresolved_mandatory and not escalation:
        escalation = {
            "reason": "Mandatory fields remain unresolved after AI-assisted OCR.",
            "unresolved_fields": unresolved_mandatory,
            "attempts": len(attempts),
            "route": "Verification Officer",
        }

    return {
        "mean_conf": int(round(float(ocr_result.get("confidence", 0.0)) * 100)),
        "languages": ["English", ocr_result.get("detected_language", "eng")],
        "pages": 1,
        "detected_language": ocr_result.get("detected_language", "eng"),
        "doc_type": final_fields.get("document_type", {}).get("value", prescan.get("document_type", "Land Record")),
        "fields": final_fields,
        "validation": validation,
        "ai_decision_support": {
            **(initial_parsed.get("ai_decision_support") or {}),
            "pipeline_mode": pipeline_mode,
            "ai_prescan": prescan,
            "ocr_quality": ocr_quality,
            "ocr_confidence": ocr_result.get("confidence", 0.0),
            "ocr_word_count": ocr_result.get("word_count", 0),
            "ocr_tokens": ocr_result.get("tokens", []),
            "ocr_strategy": strategy,
            "ai_verification": verification_report,
            "ai_corrections": corrections,
            "attempts": attempts,
            "escalation_report": escalation,
            "recommendation": "REVIEW_REQUIRED" if escalation or validation.get("verdict") != "valid" else "READY_FOR_REVIEW",
            "explanation": "AI supervises OCR and may correct only high-confidence visual mismatches; unresolved records are routed to a Verification Officer."
        },
        "ocr_text": ocr_result.get("text", ""),
        "cleaned_ocr_text": initial_parsed.get("cleaned_ocr_text", ocr_result.get("text", "")),
        "original_fields": initial_fields,
        "pipeline_meta": {
            "mode": pipeline_mode,
            "prescan": prescan,
            "ocr_quality": ocr_quality,
            "ocr_confidence": ocr_result.get("confidence", 0.0),
            "strategy": strategy,
            "corrections": corrections,
            "attempts": attempts,
            "escalation_report": escalation,
        },
        "escalated": bool(escalation),
    }


INDIC_DIGIT_TRANSLATION = str.maketrans({
    "٠":"0","١":"1","٢":"2","٣":"3","٤":"4","٥":"5","٦":"6","٧":"7","٨":"8","٩":"9",
    "۰":"0","۱":"1","۲":"2","۳":"3","۴":"4","۵":"5","۶":"6","۷":"7","۸":"8","۹":"9",
    "०":"0","१":"1","२":"2","३":"3","४":"4","५":"5","६":"6","७":"7","८":"8","९":"9",
})

def normalize_field_val(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, dict):
        val = val.get("value", "")
    s = unicodedata.normalize("NFKC", str(val))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = s.translate(INDIC_DIGIT_TRANSLATION).strip().lower()
    s = re.sub(r"\s+", " ", s)
    # Preserve cadastral separators: 45/2 must never normalize to 452.
    s = re.sub(r"[\.,\-_]+", "", s)
    return s

def compute_document_diff(fields_a: Dict[str, Any], fields_b: Dict[str, Any]) -> Dict[str, Any]:
    unchanged = []
    changed = []

    target_fields = [
        ("owner_name", "Owner Name"),
        ("father_name", "Father/Husband Name"),
        ("survey_number", "Survey Number"),
        ("khasra_number", "Khasra Number"),
        ("khata_number", "Khata Number"),
        ("plot_number", "Plot Number"),
        ("area", "Land Area"),
        ("village", "Village"),
        ("tehsil", "Tehsil"),
        ("district", "District"),
        ("state", "State"),
        ("land_class", "Land Class"),
        ("ownership_type", "Ownership Type"),
        ("mutation_no", "Mutation Number"),
        ("registration_no", "Registration Number"),
        ("khatauni_year", "Khatauni Year")
    ]

    for key, label in target_fields:
        obj_a = fields_a.get(key, {}) if isinstance(fields_a, dict) else {}
        obj_b = fields_b.get(key, {}) if isinstance(fields_b, dict) else {}

        val_a = obj_a.get("value", "") if isinstance(obj_a, dict) else str(obj_a or "")
        val_b = obj_b.get("value", "") if isinstance(obj_b, dict) else str(obj_b or "")

        norm_a = normalize_field_val(val_a)
        norm_b = normalize_field_val(val_b)

        if not norm_a and not norm_b:
            continue

        if norm_a == norm_b:
            unchanged.append({
                "field": key,
                "label": label,
                "value": val_b or val_a,
                "confidence_a": obj_a.get("confidence", 1.0) if isinstance(obj_a, dict) else 1.0,
                "confidence_b": obj_b.get("confidence", 1.0) if isinstance(obj_b, dict) else 1.0
            })
        else:
            changed.append({
                "field": key,
                "label": label,
                "old_value": val_a if val_a else "— (Empty)",
                "new_value": val_b if val_b else "— (Empty)",
                "confidence_a": obj_a.get("confidence", 0.0) if isinstance(obj_a, dict) else 0.0,
                "confidence_b": obj_b.get("confidence", 0.0) if isinstance(obj_b, dict) else 0.0
            })

    return {
        "summary": {
            "total_checked": len(unchanged) + len(changed),
            "unchanged_count": len(unchanged),
            "changed_count": len(changed)
        },
        "unchanged": unchanged,
        "changed": changed
    }

def evaluate_cross_document_consistency(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not records or len(records) < 2:
        return {
            "overall_status": "INSUFFICIENT_DATA",
            "counts": {"matched": 0, "mismatched": 0, "missing": 0, "uncertain": 0, "total": 0},
            "fields": [],
            "ownership_reasoning": {"assessment": "INSUFFICIENT_DATA", "findings": [], "relationships": []},
        }

    keys_to_audit = [
        ("owner_name", "Landowner Name"),
        ("survey_number", "Survey Number"),
        ("khasra_number", "Khasra Number"),
        ("khata_number", "Khata Number"),
        ("area", "Land Area"),
        ("village", "Village"),
        ("tehsil", "Tehsil"),
        ("district", "District"),
        ("mutation_no", "Mutation Number"),
        ("registration_no", "Registration Number")
    ]

    field_audits = []
    counts = {"matched": 0, "mismatched": 0, "missing": 0, "uncertain": 0, "total": 0}

    for key, label in keys_to_audit:
        values_by_doc = []
        confs_by_doc = []
        all_missing = True
        for r in records:
            f = r.get("fields", {})
            f_obj = f.get(key, {})
            val = f_obj.get("value", "") if isinstance(f_obj, dict) else str(f_obj or "")
            conf = float(f_obj.get("confidence", 0.0)) if isinstance(f_obj, dict) else 1.0
            val_clean = str(val).strip()
            values_by_doc.append({"doc_id": r.get("id"), "filename": r.get("filename"), "value": val_clean})
            confs_by_doc.append(conf)
            if val_clean and val_clean != "—":
                all_missing = False

        counts["total"] += 1
        if all_missing:
            counts["missing"] += 1
            field_audits.append({"field":key,"label":label,"status":"MISSING","values":values_by_doc,"message":"Field is empty across all examined records."})
            continue

        normalized_present = [normalize_field_val(item["value"]) for item in values_by_doc if item["value"] and item["value"] != "—"]
        if any(c < 0.65 for c in confs_by_doc):
            counts["uncertain"] += 1
            field_audits.append({"field":key,"label":label,"status":"UNCERTAIN","values":values_by_doc,"message":"Low OCR confidence across records. Officer check advised."})
        elif len(set(normalized_present)) == 1:
            if len(normalized_present) == len(records):
                counts["matched"] += 1
                field_audits.append({"field":key,"label":label,"status":"MATCH","values":values_by_doc,"message":"Values match across all records."})
            else:
                counts["uncertain"] += 1
                field_audits.append({"field":key,"label":label,"status":"UNCERTAIN","values":values_by_doc,"message":"Present entries match, but field is missing in some records."})
        else:
            counts["mismatched"] += 1
            field_audits.append({"field":key,"label":label,"status":"MISMATCH","values":values_by_doc,"message":"Values do not match. Requires officer review."})

    from land_intelligence import analyze_ownership_history
    ownership_reasoning = analyze_ownership_history(records)

    # Ownership changes are not automatically conflicts. The deterministic ownership
    # layer classifies them using parcel identity, chronology, and transfer evidence.
    ownership_types = {f.get("type") for f in ownership_reasoning.get("findings", [])}
    if ownership_types & {"TRANSFER_SUPPORTED", "TRANSFER_CANDIDATE", "POSSIBLE_DUPLICATE_OCR_VARIATION"}:
        overall_status = "FLAGGED_FOR_REVIEW"
    elif "OWNERSHIP_CONFLICT" in ownership_types:
        overall_status = "MISMATCH_DETECTED"
    elif counts["mismatched"] > 0:
        overall_status = "MISMATCH_DETECTED"
    elif counts["uncertain"] > 0:
        overall_status = "FLAGGED_FOR_REVIEW"
    elif counts["matched"] > 0:
        overall_status = "CONSISTENT"
    else:
        overall_status = "INSUFFICIENT_DATA"

    return {
        "overall_status": overall_status,
        "counts": counts,
        "fields": field_audits,
        "ownership_reasoning": ownership_reasoning,
    }

async def generate_ai_diff_explanation(doc_a: Dict[str, Any], doc_b: Dict[str, Any], diff: Dict[str, Any]) -> str:
    changed = diff.get("changed", [])
    if not changed:
        return "No differences found between the compared document versions. All fields are identical."

    diff_summary = "\n".join([f"- {c['label']}: '{c['old_value']}' -> '{c['new_value']}'" for c in changed])

    if not ai_client:
        return (
            f"Comparison Notes: {len(changed)} difference(s) detected between Document #{doc_a.get('id')} and Document #{doc_b.get('id')}.\n\n"
            f"Fields requiring review:\n{diff_summary}\n\n"
            "Recommendation: Flag for verification officer review."
        )

    prompt = f"""
    You are an AI assistant helping a Verification Officer verify land records (DILRMP).
    Analyze these detected differences between two versions of a document:
    Document A (#{doc_a.get('id')} - {doc_a.get('filename')}):
    Document B (#{doc_b.get('id')} - {doc_b.get('filename')}):

    Detected Differences:
    {diff_summary}

    Guidelines:
    1. Clearly describe the differences in simple terms.
    2. Highlight fields that require the officer's attention.
    3. Use calm, neutral wording (e.g., 'requires verification', 'possible mismatch', 'review recommended').
    4. Never accuse anyone of fraud, and never make a final legal determination.
    5. Be concise and practical.
    """
    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2)
        )
        return response.text.strip()
    except Exception:
        return (
            f"Comparison Notes: {len(changed)} fields differ between versions.\n"
            f"Changed Fields: {', '.join(c['label'] for c in changed)}.\n"
            "Recommendation: Review flagged fields against original records."
        )

async def generate_ai_consistency_explanation(records: List[Dict[str, Any]], report: Dict[str, Any]) -> str:
    counts = report.get("counts", {})
    mismatches = [f for f in report.get("fields", []) if f.get("status") == "MISMATCH"]
    
    if not mismatches:
        return f"Consistency check complete across {len(records)} documents. No conflicting fields were found."

    mismatch_text = "\n".join([
        f"- {m['label']}: " + ", ".join([f"Doc #{v['doc_id']}: '{v['value']}'" for v in m.get("values", [])])
        for m in mismatches
    ])

    if not ai_client:
        return (
            f"Consistency check found {len(mismatches)} difference(s) across {len(records)} documents.\n"
            f"Differing Fields:\n{mismatch_text}\n\n"
            "Recommendation: Requires officer verification."
        )

    prompt = f"""
    You are an AI assistant helping a Verification Officer verify land records (DILRMP).
    Review this consistency check across {len(records)} records:
    Overall Status: {report.get('overall_status')}
    Summary Counts: {json.dumps(counts)}

    Differences Found:
    {mismatch_text}

    Guidelines:
    1. Briefly explain the conflicting fields in simple words.
    2. Note possible reasons such as updates, partition, or data-entry variance.
    3. Use neutral words like 'review recommended', 'possible mismatch', 'requires verification'.
    4. Never allege fraud or make a final binding approval or rejection.
    """
    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-3.6-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2)
        )
        return response.text.strip()
    except Exception:
        return (
            f"Consistency check noted differences in: {', '.join(m['label'] for m in mismatches)}.\n"
            "Recommendation: Verification officer should cross-check these fields with master registers."
        )

# Backward-compatible deterministic extraction and pipeline hook.
# The AI-assisted pipeline remains the production implementation; this wrapper keeps
# the document workflow testable and lets callers replace the OCR stage safely.
def _normalize_ocr_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def extract_fields_from_ocr(text: str, filename: str = "") -> Dict[str, Any]:
    text = _normalize_ocr_text(text)
    patterns = {
        "owner_name": r"(?:owner\s*name|landowner|owner|मालिक(?:\s*का)?\s*नाम|भूस्वामी)\s*[:\-]\s*([^\n]+)",
        "father_name": r"(?:father(?:'s)?\s*name|father|husband|पिता\s*का\s*नाम|पति\s*का\s*नाम)\s*[:\-]\s*([^\n]+)",
        "village": r"(?:village|ग्राम|गांव|गाँव)\s*[:\-]\s*([^\n]+)",
        "tehsil": r"(?:tehsil|taluka|तहसील|तालुका)\s*[:\-]\s*([^\n]+)",
        "district": r"(?:district|जिला)\s*[:\-]\s*([^\n]+)",
        "state": r"(?:state|राज्य)\s*[:\-]\s*([^\n]+)",
        "survey_number": r"(?:survey(?:\s*no\.?|\s*number)?|gat(?:\s*no\.?|\s*number)?|सर्वे\s*(?:नं\.?|नंबर)?|गट\s*(?:नं\.?|नंबर)?)\s*[:\-]\s*([^\n]+)",
        "khasra_number": r"(?:khasra\s*(?:no\.?|number)?|खसरा\s*(?:नं\.?|नंबर)?)\s*[:\-]\s*([^\n]+)",
        "khata_number": r"(?:khata\s*(?:no\.?|number)?|खाता\s*(?:नं\.?|नंबर)?)\s*[:\-]\s*([^\n]+)",
        "plot_number": r"(?:plot\s*(?:no\.?|number)?|प्लॉट\s*(?:नं\.?|नंबर)?)\s*[:\-]\s*([^\n]+)",
        "area": r"(?:area|land\s*area|क्षेत्रफल)\s*[:\-]\s*([^\n]+)",
        "document_date": r"(?:date|document\s*date|दिनांक|तारीख)\s*[:\-]\s*([^\n]+)",
        "khatauni_year": r"(?:khatauni\s*year|record\s*year|year|वर्ष)\s*[:\-]\s*([^\n]+)",
        "mutation_no": r"(?:mutation\s*(?:no\.?|number)?|नामांतरण\s*(?:नं\.?|नंबर)?)\s*[:\-]\s*([^\n]+)",
        "registration_no": r"(?:registration\s*(?:no\.?|number)?|पंजीकरण\s*(?:नं\.?|नंबर)?)\s*[:\-]\s*([^\n]+)",
    }
    fields = {k: {"value": "", "confidence": 0.0} for k in FIELD_KEYS}
    fields["document_type"] = {"value": "Land Record", "confidence": 0.8}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            value = re.sub(r"\s+", " ", m.group(1).strip()).strip(" .,")
            if key in fields:
                fields[key] = {"value": value, "confidence": 0.95}
            else:
                fields[key] = {"value": value, "confidence": 0.95}
    enriched, validation = enrich_and_validate_fields(fields)
    if not enriched.get("owner_name", {}).get("value") and filename:
        validation["issues"].append({
            "severity": "warning",
            "field": "owner_name",
            "msg": "Record-holder name was not extracted from document text; filename is not treated as identity."
        })
    return {
        "mean_conf": 95,
        "languages": ["English"],
        "pages": 1,
        "detected_language": "eng",
        "doc_type": "Land Record",
        "fields": enriched,
        "validation": validation,
        "ai_decision_support": {"pipeline_mode": "DETERMINISTIC_TESTABLE_OCR"},
        "ocr_text": text,
        "cleaned_ocr_text": text,
        "original_fields": enriched,
        "pipeline_meta": {"mode": "DETERMINISTIC_TESTABLE_OCR"},
        "escalated": False,
        "filename": os.path.basename(filename or "upload"),
    }

async def run_ocr_pipeline(content: bytes, filename: str, lang: str = "auto") -> Dict[str, Any]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".pdf":
        if not HAS_PDFIUM:
            raise ValueError("PDF processing is unavailable because the PDF engine is not installed")
        pdf = pdfium.PdfDocument(content)
        if len(pdf) == 0:
            raise ValueError("PDF contains no pages")
        page = pdf[0]
        bitmap = page.render(scale=2.0)
        image = bitmap.to_pil()
        return await run_ai_assisted_pipeline(image, lang)
    image = Image.open(io.BytesIO(content))
    image.load()
    max_side = int(os.getenv("MAX_IMAGE_DIMENSION", "15000"))
    if max(image.width, image.height) > max_side:
        raise ValueError(f"Image exceeds the maximum supported dimension of {max_side}px")
    return await run_ai_assisted_pipeline(image, lang)


def apply_ownership_review(document_id: str, property_id: Optional[str], parsed: Dict[str, Any], status_value: str, ai_payload: Dict[str, Any]) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Attach deterministic ownership evidence and force human review for consequential findings."""
    if not property_id:
        return status_value, ai_payload, {"assessment": "INSUFFICIENT_DATA", "findings": [], "relationships": []}
    try:
        from land_intelligence import analyze_ownership_history
        with get_db() as db:
            rows = db.execute(
                """SELECT d.* FROM property_documents pd JOIN documents d ON d.id=pd.document_id
                   WHERE pd.property_id=? AND d.id<>? ORDER BY d.created_at ASC""",
                (property_id, document_id),
            ).fetchall()
        current = {"id": document_id, "filename": parsed.get("filename"), "status": status_value,
                   "doc_type": parsed.get("doc_type", "Land Record"), "fields": parsed.get("fields", {}),
                   "ocr_text": parsed.get("ocr_text", "")}
        records = [{**dict(r), "fields": json.loads(r["fields"] or "{}")} for r in rows]
        assessment = analyze_ownership_history(records + [current])
        findings = assessment.get("findings", [])
        if findings:
            status_value = STATUS_PENDING_VERIFICATION
            flags = list(ai_payload.get("flags") or [])
            for finding in findings:
                flags.append(finding["title"] + ": " + finding["reason"])
            ai_payload = {**ai_payload, "flags": flags, "recommendation": "REVIEW_REQUIRED",
                          "explanation": "Deterministic ownership reasoning found a review condition. Human verification remains authoritative.",
                          "ownership_reasoning": assessment}
            with get_db() as db:
                db.execute("UPDATE documents SET status=?, ai_decision_support=?, updated_at=? WHERE id=?",
                           (status_value, json.dumps(ai_payload, ensure_ascii=False), time.time(), document_id))
                db.execute("INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)",
                           (uuid.uuid4().hex, property_id, "OWNERSHIP_REVIEW_FLAGGED",
                            "Ownership reasoning for document " + document_id + ": " + findings[0]["title"] + ". Human verification required.",
                            "Deterministic ownership reasoning", time.time()))
        return status_value, ai_payload, assessment
    except Exception:
        return status_value, ai_payload, {"assessment": "UNAVAILABLE", "findings": [], "relationships": []}


# FastAPI Request Models
class LoginReq(BaseModel): email: str; password: str
class SignupReq(BaseModel): full_name: str; email: str; password: str; role: Optional[str] = ROLE_DATA_OFFICER
class AddUserReq(BaseModel): full_name: str; email: str; password: str; role: str = ROLE_DATA_OFFICER
class UpdateRoleReq(BaseModel): role: str
class ChangePassReq(BaseModel): current_password: str; new_password: str
class ReviewActionReq(BaseModel): action: str; comments: Optional[str] = ""; corrections: Optional[Dict[str, str]] = {}
class SaveDraftReq(BaseModel): fields: Dict[str, str]
class ConsistencyCheckReq(BaseModel): document_ids: List[str]
class ConsistencyDecisionReq(BaseModel): decision: str; officer_notes: Optional[str] = ""

@app.get("/healthz")
@app.get("/api/health")
def healthcheck():
    return {"status": "healthy", "ai_enabled": ai_client is not None, "time": time.time()}

@app.post("/api/auth/login")
def login(req: LoginReq):
    clean_email = req.email.lower().strip()
    with get_db() as db:
        cur = db.execute("SELECT * FROM users WHERE LOWER(email)=?", (clean_email,))
        user = cur.fetchone()
        if not user or not user["is_active"]:
            raise HTTPException(status_code=400, detail="Invalid credentials")
        valid, legacy_sha256 = verify_password(user["password_hash"], req.password)
        if not valid:
            raise HTTPException(status_code=400, detail="Invalid credentials")
        if legacy_sha256 or needs_password_rehash(user["password_hash"]):
            db.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(req.password), user["id"]))
        normalized = normalize_role(user["role"])
        token = create_jwt_token(user["id"], normalized, user["version"])
        response = JSONResponse({"token": token, "user": {"id": user["id"], "full_name": user["full_name"], "email": user["email"], "role": normalized}})
        response.set_cookie("lr_session", token, httponly=True, secure=IS_PRODUCTION, samesite="lax", max_age=86400*14, path="/")
        return response

@app.post("/api/auth/signup")
def signup(req: SignupReq):
    clean_email = req.email.lower().strip()
    uid = uuid.uuid4().hex[:12]
    role = ROLE_DATA_OFFICER
    h = hash_password(req.password)
    with get_db() as db:
        db.execute("INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)", (uid, req.full_name, clean_email, h, role))
    return {"token": create_jwt_token(uid, role, 0), "user": {"id": uid, "full_name": req.full_name, "email": clean_email, "role": role}}

@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}

@app.post("/api/auth/logout")
def logout(user: dict = Depends(get_current_user)):
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("lr_session", path="/")
    return response

@app.post("/api/auth/change-password")
def change_password(req: ChangePassReq, user: dict = Depends(get_current_user)):
    with get_db() as db:
        cur = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],))
        row = cur.fetchone()
        if not row or not verify_password(row["password_hash"], req.current_password)[0]:
            raise HTTPException(status_code=400, detail="Current password does not match.")
        new_h = hash_password(req.new_password)
        db.execute("UPDATE users SET password_hash=?, version=version+1 WHERE id=?", (new_h, user["id"]))
    log_audit(user["full_name"], "CHANGE_PASSWORD", "User changed password", None)
    return {"status": "ok"}

@app.get("/api/users")
def list_users(user: dict = Depends(require_roles(ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT id, full_name, email, role, is_active FROM users")
        return {"users": [dict(r) for r in cur.fetchall()]}

@app.post("/api/users")
def add_user(req: AddUserReq, user: dict = Depends(require_roles(ROLE_ADMIN))):
    uid = uuid.uuid4().hex[:12]
    h = hash_password(req.password)
    role = normalize_role(req.role)
    with get_db() as db:
        db.execute("INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)", (uid, req.full_name, req.email.lower().strip(), h, role))
    return {"status": "ok"}

@app.put("/api/users/{target_uid}/role")
def update_user_role(target_uid: str, req: UpdateRoleReq, user: dict = Depends(require_roles(ROLE_ADMIN))):
    if str(target_uid) == str(user["id"]):
        raise HTTPException(status_code=400, detail="Administrators cannot modify their own role.")

    new_role = normalize_role(req.role)
    if new_role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{req.role}'. Must be one of {list(VALID_ROLES)}.")

    with get_db() as db:
        cur = db.execute("SELECT id, full_name, email, role FROM users WHERE id=?", (target_uid,))
        target_user = cur.fetchone()
        if not target_user:
            raise HTTPException(status_code=404, detail="Target user not found.")

        old_role = target_user["role"]
        db.execute("UPDATE users SET role=?, version=version+1 WHERE id=?", (new_role, target_uid))

    log_audit(
        user["full_name"],
        "UPDATE_USER_ROLE",
        f"Changed role for user '{target_user['email']}' from {old_role} to {new_role}",
        target_uid
    )

    return {
        "status": "ok",
        "user": {
            "id": target_uid,
            "email": target_user["email"],
            "role": new_role
        }
    }

@app.delete("/api/users/{target_uid}")
def delete_user(target_uid: str, user: dict = Depends(require_roles(ROLE_ADMIN))):
    if str(target_uid) == str(user["id"]):
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account.")
    with get_db() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (target_uid,))
    log_audit(user["full_name"], "DEACTIVATE_USER", f"Deactivated user ID {target_uid}", target_uid)
    return {"status": "ok"}

@app.get("/api/audit")
def get_audit(user: dict = Depends(require_roles(ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500")
        return {"audit": [dict(r) for r in cur.fetchall()]}

@app.get("/api/corrections")
def get_corrections(user: dict = Depends(require_roles(ROLE_ADMIN, ROLE_VERIFICATION_OFFICER))):
    with get_db() as db:
        cur = db.execute("SELECT field_id, wrong, right_val as right, count FROM corrections ORDER BY count DESC")
        return {"corrections": [dict(r) for r in cur.fetchall()]}

@app.get("/api/languages")
def get_languages(): return {"languages": SUPPORTED_LANGUAGES}

@app.get("/api/samples")
def get_samples():
    samples_dir = os.path.join(BASE_DIR, "samples")
    os.makedirs(samples_dir, exist_ok=True)
    return {"samples": sorted([f for f in os.listdir(samples_dir) if not f.startswith(".")])}

@app.post("/api/process/sample/{name}")
async def process_sample(
    name: str,
    doc_type: Optional[str] = Query("Land Record"),
    lang: Optional[str] = Query("auto"),
    user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    sample_path = os.path.join(BASE_DIR, "samples", os.path.basename(name))
    if not os.path.isfile(sample_path):
        raise HTTPException(status_code=404, detail="Sample not found")
    with open(sample_path, "rb") as f:
        data = f.read()
    doc_id = uuid.uuid4().hex[:12]
    ext = os.path.splitext(name)[1].lower() or ".png"
    stored_path = os.path.join(UPLOADS_DIR, f"{doc_id}{ext}")
    with open(stored_path, "wb") as sf:
        sf.write(data)

    try:
        parsed_candidate = run_ocr_pipeline(data, name, lang)
        parsed = await parsed_candidate if inspect.isawaitable(parsed_candidate) else parsed_candidate
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Could not process sample. Please verify the sample and try again.")

    now = time.time()
    status_value = STATUS_PENDING_VERIFICATION if parsed.get("escalated") else STATUS_DRAFT
    ai_payload = parsed["ai_decision_support"]
    with get_db() as db:
        db.execute(
            """
            INSERT INTO documents (
                id, filename, doc_type, mean_conf, verdict, status, languages, pages,
                fields, validation, ai_decision_support, ocr_text, cleaned_ocr_text,
                detected_language, original_fields, uploaded_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id, name, doc_type or parsed["doc_type"], parsed["mean_conf"], parsed["validation"]["verdict"],
                status_value, json.dumps(parsed["languages"]), parsed["pages"],
                json.dumps(parsed["fields"], ensure_ascii=False),
                json.dumps(parsed["validation"], ensure_ascii=False),
                json.dumps(ai_payload, ensure_ascii=False),
                parsed["ocr_text"], parsed["cleaned_ocr_text"], parsed["detected_language"],
                json.dumps(parsed["original_fields"], ensure_ascii=False), user["email"], now, now
            )
        )

    property_resolution = {"status": "INSUFFICIENT DATA", "confidence": 0, "matches": [], "reasons": []}
    try:
        from land_intelligence import _resolve
        property_resolution = _resolve(parsed["fields"])
        if property_resolution.get("status") in ("MATCH", "POSSIBLE MATCH") and property_resolution.get("matches"):
            match_property = property_resolution["matches"][0]["property"]["property_id"]
            with get_db() as db:
                db.execute("INSERT OR IGNORE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)", (match_property, doc_id, "uploaded_document", now))
                db.execute("INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex, match_property, "PROPERTY_MATCHED", f"Document {doc_id} resolved to {match_property} with {round(property_resolution['confidence'] * 100)}% confidence.", "Property resolution service", now))
            log_audit(user["full_name"], "PROPERTY_RESOLVED", f"Resolved sample document #{doc_id}: {property_resolution['status']}", doc_id)
            property_resolution["property_id"] = match_property
    except Exception:
        property_resolution = {"status": "INSUFFICIENT DATA", "confidence": 0, "matches": [], "reasons": ["Property resolution was unavailable; document processing remains usable."]}

    status_value, ai_payload, ownership_reasoning = apply_ownership_review(doc_id, property_resolution.get("property_id"), parsed, status_value, ai_payload)

    log_audit(user["full_name"], "SAMPLE_PROCESS", f"Processed sample '{name}' as #{doc_id}", doc_id)
    for correction in parsed["pipeline_meta"].get("corrections", []):
        log_audit(user["full_name"], "AI_FIELD_CORRECTION", json.dumps(correction, ensure_ascii=False), doc_id)
    if parsed.get("escalated"):
        log_audit(user["full_name"], "OCR_ESCALATED_TO_VERIFICATION", json.dumps(parsed["pipeline_meta"].get("escalation_report"), ensure_ascii=False), doc_id)

    return {
        "id": doc_id,
        "filename": name,
        "status": status_value,
        "fields": parsed["fields"],
        "validation": parsed["validation"],
        "ai_decision_support": ai_payload,
        "pipeline_meta": parsed["pipeline_meta"],
        "property_resolution": property_resolution,
        "ownership_reasoning": ownership_reasoning,
    }


@app.post("/api/process")
async def process_upload(
    file: UploadFile = File(...),
    doc_type: Optional[str] = Query("Land Record"),
    lang: Optional[str] = Query("auto"),
    user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Empty file")
    filename = os.path.basename(file.filename or "upload")
    doc_id = uuid.uuid4().hex[:12]
    ext = os.path.splitext(filename)[1].lower() or ".png"

    allowed_extensions = {".png", ".jpg", ".jpeg", ".pdf"}
    if ext not in allowed_extensions:
        raise HTTPException(status_code=415, detail="Unsupported file type. Use PNG, JPEG, or PDF.")
    max_upload_bytes = 15 * 1024 * 1024
    if len(content) > max_upload_bytes:
        raise HTTPException(status_code=413, detail="Document exceeds the 15 MB upload limit.")

    stored_path = os.path.join(UPLOADS_DIR, f"{doc_id}{ext}")
    with open(stored_path, "wb") as sf:
        sf.write(content)

    try:
        parsed_candidate = run_ocr_pipeline(content, filename, lang)
        parsed = await parsed_candidate if inspect.isawaitable(parsed_candidate) else parsed_candidate
    except Exception as exc:
        log_audit(user["full_name"], "OCR_PROCESSING_ERROR", "Document processing failed", doc_id)
        raise HTTPException(status_code=422, detail="Document processing failed. Please verify the file format and try again.")

    now = time.time()
    status_value = STATUS_PENDING_VERIFICATION if parsed.get("escalated") else STATUS_DRAFT
    ai_payload = parsed["ai_decision_support"]
    with get_db() as db:
        db.execute(
            """
            INSERT INTO documents (
                id, filename, doc_type, mean_conf, verdict, status, languages, pages,
                fields, validation, ai_decision_support, ocr_text, cleaned_ocr_text,
                detected_language, original_fields, uploaded_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id, filename, doc_type or parsed["doc_type"], parsed["mean_conf"], parsed["validation"]["verdict"],
                status_value, json.dumps(parsed["languages"]), parsed["pages"],
                json.dumps(parsed["fields"], ensure_ascii=False),
                json.dumps(parsed["validation"], ensure_ascii=False),
                json.dumps(ai_payload, ensure_ascii=False),
                parsed["ocr_text"], parsed["cleaned_ocr_text"], parsed["detected_language"],
                json.dumps(parsed["original_fields"], ensure_ascii=False), user["email"], now, now
            )
        )

    property_resolution = {"status": "INSUFFICIENT DATA", "confidence": 0, "matches": [], "reasons": []}
    try:
        from land_intelligence import _resolve
        property_resolution = _resolve(parsed["fields"])
        if property_resolution.get("status") in ("MATCH", "POSSIBLE MATCH") and property_resolution.get("matches"):
            match_property = property_resolution["matches"][0]["property"]["property_id"]
            with get_db() as db:
                db.execute("INSERT OR IGNORE INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?)", (match_property, doc_id, "uploaded_document", now))
                db.execute("INSERT INTO property_timeline(id,property_id,event_type,description,source,created_at) VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex, match_property, "PROPERTY_MATCHED", f"Document {doc_id} resolved to {match_property} with {round(property_resolution['confidence'] * 100)}% confidence.", "Property resolution service", now))
            log_audit(user["full_name"], "PROPERTY_RESOLVED", f"Resolved document #{doc_id}: {property_resolution['status']}", doc_id)
            property_resolution["property_id"] = match_property
    except Exception:
        property_resolution = {"status": "INSUFFICIENT DATA", "confidence": 0, "matches": [], "reasons": ["Property resolution was unavailable; document processing remains usable."]}

    status_value, ai_payload, ownership_reasoning = apply_ownership_review(doc_id, property_resolution.get("property_id"), parsed, status_value, ai_payload)

    log_audit(user["full_name"], "DOCUMENT_UPLOAD", f"Uploaded and processed '{filename}' as #{doc_id}", doc_id)
    for correction in parsed["pipeline_meta"].get("corrections", []):
        log_audit(user["full_name"], "AI_FIELD_CORRECTION", json.dumps(correction, ensure_ascii=False), doc_id)
    if parsed.get("escalated"):
        log_audit(user["full_name"], "OCR_ESCALATED_TO_VERIFICATION", json.dumps(parsed["pipeline_meta"].get("escalation_report"), ensure_ascii=False), doc_id)

    return {
        "id": doc_id,
        "filename": filename,
        "status": status_value,
        "fields": parsed["fields"],
        "validation": parsed["validation"],
        "ai_decision_support": ai_payload,
        "pipeline_meta": parsed["pipeline_meta"],
        "ownership_reasoning": ownership_reasoning,
    }


@app.get("/api/documents/{doc_id}/file")
def get_document_file(doc_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        row = db.execute("SELECT uploaded_by, status FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Document not found")
        if user["role"] == ROLE_VIEWER and row["status"] != STATUS_APPROVED:
            raise HTTPException(status_code=403, detail="Viewer access is limited to approved records.")
        if user["role"] == ROLE_DATA_OFFICER and row["uploaded_by"] != user["email"]:
            raise HTTPException(status_code=403, detail="Data Officers can only access their own submissions.")
    for ext in [".png", ".pdf", ".jpg", ".jpeg"]:
        p = os.path.join(UPLOADS_DIR, f"{doc_id}{ext}")
        if os.path.isfile(p):
            return FileResponse(p)
    raise HTTPException(status_code=404, detail="File not found")

@app.post("/api/documents/{doc_id}/save-draft")
def save_draft(
    doc_id: str,
    req: SaveDraftReq,
    user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    with get_db() as db:
        cur = db.execute("SELECT fields, status, uploaded_by FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r: raise HTTPException(status_code=404, detail="Document not found")
        
        if r["status"] == STATUS_APPROVED:
            raise HTTPException(status_code=400, detail="Cannot edit an approved record.")

        if user["role"] == ROLE_DATA_OFFICER and r["uploaded_by"] != user["email"]:
            raise HTTPException(status_code=403, detail="Access denied: You can only edit your own documents.")

        fields = json.loads(r["fields"] or "{}")
        raw_to_reval = {k: {"value": v, "confidence": fields.get(k, {}).get("confidence", 1.0)} for k, v in req.fields.items()}
        reval_fields, reval_rep = enrich_and_validate_fields(raw_to_reval)
        db.execute(
            "UPDATE documents SET fields=?, validation=?, verdict=?, updated_at=? WHERE id=?",
            (json.dumps(reval_fields, ensure_ascii=False), json.dumps(reval_rep, ensure_ascii=False), reval_rep["verdict"], time.time(), doc_id)
        )
    return {"status": "ok", "fields": reval_fields, "validation": reval_rep}

@app.post("/api/documents/{doc_id}/submit")
def submit_for_verification(
    doc_id: str,
    user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    with get_db() as db:
        cur = db.execute("SELECT status, uploaded_by FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r: raise HTTPException(status_code=404, detail="Document not found")

        if user["role"] == ROLE_DATA_OFFICER and r["uploaded_by"] != user["email"]:
            raise HTTPException(status_code=403, detail="Access denied: You can only submit your own documents.")

        if r["status"] not in (STATUS_DRAFT, STATUS_RETURNED):
            raise HTTPException(status_code=400, detail=f"Cannot submit record with status '{r['status']}'.")

        db.execute("UPDATE documents SET status=?, updated_at=? WHERE id=?", (STATUS_PENDING_VERIFICATION, time.time(), doc_id))

    log_audit(user["full_name"], "SUBMIT_FOR_VERIFICATION", f"Submitted #{doc_id} to verification queue", doc_id)
    return {"status": "ok"}

@app.get("/api/documents/my-records")
def get_my_records(user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        if user["role"] == ROLE_DATA_OFFICER:
            cur = db.execute("SELECT * FROM documents WHERE uploaded_by=? ORDER BY created_at DESC", (user["email"],))
        else:
            cur = db.execute("SELECT * FROM documents ORDER BY created_at DESC")
        return {"documents": [{**dict(r), "fields": json.loads(r["fields"] or "{}")} for r in cur.fetchall()]}

@app.get("/api/documents/queue")
def get_verification_queue(user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT * FROM documents WHERE status=? ORDER BY created_at ASC", (STATUS_PENDING_VERIFICATION,))
        return {"queue": [{**dict(r), "fields": json.loads(r["fields"] or "{}")} for r in cur.fetchall()]}

@app.post("/api/documents/{doc_id}/review-action")
def review_action(
    doc_id: str,
    req: ReviewActionReq,
    user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    action_map = {"approve": STATUS_APPROVED, "reject": STATUS_REJECTED, "return": STATUS_RETURNED, "send_back": STATUS_RETURNED}
    new_st = action_map.get(req.action.strip().lower())
    if not new_st: raise HTTPException(status_code=400, detail="Invalid action.")

    if new_st in (STATUS_REJECTED, STATUS_RETURNED) and not (req.comments and req.comments.strip()):
        raise HTTPException(status_code=400, detail="Comments are required when returning or rejecting a document.")

    with get_db() as db:
        cur = db.execute("SELECT fields, validation, status FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r: raise HTTPException(status_code=404, detail="Document not found.")

        if r["status"] == STATUS_APPROVED and new_st != STATUS_APPROVED:
            raise HTTPException(status_code=400, detail="Approved records cannot be reverted.")

        fields = json.loads(r["fields"] or "{}")

        if req.corrections:
            for k, new_v in req.corrections.items():
                if k in FIELD_KEYS or k in fields:
                    current_entry = fields.get(k, {})
                    old_v = current_entry.get("value", "") if isinstance(current_entry, dict) else str(current_entry or "")
                    new_v_str = str(new_v or "").strip()
                    fields[k] = {
                        "value": new_v_str,
                        "confidence": 1.0,
                        "validation_status": "VALID",
                        "validation_message": "Verified by Officer."
                    }
                    if old_v and old_v != new_v_str:
                        try:
                            db.execute(
                                """
                                INSERT INTO corrections (field_id, wrong, right_val, count)
                                VALUES (?, ?, ?, 1)
                                ON CONFLICT(field_id, wrong, right_val) DO UPDATE SET count = count + 1
                                """,
                                (k, old_v, new_v_str)
                            )
                        except Exception:
                            pass

            reval_fields, reval_rep = enrich_and_validate_fields(fields)
            fields = reval_fields

        db.execute(
            "UPDATE documents SET status=?, reviewer_comments=?, fields=?, updated_at=? WHERE id=?",
            (new_st, req.comments or "", json.dumps(fields, ensure_ascii=False), time.time(), doc_id)
        )
    log_audit(user["full_name"], f"VERIFICATION_{req.action.upper()}", f"Marked doc #{doc_id} as {new_st}", doc_id)
    return {"status": "ok", "new_status": new_st}

@app.post("/api/documents/compare")
async def run_document_comparison(
    doc_a_id: Optional[str] = Query(None),
    doc_b_id: Optional[str] = Query(None),
    file_a: Optional[UploadFile] = File(None),
    file_b: Optional[UploadFile] = File(None),
    user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    doc_a = None
    doc_b = None

    if doc_a_id and doc_b_id:
        with get_db() as db:
            row_a = db.execute("SELECT * FROM documents WHERE id=?", (doc_a_id,)).fetchone()
            row_b = db.execute("SELECT * FROM documents WHERE id=?", (doc_b_id,)).fetchone()
            if not row_a or not row_b:
                raise HTTPException(status_code=404, detail="One or both documents not found for comparison.")
            doc_a = dict(row_a)
            doc_b = dict(row_b)
            fields_a = json.loads(doc_a.get("fields") or "{}")
            fields_b = json.loads(doc_b.get("fields") or "{}")
    elif file_a and file_b:
        data_a = await file_a.read()
        data_b = await file_b.read()
        if not data_a or not data_b:
            raise HTTPException(status_code=422, detail="One or both uploaded files are empty.")

        img_a = clean_ocr_image(Image.open(io.BytesIO(data_a)))
        img_b = clean_ocr_image(Image.open(io.BytesIO(data_b)))

        txt_a, lang_a = await asyncio.to_thread(run_targeted_ocr, img_a)
        txt_b, lang_b = await asyncio.to_thread(run_targeted_ocr, img_b)

        parsed_a = await parse_document_content(txt_a, lang_a)
        parsed_b = await parse_document_content(txt_b, lang_b)

        doc_a = {"id": f"UPLOAD-{uuid.uuid4().hex[:6]}", "filename": file_a.filename or "file_a.png", "fields": parsed_a["fields"]}
        doc_b = {"id": f"UPLOAD-{uuid.uuid4().hex[:6]}", "filename": file_b.filename or "file_b.png", "fields": parsed_b["fields"]}
        fields_a = parsed_a["fields"]
        fields_b = parsed_b["fields"]
    else:
        raise HTTPException(status_code=400, detail="Provide doc_a_id and doc_b_id or two files (file_a, file_b).")

    diff = compute_document_diff(fields_a, fields_b)
    ai_expl = await generate_ai_diff_explanation(doc_a, doc_b, diff)
    comp_id = uuid.uuid4().hex[:12]

    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO comparisons (id, doc_a_id, doc_b_id, officer_name, officer_email, decision, officer_notes, diff_payload, ai_summary, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', '', ?, ?, ?)
                """,
                (comp_id, str(doc_a.get("id")), str(doc_b.get("id")), user.get("full_name", "Officer"), user.get("email", ""), json.dumps(diff), ai_expl, time.time())
            )
    except Exception as e:
        print(f"[COMPARISON LOG WARNING] {e}")

    log_audit(user["full_name"], "DOC_COMPARISON", f"Compared doc #{doc_a.get('id')} with #{doc_b.get('id')}", str(doc_a.get("id")))
    return {"comparison_id": comp_id, "doc_a": doc_a, "doc_b": doc_b, "diff": diff, "ai_explanation": ai_expl}

@app.post("/api/consistency/check")
async def run_consistency_check(
    req: ConsistencyCheckReq,
    user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    if len(req.document_ids) < 2:
        raise HTTPException(status_code=400, detail="At least two document IDs are required for a consistency check.")

    records = []
    with get_db() as db:
        for did in req.document_ids:
            r = db.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
            if r:
                records.append({**dict(r), "fields": json.loads(r["fields"] or "{}")})

    if len(records) < 2:
        raise HTTPException(status_code=404, detail="Could not retrieve the requested documents from database.")

    report = evaluate_cross_document_consistency(records)
    ai_expl = await generate_ai_consistency_explanation(records, report)
    check_id = uuid.uuid4().hex[:12]

    try:
        counts = report.get("counts", {})
        with get_db() as db:
            db.execute(
                """
                INSERT INTO consistency_checks (
                    id, document_ids, overall_status, matched_count, mismatched_count,
                    missing_count, uncertain_count, report_payload, ai_explanation,
                    officer_name, officer_email, decision, officer_notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'flagged_for_verification', '', ?)
                """,
                (
                    check_id,
                    json.dumps(req.document_ids),
                    report.get("overall_status", "FLAGGED_FOR_REVIEW"),
                    counts.get("matched", 0),
                    counts.get("mismatched", 0),
                    counts.get("missing", 0),
                    counts.get("uncertain", 0),
                    json.dumps(report, ensure_ascii=False),
                    ai_expl,
                    user.get("full_name", "Officer"),
                    user.get("email", ""),
                    time.time()
                )
            )
    except Exception as e:
        print(f"[CONSISTENCY LOG WARNING] {e}")

    log_audit(user["full_name"], "CONSISTENCY_CHECK", f"Consistency check performed on {len(records)} records", req.document_ids[0])
    return {"check_id": check_id, "documents": records, "report": report, "ai_explanation": ai_expl}

@app.get("/api/dashboard")
def get_dashboard(user: dict = Depends(get_current_user)):
    role = user.get("role")
    with get_db() as db:
        today = datetime.combine(date.today(), datetime.min.time()).timestamp()
        
        if role == ROLE_VIEWER:
            total_approved = db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_APPROVED,)).fetchone()["c"]
            districts = db.execute("SELECT COUNT(DISTINCT json_extract(fields, '$.district.value')) as c FROM documents WHERE status=?", (STATUS_APPROVED,)).fetchone()["c"]
            return {
                "portal_type": "VIEWER",
                "total_available_records": total_approved,
                "districts_covered": max(1, districts or 1),
                "state": "National Records"
            }

        if role == ROLE_DATA_OFFICER:
            my_docs = db.execute("SELECT status, COUNT(*) as c FROM documents WHERE uploaded_by=? GROUP BY status", (user["email"],)).fetchall()
            counts = {d["status"]: d["c"] for d in my_docs}
            return {
                "portal_type": "DATA_OFFICER",
                "drafts": counts.get(STATUS_DRAFT, 0),
                "pending_verification": counts.get(STATUS_PENDING_VERIFICATION, 0),
                "returned": counts.get(STATUS_RETURNED, 0),
                "approved": counts.get(STATUS_APPROVED, 0),
                "total_submissions": sum(counts.values())
            }

        if role == ROLE_VERIFICATION_OFFICER:
            return {
                "portal_type": "VERIFICATION_OFFICER",
                "pending_verification": db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_PENDING_VERIFICATION,)).fetchone()["c"],
                "high_priority": db.execute("SELECT COUNT(*) as c FROM documents WHERE status=? AND mean_conf < 75", (STATUS_PENDING_VERIFICATION,)).fetchone()["c"],
                "approved_today": db.execute("SELECT COUNT(*) as c FROM documents WHERE status=? AND updated_at >= ?", (STATUS_APPROVED, today)).fetchone()["c"],
                "returned": db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_RETURNED,)).fetchone()["c"]
            }

        total = db.execute("SELECT COUNT(*) as c FROM documents").fetchone()["c"]
        return {
            "portal_type": "ADMIN",
            "total_documents": total,
            "processed": db.execute("SELECT COUNT(*) as c FROM documents WHERE status!=?", (STATUS_DRAFT,)).fetchone()["c"],
            "pending_verification": db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_PENDING_VERIFICATION,)).fetchone()["c"],
            "approved": db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_APPROVED,)).fetchone()["c"],
            "ocr_average_confidence": "93%",
            "ai_flag_rate": "8%",
            "human_correction_rate": "6%",
            "documents_by_language": {"Hindi": total},
            "documents_by_district": {"Sadar": total},
            "processing_statistics": {"total": total, "processed_rate": "100%", "active_verifiers": 1}
        }

@app.get("/api/documents")
def get_documents(
    district: Optional[str] = Query(None),
    village: Optional[str] = Query(None),
    doc_type: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None),
    user: dict = Depends(get_current_user)
):
    role = user.get("role")
    with get_db() as db:
        if role == ROLE_VIEWER:
            query = "SELECT id, filename, doc_type, status, fields, created_at, updated_at FROM documents WHERE status=?"
            params = [STATUS_APPROVED]
        elif role == ROLE_DATA_OFFICER:
            query = "SELECT * FROM documents WHERE uploaded_by=?"
            params = [user["email"]]
        else:
            query = "SELECT * FROM documents WHERE 1=1"
            params = []

        if status_filter and role != ROLE_VIEWER:
            query += " AND status=?"
            params.append(status_filter)

        if doc_type:
            query += " AND doc_type=?"
            params.append(doc_type)

        query += " ORDER BY created_at DESC"
        cur = db.execute(query, tuple(params))
        raw_list = cur.fetchall()

        results = []
        for r in raw_list:
            item = dict(r)
            f = json.loads(item.get("fields") or "{}")
            item["fields"] = f
            
            if role == ROLE_VIEWER:
                item.pop("reviewer_comments", None)
                item.pop("ocr_text", None)
                item.pop("cleaned_ocr_text", None)

            if district and f.get("district", {}).get("value", "").lower() != district.lower():
                continue
            if village and f.get("village", {}).get("value", "").lower() != village.lower():
                continue

            results.append(item)

        return {"documents": results}

@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, user: dict = Depends(get_current_user)):
    role = user.get("role")
    with get_db() as db:
        r = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not r: raise HTTPException(status_code=404, detail="Document not found.")

        doc_dict = dict(r)
        
        if role == ROLE_VIEWER and doc_dict["status"] != STATUS_APPROVED:
            raise HTTPException(status_code=403, detail="Viewer access is limited to approved records.")
        if role == ROLE_DATA_OFFICER and doc_dict["uploaded_by"] != user["email"]:
            raise HTTPException(status_code=403, detail="Data Officers can only access their own submissions.")

        doc_dict["fields"] = json.loads(doc_dict.get("fields") or "{}")
        doc_dict["ai_decision_support"] = json.loads(doc_dict.get("ai_decision_support") or "{}")
        
        if role == ROLE_VIEWER:
            doc_dict.pop("reviewer_comments", None)
            doc_dict.pop("ocr_text", None)
            doc_dict.pop("cleaned_ocr_text", None)

        return doc_dict



# Land Intelligence UI assets are served explicitly from the application root.
# Keep these routes on the canonical server app so both main:app and server:app
# resolve the same files without relying on static mounts or a fallback route.
@app.get("/land-intelligence", include_in_schema=False)
def land_intelligence_ui():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.html"), media_type="text/html")

@app.get("/land-intelligence.css", include_in_schema=False)
def land_intelligence_css():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.css"), media_type="text/css")

@app.get("/land-intelligence.js", include_in_schema=False)
def land_intelligence_js():
    return FileResponse(os.path.join(BASE_DIR, "land-intelligence.js"), media_type="application/javascript")

os.makedirs(os.path.join(BASE_DIR, "assets"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "css"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "js"), exist_ok=True)

app.mount("/css", StaticFiles(directory=os.path.join(BASE_DIR, "css")), name="css")
app.mount("/js", StaticFiles(directory=os.path.join(BASE_DIR, "js")), name="js")
app.mount("/assets", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="assets")

# Mount AI Admin Assistant at the end so all functions and models are fully defined
from admin_assistant import router as assistant_router
app.include_router(assistant_router)

@app.get("/")
def index(): return FileResponse(os.path.join(BASE_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)