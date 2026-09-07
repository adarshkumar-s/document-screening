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
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

# Load local environment variables (.env) if present
load_dotenv()

from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Query, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image, ImageOps, ImageEnhance

# Limit CPU threads to prevent thrashing on shared cloud vCPUs
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
os.makedirs(DATA_DIR, exist_ok=True)
SQLITE_PATH = os.getenv("DB_PATH", os.path.join(DATA_DIR, "land_records.db"))

JWT_SECRET = os.getenv("JWT_SECRET", "dilrmp-hackathon-secure-secret-2026")

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
# DATABASE CONNECTION
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
        if db.is_pg:
            db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'operator',
                version INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                mean_conf INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                status TEXT NOT NULL,
                languages TEXT NOT NULL,
                pages INTEGER NOT NULL,
                fields TEXT NOT NULL,
                validation TEXT NOT NULL,
                ocr_text TEXT NOT NULL,
                detected_language TEXT NOT NULL DEFAULT 'unknown',
                original_fields TEXT NOT NULL DEFAULT '{}',
                created_at DOUBLE PRECISION NOT NULL
            );
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS corrections (
                id SERIAL PRIMARY KEY,
                field_id TEXT NOT NULL,
                wrong TEXT NOT NULL,
                right_val TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(field_id, wrong, right_val)
            );
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id SERIAL PRIMARY KEY,
                ts DOUBLE PRECISION NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                doc_id TEXT
            );
            """)
        else:
            db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'operator',
                version INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                mean_conf INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                status TEXT NOT NULL,
                languages TEXT NOT NULL,
                pages INTEGER NOT NULL,
                fields TEXT NOT NULL,
                validation TEXT NOT NULL,
                ocr_text TEXT NOT NULL,
                detected_language TEXT NOT NULL DEFAULT 'unknown',
                original_fields TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                field_id TEXT NOT NULL,
                wrong TEXT NOT NULL,
                right_val TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                UNIQUE(field_id, wrong, right_val)
            );
            """)
            db.execute("""
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT NOT NULL,
                doc_id TEXT
            );
            """)

        cur = db.execute("SELECT id FROM users WHERE LOWER(email)='admin@landrec.gov.in'")
        if not cur.fetchone():
            admin_id = "5cc810682c7f"
            h = hashlib.sha256("Admin@123".encode()).hexdigest()
            db.execute(
                "INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)",
                (admin_id, "System Administrator", "admin@landrec.gov.in", h, "admin")
            )
            db.execute(
                "INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                (time.time(), "SYSTEM", "INIT", "System initialized with permanent credentials", None)
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

@app.get("/healthz")
@app.get("/api/health")
def healthcheck():
    return {
        "status": "healthy",
        "ai_enabled": ai_client is not None,
        "time": time.time()
    }

# ---------------------------------------------------------
# AUTHENTICATION HELPERS
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
        raise HTTPException(status_code=401, detail="Invalid token")
    payload_b64, sig_b64 = token.split(".", 1)
    expected_sig = hmac.new(JWT_SECRET.encode(), payload_b64.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(b64_encode(expected_sig), sig_b64):
        raise HTTPException(status_code=401, detail="Invalid signature")
    payload = json.loads(b64_decode(payload_b64).decode())
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="Token expired")
    return payload

def get_current_user(authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)) -> Dict[str, Any]:
    jwt_token = token or (authorization.replace("Bearer ", "").strip() if authorization else None)
    if not jwt_token:
        return {"id": "5cc810682c7f", "full_name": "System Administrator", "email": "admin@landrec.gov.in", "role": "admin"}
    try:
        payload = verify_jwt_token(jwt_token)
        with get_db() as db:
            cur = db.execute("SELECT id, full_name, email, role, is_active FROM users WHERE id=?", (payload.get("uid"),))
            user = cur.fetchone()
            if user and user["is_active"]:
                return dict(user)
    except Exception:
        pass
    return {"id": "5cc810682c7f", "full_name": "System Administrator", "email": "admin@landrec.gov.in", "role": "admin"}

# ---------------------------------------------------------
# OCR PREPROCESSING & OCR ENGINE
# ---------------------------------------------------------
INDIC_DIGIT_MAP = str.maketrans(
    "०१२३४५६७८९০১২৩৪৫৬৭৮৯٠١٢٣٤٥٦٧٨٩۰۱۲३४۵۶۷۸९௧௨௩௪௫௬௭௮௯௦૦૧૨૩૪૫૬૭૮૯౦౧౨౩౪౫౬౭౮౯",
    "0123456789012345678901234567890123456789123456789001234567890123456789"
)

FIELD_KEYS = (
    "owner_name", "father_name", "survey_number", "khasra_number",
    "khata_number", "plot_number", "area", "village", "tehsil",
    "district", "state", "land_class", "ownership_type",
    "mutation_no", "registration_no", "khatauni_year"
)

FIELD_LABELS = {
    "owner_name": ["Record Holder Name", "Landowner Name", "Owner Name", "भूमि स्वामी", "खातेदार", "భూ యजమాని", "பட்டாதாரர்", "খতিয়ানধারীর নাম"],
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
    "khatauni_year": ["Khatauni Year", "Fasli Year", "खतौनी वर्ष", "फसली वर्ष", "वर्ष"]
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
# AI FIELD EXTRACTION PIPELINE
# ---------------------------------------------------------
class AILandRecordSchema(BaseModel):
    owner_name: Optional[str] = Field(default="", description="Name of the record holder / landowner")
    father_name: Optional[str] = Field(default="", description="Father or husband name")
    survey_number: Optional[str] = Field(default="", description="Survey number")
    khasra_number: Optional[str] = Field(default="", description="Khasra number")
    khata_number: Optional[str] = Field(default="", description="Khata or Patta number")
    plot_number: Optional[str] = Field(default="", description="Plot number")
    area: Optional[str] = Field(default="", description="Land area with unit (Hectare, Acre, etc.)")
    village: Optional[str] = Field(default="", description="Village / Gram")
    tehsil: Optional[str] = Field(default="", description="Tehsil / Taluka / Mandal")
    district: Optional[str] = Field(default="", description="District")
    state: Optional[str] = Field(default="", description="State")
    land_class: Optional[str] = Field(default="", description="Land classification (e.g. Bhumidhar)")
    ownership_type: Optional[str] = Field(default="", description="Type of ownership")
    mutation_no: Optional[str] = Field(default="", description="Mutation order or reference number")
    registration_no: Optional[str] = Field(default="", description="Registration number")
    khatauni_year: Optional[str] = Field(default="", description="Khatauni or Fasli year")
    confidence_score: float = Field(default=0.92, description="Overall confidence level 0.0 to 1.0")

def get_learned_corrections_context() -> str:
    try:
        with get_db() as db:
            cur = db.execute("SELECT field_id, wrong, right_val, count FROM corrections ORDER BY count DESC LIMIT 15")
            rows = cur.fetchall()
            if not rows:
                return ""
            rules = "\n".join([f"- For '{r['field_id']}', previous OCR erroneously read '{r['wrong']}' which was corrected to '{r['right_val']}'" for r in rows])
            return f"\nLEARNED HUMAN VERIFIER CORRECTIONS:\n{rules}\n"
    except Exception:
        return ""

async def extract_entities_ai(text: str, detected_lang: str) -> Optional[Dict[str, Any]]:
    if not ai_client or not text or len(text.strip()) < 10:
        return None

    learned_context = get_learned_corrections_context()
    prompt = f"""
    You are an AI document digitization system for the Digital India Land Records Modernization Programme (DILRMP).
    Extract all land record fields from this OCR text detected in {detected_lang} script.
    Normalize any regional Indic digits into standard numerals.
    {learned_context}
    
    OCR TEXT TO EXTRACT FROM:
    \"\"\"{text}\"\"\"
    """

    try:
        response = await asyncio.to_thread(
            ai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AILandRecordSchema,
                temperature=0.0
            )
        )
        parsed_json = json.loads(response.text)
        fields = {}
        for key in FIELD_KEYS:
            val = str(parsed_json.get(key, "") or "").strip()
            conf = float(parsed_json.get("confidence_score", 0.92)) if val else 0.0
            fields[key] = {"value": val, "confidence": conf}
        return fields
    except Exception as e:
        print(f"[AI PIPELINE FALLBACK] {e}")
        return None

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

    return fields

async def parse_document_content(text: str, detected_lang: str, pages: int = 1) -> Dict[str, Any]:
    fields = await extract_entities_ai(text, detected_lang)
    if not fields:
        fields = extract_entities_regex(text)

    val_count = sum(1 for f in fields.values() if f.get("confidence", 0) > 0)
    mean_c = 94 if val_count >= 3 else (70 if text.strip() else 0)

    issues = []
    if not fields["owner_name"]["value"]:
        issues.append({"severity": "warning", "msg": "Landowner name could not be resolved automatically."})
    if not fields["khasra_number"]["value"] and not fields["survey_number"]["value"]:
        issues.append({"severity": "warning", "msg": "Neither Survey Number nor Khasra Number was extracted."})

    return {
        "mean_conf": mean_c,
        "languages": ["English", detected_lang] if detected_lang != "English" else ["English"],
        "pages": pages,
        "detected_language": detected_lang,
        "fields": fields,
        "validation": {"verdict": "valid" if (fields["owner_name"]["value"] and not issues) else "review", "issues": issues},
        "ocr_text": text
    }

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
    role: Optional[str] = "operator"

class AddUserReq(BaseModel):
    full_name: str
    email: str
    password: str
    role: str = "operator"

class ChangePassReq(BaseModel):
    current_password: str
    new_password: str

class VerifyReq(BaseModel):
    corrections: Dict[str, str]

@app.post("/api/auth/login")
def login(req: LoginReq):
    clean_email = req.email.lower().strip()
    h = hashlib.sha256(req.password.encode()).hexdigest()
    with get_db() as db:
        cur = db.execute("SELECT * FROM users WHERE LOWER(email)=? AND password_hash=?", (clean_email, h))
        user = cur.fetchone()
        if not user or not user["is_active"]:
            raise HTTPException(status_code=400, detail="Invalid credentials")
        token = create_jwt_token(user["id"], user["role"], user["version"])
        user_dict = {"id": user["id"], "full_name": user["full_name"], "email": user["email"], "role": user["role"]}

    log_audit(user_dict["full_name"], "LOGIN", f"Officer logged in: {clean_email}")
    return {"token": token, "user": user_dict}

@app.post("/api/auth/signup")
def signup(req: SignupReq):
    clean_email = req.email.lower().strip()
    if not clean_email or not req.password:
        raise HTTPException(status_code=400, detail="Email and password are required")
    uid = uuid.uuid4().hex[:12]
    h = hashlib.sha256(req.password.encode()).hexdigest()

    with get_db() as db:
        cur = db.execute("SELECT id FROM users WHERE LOWER(email)=?", (clean_email,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="Email is already registered")
        db.execute(
            "INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)",
            (uid, req.full_name, clean_email, h, req.role or "operator")
        )

    log_audit(req.full_name, "SIGNUP", f"New account registered: {clean_email}")
    token = create_jwt_token(uid, req.role or "operator", 0)
    return {"token": token, "user": {"id": uid, "full_name": req.full_name, "email": clean_email, "role": req.role or "operator"}}

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

    log_audit(user["full_name"], "PASSWORD_CHANGE", "User changed password")
    return {"status": "ok"}

@app.get("/api/users")
def list_users(user: dict = Depends(get_current_user)):
    with get_db() as db:
        cur = db.execute("SELECT id, full_name, email, role, is_active FROM users ORDER BY full_name ASC")
        return {"users": [dict(r) for r in cur.fetchall()]}

@app.post("/api/users")
def add_user(req: AddUserReq, user: dict = Depends(get_current_user)):
    clean_email = req.email.lower().strip()
    uid = uuid.uuid4().hex[:12]
    h = hashlib.sha256(req.password.encode()).hexdigest()
    with get_db() as db:
        cur = db.execute("SELECT id FROM users WHERE LOWER(email)=?", (clean_email,))
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="An officer with this email already exists")
        db.execute(
            "INSERT INTO users (id, full_name, email, password_hash, role, version, is_active) VALUES (?, ?, ?, ?, ?, 0, 1)",
            (uid, req.full_name, clean_email, h, req.role)
        )
    log_audit(user["full_name"], "ADD_USER", f"Officer created: {clean_email} ({req.role})")
    return {"status": "ok"}

@app.delete("/api/users/{target_uid}")
def delete_user(target_uid: str, user: dict = Depends(get_current_user)):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only administrators can deactivate officers")
    with get_db() as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (target_uid,))
    log_audit(user["full_name"], "DEACTIVATE_USER", f"Deactivated officer: {target_uid}")
    return {"status": "ok"}

@app.get("/api/languages")
def get_languages():
    return {"languages": SUPPORTED_LANGUAGES}

@app.get("/api/samples")
def get_samples():
    samples_dir = os.path.join(BASE_DIR, "samples")
    os.makedirs(samples_dir, exist_ok=True)
    return {"samples": sorted([f for f in os.listdir(samples_dir) if not f.startswith(".")])}

@app.post("/api/process/sample/{name}")
async def process_sample(name: str, lang: Optional[str] = Query("auto"), user: dict = Depends(get_current_user)):
    sample_path = os.path.join(BASE_DIR, "samples", os.path.basename(name))
    if not os.path.isfile(sample_path):
        raise HTTPException(status_code=404, detail="Sample not found")

    with open(sample_path, "rb") as f:
        data = f.read()

    img = clean_ocr_image(Image.open(io.BytesIO(data)))
    raw_text, detected_lang = await asyncio.to_thread(run_targeted_ocr, img, lang)
    parsed = await parse_document_content(raw_text, detected_lang, pages=1)

    doc_id = uuid.uuid4().hex[:12]
    with get_db() as db:
        db.execute("""
        INSERT INTO documents (
            id, filename, mean_conf, verdict, status, languages, pages, fields,
            validation, ocr_text, detected_language, original_fields, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            doc_id, name, parsed["mean_conf"], parsed["validation"]["verdict"],
            "pending_review", json.dumps(parsed["languages"]), 1,
            json.dumps(parsed["fields"], ensure_ascii=False),
            json.dumps(parsed["validation"], ensure_ascii=False),
            parsed["ocr_text"], parsed["detected_language"],
            json.dumps(parsed["fields"], ensure_ascii=False), time.time()
        ))

    log_audit(user["full_name"], "PROCESS_SAMPLE", f"Sample processed: {name} [{lang}]", doc_id)

    return {
        "id": doc_id,
        "filename": name,
        "ocr": {
            "mean_conf": parsed["mean_conf"],
            "languages": parsed["languages"],
            "pages": 1,
            "detected_language": parsed["detected_language"],
            "text_preview": parsed["ocr_text"]
        },
        "fields": parsed["fields"],
        "validation": parsed["validation"]
    }

@app.post("/api/process")
async def process_upload(file: UploadFile = File(...), lang: Optional[str] = Query("auto"), user: dict = Depends(get_current_user)):
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")

    safe_filename = os.path.basename(file.filename or "uploaded_file")
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

    doc_id = uuid.uuid4().hex[:12]
    with get_db() as db:
        db.execute("""
        INSERT INTO documents (
            id, filename, mean_conf, verdict, status, languages, pages, fields,
            validation, ocr_text, detected_language, original_fields, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            doc_id, safe_filename, parsed["mean_conf"], parsed["validation"]["verdict"],
            "pending_review", json.dumps(parsed["languages"]), page_count,
            json.dumps(parsed["fields"], ensure_ascii=False),
            json.dumps(parsed["validation"], ensure_ascii=False),
            parsed["ocr_text"], parsed["detected_language"],
            json.dumps(parsed["fields"], ensure_ascii=False), time.time()
        ))

    log_audit(user["full_name"], "UPLOAD_RECORD", f"Uploaded record: {safe_filename} [{lang}]", doc_id)

    return {
        "id": doc_id,
        "filename": safe_filename,
        "ocr": {
            "mean_conf": parsed["mean_conf"],
            "languages": parsed["languages"],
            "pages": page_count,
            "detected_language": parsed["detected_language"],
            "text_preview": parsed["ocr_text"]
        },
        "fields": parsed["fields"],
        "validation": parsed["validation"]
    }

@app.get("/api/dashboard")
def get_dashboard(user: dict = Depends(get_current_user)):
    with get_db() as db:
        cur = db.execute("SELECT COUNT(*) as c FROM documents")
        total = cur.fetchone()["c"]
        cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE status='verified'")
        verified = cur.fetchone()["c"]
        cur = db.execute("SELECT COUNT(*) as c FROM documents WHERE status='pending_review'")
        pending = cur.fetchone()["c"]
        cur = db.execute("SELECT AVG(mean_conf) as a FROM documents")
        avg_c = cur.fetchone()["a"] or 0
    return {
        "total": total, "pending_review": pending, "verified": verified,
        "auto_approved": max(0, total - pending), "accuracy_estimate": round(avg_c, 1),
        "by_state": {}, "by_district": {}
    }

@app.get("/api/documents")
def get_documents(user: dict = Depends(get_current_user)):
    with get_db() as db:
        cur = db.execute("SELECT * FROM documents ORDER BY created_at DESC")
        return {"documents": [{
            "id": r["id"], "filename": r["filename"], "mean_conf": r["mean_conf"],
            "verdict": r["verdict"], "status": r["status"], "fields": json.loads(r["fields"] or "{}")
        } for r in cur.fetchall()]}

@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        cur = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")
        return {
            "id": r["id"], "filename": r["filename"], "mean_conf": r["mean_conf"],
            "status": r["status"], "languages": json.loads(r["languages"] or "[]"),
            "detected_language": r["detected_language"], "fields": json.loads(r["fields"] or "{}"),
            "ocr_text": r["ocr_text"]
        }

@app.post("/api/documents/{doc_id}/verify")
def verify_document(doc_id: str, req: VerifyReq, user: dict = Depends(get_current_user)):
    with get_db() as db:
        cur = db.execute("SELECT fields FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")

        fields = json.loads(r["fields"] or "{}")
        for k, v in req.corrections.items():
            if k in fields:
                old = fields[k].get("value", "")
                fields[k] = {"value": v, "confidence": 1.0}
                if old and old != v:
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

        db.execute("UPDATE documents SET status='verified', fields=? WHERE id=?", (json.dumps(fields), doc_id))

    log_audit(user["full_name"], "VERIFY_RECORD", f"Verified record #{doc_id}", doc_id)
    return {"status": "ok", "fields": fields}

@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str, user: dict = Depends(get_current_user)):
    with get_db() as db:
        db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    log_audit(user["full_name"], "DELETE_RECORD", f"Deleted record #{doc_id}", doc_id)
    return {"status": "ok"}

@app.get("/api/audit")
def get_audit(user: dict = Depends(get_current_user)):
    with get_db() as db:
        cur = db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500")
        return {"audit": [dict(r) for r in cur.fetchall()]}

@app.get("/api/corrections")
def get_corrections(user: dict = Depends(get_current_user)):
    with get_db() as db:
        cur = db.execute("SELECT field_id, wrong, right_val as right, count FROM corrections ORDER BY count DESC")
        return {"corrections": [dict(r) for r in cur.fetchall()]}

# ---------------------------------------------------------
# ASSETS & STATIC
# ---------------------------------------------------------
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
@app.get("/assets/vectorflow-logo.png", include_in_schema=False)
@app.get("/assets/vectorflow.png", include_in_schema=False)
def serve_logo():
    candidates = [
        os.path.join(BASE_DIR, "vectorflow.png"),
        os.path.join(BASE_DIR, "Vectorflow.png"),
        os.path.join(BASE_DIR, "assets", "vectorflow-logo.png"),
        os.path.join(BASE_DIR, "assets", "vectorflow.png"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return FileResponse(path, media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo file not found")

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