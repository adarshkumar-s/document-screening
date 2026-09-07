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
from datetime import datetime, date
from typing import Optional, Dict, Any, List, Tuple
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Query, Request, Depends, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, ImageEnhance

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

JWT_SECRET = os.getenv("JWT_SECRET", "dilrmp-hackathon-secure-secret-2026")

# ---------------------------------------------------------
# ROLES & STATUSES
# ---------------------------------------------------------
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
    "verification_officER": ROLE_VERIFICATION_OFFICER,
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

app = FastAPI(title="DILRMP Intelligent Land Record Digitization & Validation System")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# DATABASE ADAPTER
# ---------------------------------------------------------
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
            self.conn = sqlite3.connect(SQLITE_PATH, timeout=10.0, check_same_thread=False)
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
        for stmt in [
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
            """
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                field_id TEXT NOT NULL,
                wrong TEXT NOT NULL,
                right_val TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(field_id, wrong, right_val)
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                doc_id TEXT
            );
            """,
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
        ]:
            try:
                db.execute(stmt)
            except Exception:
                pass

        for alter_sql in [
            "ALTER TABLE documents ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'Land Record'",
            "ALTER TABLE documents ADD COLUMN uploaded_by TEXT NOT NULL DEFAULT 'SYSTEM'",
            "ALTER TABLE documents ADD COLUMN reviewer_comments TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE documents ADD COLUMN updated_at REAL NOT NULL DEFAULT 0",
            "ALTER TABLE documents ADD COLUMN ai_decision_support TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE documents ADD COLUMN cleaned_ocr_text TEXT NOT NULL DEFAULT ''"
        ]:
            try:
                db.execute(alter_sql)
            except Exception:
                pass

        try:
            db.execute("UPDATE documents SET status='DRAFT' WHERE LOWER(status)='draft'")
            db.execute("UPDATE documents SET status='PENDING_VERIFICATION' WHERE LOWER(status) IN ('pending_review', 'pending')")
            db.execute("UPDATE documents SET status='APPROVED' WHERE LOWER(status) IN ('verified', 'valid', 'approved')")
            db.execute("UPDATE documents SET status='RETURNED_TO_DATA_OFFICER' WHERE LOWER(status) IN ('sent_back', 'returned')")
            db.execute("UPDATE documents SET status='REJECTED' WHERE LOWER(status)='rejected'")
        except Exception:
            pass

        cur = db.execute("SELECT id, role FROM users WHERE LOWER(email)='admin@landrec.gov.in'")
        row = cur.fetchone()
        if not row:
            admin_id = "5cc810682c7f"
            h = hashlib.sha256("Admin@123".encode()).hexdigest()
            db.execute(
                "INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)",
                (admin_id, "System Administrator", "admin@landrec.gov.in", h, ROLE_ADMIN)
            )
            db.execute(
                "INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                (time.time(), "SYSTEM", "INIT", "System initialized with permanent administrator credentials", None)
            )

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

# ---------------------------------------------------------
# AUTHENTICATION & RBAC
# ---------------------------------------------------------
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

def get_current_user(authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)) -> Dict[str, Any]:
    jwt_token = token or (authorization.replace("Bearer ", "").strip() if authorization else None)
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

# ---------------------------------------------------------
# OCR & PREPROCESSING
# ---------------------------------------------------------
INDIC_DIGIT_MAP = str.maketrans(
    "०१२३४५६७८९০১২৩৪৫৬৭৮৯٠١٢٣٤٥٦٧٨٩۰۱۲३४५६७८९௧௨௩௪௫௬௭௮௯௦૦૧૨૩૪૫૬૭૮૯౦౧౨౩౪౫౬౭౮౯",
    "0123456789012345678901234567890123456789123456789001234567890123456789"
)

FIELD_KEYS = (
    "owner_name", "father_name", "survey_number", "khasra_number",
    "khata_number", "plot_number", "area", "village", "tehsil",
    "district", "state", "land_class", "ownership_type",
    "mutation_no", "registration_no", "khatauni_year"
)

FIELD_LABELS = {
    "owner_name": ["Record Holder Name", "Landowner Name", "Owner Name", "भूमि स्वामी", "खातेदार", "భూ యజమాని", "பட்டாதாரர்", "খতিয়ানধারীর নাম"],
    "father_name": ["Father's Name", "Father Name", "Husband Name", "पिता का नाम", "पिता/पति", "తండ్రి పేరు", "தந்தை பெயர்"],
    "survey_number": ["Survey Number", "Survey No", "सर्वे नंबर", "सर्वे क्रमांक", "సర్వే నంబర్"],
    "khasra_number": ["Khasra Number", "Khasra No", "खसरा नंबर", "खसरा संख्या", "ఖస్రా నంబర్"],
    "khata_number": ["Khata Number", "Khata No", "खाता नंबर", "खाता संख्या", "ఖాతా సంఖ్య", "பட்டா எண்"],
    "plot_number": ["Plot Number", "Plot No", "प्लॉट नंबर", "प्लॉट क्रमांक"],
    "area": ["Plot Area", "Land Area", "Area", "क्षेत्रफल", "रकबा", "విస్తీర్ణం"],
    "village": ["Village Name", "Village", "Gram", "ग्राम", "गाँव", "मौजा", "గ్రామం"],
    "tehsil": ["Tehsil", "Taluk", "Mandal", "तहसील", "तालुका", "मंडल", "మండలం"],
    "district": ["District Name", "District", "जिला", "जिल्हा", "జిల్లా"],
    "state": ["State Name", "State", "राज्य", "రాష్ట్రం"],
    "land_class": ["Land Classification", "Land Class", "भूमि का प्रकार", "भू-वर्गीकरण", "श्रेणी"],
    "ownership_type": ["Ownership Type", "स्वामित्व प्रकार", "स्वामित्व"],
    "mutation_no": ["Mutation Number", "Mutation No", "नामांतरण संख्या", "नामांतरण नंबर", "మ్యుటేషన్ నంబర్"],
    "registration_no": ["Registration Number", "Registration No", "पंजीकरण संख्या", "రిజిస్ట్రేషన్ సంఖ్య"],
    "khatauni_year": ["Khatauni Year", "Fasli Year", "खतौनी वर्ष", "फसली वर्ष", "वर्ष", "Date", "दिनांक"]
}

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
        "ben": sum(1 for c in text if 0x0980 <= ord(c) <= 0x09FF),
        "pan": sum(1 for c in text if 0x0A00 <= ord(c) <= 0x0A7F),
        "guj": sum(1 for c in text if 0x0A80 <= ord(c) <= 0x0AFF),
        "ori": sum(1 for c in text if 0x0B00 <= ord(c) <= 0x0B7F),
        "tam": sum(1 for c in text if 0x0B80 <= ord(c) <= 0x0BFF),
        "tel": sum(1 for c in text if 0x0C00 <= ord(c) <= 0x0C7F),
        "kan": sum(1 for c in text if 0x0C80 <= ord(c) <= 0x0CFF),
        "urd": sum(1 for c in text if 0x0600 <= ord(c) <= 0x06FF),
    }
    top = max(counts, key=counts.get)
    return top if counts[top] >= 1 else "eng"

def run_targeted_ocr(image: Image.Image, lang_code: str = "auto") -> tuple[str, str]:
    if not HAS_TESSERACT:
        return "", "English"

    cfg = "--oem 1 --psm 3"
    raw_text = ""
    lang_map = {
        "hin": "hin+eng", "tel": "tel+eng", "tam": "tam+eng", "mar": "mar+hin+eng",
        "guj": "guj+eng", "ben": "ben+eng", "pan": "pan+eng", "kan": "kan+eng",
        "ori": "ori+eng", "urd": "urd+eng", "eng": "eng"
    }

    if lang_code in lang_map:
        try:
            raw_text = pytesseract.image_to_string(image, lang=lang_map[lang_code], config=cfg)
        except Exception:
            raw_text = ""

    if not raw_text or len(raw_text.strip()) < 15:
        for combo in ["hin+eng+tel", "tam+ben+guj", "kan+ori+pan", "urd+eng", "eng"]:
            try:
                raw_text = pytesseract.image_to_string(image, lang=combo, config=cfg)
                if len(raw_text.strip()) >= 15:
                    break
            except Exception:
                continue

    if len(raw_text.strip()) < 10:
        try:
            raw_text = pytesseract.image_to_string(image, lang="hin+tel+eng", config="--oem 1 --psm 6")
        except Exception:
            raw_text = pytesseract.image_to_string(image, lang="eng", config="--oem 1 --psm 6")

    detected = detect_primary_script(raw_text)
    script_names = {
        "hin": "Hindi", "tel": "Telugu", "tam": "Tamil", "ben": "Bengali",
        "guj": "Gujarati", "mar": "Marathi", "pan": "Punjabi", "kan": "Kannada",
        "ori": "Odia", "urd": "Urdu", "eng": "English"
    }
    return raw_text, script_names.get(detected, "English")

# ---------------------------------------------------------
# DETERMINISTIC FIELD VALIDATION ENGINE
# ---------------------------------------------------------
INDIAN_STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh", "goa", "gujarat",
    "haryana", "himachal pradesh", "jharkhand", "karnataka", "kerala", "madhya pradesh",
    "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland", "odisha", "punjab",
    "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura", "uttar pradesh",
    "uttarakhand", "west bengal", "delhi", "jammu & kashmir", "ladakh"
}

def validate_date_string(val: str) -> Tuple[str, str]:
    val_clean = val.strip()
    if re.search(r"\b(1[34]\d{2}(?:\s*-\s*1[34]\d{2})?)\s*(?:fasli|फ़सली|फसली)?\b", val_clean, re.IGNORECASE):
        return "VALID", "Valid agricultural Fasli / revenue period."
    if re.match(r"^\b(19\d{2}|20\d{2})(?:\s*-\s*(19\d{2}|20\d{2}))?\b$", val_clean):
        return "VALID", "Valid administrative calendar year range."

    date_patterns = [
        ("%d/%m/%Y", r"^\d{1,2}/\d{1,2}/\d{4}$"),
        ("%d-%m-%Y", r"^\d{1,2}-\d{1,2}-\d{4}$"),
        ("%Y-%m-%d", r"^\d{4}-\d{1,2}-\d{1,2}$"),
        ("%d.%m.%Y", r"^\d{1,2}\.\d{1,2}\.\d{4}$")
    ]
    for fmt, pat in date_patterns:
        if re.match(pat, val_clean):
            try:
                parsed = datetime.strptime(val_clean, fmt)
                if parsed.year < 1900 or parsed.year > 2035:
                    return "WARNING", f"Year {parsed.year} is outside normal archival span (1900-2035)."
                return "VALID", "Valid calendar date."
            except ValueError as ve:
                return "INVALID", f"Invalid date: {ve}"

    if re.search(r"\d+[\/\-\.]\d+[\/\-\.]\d+", val_clean):
        return "INVALID", "Malformed calendar date: invalid day/month sequence."
    return "WARNING", "Non-standard date format. Verify record year."

def validate_area_string(val: str) -> Tuple[str, str]:
    val_clean = val.strip().lower()
    nums = re.findall(r"[-+]?\d*\.\d+|\d+", val_clean)
    if not nums:
        return "INVALID", "Missing numerical area magnitude."

    try:
        qty = float(nums[0])
    except ValueError:
        return "INVALID", "Unparseable numeric value in area."

    has_unit = any(u in val_clean for u in ["hectare", "hec", "हेक्टेयर", "हे.", "acre", "एकर", "bigha", "बीघा", "sq", "cent", "gunta", "biswa"])
    if not has_unit:
        return "WARNING", "Numeric magnitude present, but land area unit (Acre/Hectare/Bigha) is unspecified."

    if "hectare" in val_clean or "हे" in val_clean:
        if qty <= 0.0:
            return "INVALID", "Area cannot be zero or negative."
        if qty > 50.0:
            return "WARNING", f"Area ({qty} Hectares) is unusually high for individual parcel. Verify subdivision."
        return "VALID", "Area within expected cadastral range."

    if "acre" in val_clean or "एकर" in val_clean:
        if qty <= 0.0:
            return "INVALID", "Area cannot be zero or negative."
        if qty > 100.0:
            return "WARNING", f"Area ({qty} Acres) is unusually high compared with expected parcel range."
        return "VALID", "Area within standard parcel parameters."

    if qty <= 0.0:
        return "INVALID", "Area must be positive."
    return "VALID", "Area specification format is valid."

def validate_single_field(field_name: str, value: str, confidence: float) -> Tuple[str, str]:
    val = (value or "").strip()
    if not val or val == "—":
        if field_name in ("owner_name", "khasra_number", "survey_number", "village", "district"):
            return "MISSING", f"Required statutory field '{field_name}' is missing."
        return "MISSING", "Field is empty."

    if field_name in ("owner_name", "father_name"):
        if re.search(r"\d", val):
            return "INVALID", "Name contains numeric digits."
        if len(val) < 3:
            return "WARNING", "Name string is unusually short (< 3 characters)."
        if re.search(r"[!@#$%^&*()_=+\[\]{};:<>?/\\]", val):
            return "WARNING", "Name contains abnormal special characters."
        if confidence < 0.70:
            return "WARNING", f"Low OCR text confidence ({int(confidence * 100)}%). Verify spelling."
        return "VALID", "Valid name syntax."

    elif field_name in ("survey_number", "khasra_number"):
        if re.match(r"^\d+([\/A-Za-z\-\.\_]\d*)*$", val):
            return "VALID", "Valid cadastral survey/khasra syntax."
        return "WARNING", f"Non-standard survey number pattern '{val}'."

    elif field_name == "khata_number":
        if re.match(r"^\d{1,8}$", val):
            return "VALID", "Valid Khata/Patta format."
        return "WARNING", f"Khata number contains non-numeric characters: '{val}'."

    elif field_name == "plot_number":
        if re.match(r"^[A-Za-z0-9\-\.\/]+$", val):
            return "VALID", "Valid plot identifier."
        return "WARNING", "Plot identifier contains abnormal characters."

    elif field_name == "area":
        return validate_area_string(val)

    elif field_name == "khatauni_year":
        return validate_date_string(val)

    elif field_name == "state":
        clean_state = re.sub(r"\(.*?\)", "", val).strip().lower()
        if clean_state in INDIAN_STATES or any(s in clean_state for s in INDIAN_STATES):
            return "VALID", "Recognized official State/UT."
        return "WARNING", f"Unrecognized state jurisdiction: '{val}'."

    elif field_name in ("village", "tehsil", "district"):
        if re.search(r"\d", val):
            return "WARNING", f"Administrative division '{field_name}' contains unexpected numbers."
        if len(val) < 2:
            return "INVALID", f"Invalid {field_name} string length."
        return "VALID", "Valid administrative name."

    elif field_name in ("mutation_no", "registration_no"):
        if len(val) < 3:
            return "WARNING", "Reference number is short. Check if partial OCR slip."
        return "VALID", "Valid statutory reference structure."

    if confidence < 0.65:
        return "WARNING", "Field confidence is below quality threshold."
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
            if v_status == "WARNING" or key in ("owner_name", "khasra_number", "survey_number", "area"):
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

# ---------------------------------------------------------
# STRUCTURED AI DECISION-SUPPORT
# ---------------------------------------------------------
class AIFieldAnalysisItem(BaseModel):
    field: str = Field(description="Target field name from schema")
    extracted_value: str = Field(description="Extracted cleaned value")
    interpretation: str = Field(description="Contextual or semantic explanation")
    requires_human_review: bool = Field(default=False)

class AIDecisionSupportSchema(BaseModel):
    document_classification: str = Field(default="Land Record")
    cleaned_ocr_summary: str = Field(description="Normalized view of raw OCR text")
    summary: str = Field(description="Executive summary of document contents")
    flags: List[str] = Field(default=[])
    field_analysis: List[AIFieldAnalysisItem] = Field(default=[])
    recommendation: str = Field(default="REVIEW_REQUIRED")
    explanation: str = Field(description="Objective narrative explaining detected inconsistencies")

def get_learned_corrections_context() -> str:
    try:
        with get_db() as db:
            cur = db.execute("SELECT field_id, wrong, right_val, count FROM corrections ORDER BY count DESC LIMIT 15")
            rows = cur.fetchall()
            if not rows:
                return ""
            rules = "\n".join([f"- For '{r['field_id']}', previous OCR read '{r['wrong']}' which was corrected to '{r['right_val']}'" for r in rows])
            return f"\nHISTORICAL HUMAN VERIFIER CORRECTIONS:\n{rules}\n"
    except Exception:
        return ""

async def run_ai_decision_support(raw_ocr_text: str, detected_lang: str) -> Tuple[Dict[str, Any], Dict[str, Any], str]:
    if not ai_client or not raw_ocr_text or len(raw_ocr_text.strip()) < 10:
        return {}, {}, ""

    learned_context = get_learned_corrections_context()
    prompt = f"""
    You are an AI Decision Support Assistant for Government Land Record Verification Officers (DILRMP).
    Assist the officer by:
    1. Cleaning up OCR noise and resolving Indic character misrecognitions.
    2. Classifying document type (Land Record, Mutation Record, Sale Deed, or Tax Receipt).
    3. Extracting all 16 cadastral parameters.
    4. Explaining ambiguities, semantic equivalents, and inconsistencies neutrally.
    5. Highlighting details requiring manual human confirmation.
    
    IMPORTANT: Do NOT automatically approve or reject official records.
    Recommendation must be: 'REVIEW_REQUIRED', 'CAUTION_DISCREPANCY', or 'ROUTINE_CLEAR'.
    
    {learned_context}
    DETECTED SCRIPT: {detected_lang}
    RAW OCR TEXT:
    \"\"\"{raw_ocr_text}\"\"\"
    """

    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIDecisionSupportSchema,
                temperature=0.0
            )
        )
        parsed_json = json.loads(response.text)

        raw_fields = {}
        for item in parsed_json.get("field_analysis", []):
            f_key = item.get("field", "")
            f_val = item.get("extracted_value", "").strip()
            if f_key in FIELD_KEYS:
                raw_fields[f_key] = {
                    "value": f_val,
                    "confidence": 0.90 if f_val else 0.0,
                    "requires_review": item.get("requires_human_review", False),
                    "interpretation": item.get("interpretation", "")
                }

        for k in FIELD_KEYS:
            if k not in raw_fields:
                raw_fields[k] = {"value": "", "confidence": 0.0}

        doc_classification = parsed_json.get("document_classification", "Land Record")
        raw_fields["document_type"] = {"value": doc_classification, "confidence": 0.95}

        decision_support = {
            "summary": parsed_json.get("summary", ""),
            "document_classification": doc_classification,
            "flags": parsed_json.get("flags", []),
            "field_analysis": parsed_json.get("field_analysis", []),
            "recommendation": parsed_json.get("recommendation", "REVIEW_REQUIRED"),
            "explanation": parsed_json.get("explanation", ""),
            "generated_at": time.time()
        }

        cleaned_text = parsed_json.get("cleaned_ocr_summary", raw_ocr_text)
        return raw_fields, decision_support, cleaned_text
    except Exception as e:
        print(f"[AI DECISION SUPPORT ERROR] {e}")
        return {}, {}, ""

def extract_entities_regex(text: str) -> Dict[str, Any]:
    text = (text or "").replace("\r\n", "\n")
    fields = {k: {"value": "", "confidence": 0.0} for k in FIELD_KEYS}
    numeric_keys = {"survey_number", "khasra_number", "khata_number", "plot_number", "mutation_no", "registration_no", "khatauni_year"}

    for key, labels in FIELD_LABELS.items():
        escaped = "|".join(re.escape(x) for x in labels)
        pat = rf"(?:{escaped})\s*[:：\-।|–—\s]?\s*([^\n\r\|;]+)"
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip(" \t:|-।–—|;")
            if key in numeric_keys:
                val = val.translate(INDIC_DIGIT_MAP)
                val = re.sub(r"[^\d\/\.\-]", "", val)
            if val and len(val) > 0:
                fields[key] = {"value": val, "confidence": 0.85}

    if not fields["owner_name"]["value"]:
        m_hon = re.search(r"\b(श्री|श्रीमती|శ్రీ|திரு|Shri|Smt|Mr\.)\s+([^\n,\|;]+)", text)
        if m_hon:
            fields["owner_name"] = {"value": m_hon.group(0).strip(" \t:|-।"), "confidence": 0.80}

    fields["document_type"] = {"value": "Land Record", "confidence": 0.80}
    return fields

async def parse_document_content(text: str, detected_lang: str, pages: int = 1) -> Dict[str, Any]:
    raw_fields, decision_support, cleaned_ocr = await run_ai_decision_support(text, detected_lang)

    if not raw_fields:
        raw_fields = extract_entities_regex(text)
        cleaned_ocr = text
        decision_support = {
            "summary": "Extracted via local pattern rules (AI offline).",
            "document_classification": "Land Record",
            "flags": ["Offline regex mode active"],
            "field_analysis": [],
            "recommendation": "REVIEW_REQUIRED",
            "explanation": "Automatic extraction completed without GenAI decision support. Human verification mandatory."
        }

    enriched_fields, validation_report = enrich_and_validate_fields(raw_fields)
    val_count = sum(1 for k, f in enriched_fields.items() if k != "document_type" and f.get("value"))
    mean_c = 94 if val_count >= 3 else (70 if text.strip() else 0)
    doc_type = raw_fields.get("document_type", {}).get("value", "Land Record")

    return {
        "mean_conf": mean_c,
        "languages": ["English", detected_lang] if detected_lang != "English" else ["English"],
        "pages": pages,
        "detected_language": detected_lang,
        "doc_type": doc_type,
        "fields": enriched_fields,
        "validation": validation_report,
        "ai_decision_support": decision_support,
        "ocr_text": text,
        "cleaned_ocr_text": cleaned_ocr
    }

# ---------------------------------------------------------
# COMPARISON & CONSISTENCY LOGIC
# ---------------------------------------------------------
COMPARISON_FIELDS = [
    ("owner_name", "Landowner Name"),
    ("survey_number", "Survey Number"),
    ("khasra_number", "Khasra Number"),
    ("khata_number", "Khata / Patta Number"),
    ("area", "Land Area / Extent"),
    ("village", "Village / Gram"),
    ("tehsil", "Tehsil / Mandal"),
    ("district", "District"),
    ("state", "State"),
    ("mutation_no", "Mutation Number"),
    ("registration_no", "Registration / Deed Number"),
    ("khatauni_year", "Record Year / Fasli")
]

class ComparisonDecisionReq(BaseModel):
    decision: str
    officer_notes: Optional[str] = ""

async def generate_ai_diff_explanation(diff_summary: List[Dict[str, Any]], meta_a: dict, meta_b: dict) -> str:
    if not ai_client:
        return "AI analysis unavailable (Gemini client unconfigured). Manual inspection required."

    changes_only = [d for d in diff_summary if d["status"] == "CHANGED"]
    if not changes_only:
        return "No discrepancies detected. All parameters match identically."

    prompt = f"""
    You are an objective land records auditing assistant assisting a Government Verification Officer.
    Compare Version A (Old) and Version B (New) differences:
    {json.dumps(changes_only, indent=2, ensure_ascii=False)}
    
    1. Explain what each difference signifies in revenue terms (sale, succession, area revision, etc.).
    2. Do NOT declare fraud. Highlight points for the Verification Officer to inspect.
    """

    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1)
        )
        return response.text.strip()
    except Exception as e:
        return f"Could not generate AI explanation: {e}"

def compute_document_diff(fields_a: dict, fields_b: dict) -> Dict[str, Any]:
    unchanged = []
    changed = []

    for key, label in COMPARISON_FIELDS:
        val_a = str(fields_a.get(key, {}).get("value", "") or "").strip()
        val_b = str(fields_b.get(key, {}).get("value", "") or "").strip()

        clean_a = re.sub(r"\s+", " ", val_a).lower()
        clean_b = re.sub(r"\s+", " ", val_b).lower()

        is_match = (clean_a == clean_b) and bool(clean_a)

        item = {
            "field": key,
            "label": label,
            "old_value": val_a if val_a else "—",
            "new_value": val_b if val_b else "—",
            "status": "UNCHANGED" if is_match else "CHANGED",
            "reason": ""
        }

        if not is_match:
            if key == "owner_name" and val_a and val_b:
                item["reason"] = "Ownership alteration (transfer, sale, or succession)"
            elif key == "area" and val_a and val_b:
                item["reason"] = "Plot area variance detected between versions"
            elif not val_a and val_b:
                item["reason"] = "Field added in current record"
            elif val_a and not val_b:
                item["reason"] = "Field missing in current record"
            else:
                item["reason"] = "Value mismatch between versions"
            changed.append(item)
        else:
            unchanged.append(item)

    return {
        "summary": {
            "total_checked": len(COMPARISON_FIELDS),
            "unchanged_count": len(unchanged),
            "changed_count": len(changed)
        },
        "unchanged": unchanged,
        "changed": changed
    }

CROSS_CHECK_FIELDS = [
    ("owner_name", "Owner Name", "Person / Entity Name"),
    ("survey_number", "Survey Number", "Cadastral Survey"),
    ("khasra_number", "Khasra Number", "Plot / Khasra Identifier"),
    ("area", "Land Area", "Parcel Extent / Measurement"),
    ("village", "Village / Gram", "Local Revenue Jurisdiction"),
    ("district", "District", "Administrative District"),
    ("tehsil", "Tehsil / Taluk", "Sub-District Division"),
    ("khatauni_year", "Dates / Year", "Record Date / Fasli / Transaction Year"),
    ("registration_no", "Document / Registration No", "Deed / Receipt / Registration Reference")
]

def clean_val(v: Any) -> str:
    s = str(v or "").strip()
    return re.sub(r"\s+", " ", s)

def are_strings_approx_equal(s1: str, s2: str) -> bool:
    if not s1 or not s2:
        return False
    norm1 = re.sub(r"\b(shri|smt|mr|mrs|sri|dr)\b", "", s1.lower()).replace(".", "").strip()
    norm2 = re.sub(r"\b(shri|smt|mr|mrs|sri|dr)\b", "", s2.lower()).replace(".", "").strip()
    if norm1 == norm2:
        return True
    num1 = re.findall(r"[-+]?\d*\.\d+|\d+", norm1)
    num2 = re.findall(r"[-+]?\d*\.\d+|\d+", norm2)
    if num1 and num2 and num1[0] == num2[0]:
        return True
    return False

def evaluate_cross_document_consistency(doc_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    field_reports = []
    matched_count = 0
    mismatched_count = 0
    missing_count = 0
    uncertain_count = 0

    for key, label, description in CROSS_CHECK_FIELDS:
        doc_values = []
        raw_non_empty = []

        for doc in doc_records:
            fields = doc.get("fields", {})
            val = clean_val(fields.get(key, {}).get("value", ""))
            doc_type = doc.get("doc_type") or doc.get("filename", "Record")
            doc_values.append({
                "doc_id": doc.get("id"),
                "doc_name": doc.get("filename"),
                "doc_type": doc_type,
                "value": val if val else "—"
            })
            if val:
                raw_non_empty.append(val)

        if len(raw_non_empty) == 0:
            status_code = "MISSING"
            reason = f"Parameter '{label}' was not found in any submitted document."
            missing_count += 1
        elif len(raw_non_empty) < len(doc_records):
            unique_vals = list(set([v.lower() for v in raw_non_empty]))
            if len(unique_vals) == 1:
                status_code = "MISSING"
                reason = f"Present in {len(raw_non_empty)} of {len(doc_records)} documents with identical value, but omitted in others."
                missing_count += 1
            else:
                status_code = "MISMATCH"
                reason = "Incomplete and conflicting values across documents."
                mismatched_count += 1
        else:
            unique_vals = list(set([v.lower() for v in raw_non_empty]))
            if len(unique_vals) == 1:
                status_code = "MATCH"
                reason = "Exact matching value verified across all related documents."
                matched_count += 1
            else:
                all_approx = True
                first = raw_non_empty[0]
                for other in raw_non_empty[1:]:
                    if not are_strings_approx_equal(first, other):
                        all_approx = False
                        break

                if all_approx:
                    status_code = "UNCERTAIN"
                    reason = "Minor spelling, honorific prefix, or unit variation detected. Requires human verification."
                    uncertain_count += 1
                else:
                    status_code = "MISMATCH"
                    reason = "Conflicting values detected across records."
                    mismatched_count += 1

        field_reports.append({
            "field": key,
            "label": label,
            "description": description,
            "status": status_code,
            "reason": reason,
            "values": doc_values
        })

    if mismatched_count > 0:
        overall = "MISMATCH_DETECTED"
    elif uncertain_count > 0:
        overall = "UNCERTAIN_NEEDS_REVIEW"
    elif missing_count > 0:
        overall = "INCOMPLETE_RECORDS"
    else:
        overall = "CONSISTENT"

    return {
        "overall_status": overall,
        "counts": {
            "matched": matched_count,
            "mismatched": mismatched_count,
            "missing": missing_count,
            "uncertain": uncertain_count,
            "total": len(CROSS_CHECK_FIELDS)
        },
        "fields": field_reports
    }

async def generate_ai_consistency_explanation(consistency_data: Dict[str, Any], doc_summaries: List[Dict[str, Any]]) -> str:
    if not ai_client:
        return "AI analysis unavailable (Gemini client unconfigured). Manual officer inspection required."

    mismatches = [f for f in consistency_data["fields"] if f["status"] in ("MISMATCH", "UNCERTAIN")]
    if not mismatches:
        return "All extracted cadastral parameters are consistent across the provided documents. Title chain exhibits no detected conflicts."

    prompt = f"""
    You are an objective Indian land records revenue auditor assisting a Government Verification Officer.
    Analyze cross-document consistency check across {len(doc_summaries)} related records:
    DOCUMENTS: {json.dumps(doc_summaries, ensure_ascii=False)}
    CONFLICTS: {json.dumps(mismatches, ensure_ascii=False)}
    
    1. Explain what each mismatch indicates in revenue context.
    2. Do NOT make a legal determination or declare fraud. Highlight points for the officer.
    """

    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1)
        )
        return response.text.strip()
    except Exception as e:
        return f"Could not generate AI explanation: {e}"

# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------
class LoginReq(BaseModel):
    email: str
    password: str

class SignupReq(BaseModel):
    full_name: str
    email: str
    password: str
    role: Optional[str] = ROLE_DATA_OFFICER

class AddUserReq(BaseModel):
    full_name: str
    email: str
    password: str
    role: str = ROLE_DATA_OFFICER

class ChangePassReq(BaseModel):
    current_password: str
    new_password: str

class ReviewActionReq(BaseModel):
    action: str  # "approve", "reject", "return"
    comments: Optional[str] = ""
    corrections: Optional[Dict[str, str]] = {}

class SaveDraftReq(BaseModel):
    fields: Dict[str, str]

class ConsistencyCheckReq(BaseModel):
    document_ids: List[str]

class ConsistencyDecisionReq(BaseModel):
    decision: str
    officer_notes: Optional[str] = ""

@app.get("/healthz")
@app.get("/api/health")
def healthcheck():
    return {
        "status": "healthy",
        "ai_enabled": ai_client is not None,
        "time": time.time()
    }

@app.post("/api/auth/login")
def login(req: LoginReq):
    clean_email = req.email.lower().strip()
    h = hashlib.sha256(req.password.encode()).hexdigest()
    with get_db() as db:
        cur = db.execute("SELECT * FROM users WHERE LOWER(email)=? AND password_hash=?", (clean_email, h))
        user = cur.fetchone()
        if not user or not user["is_active"]:
            raise HTTPException(status_code=400, detail="Invalid official credentials")
        normalized = normalize_role(user["role"])
        token = create_jwt_token(user["id"], normalized, user["version"])
        user_dict = {"id": user["id"], "full_name": user["full_name"], "email": user["email"], "role": normalized}

    log_audit(user_dict["full_name"], "LOGIN", f"User logged in: {clean_email} ({normalized})")
    return {"token": token, "user": user_dict}

@app.post("/api/auth/signup")
def signup(req: SignupReq):
    clean_email = req.email.lower().strip()
    if not clean_email or not req.password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    uid = uuid.uuid4().hex[:12]
    h = hashlib.sha256(req.password.encode()).hexdigest()
    assigned_role = normalize_role(req.role or ROLE_DATA_OFFICER)

    if assigned_role == ROLE_ADMIN:
        assigned_role = ROLE_DATA_OFFICER

    with get_db() as db:
        cur = db.execute("SELECT id FROM users WHERE LOWER(email)=?", (clean_email,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Email is already registered")
        db.execute(
            "INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)",
            (uid, req.full_name, clean_email, h, assigned_role)
        )

    log_audit(req.full_name, "SIGNUP", f"New user registered: {clean_email} [{assigned_role}]")
    token = create_jwt_token(uid, assigned_role, 0)
    return {"token": token, "user": {"id": uid, "full_name": req.full_name, "email": clean_email, "role": assigned_role}}

@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}

@app.post("/api/auth/logout")
def logout(user: dict = Depends(get_current_user)):
    log_audit(user["full_name"], "LOGOUT", "User logged out")
    return {"status": "ok"}

@app.post("/api/auth/change-password")
def change_password(req: ChangePassReq, user: dict = Depends(get_current_user)):
    cur_h = hashlib.sha256(req.current_password.encode()).hexdigest()
    new_h = hashlib.sha256(req.new_password.encode()).hexdigest()

    with get_db() as db:
        cur = db.execute("SELECT id, password_hash, version FROM users WHERE id=?", (user["id"],))
        db_user = cur.fetchone()
        if not db_user or db_user["password_hash"] != cur_h:
            raise HTTPException(status_code=400, detail="Current password is incorrect")
        new_ver = db_user["version"] + 1
        db.execute("UPDATE users SET password_hash=?, version=? WHERE id=?", (new_h, new_ver, user["id"]))

    log_audit(user["full_name"], "PASSWORD_CHANGE", "User changed account password")
    return {"status": "ok"}

@app.get("/api/users")
def list_users(user: dict = Depends(require_roles(ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT id, full_name, email, role, is_active FROM users ORDER BY full_name ASC")
        return {"users": [{
            "id": r["id"],
            "full_name": r["full_name"],
            "email": r["email"],
            "role": normalize_role(r["role"]),
            "is_active": r["is_active"]
        } for r in cur.fetchall()]}

@app.post("/api/users")
def add_user(req: AddUserReq, user: dict = Depends(require_roles(ROLE_ADMIN))):
    clean_email = req.email.lower().strip()
    uid = uuid.uuid4().hex[:12]
    h = hashlib.sha256(req.password.encode()).hexdigest()
    assigned_role = normalize_role(req.role)

    with get_db() as db:
        cur = db.execute("SELECT id FROM users WHERE LOWER(email)=?", (clean_email,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="User with this email already exists")
        db.execute(
            "INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)",
            (uid, req.full_name, clean_email, h, assigned_role)
        )
    log_audit(user["full_name"], "ADD_USER", f"Admin created user: {clean_email} ({assigned_role})")
    return {"status": "ok"}

@app.delete("/api/users/{target_uid}")
def delete_user(target_uid: str, user: dict = Depends(require_roles(ROLE_ADMIN))):
    with get_db() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (target_uid,))
    log_audit(user["full_name"], "DEACTIVATE_USER", f"Admin deactivated user: {target_uid}")
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
def get_languages():
    return {"languages": SUPPORTED_LANGUAGES}

@app.get("/api/samples")
def get_samples():
    samples_dir = os.path.join(BASE_DIR, "samples")
    os.makedirs(samples_dir, exist_ok=True)
    return {"samples": sorted([f for f in os.listdir(samples_dir) if not f.startswith(".")])}

# ---------------------------------------------------------
# INGESTION & DOCUMENT FILE STORAGE
# ---------------------------------------------------------
@app.post("/api/process/sample/{name}")
async def process_sample(
    name: str,
    doc_type: Optional[str] = Query("Land Record"),
    lang: Optional[str] = Query("auto"),
    user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_ADMIN))
):
    sample_path = os.path.join(BASE_DIR, "samples", os.path.basename(name))
    if not os.path.isfile(sample_path):
        raise HTTPException(status_code=404, detail="Sample not found")

    with open(sample_path, "rb") as f:
        data = f.read()

    doc_id = uuid.uuid4().hex[:12]
    now = time.time()
    effective_type = doc_type or "Land Record"

    stored_ext = ".pdf" if name.lower().endswith(".pdf") else ".png"
    stored_path = os.path.join(UPLOADS_DIR, f"{doc_id}{stored_ext}")
    with open(stored_path, "wb") as sf:
        sf.write(data)

    log_audit(user["full_name"], "DOC_INGESTION_START", f"Sample ingestion started: {name}", doc_id)

    img = clean_ocr_image(Image.open(io.BytesIO(data)))
    raw_text, detected_lang = await asyncio.to_thread(run_targeted_ocr, img, lang)
    parsed = await parse_document_content(raw_text, detected_lang, pages=1)

    effective_type = effective_type if effective_type != "Land Record" else parsed.get("doc_type", "Land Record")

    with get_db() as db:
        db.execute("""
        INSERT INTO documents (
            id, filename, doc_type, mean_conf, verdict, status, languages, pages, fields,
            validation, ai_decision_support, ocr_text, cleaned_ocr_text, detected_language, original_fields, uploaded_by,
            reviewer_comments, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            doc_id, name, effective_type, parsed["mean_conf"], parsed["validation"]["verdict"],
            STATUS_DRAFT, json.dumps(parsed["languages"]), 1,
            json.dumps(parsed["fields"], ensure_ascii=False),
            json.dumps(parsed["validation"], ensure_ascii=False),
            json.dumps(parsed["ai_decision_support"], ensure_ascii=False),
            parsed["ocr_text"], parsed["cleaned_ocr_text"], parsed["detected_language"],
            json.dumps(parsed["fields"], ensure_ascii=False),
            user["email"], "", now, now
        ))

    log_audit(user["full_name"], "STATUS_CHANGE", f"Status changed to {STATUS_DRAFT} after extraction: {name}", doc_id)

    return {
        "id": doc_id,
        "filename": name,
        "doc_type": effective_type,
        "status": STATUS_DRAFT,
        "ocr": {
            "mean_conf": parsed["mean_conf"],
            "languages": parsed["languages"],
            "pages": 1,
            "detected_language": parsed["detected_language"],
            "text_preview": parsed["ocr_text"],
            "cleaned_text": parsed["cleaned_ocr_text"]
        },
        "fields": parsed["fields"],
        "validation": parsed["validation"],
        "ai_decision_support": parsed["ai_decision_support"]
    }

@app.post("/api/process")
async def process_upload(
    file: UploadFile = File(...),
    doc_type: Optional[str] = Query("Land Record"),
    lang: Optional[str] = Query("auto"),
    user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_ADMIN))
):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")

    safe_filename = os.path.basename(file.filename or "uploaded_file")
    doc_id = uuid.uuid4().hex[:12]
    now = time.time()
    effective_type = doc_type or "Land Record"

    stored_ext = ".pdf" if safe_filename.lower().endswith(".pdf") else ".png"
    stored_path = os.path.join(UPLOADS_DIR, f"{doc_id}{stored_ext}")
    with open(stored_path, "wb") as sf:
        sf.write(content)

    log_audit(user["full_name"], "DOC_UPLOAD", f"Uploaded file: {safe_filename} (Status: {STATUS_PROCESSING})", doc_id)

    page_count = 1
    if safe_filename.lower().endswith(".pdf") and HAS_PDFIUM:
        pdf = pdfium.PdfDocument(content)
        page_count = len(pdf)
        raw_img = pdf[0].render(scale=1.5).to_pil()
    else:
        raw_img = Image.open(io.BytesIO(content))

    proc_img = clean_ocr_image(raw_img)
    raw_text, detected_lang = await asyncio.to_thread(run_targeted_ocr, proc_img, lang)
    parsed = await parse_document_content(raw_text, detected_lang, pages=page_count)

    effective_type = effective_type if effective_type != "Land Record" else parsed.get("doc_type", "Land Record")

    with get_db() as db:
        db.execute("""
        INSERT INTO documents (
            id, filename, doc_type, mean_conf, verdict, status, languages, pages, fields,
            validation, ai_decision_support, ocr_text, cleaned_ocr_text, detected_language, original_fields, uploaded_by,
            reviewer_comments, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            doc_id, safe_filename, effective_type, parsed["mean_conf"], parsed["validation"]["verdict"],
            STATUS_DRAFT, json.dumps(parsed["languages"]), page_count,
            json.dumps(parsed["fields"], ensure_ascii=False),
            json.dumps(parsed["validation"], ensure_ascii=False),
            json.dumps(parsed["ai_decision_support"], ensure_ascii=False),
            parsed["ocr_text"], parsed["cleaned_ocr_text"], parsed["detected_language"],
            json.dumps(parsed["fields"], ensure_ascii=False),
            user["email"], "", now, now
        ))

    log_audit(user["full_name"], "STATUS_CHANGE", f"Status changed to {STATUS_DRAFT} (Ingestion complete)", doc_id)

    return {
        "id": doc_id,
        "filename": safe_filename,
        "doc_type": effective_type,
        "status": STATUS_DRAFT,
        "ocr": {
            "mean_conf": parsed["mean_conf"],
            "languages": parsed["languages"],
            "pages": page_count,
            "detected_language": parsed["detected_language"],
            "text_preview": parsed["ocr_text"],
            "cleaned_text": parsed["cleaned_ocr_text"]
        },
        "fields": parsed["fields"],
        "validation": parsed["validation"],
        "ai_decision_support": parsed["ai_decision_support"]
    }

@app.get("/api/documents/{doc_id}/file")
def get_document_file(doc_id: str, user: dict = Depends(get_current_user)):
    for ext in [".pdf", ".png", ".jpg", ".jpeg"]:
        candidate = os.path.join(UPLOADS_DIR, f"{doc_id}{ext}")
        if os.path.isfile(candidate):
            media = "application/pdf" if ext == ".pdf" else "image/png"
            return FileResponse(candidate, media_type=media)
    raise HTTPException(status_code=404, detail="Original scan file not found on server")

@app.post("/api/documents/{doc_id}/save-draft")
def save_draft(doc_id: str, req: SaveDraftReq, user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT fields, status, uploaded_by FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")
        if user["role"] == ROLE_DATA_OFFICER and r["uploaded_by"] != user["email"]:
            raise HTTPException(status_code=403, detail="You can only edit your own submissions")
        if r["status"] in (STATUS_APPROVED, STATUS_REJECTED):
            raise HTTPException(status_code=400, detail=f"Cannot edit record in final status '{r['status']}'")

        fields = json.loads(r["fields"] or "{}")
        raw_to_revalidate = {}
        for k, v in req.fields.items():
            raw_to_revalidate[k] = {"value": v, "confidence": fields.get(k, {}).get("confidence", 1.0)}

        revalidated_fields, revalidated_report = enrich_and_validate_fields(raw_to_revalidate)

        now = time.time()
        db.execute(
            "UPDATE documents SET fields=?, validation=?, verdict=?, updated_at=? WHERE id=?",
            (json.dumps(revalidated_fields, ensure_ascii=False), json.dumps(revalidated_report, ensure_ascii=False), revalidated_report["verdict"], now, doc_id)
        )

    log_audit(user["full_name"], "SAVE_DRAFT", f"Draft saved and revalidated for record #{doc_id}", doc_id)
    return {"status": "ok", "fields": revalidated_fields, "validation": revalidated_report}

@app.post("/api/documents/{doc_id}/submit")
def submit_for_verification(doc_id: str, user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT status, uploaded_by FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")
        if user["role"] == ROLE_DATA_OFFICER and r["uploaded_by"] != user["email"]:
            raise HTTPException(status_code=403, detail="You can only submit your own documents")
        
        current_st = r["status"]
        if current_st not in (STATUS_DRAFT, STATUS_RETURNED):
            raise HTTPException(status_code=400, detail=f"Only {STATUS_DRAFT} or {STATUS_RETURNED} records can be submitted. Current status is {current_st}")

        now = time.time()
        db.execute("UPDATE documents SET status=?, updated_at=? WHERE id=?", (STATUS_PENDING_VERIFICATION, now, doc_id))

    log_audit(
        user["full_name"], 
        "STATUS_CHANGE", 
        f"Status changed from {current_st} to {STATUS_PENDING_VERIFICATION} (Submitted for Verification)", 
        doc_id
    )
    return {"status": "ok", "new_status": STATUS_PENDING_VERIFICATION}

@app.get("/api/documents/my-records")
def get_my_records(user: dict = Depends(require_roles(ROLE_DATA_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT * FROM documents WHERE uploaded_by=? ORDER BY created_at DESC", (user["email"],))
        return {"documents": [{
            "id": r["id"], "filename": r["filename"], "doc_type": r["doc_type"], "mean_conf": r["mean_conf"],
            "verdict": r["verdict"], "status": r["status"], "fields": json.loads(r["fields"] or "{}"),
            "reviewer_comments": r["reviewer_comments"], "created_at": r["created_at"]
        } for r in cur.fetchall()]}

# ---------------------------------------------------------
# VERIFICATION OFFICER QUEUE & STATUTORY DECISIONS
# ---------------------------------------------------------
@app.get("/api/documents/queue")
def get_verification_queue(user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT * FROM documents WHERE status=? ORDER BY created_at ASC", (STATUS_PENDING_VERIFICATION,))
        return {"queue": [{
            "id": r["id"], "filename": r["filename"], "doc_type": r["doc_type"], "mean_conf": r["mean_conf"],
            "verdict": r["verdict"], "status": r["status"], "uploaded_by": r["uploaded_by"],
            "fields": json.loads(r["fields"] or "{}"), "created_at": r["created_at"]
        } for r in cur.fetchall()]}

@app.post("/api/documents/{doc_id}/review-action")
def review_action(doc_id: str, req: ReviewActionReq, user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    action_clean = req.action.strip().lower()
    
    status_map = {
        "approve": STATUS_APPROVED,
        "reject": STATUS_REJECTED,
        "return": STATUS_RETURNED,
        "send_back": STATUS_RETURNED
    }
    if action_clean not in status_map:
        raise HTTPException(status_code=400, detail="Invalid action. Allowed actions: approve, reject, return")

    new_status = status_map[action_clean]

    if new_status in (STATUS_REJECTED, STATUS_RETURNED) and not (req.comments and req.comments.strip()):
        raise HTTPException(status_code=400, detail=f"Reviewer notes/reason are mandatory when marking as {new_status}")

    with get_db() as db:
        cur = db.execute("SELECT fields, status FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")

        current_st = r["status"]
        if current_st != STATUS_PENDING_VERIFICATION and user["role"] != ROLE_ADMIN:
            raise HTTPException(status_code=400, detail=f"Cannot act on document in status '{current_st}'. Must be in {STATUS_PENDING_VERIFICATION}")

        fields = json.loads(r["fields"] or "{}")

        if new_status == STATUS_APPROVED and req.corrections:
            for k, v in req.corrections.items():
                if k in fields:
                    old = fields[k].get("value", "")
                    fields[k]["value"] = v
                    fields[k]["confidence"] = 1.0
                    v_stat, v_msg = validate_single_field(k, v, 1.0)
                    fields[k]["validation_status"] = v_stat
                    fields[k]["validation_message"] = v_msg

                    if old and old.strip() != v.strip():
                        if db.is_pg:
                            db.execute("""
                            INSERT INTO corrections (field_id, wrong, right_val, count) VALUES (%s, %s, %s, 1)
                            ON CONFLICT (field_id, wrong, right_val) DO UPDATE SET count = corrections.count + 1
                            """, (k, old, v))
                        else:
                            db.execute("""
                            INSERT INTO corrections (field_id, wrong, right_val, count) VALUES (?, ?, ?, 1)
                            ON CONFLICT (field_id, wrong, right_val) DO UPDATE SET count = count + 1
                            """, (k, old, v))

        now = time.time()
        db.execute(
            "UPDATE documents SET status=?, fields=?, reviewer_comments=?, updated_at=? WHERE id=?",
            (new_status, json.dumps(fields, ensure_ascii=False), req.comments or "", now, doc_id)
        )

    action_label = "VERIFICATION_" + ("APPROVE" if new_status == STATUS_APPROVED else ("REJECT" if new_status == STATUS_REJECTED else "RETURN"))
    audit_detail = f"Status changed from {current_st} to {new_status}. Reason: {req.comments or 'None'}"
    log_audit(user["full_name"], action_label, audit_detail, doc_id)

    return {"status": "ok", "new_status": new_status, "fields": fields}

# ---------------------------------------------------------
# COMPARISON & CONSISTENCY ENDPOINTS
# ---------------------------------------------------------
@app.post("/api/documents/compare")
async def run_document_comparison(
    doc_a_id: Optional[str] = Query(None),
    doc_b_id: Optional[str] = Query(None),
    file_a: Optional[UploadFile] = File(None),
    file_b: Optional[UploadFile] = File(None),
    lang: Optional[str] = Query("auto"),
    user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    doc_a_meta, doc_b_meta = {}, {}
    fields_a, fields_b = {}, {}

    if file_a and file_b:
        content_a = await file_a.read()
        content_b = await file_b.read()

        async def process_file(content, filename):
            if filename.lower().endswith(".pdf") and HAS_PDFIUM:
                pdf = pdfium.PdfDocument(content)
                img = pdf[0].render(scale=1.5).to_pil()
            else:
                img = Image.open(io.BytesIO(content))
            proc_img = clean_ocr_image(img)
            text, detected = await asyncio.to_thread(run_targeted_ocr, proc_img, lang)
            return await parse_document_content(text, detected, pages=1)

        parsed_a, parsed_b = await asyncio.gather(
            process_file(content_a, file_a.filename or "old_doc"),
            process_file(content_b, file_b.filename or "new_doc")
        )

        id_a, id_b = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
        now = time.time()
        with get_db() as db:
            for doc_id, filename, parsed in [(id_a, file_a.filename, parsed_a), (id_b, file_b.filename, parsed_b)]:
                db.execute("""
                INSERT INTO documents (
                    id, filename, doc_type, mean_conf, verdict, status, languages, pages, fields,
                    validation, ai_decision_support, ocr_text, cleaned_ocr_text, detected_language, original_fields, uploaded_by,
                    reviewer_comments, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    doc_id, filename, parsed["doc_type"], parsed["mean_conf"], parsed["validation"]["verdict"],
                    STATUS_PENDING_VERIFICATION, json.dumps(parsed["languages"]), 1,
                    json.dumps(parsed["fields"], ensure_ascii=False),
                    json.dumps(parsed["validation"], ensure_ascii=False),
                    json.dumps(parsed["ai_decision_support"], ensure_ascii=False),
                    parsed["ocr_text"], parsed["cleaned_ocr_text"], parsed["detected_language"],
                    json.dumps(parsed["fields"], ensure_ascii=False),
                    user["email"], "Ingested for comparison", now, now
                ))

        doc_a_id, doc_b_id = id_a, id_b
        fields_a = parsed_a["fields"]
        fields_b = parsed_b["fields"]
        doc_a_meta = {"id": id_a, "filename": file_a.filename, "status": STATUS_PENDING_VERIFICATION}
        doc_b_meta = {"id": id_b, "filename": file_b.filename, "status": STATUS_PENDING_VERIFICATION}

    elif doc_a_id and doc_b_id:
        with get_db() as db:
            curA = db.execute("SELECT id, filename, fields, status, mean_conf FROM documents WHERE id=?", (doc_a_id,))
            rowA = curA.fetchone()
            curB = db.execute("SELECT id, filename, fields, status, mean_conf FROM documents WHERE id=?", (doc_b_id,))
            rowB = curB.fetchone()

            if not rowA or not rowB:
                raise HTTPException(status_code=404, detail="One or both comparison documents do not exist.")

            doc_a_meta = dict(rowA)
            doc_b_meta = dict(rowB)
            fields_a = json.loads(rowA["fields"] or "{}")
            fields_b = json.loads(rowB["fields"] or "{}")
    else:
        raise HTTPException(status_code=400, detail="Provide either (doc_a_id and doc_b_id) OR (file_a and file_b).")

    diff_result = compute_document_diff(fields_a, fields_b)
    ai_summary = await generate_ai_diff_explanation(
        diff_result["changed"] + diff_result["unchanged"],
        doc_a_meta,
        doc_b_meta
    )

    comp_id = uuid.uuid4().hex[:12]
    now = time.time()
    with get_db() as db:
        db.execute("""
        INSERT INTO comparisons (
            id, doc_a_id, doc_b_id, officer_name, officer_email,
            decision, officer_notes, diff_payload, ai_summary, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', '', ?, ?, ?)
        """, (
            comp_id, doc_a_id, doc_b_id, user["full_name"], user["email"],
            json.dumps(diff_result, ensure_ascii=False), ai_summary, now
        ))

    log_audit(user["full_name"], "COMPARE_DOCUMENTS", f"Compared #{doc_a_id} vs #{doc_b_id}", comp_id)

    return {
        "comparison_id": comp_id,
        "doc_a": doc_a_meta,
        "doc_b": doc_b_meta,
        "diff": diff_result,
        "ai_explanation": ai_summary
    }

@app.post("/api/documents/compare/{comparison_id}/decision")
def record_comparison_decision(
    comparison_id: str,
    req: ComparisonDecisionReq,
    user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    if req.decision not in ("approved", "flagged_discrepancy", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid decision.")

    with get_db() as db:
        cur = db.execute("SELECT id FROM comparisons WHERE id=?", (comparison_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Comparison record not found")

        db.execute("""
        UPDATE comparisons 
        SET decision=?, officer_notes=?, officer_name=?, officer_email=?
        WHERE id=?
        """, (req.decision, req.officer_notes or "", user["full_name"], user["email"], comparison_id))

    log_audit(user["full_name"], "COMPARISON_DECISION", f"Comparison #{comparison_id} decided: {req.decision}", comparison_id)
    return {"status": "ok", "comparison_id": comparison_id, "decision": req.decision}

@app.get("/api/documents/comparisons")
def list_comparisons(user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT * FROM comparisons ORDER BY created_at DESC LIMIT 50")
        return {"comparisons": [dict(r) for r in cur.fetchall()]}

@app.post("/api/consistency/check")
async def run_consistency_check(
    req: ConsistencyCheckReq,
    user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    if len(req.document_ids) < 2:
        raise HTTPException(status_code=400, detail="Consistency checking requires at least 2 related documents.")

    doc_records = []
    with get_db() as db:
        for doc_id in req.document_ids:
            cur = db.execute("SELECT id, filename, doc_type, status, fields, mean_conf FROM documents WHERE id=?", (doc_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Document ID '{doc_id}' not found.")
            doc_data = dict(row)
            doc_data["fields"] = json.loads(row["fields"] or "{}")
            doc_records.append(doc_data)

    report = evaluate_cross_document_consistency(doc_records)
    summaries = [{
        "id": d["id"],
        "filename": d["filename"],
        "doc_type": d["doc_type"],
        "status": d["status"],
        "fields": {k: d["fields"].get(k, {}).get("value", "") for k, _, _ in CROSS_CHECK_FIELDS}
    } for d in doc_records]

    ai_explanation = await generate_ai_consistency_explanation(report, summaries)

    check_id = uuid.uuid4().hex[:12]
    now = time.time()
    with get_db() as db:
        db.execute("""
        INSERT INTO consistency_checks (
            id, document_ids, overall_status, matched_count, mismatched_count,
            missing_count, uncertain_count, report_payload, ai_explanation,
            officer_name, officer_email, decision, officer_notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'flagged_for_verification', '', ?)
        """, (
            check_id, json.dumps(req.document_ids), report["overall_status"],
            report["counts"]["matched"], report["counts"]["mismatched"],
            report["counts"]["missing"], report["counts"]["uncertain"],
            json.dumps(report, ensure_ascii=False), ai_explanation,
            user["full_name"], user["email"], now
        ))

    log_audit(user["full_name"], "CROSS_DOC_CHECK", f"Checked {len(req.document_ids)} documents for consistency", check_id)

    return {
        "check_id": check_id,
        "documents": summaries,
        "report": report,
        "ai_explanation": ai_explanation,
        "decision": "flagged_for_verification"
    }

@app.post("/api/consistency/{check_id}/decision")
def record_consistency_decision(
    check_id: str,
    req: ConsistencyDecisionReq,
    user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))
):
    if req.decision not in ("approved", "flagged_discrepancy", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid decision.")

    with get_db() as db:
        cur = db.execute("SELECT id FROM consistency_checks WHERE id=?", (check_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Consistency report not found")

        db.execute("""
        UPDATE consistency_checks 
        SET decision=?, officer_notes=?, officer_name=?, officer_email=?
        WHERE id=?
        """, (req.decision, req.officer_notes or "", user["full_name"], user["email"], check_id))

    log_audit(user["full_name"], "CONSISTENCY_DECISION", f"Report #{check_id} decided as: {req.decision}", check_id)
    return {"status": "ok", "check_id": check_id, "decision": req.decision}

@app.get("/api/consistency/reports")
def list_consistency_reports(user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT * FROM consistency_checks ORDER BY created_at DESC LIMIT 50")
        return {"reports": [dict(r) for r in cur.fetchall()]}

@app.get("/api/consistency/{check_id}")
def get_consistency_report(check_id: str, user: dict = Depends(require_roles(ROLE_VERIFICATION_OFFICER, ROLE_ADMIN))):
    with get_db() as db:
        cur = db.execute("SELECT * FROM consistency_checks WHERE id=?", (check_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Consistency report not found")
        data = dict(row)
        data["document_ids"] = json.loads(row["document_ids"] or "[]")
        data["report"] = json.loads(row["report_payload"] or "{}")
        return data

# ---------------------------------------------------------
# DASHBOARDS: ROLE-SPECIFIC METRICS
# ---------------------------------------------------------
@app.get("/api/dashboard")
def get_dashboard(user: dict = Depends(get_current_user)):
    user_role = user.get("role", ROLE_VIEWER)

    if user_role == ROLE_VIEWER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dashboard view is disabled for public / viewer accounts."
        )

    with get_db() as db:
        today_start = datetime.combine(date.today(), datetime.min.time()).timestamp()

        if user_role == ROLE_VERIFICATION_OFFICER:
            cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_PENDING_VERIFICATION,))
            pending_ver = cur.fetchone()["c"]

            cur = db.execute("""
                SELECT COUNT(*) as c FROM documents 
                WHERE status=? AND (mean_conf < 75 OR verdict != 'valid')
            """, (STATUS_PENDING_VERIFICATION,))
            high_priority = cur.fetchone()["c"]

            cur = db.execute("""
                SELECT COUNT(*) as c FROM documents 
                WHERE status=? AND updated_at >= ?
            """, (STATUS_APPROVED, today_start))
            approved_today = cur.fetchone()["c"]

            cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_RETURNED,))
            returned = cur.fetchone()["c"]

            return {
                "portal_type": "VERIFICATION_OFFICER",
                "pending_verification": pending_ver,
                "high_priority": high_priority,
                "approved_today": approved_today,
                "returned": returned
            }

        if user_role == ROLE_ADMIN:
            cur = db.execute("SELECT COUNT(*) as c FROM documents")
            total_docs = cur.fetchone()["c"]

            cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE status != ?", (STATUS_DRAFT,))
            processed = cur.fetchone()["c"]

            cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_PENDING_VERIFICATION,))
            pending_ver = cur.fetchone()["c"]

            cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE status=?", (STATUS_APPROVED,))
            approved = cur.fetchone()["c"]

            cur = db.execute("SELECT AVG(mean_conf) as a FROM documents")
            avg_c = round(cur.fetchone()["a"] or 93.0, 1)

            cur = db.execute("""
                SELECT COUNT(*) as c FROM documents 
                WHERE verdict != 'valid' 
                OR ai_decision_support LIKE '%CAUTION_DISCREPANCY%' 
                OR ai_decision_support LIKE '%REVIEW_REQUIRED%'
            """)
            flagged_count = cur.fetchone()["c"]
            ai_flag_rate = round((flagged_count / max(1, total_docs)) * 100, 1)

            cur = db.execute("SELECT COUNT(*) as c FROM corrections")
            corr_count = cur.fetchone()["c"]
            human_corr_rate = round((corr_count / max(1, total_docs)) * 100, 1)

            cur = db.execute("SELECT detected_language, COUNT(*) as count FROM documents GROUP BY detected_language")
            by_lang = {r["detected_language"]: r["count"] for r in cur.fetchall() if r["detected_language"] != "unknown"}

            cur = db.execute("SELECT fields FROM documents LIMIT 500")
            by_district = {}
            for row in cur.fetchall():
                f = json.loads(row["fields"] or "{}")
                dist = f.get("district", {}).get("value", "").strip()
                if dist and dist != "—":
                    by_district[dist] = by_district.get(dist, 0) + 1

            return {
                "portal_type": "ADMIN",
                "total_documents": total_docs,
                "processed": processed,
                "pending_verification": pending_ver,
                "approved": approved,
                "ocr_average_confidence": f"{int(avg_c)}%",
                "ai_flag_rate": f"{int(ai_flag_rate)}%",
                "human_correction_rate": f"{int(human_corr_rate)}%",
                "documents_by_language": by_lang,
                "documents_by_district": by_district,
                "processing_statistics": {
                    "total": total_docs,
                    "processed_rate": f"{round((processed / max(1, total_docs)) * 100, 1)}%",
                    "active_verifiers": 1
                }
            }

        cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE uploaded_by=?", (user["email"],))
        my_total = cur.fetchone()["c"]
        cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE uploaded_by=? AND status=?", (user["email"], STATUS_DRAFT))
        my_drafts = cur.fetchone()["c"]
        cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE uploaded_by=? AND status=?", (user["email"], STATUS_RETURNED))
        my_returned = cur.fetchone()["c"]

        return {
            "portal_type": "DATA_OFFICER",
            "my_total": my_total,
            "my_drafts": my_drafts,
            "my_returned": my_returned
        }

@app.get("/api/documents")
def get_documents(user: dict = Depends(get_current_user)):
    user_role = user.get("role", ROLE_VIEWER)
    with get_db() as db:
        if user_role == ROLE_VIEWER:
            cur = db.execute("SELECT * FROM documents WHERE status=? ORDER BY created_at DESC", (STATUS_APPROVED,))
        elif user_role == ROLE_DATA_OFFICER:
            cur = db.execute("SELECT * FROM documents WHERE status=? OR uploaded_by=? ORDER BY created_at DESC", (STATUS_APPROVED, user["email"]))
        else:
            cur = db.execute("SELECT * FROM documents ORDER BY created_at DESC")

        return {"documents": [{
            "id": r["id"], "filename": r["filename"], "doc_type": r["doc_type"], "mean_conf": r["mean_conf"],
            "verdict": r["verdict"], "status": r["status"], "uploaded_by": r["uploaded_by"],
            "fields": json.loads(r["fields"] or "{}"), "created_at": r["created_at"]
        } for r in cur.fetchall()]}

@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, user: dict = Depends(get_current_user)):
    user_role = user.get("role", ROLE_VIEWER)
    with get_db() as db:
        cur = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")
        
        if user_role == ROLE_VIEWER and r["status"] != STATUS_APPROVED:
            raise HTTPException(status_code=403, detail="Access denied: Viewers can only inspect fully APPROVED records.")

        return {
            "id": r["id"], "filename": r["filename"], "doc_type": r["doc_type"], "mean_conf": r["mean_conf"],
            "status": r["status"], "languages": json.loads(r["languages"] or "[]"),
            "detected_language": r["detected_language"], "fields": json.loads(r["fields"] or "{}"),
            "uploaded_by": r["uploaded_by"], "reviewer_comments": r["reviewer_comments"],
            "ai_decision_support": json.loads(r["ai_decision_support"] or "{}"),
            "ocr_text": r["ocr_text"], "cleaned_ocr_text": r["cleaned_ocr_text"], "created_at": r["created_at"]
        }

@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, user: dict = Depends(require_roles(ROLE_ADMIN))):
    with get_db() as db:
        db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    log_audit(user["full_name"], "DELETE_RECORD", f"Deleted record #{doc_id}", doc_id)
    return {"status": "ok"}

css_dir = os.path.join(BASE_DIR, "css")
js_dir = os.path.join(BASE_DIR, "js")
assets_dir = os.path.join(BASE_DIR, "assets")

if os.path.exists(css_dir):
    app.mount("/css", StaticFiles(directory=css_dir), name="css")
if os.path.exists(js_dir):
    app.mount("/js", StaticFiles(directory=js_dir), name="js")
if os.path.exists(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

@app.get("/vectorflow.png", include_in_schema=False)
def serve_logo():
    candidates = [
        os.path.join(BASE_DIR, "vectorflow.png"),
        os.path.join(BASE_DIR, "assets", "vectorflow.png")
    ]
    for path in candidates:
        if os.path.isfile(path):
            return FileResponse(path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo not found")

@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    fav_path = os.path.join(BASE_DIR, "favicon.svg")
    if os.path.isfile(fav_path):
        return FileResponse(fav_path, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon not found")

@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)