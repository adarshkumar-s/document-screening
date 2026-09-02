import os
import io
import time
import json
import sqlite3
import hashlib
import re
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, UploadFile, File, Header, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

# PDF processing
try:
    import pypdfium2 as pdfium
    HAS_PDFIUM = True
except ImportError:
    HAS_PDFIUM = False

# Tesseract OCR integration
try:
    import pytesseract
    tesseract_cmd = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if os.path.isfile(tesseract_cmd):
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "land_records.db"))
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}
TESSERACT_LANGUAGE_CODES = ("eng", "hin", "tam", "tel", "mar", "guj", "ben", "pan", "kan", "ori", "urd")
LANGUAGE_NAMES = {
    "eng": "English", "hin": "Hindi", "tam": "Tamil", "tel": "Telugu", "mar": "Marathi",
    "guj": "Gujarati", "ben": "Bengali", "pan": "Punjabi", "kan": "Kannada", "ori": "Odia", "urd": "Urdu",
}
app = FastAPI(title="DILRMP Land Records Digitization API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# DATABASE INITIALIZATION
# ---------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with get_db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'operator',
            is_active INTEGER NOT NULL DEFAULT 1
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
        )
        """)
        document_columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
        if "detected_language" not in document_columns:
            conn.execute("ALTER TABLE documents ADD COLUMN detected_language TEXT NOT NULL DEFAULT 'unknown'")
        if "original_fields" not in document_columns:
            conn.execute("ALTER TABLE documents ADD COLUMN original_fields TEXT NOT NULL DEFAULT '{}'")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            field_id TEXT NOT NULL,
            wrong TEXT NOT NULL,
            right TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 1,
            UNIQUE(field_id, wrong, right)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            username TEXT NOT NULL,
            action TEXT NOT NULL,
            detail TEXT NOT NULL,
            doc_id INTEGER
        )
        """)
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email='admin@landrec.gov.in'")
        if not cur.fetchone():
            h = hashlib.sha256("Admin@123".encode()).hexdigest()
            conn.execute("INSERT INTO users (full_name, email, password_hash, role) VALUES (?, ?, ?, ?)",
                         ("System Administrator", "admin@landrec.gov.in", h, "admin"))
            conn.commit()

init_db()

# ---------------------------------------------------------
# AUTH HELPERS & SESSIONS
# ---------------------------------------------------------
SESSIONS: Dict[str, Dict[str, Any]] = {}

def get_current_user(authorization: Optional[str] = Header(None), token: Optional[str] = None):
    auth_token = None
    if authorization and authorization.startswith("Bearer "):
        auth_token = authorization.split(" ")[1]
    elif token:
        auth_token = token
    
    if not auth_token or auth_token not in SESSIONS:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return SESSIONS[auth_token]

# ---------------------------------------------------------
# REQUEST SCHEMAS
# ---------------------------------------------------------
class LoginReq(BaseModel):
    email: str
    password: str

class SignupReq(BaseModel):
    full_name: str
    email: str
    password: str

class ChangePassReq(BaseModel):
    current_password: str
    new_password: str

class VerifyReq(BaseModel):
    corrections: Dict[str, str]

class AddUserReq(BaseModel):
    full_name: str
    email: str
    password: str
    role: str

class UpdateUserReq(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None

# ---------------------------------------------------------
# INDIC OCR ENGINE & SCRIPT DISPATCHER
# ---------------------------------------------------------

INDIC_DIGIT_MAP = str.maketrans(
    "०१२३४५६७८९০১২৩৪৫৬৭৮৯٠١٢٣٤٥٦٧٨٩۰۱۲३۴۵۶۷۸۹",
    "0123456789012345678901234567890123456789"
)

FIELD_KEYS = (
    "owner_name", "father_name", "survey_number", "khasra_number",
    "khata_number", "plot_number", "area", "village", "tehsil",
    "district", "state", "land_class", "ownership_type",
    "mutation_no", "registration_no", "khatauni_year"
)

FIELD_LABELS = {
    "owner_name": [
        "Record Holder Name", "Landowner Name", "Land Owner Name", "Owner Name",
        "Record Holder", "Landowner", "Land Owner", "Owner",
        "भूमि स्वामी का नाम", "खातेदार का नाम", "भूमिधारक का नाम", "मालिक का नाम",
        "भूमि स्वामी", "खातेदार", "भूमिधारक", "मालिक",
        "জমির মালিকের নাম", "খতিয়ানধারীর নাম", "মালিকের নাম", "রায়তের নাম",
        "জমির মালিক", "খতিয়ানধারী", "রায়ত",
        "भूमिधारकाचे नाव", "खातेदाराचे नाव", "जमीन मालक", "मालकाचे नाव",
        "உரிமையாளர் பெயர்", "நில உரிமையாளர்", "பட்டாதாரர் பெயர்", "மாலிகர் பெயர்",
        "భూ యజమాని పేరు", "భూమి యజమాని", "పట్టాదారు పేరు", "యజమాని పేరు",
        "જમીન માલિકનું નામ", "ખાતેદારનું નામ", "માલિકનું નામ",
        "ਜ਼ਮੀਨ ਮਾਲਕ ਦਾ ਨਾਮ", "ਖਾਤੇਦਾਰ ਦਾ ਨਾਮ", "ਮਾਲਕ ਦਾ ਨਾਮ",
        "ಭೂ ಮಾಲೀಕರ ಹೆಸರು", "ಖಾತೆದಾರರ ಹೆಸರು", "ಮಾಲೀಕರ ಹೆಸರು",
        "ଜମି ମାଲିକଙ୍କ ନାମ", "ଖାତାଧାରୀଙ୍କ ନାମ",
        "زمین کے مالک کا نام", "مالک کا نام", "کھاتہ دار کا نام"
    ],
    "father_name": [
        "Father's Name", "Father Name", "Husband Name", "Guardian Name",
        "पिता का नाम", "पिता/पति", "पति का नाम", "पिता", "पति",
        "পিতার নাম", "স্বামীর নাম", "অভিভাবকের নাম", "পিতা", "স্বামী",
        "वडिलांचे नाव", "पतीचे नाव",
        "தந்தை பெயர்", "கணவர் பெயர்", "தந்தையின் பெயர்",
        "తండ్రి పేరు", "భర్త పేరు", "తండ్రి/భర్త పేరు",
        "પિતાનું નામ", "પતિનું નામ",
        "ਪਿਤਾ ਦਾ ਨਾਮ", "ਪਤੀ ਦਾ ਨਾਮ",
        "ತಂದೆಯ ಹೆಸರು", "ಗಂಡನ ಹೆಸರು",
        "ପିତାଙ୍କ ନାମ", "ସ୍ୱାମୀଙ୍କ ନାମ",
        "والد کا نام", "شوہر کا نام"
    ],
    "survey_number": [
        "Survey Number", "Survey No", "Survey",
        "सर्वे नंबर", "सर्वेक्षण संख्या", "सर्वे क्रमांक", "सर्वे नं",
        "সার্ভে নম্বর", "সার্ভে নং", "জরিপ নম্বর", "জরিপ নং",
        "சர்வே எண்", "சர்வே எண்.", "சர்வே நம்பர்",
        "సర్వే నంబర్", "సర్వే నెం",
        "સર્વે નંબર", "સર્વે ક્રમાંક",
        "ਸਰਵੇ ਨੰਬਰ", "ਸਰਵੇ ਨੰ.",
        "ಸರ್ವೆ ಸಂಖ್ಯೆ", "ಸರ್ವೇ ನಂಬರ್",
        "ସର୍ଭେ ନମ୍ବର",
        "سروے نمبر"
    ],
    "khasra_number": [
        "Khasra Number", "Khasra No", "Khasra",
        "खसरा नंबर", "खसरा संख्या", "खसरा क्रमांक", "खसरा नं", "खसरा",
        "খসড়া নম্বর", "খসরা নম্বর", "দাগ নম্বর", "দাগ নং",
        "கசரா எண்", "கச்ரா எண்",
        "ఖస్రా నంబర్",
        "ખસરા નંબર", "ખસરા ક્રમાંક",
        "ਖਸਰਾ ਨੰਬਰ",
        "ಖಸ್ರಾ ಸಂಖ್ಯೆ",
        "ଖସରା ନମ୍ବର",
        "خسرہ نمبر"
    ],
    "khata_number": [
        "Khata Number", "Khata No", "Khata",
        "खाता नंबर", "खाता संख्या", "खाता क्र.", "खाता नं", "खाता",
        "খাতা নম্বর", "খতিয়ান নম্বর", "খতিয়ান নং", "খাতা নং",
        "खाते क्रमांक", "खाता क्रमांक",
        "கணக்கு எண்", "கதா எண்", "காத்தா எண்",
        "ఖాతా నంబర్", "ఖాతా సంఖ్య",
        "ખાતા નંબર", "ખાતા ક્રમાંક",
        "ਖਾਤਾ ਨੰਬਰ", "ਖਾਤਾ ਨੰ.",
        "ಖಾತೆ ಸಂಖ್ಯೆ",
        "ଖାତା ନମ୍ବର",
        "کھاتہ نمبر"
    ],
    "plot_number": [
        "Plot Number", "Plot No", "Plot",
        "प्लॉट नंबर", "प्लॉट संख्या", "प्लॉट क्रमांक",
        "প্লট নম্বর", "প্লট নং", "দাগ নম্বর",
        "மனை எண்", "பிளாட் எண்",
        "ప్లాట్ నంబర్",
        "પ્લોટ નંબર",
        "ਪਲਾਟ ਨੰਬਰ",
        "ಪ್ಲಾಟ್ ಸಂಖ್ಯೆ",
        "ପ୍ଲଟ୍ ନମ୍ବର",
        "پلاٹ نمبر"
    ],
    "area": [
        "Plot Area", "Land Area", "Area", "Extent",
        "क्षेत्रफल", "रकबा", "जमीन क्षेत्रफल",
        "জমির পরিমাণ", "ক্ষেত্রফল", "কালি",
        "क्षेत्रफळ", "जमिनीचे क्षेत्रफळ",
        "பரப்பளவு", "நிலப்பரப்பு",
        "విస్తీర్ణం", "భూవిస్తీర్ణం",
        "વિસ્તાર", "જમીનનું ક્ષેત્રફળ",
        "ਰਕਬਾ", "ਖੇਤਰਫਲ",
        "ವಿಸ್ತೀರ್ಣ", "ಭೂ ವಿಸ್ತೀರ್ಣ",
        "କ୍ଷେତ୍ରଫଳ", "ଜମିର ପରିମାଣ",
        "رقبہ", "رقبہ/رقبہ"
    ],
    "village": [
        "Village Name", "Village", "Gram", "Mauza",
        "ग्राम", "गाँव", "गाव", "ग्रामाचे नाव", "मौजा",
        "গ্রাম", "গ্রামের নাম", "মৌজা",
        "கிராமம்", "கிராமத்தின் பெயர்",
        "గ్రామం", "గ్రామం పేరు",
        "ગામ", "ગામનું નામ",
        "ਪਿੰਡ", "ਪਿੰਡ ਦਾ ਨਾਮ",
        "ಗ್ರಾಮ", "ಗ್ರಾಮದ ಹೆಸರು",
        "ଗ୍ରାମ", "ଗାଁ",
        "گاؤں", "موضع"
    ],
    "tehsil": [
        "Tehsil", "Taluk", "Taluka", "Mandal", "Tahsil", "Block",
        "तहसील", "तालुका", "मंडल",
        "তহশিল", "উপজেলা", "ব্লক", "থানা",
        "தாலுகா", "வட்டம்",
        "తహసీల్", "తాలూకా", "మండలం",
        "તાલુકો", "તાલુકા",
        "ਤਹਿਸੀਲ", "ਤਾਲੂਕਾ",
        "ತಾಲ್ಲೂಕು", "ತಹಶೀಲ್ದಾರ್",
        "ତହସିଲ", "ତାଲୁକା",
        "تحصیل", "تعلقہ"
    ],
    "district": [
        "District Name", "District", "Dietrict",
        "जिला", "जिल्हा",
        "জেলা", "জেলার নাম",
        "மாவட்டம்", "மாவட்டத்தின் பெயர்",
        "జిల్లా", "జిల్లా పేరు",
        "જિલ્લો", "જિલ્લાનું નામ",
        "ਜ਼ਿਲ੍ਹਾ", "ਜ਼ਿਲ੍ਹੇ ਦਾ ਨਾਮ",
        "ಜಿಲ್ಲೆ", "ಜಿಲ್ಲೆಯ ಹೆಸರು",
        "ଜିଲ୍ଲା", "ଜିଲ୍ଲାର ନାମ",
        "ضلع", "ضلع کا نام"
    ],
    "state": [
        "State Name", "State",
        "राज्य",
        "রাজ্য",
        "மாநிலம்",
        "రాష్ట్రం",
        "રાજ્ય",
        "ਰਾਜ", "ਰਾਜ ਦਾ ਨਾਮ",
        "ರಾಜ್ಯ",
        "ରାଜ୍ୟ",
        "ریاست"
    ],
    "land_class": [
        "Land Classification", "Land Class", "Land Type", "Cand type",
        "भूमि का प्रकार", "भूमि प्रकार", "भू-वर्गीकरण", "श्रेणी", "किस्म जमीन",
        "জমির ধরন", "জমির শ্রেণী", "শ্রেণী",
        "நில வகை", "நிலத்தின் வகை",
        "భూమి రకం", "భూ వర్గీకరణ",
        "जमिनीचा प्रकार", "भूमीचा प्रकार",
        "જમીનનો પ્રકાર",
        "ਜ਼ਮੀਨ ਦੀ ਕਿਸਮ",
        "ಭೂಮಿಯ ಪ್ರಕಾರ",
        "ଜମିର ପ୍ରକାର",
        "زمین کی قسم"
    ],
    "ownership_type": [
        "Ownership Type", "Ownership",
        "स्वामित्व प्रकार", "स्वामित्व",
        "মালিকানার ধরন", "মালিকানা",
        "உரிமை வகை", "உரிமை",
        "యాజమాన్య రకం", "యాజమాన్యం",
        "मालकी हक्क", "मालकी प्रकार",
        "માલિકી પ્રકાર", "માલિકી",
        "ਮਾਲਕੀ ਕਿਸਮ", "ਮਾਲਕੀ",
        "ಮಾಲೀಕತ್ವದ ಪ್ರಕಾರ", "ಮಾಲೀಕತ್ವ",
        "ମାଲିକାନା ପ୍ରକାର", "ମାଲିକାନା",
        "ملکیت کی قسم", "ملکیت"
    ],
    "mutation_no": [
        "Mutation Number", "Mutation No", "Mutation",
        "नामांतरण संख्या", "नामांतरण नंबर", "दाखिल खारिज नंबर", "दाखिल खारिज",
        "নামজারি নম্বর", "নামজারি নং", "মিউটেশন নম্বর", "দাখিল খারিজ",
        "பட்டா மாற்றம் எண்", "மாற்று எண்",
        "మ్యూటేషన్ నంబర్", "మార్పిడి నంబర్",
        "फेरफार क्रमांक", "नामांतरण क्रमांक",
        "મ્યુટેશન નંબર", "નામ ફેરફાર નંબર",
        "ਮਿਊਟੇਸ਼ਨ ਨੰਬਰ", "ਇੰਤਕਾਲ ਨੰਬਰ",
        "ಮ್ಯುಟೇಶನ್ ಸಂಖ್ಯೆ", "ಖಾತೆ ಬದಲಾವಣೆ ಸಂಖ್ಯೆ",
        "ମ୍ୟୁଟେସନ ନମ୍ବର", "ନାମାନ୍ତରଣ ନମ୍ବର",
        "انتقال نمبر", "نام منتقلی نمبر"
    ],
    "registration_no": [
        "Registration Number", "Registration No", "Reg No",
        "पंजीकरण संख्या", "पंजीकरण नंबर",
        "নিবন্ধন নম্বর", "রেজিস্ট্রেশন নম্বর", "দলিল নম্বর", "দলিল নং",
        "பதிவு எண்", "பதிவு எண்ண",
        "రిజిస్ట్రేషన్ నంబర్", "రిజిస్ట్రేషన్ సంఖ్య",
        "नोंदणी क्रमांक", "नोंदणी नंबर",
        "નોંધણી નંબર", "રજિસ્ટ્રેશન નંબર",
        "ਰਜਿਸਟ੍ਰੇਸ਼ਨ ਨੰਬਰ", "ਰਜਿਸਟਰੀ ਨੰਬਰ",
        "ನೋಂದಣಿ ಸಂಖ್ಯೆ", "ರಿಜಿಸ್ಟ್ರೇಶನ್ ನಂಬರ್",
        "ପଞ୍ଜିକରଣ ନମ୍ବର", "ରେଜିଷ୍ଟ୍ରେସନ ନମ୍ବର",
        "رجسٹریشن نمبر", "اندراج نمبر"
    ],
    "khatauni_year": [
        "Khatauni Year", "Fasli Year", "Record Year", "Year",
        "खतौनी वर्ष", "फसली वर्ष", "वर्ष",
        "খতিয়ান বছর", "সন", "সাল", "বছর",
        "பட்டா ஆண்டு", "ஆண்டு",
        "ఖతౌని సంవత్సరం", "సంవత్సరం",
        "खतावणी वर्ष",
        "ખતૌની વર્ષ", "વર્ષ",
        "ਖਤੌਨੀ ਸਾਲ", "ਸਾਲ",
        "ಖಾತೆ ವರ್ಷ", "ವರ್ಷ",
        "ଖତିଆନ ବର୍ଷ", "ବର୍ଷ",
        "کھتونی سال", "سال"
    ],
}

LANGUAGE_SCRIPT_RANGES = {
    "Hindi": (0x0900, 0x097F),
    "Bengali": (0x0980, 0x09FF),
    "Marathi": (0x0900, 0x097F),
    "Tamil": (0x0B80, 0x0BFF),
    "Telugu": (0x0C00, 0x0C7F),
    "Gujarati": (0x0A80, 0x0AFF),
    "Punjabi": (0x0A00, 0x0A7F),
    "Kannada": (0x0C80, 0x0CFF),
    "Odia": (0x0B00, 0x0B7F),
    "Urdu": (0x0600, 0x06FF),
    "English": (0x0041, 0x007A),
}

ALL_LABEL_WORDS = []
for labels in FIELD_LABELS.values():
    ALL_LABEL_WORDS.extend(labels)
ALL_LABEL_WORDS.extend([
    "Dietrict", "Cand type", "Land type", "Tehsil", "Huzur", 
    "Owner name", "Record Holder", "Khasra", "Khata", "Survey", "Mutation"
])
ALL_LABEL_WORDS = sorted(list(set(ALL_LABEL_WORDS)), key=len, reverse=True)
LABEL_STOP_REGEX = r"(?=\s*(?:" + "|".join(re.escape(w) for w in ALL_LABEL_WORDS) + r")\s*[:：\-]|\n|$)"

def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t]+", " ", text)

def normalize_digits(text: str) -> str:
    return (text or "").translate(INDIC_DIGIT_MAP)

def clean_extracted_value(value: str, numeric: bool = False) -> str:
    value = (value or "").strip(" \t:|-")
    for lbl in [
        "Survey No", "Survey", "Khasra No", "Khasra", "Khata No", "Khata", 
        "Area", "Tehsil", "Dietrict", "District", "Owner name", "Owner", 
        "Mutation No", "Mutation", "Cand type", "Land type", "দাগ নং", "খতিয়ান নং"
    ]:
        m = re.search(rf"\b{re.escape(lbl)}\b\s*[:：\-]?", value, re.IGNORECASE)
        if m and m.start() > 0:
            value = value[:m.start()].strip()
            
    value = re.sub(r"\s{2,}", " ", value)
    if numeric:
        value = normalize_digits(value)
        value = re.sub(r"[^\d\/\.\-]", "", value)
    return value.strip(" \t:|-")

INVALID_NAME_PATTERNS = [
    r"^record\s*holder",
    r"^land\s*owner",
    r"^खातेदार",
    r"^भूमि\s*स्वामी",
    r"^মালিক",
    r"^রায়ত",
    r"^নাম\b",
    r"^name\b",
    r"^owner\b",
    r"^column\b",
    r"^col\b",
    r"^\(\d+\)$",
    r"^[\d\.\s\-\/\(\)]+$"
]

def is_valid_name(val: str) -> bool:
    if not val:
        return False
    clean = val.strip().lower()
    if len(clean) < 3:
        return False
    for pat in INVALID_NAME_PATTERNS:
        if re.search(pat, clean, re.IGNORECASE):
            return False
    if re.search(r"record\s*holder\s*\(\d+\)", clean):
        return False
    if re.search(r"खातेदार\s*\(\d+\)", clean):
        return False
    return True

def installed_tesseract_languages() -> list[str]:
    if not HAS_TESSERACT:
        return []
    try:
        langs = set(pytesseract.get_languages(config=""))
        return [code for code in TESSERACT_LANGUAGE_CODES if code in langs]
    except Exception:
        return []

def detect_language_from_text(text: str) -> str:
    scores = {}
    for name, (lo, hi) in LANGUAGE_SCRIPT_RANGES.items():
        count = sum(1 for ch in text if lo <= ord(ch) <= hi)
        if count:
            scores[name] = count
    if not scores:
        return "unknown"
    top = max(scores, key=scores.get)
    if top == "Hindi" and scores.get("Marathi", 0) == scores["Hindi"]:
        return "Hindi/Marathi"
    return top

def field_pattern(key: str) -> str:
    labels = sorted(FIELD_LABELS[key], key=len, reverse=True)
    escaped = "|".join(re.escape(x) for x in labels)
    return rf"(?:{escaped})\s*[:：\-]?\s*(.*?){LABEL_STOP_REGEX}"

def extract_fields_from_ocr(raw_text: str, filename: str = "", file_bytes: Optional[bytes] = None,
                            languages: Optional[list[str]] = None, pages: int = 1,
                            mean_ocr_conf: Optional[int] = None) -> Dict[str, Any]:
    text = normalize_text(raw_text)
    fields = {key: {"value": "", "confidence": 0.0} for key in FIELD_KEYS}
    numeric_keys = {
        "survey_number", "khasra_number", "khata_number", "plot_number",
        "mutation_no", "registration_no", "khatauni_year"
    }

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # 1. Regex Entity Matching
    for key in FIELD_KEYS:
        match = re.search(field_pattern(key), text, flags=re.IGNORECASE)
        if match:
            raw_val = match.group(1)
            value = clean_extracted_value(raw_val, numeric=(key in numeric_keys))
            if value:
                base = 0.88 if mean_ocr_conf is None else min(0.96, max(0.72, mean_ocr_conf / 100))
                fields[key] = {"value": value, "confidence": round(base, 2)}

    # 2. Heuristic Owner Name Extraction
    extracted_name = fields["owner_name"]["value"]
    if not is_valid_name(extracted_name):
        fields["owner_name"] = {"value": "", "confidence": 0.0}

        name_headers = [
            "record holder", "landowner", "land owner", "owner name",
            "खातेदार का नाम", "भूमि स्वामी", "खातेदार", "मालक", "பட்டாதாரர்",
            "মালিকের নাম", "রায়তের নাম", "খতিয়ানধারীর নাম"
        ]
        
        for idx, line in enumerate(lines):
            line_clean = line.lower().strip()
            if any(hdr in line_clean for hdr in name_headers):
                parts = re.split(r"[:：\-]", line, maxsplit=1)
                if len(parts) > 1 and is_valid_name(clean_extracted_value(parts[1])):
                    fields["owner_name"] = {"value": clean_extracted_value(parts[1]), "confidence": 0.88}
                    break

                for offset in (1, 2):
                    if idx + offset < len(lines):
                        candidate = clean_extracted_value(lines[idx + offset])
                        if is_valid_name(candidate) and not re.match(r"^\d+$", candidate):
                            fields["owner_name"] = {"value": candidate, "confidence": 0.86}
                            break
                if fields["owner_name"]["value"]:
                    break

    # 3. Honorific Title Fallback
    if not fields["owner_name"]["value"]:
        honorific_match = re.search(
            r"\b(श्री|श्रीमती|मोहम्मद|শ্রী|শ্রীমতি|Shri|Smt|Mr\.|Mrs\.)\s+([A-Za-z\u0900-\u0D7F]+(?:\s+[A-Za-z\u0900-\u0D7F]+){1,3})",
            text
        )
        if honorific_match and is_valid_name(honorific_match.group(0)):
            fields["owner_name"] = {"value": clean_extracted_value(honorific_match.group(0)), "confidence": 0.82}

    # 4. Rule Validation
    issues = []
    if not text.strip():
        issues.append({
            "severity": "error",
            "msg": "No OCR text was detected. Check image contrast, lighting, or language models."
        })
    if not fields["owner_name"]["value"]:
        issues.append({
            "severity": "warning",
            "msg": "Record-holder name was not detected. Please verify manually."
        })

    if mean_ocr_conf is not None and mean_ocr_conf < 70:
        issues.append({
            "severity": "warning",
            "msg": "Overall OCR confidence is low. Please verify extracted values."
        })

    conf_vals = [f["confidence"] for f in fields.values() if f["confidence"] > 0]
    mean_field_conf = (sum(conf_vals) / len(conf_vals) * 100) if conf_vals else 0
    mean_c = int(round(mean_ocr_conf if mean_ocr_conf is not None else mean_field_conf))
    verdict = "review" if issues or mean_c < 80 else "valid"

    return {
        "mean_conf": mean_c,
        "languages": languages or [],
        "pages": pages,
        "detected_language": detect_language_from_text(text),
        "fields": fields,
        "validation": {"verdict": verdict, "issues": issues},
        "ocr_text": text,
    }

# ---------------------------------------------------------
# IMAGE PREPROCESSING & TARGETED SCRIPT EXECUTION
# ---------------------------------------------------------
def preprocess_image(image: Image.Image) -> Image.Image:
    """Preprocesses document images to preserve Indic matras and fine strokes."""
    # 1. Orientation correction & convert to grayscale
    image = ImageOps.exif_transpose(image).convert("L")
    
    # 2. Upscale small images (Tesseract Indic works best at 1800px+ width)
    if image.width < 1800:
        scale = 1800 / max(1, image.width)
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS)

    # 3. Contrast enhancement & subtle sharpening for faded ink
    image = ImageOps.autocontrast(image, cutoff=2)
    enhancer = ImageEnhance.Sharpness(image)
    image = enhancer.enhance(1.4)
    return image

def ocr_page_with_lang(image: Image.Image, lang_code: str) -> tuple[str, float]:
    """Runs Tesseract with PSM 3 (automatic segmentation) to handle tabular land documents."""
    try:
        data = pytesseract.image_to_data(
            image,
            lang=lang_code,
            config="--oem 1 --psm 3",
            output_type=pytesseract.Output.DICT,
        )
    except Exception:
        return "", 0.0

    words = []
    confidences = []
    for i, word in enumerate(data.get("text", [])):
        word = (word or "").strip()
        if word:
            words.append(word)
            try:
                c = float(data["conf"][i])
                if c >= 0:
                    confidences.append(c)
            except Exception:
                pass
    text = " ".join(words)
    conf = sum(confidences) / len(confidences) if confidences else 0.0
    return text, conf

def get_best_ocr_result(image: Image.Image, installed: list[str]) -> tuple[str, float]:
    """
    Executes targeted language combinations rather than overloading all 11 scripts at once.
    This prevents cross-script corruption between Devanagari, Bengali, and Dravidian characters.
    """
    candidate_scripts = []
    if "hin" in installed:
        candidate_scripts.append("eng+hin")
    if "ben" in installed:
        candidate_scripts.append("eng+ben")
    if "mar" in installed and "eng+hin" not in candidate_scripts:
        candidate_scripts.append("eng+mar")
    
    # Fallback to English if none matched
    if not candidate_scripts:
        candidate_scripts.append("eng")

    best_text = ""
    best_conf = -1.0

    for cand in candidate_scripts:
        text, conf = ocr_page_with_lang(image, cand)
        if conf > best_conf:
            best_conf = conf
            best_text = text

    return best_text, max(0.0, best_conf)

def run_ocr_pipeline(file_bytes: Optional[bytes], filename: str) -> Dict[str, Any]:
    if not HAS_TESSERACT:
        raise HTTPException(status_code=503, detail="OCR engine is unavailable: pytesseract not found.")
    if not file_bytes:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")

    installed = installed_tesseract_languages()
    if "eng" not in installed:
        raise HTTPException(status_code=503, detail="Tesseract English language model (eng) is required.")

    try:
        images = []
        if filename.lower().endswith(".pdf"):
            if not HAS_PDFIUM:
                raise HTTPException(status_code=503, detail="PDF processing module (pypdfium2) not installed.")
            pdf = pdfium.PdfDocument(file_bytes)
            page_count = len(pdf)
            if page_count < 1:
                raise ValueError("PDF has no pages")
            max_pages = min(page_count, 10)
            for idx in range(max_pages):
                page = pdf[idx]
                images.append(page.render(scale=2.5).to_pil())
        else:
            image = Image.open(io.BytesIO(file_bytes))
            image.verify()
            image = Image.open(io.BytesIO(file_bytes))
            images = [image]

        page_texts = []
        page_confs = []
        for img in images:
            processed = preprocess_image(img)
            text, conf = get_best_ocr_result(processed, installed)
            page_texts.append(text)
            page_confs.append(conf)

        raw_text = "\n".join(t for t in page_texts if t).strip()
        mean_ocr_conf = int(round(sum(page_confs) / len(page_confs))) if page_confs else 0
        languages = [LANGUAGE_NAMES[c] for c in installed if c in LANGUAGE_NAMES]

        return extract_fields_from_ocr(
            raw_text,
            filename=filename,
            file_bytes=file_bytes,
            languages=languages,
            pages=len(images),
            mean_ocr_conf=mean_ocr_conf,
        )

    except pytesseract.TesseractNotFoundError:
        raise HTTPException(status_code=503, detail="Tesseract binary not found on the server.")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail="The uploaded file could not be parsed.") from exc

# ---------------------------------------------------------
# AUTHENTICATION ENDPOINTS
# ---------------------------------------------------------
@app.post("/api/auth/login")
def login(req: LoginReq):
    h = hashlib.sha256(req.password.encode()).hexdigest()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email=? AND password_hash=?", (req.email, h))
        user = cur.fetchone()
        if not user:
            raise HTTPException(status_code=400, detail="ईमेल या पासवर्ड गलत है (Invalid credentials)")
        if not user["is_active"]:
            raise HTTPException(status_code=403, detail="यह खाता निष्क्रिय है (Account disabled)")
        
        token_str = hashlib.sha256(f"{user['id']}-{time.time()}".encode()).hexdigest()
        user_dict = {
            "id": user["id"],
            "full_name": user["full_name"],
            "email": user["email"],
            "role": user["role"]
        }
        SESSIONS[token_str] = user_dict
        
        conn.execute("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), user["full_name"], "LOGIN", "लॉगिन सफल (Login successful)", None))
        conn.commit()
        return {"token": token_str, "user": user_dict}

@app.post("/api/auth/signup")
def signup(req: SignupReq):
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="पासवर्ड न्यूनतम 8 वर्णों का होना चाहिए (Min 8 chars)")
    h = hashlib.sha256(req.password.encode()).hexdigest()
    with get_db() as conn:
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO users (full_name, email, password_hash, role) VALUES (?, ?, ?, 'operator')",
                        (req.full_name, req.email, h))
            user_id = cur.lastrowid
            conn.commit()
            
            token_str = hashlib.sha256(f"{user_id}-{time.time()}".encode()).hexdigest()
            user_dict = {"id": user_id, "full_name": req.full_name, "email": req.email, "role": "operator"}
            SESSIONS[token_str] = user_dict
            
            conn.execute("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                         (time.time(), req.full_name, "SIGNUP", "नया खाता पंजीकृत (Account registered)", None))
            conn.commit()
            return {"token": token_str, "user": user_dict}
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="यह ईमेल पहले से पंजीकृत है (Email already exists)")

@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)):
    return {"user": user}

@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(None), token: Optional[str] = None,
           user: dict = Depends(get_current_user)):
    auth_token = None
    if authorization and authorization.startswith("Bearer "):
        auth_token = authorization.split(" ", 1)[1]
    elif token:
        auth_token = token
    if auth_token:
        SESSIONS.pop(auth_token, None)
    return {"status": "ok"}

@app.post("/api/auth/change-password")
def change_password(req: ChangePassReq, user: dict = Depends(get_current_user)):
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    curr_h = hashlib.sha256(req.current_password.encode()).hexdigest()
    new_h = hashlib.sha256(req.new_password.encode()).hexdigest()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE id=? AND password_hash=?", (user["id"], curr_h))
        if not cur.fetchone():
            raise HTTPException(status_code=400, detail="वर्तमान पासवर्ड गलत है (Incorrect current password)")
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (new_h, user["id"]))
        conn.execute("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), user["full_name"], "PASSWORD_CHANGE", "पासवर्ड अपडेट किया गया", None))
        conn.commit()
    return {"status": "ok"}

# ---------------------------------------------------------
# OCR PROCESS ENDPOINTS
# ---------------------------------------------------------
@app.post("/api/process")
async def process_upload(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    safe_filename = os.path.basename(file.filename or "")
    extension = os.path.splitext(safe_filename)[1].lower()
    if not safe_filename or extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Only PDF, JPG, PNG, and TIFF land-record files are accepted.")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="The uploaded file exceeds the 15 MB limit.")
    ocr_res = run_ocr_pipeline(content, safe_filename)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO documents (
            filename, mean_conf, verdict, status, languages, pages, fields,
            validation, ocr_text, detected_language, original_fields, created_at
        )
        VALUES (?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            safe_filename,
            ocr_res["mean_conf"],
            ocr_res["validation"]["verdict"],
            json.dumps(ocr_res["languages"], ensure_ascii=False),
            ocr_res["pages"],
            json.dumps(ocr_res["fields"], ensure_ascii=False),
            json.dumps(ocr_res["validation"], ensure_ascii=False),
            ocr_res["ocr_text"],
            ocr_res.get("detected_language", "unknown"),
            json.dumps(ocr_res["fields"], ensure_ascii=False),
            time.time()
        ))
        doc_id = cur.lastrowid
        conn.execute("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), user["full_name"], "UPLOAD_RECORD", f"दस्तावेज़ अपलोड: {safe_filename}", doc_id))
        conn.commit()

    return {
        "id": doc_id,
        "filename": safe_filename,
        "ocr": {
            "mean_conf": ocr_res["mean_conf"],
            "languages": ocr_res["languages"],
            "pages": ocr_res["pages"],
            "detected_language": ocr_res.get("detected_language", "unknown"),
            "text_preview": ocr_res["ocr_text"]
        },
        "fields": ocr_res["fields"],
        "validation": ocr_res["validation"]
    }

@app.get("/api/samples")
def get_samples(user: dict = Depends(get_current_user)):
    samples_dir = os.path.join(BASE_DIR, "samples")
    if not os.path.exists(samples_dir):
        os.makedirs(samples_dir, exist_ok=True)
    files = [f for f in os.listdir(samples_dir) if f.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff'))]
    return {"samples": sorted(files)}

@app.post("/api/process/sample/{name}")
def process_sample_file(name: str, user: dict = Depends(get_current_user)):
    samples_dir = os.path.join(BASE_DIR, "samples")
    safe_name = os.path.basename(name)
    if safe_name != name or os.path.splitext(safe_name)[1].lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Invalid sample file name")
    sample_path = os.path.join(samples_dir, safe_name)
    if not os.path.isfile(sample_path):
        raise HTTPException(status_code=404, detail="Sample file not found")

    with open(sample_path, "rb") as sample_file:
        ocr_res = run_ocr_pipeline(sample_file.read(), safe_name)
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO documents (
            filename, mean_conf, verdict, status, languages, pages, fields,
            validation, ocr_text, detected_language, original_fields, created_at
        )
        VALUES (?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            safe_name,
            ocr_res["mean_conf"],
            ocr_res["validation"]["verdict"],
            json.dumps(ocr_res["languages"], ensure_ascii=False),
            ocr_res["pages"],
            json.dumps(ocr_res["fields"], ensure_ascii=False),
            json.dumps(ocr_res["validation"], ensure_ascii=False),
            ocr_res["ocr_text"],
            ocr_res.get("detected_language", "unknown"),
            json.dumps(ocr_res["fields"], ensure_ascii=False),
            time.time()
        ))
        doc_id = cur.lastrowid
        conn.execute("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), user["full_name"], "PROCESS_SAMPLE", f"नमूना विश्लेषित: {safe_name}", doc_id))
        conn.commit()

    return {
        "id": doc_id,
        "filename": safe_name,
        "ocr": {
            "mean_conf": ocr_res["mean_conf"],
            "languages": ocr_res["languages"],
            "pages": ocr_res["pages"],
            "detected_language": ocr_res.get("detected_language", "unknown"),
            "text_preview": ocr_res["ocr_text"]
        },
        "fields": ocr_res["fields"],
        "validation": ocr_res["validation"]
    }

# ---------------------------------------------------------
# DASHBOARD & RECORDS
# ---------------------------------------------------------
@app.get("/api/dashboard")
def dashboard(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM documents")
        total = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM documents WHERE status='verified'")
        verified = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM documents WHERE status='pending_review'")
        pending = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM documents WHERE verdict='rejected'")
        rejected = cur.fetchone()["c"]
        cur.execute("SELECT AVG(mean_conf) as a FROM documents")
        avg_c = cur.fetchone()["a"] or 0

        by_state = {}
        by_district = {}
        cur.execute("SELECT fields FROM documents")
        for row in cur.fetchall():
            try:
                fields = json.loads(row["fields"])
            except Exception:
                continue
            state = (fields.get("state") or {}).get("value", "").strip()
            district = (fields.get("district") or {}).get("value", "").strip()
            if state:
                by_state[state] = by_state.get(state, 0) + 1
            if district:
                by_district[district] = by_district.get(district, 0) + 1

    return {
        "total": total,
        "avg_ocr_confidence": round(avg_c, 1),
        "auto_approved": max(0, total - pending - rejected),
        "pending_review": pending,
        "verified": verified,
        "rejected": rejected,
        "accuracy_estimate": round(avg_c, 1),
        "by_state": dict(sorted(by_state.items(), key=lambda x: (-x[1], x[0]))),
        "by_district": dict(sorted(by_district.items(), key=lambda x: (-x[1], x[0]))),
    }

@app.get("/api/documents")
def get_documents(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM documents ORDER BY id DESC")
        rows = cur.fetchall()
        docs = []
        for r in rows:
            docs.append({
                "id": r["id"],
                "filename": r["filename"],
                "mean_conf": r["mean_conf"],
                "verdict": r["verdict"],
                "status": r["status"],
                "fields": json.loads(r["fields"])
            })
    return {"documents": docs}

@app.get("/api/documents/{doc_id}")
def get_document_detail(doc_id: int, user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="अभिलेख नहीं मिला (Record not found)")
        return {
            "id": r["id"],
            "filename": r["filename"],
            "mean_conf": r["mean_conf"],
            "status": r["status"],
            "languages": r["languages"],
            "detected_language": r["detected_language"] if "detected_language" in r.keys() else "unknown",
            "fields": json.loads(r["fields"]),
            "ocr_text": r["ocr_text"]
        }

@app.post("/api/documents/{doc_id}/verify")
def verify_document(doc_id: int, req: VerifyReq, user: dict = Depends(get_current_user)):
    if user["role"] not in ["verifier", "admin"]:
        raise HTTPException(status_code=403, detail="सत्यापन अधिकार आवश्यक हैं (Verifier role required)")
    
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT fields FROM documents WHERE id=?", (doc_id,))
        r = cur.fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Document not found")
        
        fields = json.loads(r["fields"])
        for fid, new_val in req.corrections.items():
            if fid in fields:
                old_val = fields[fid]["value"]
                fields[fid]["value"] = new_val
                fields[fid]["confidence"] = 1.0
                
                if old_val and new_val and old_val != new_val:
                    cur.execute("""
                    INSERT INTO corrections (field_id, wrong, right, count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(field_id, wrong, right) DO UPDATE SET count=count+1
                    """, (fid, old_val, new_val))
        
        conn.execute("UPDATE documents SET status='verified', fields=? WHERE id=?", (json.dumps(fields), doc_id))
        conn.execute("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), user["full_name"], "VERIFY_RECORD", f"अभिलेख सत्यापित ({len(req.corrections)} सुधार)", doc_id))
        conn.commit()

    return {"status": "ok", "fields": fields}

@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="केवल प्रशासक हटा सकते हैं (Admin only)")
    with get_db() as conn:
        conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.execute("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                     (time.time(), user["full_name"], "DELETE_RECORD", f"अभिलेख #{doc_id} हटाया गया", doc_id))
        conn.commit()
    return {"status": "ok"}

# ---------------------------------------------------------
# AI CORRECTIONS & AUDIT
# ---------------------------------------------------------
@app.get("/api/corrections")
def get_corrections(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT field_id, wrong, right, count FROM corrections ORDER BY count DESC")
        return {"corrections": [dict(r) for r in cur.fetchall()]}

@app.get("/api/ai/training-data")
def get_training_data(user: dict = Depends(get_current_user)):
    if user["role"] not in ["verifier", "admin"]:
        raise HTTPException(status_code=403, detail="Verifier role required")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, languages, detected_language, ocr_text, fields, original_fields
            FROM documents
            WHERE status='verified'
            ORDER BY id DESC
        """)
        examples = []
        for r in cur.fetchall():
            examples.append({
                "document_id": r["id"],
                "languages": json.loads(r["languages"] or "[]"),
                "detected_language": r["detected_language"] or "unknown",
                "ocr_text": r["ocr_text"] or "",
                "verified_fields": json.loads(r["fields"] or "{}"),
                "original_ocr_fields": json.loads(r["original_fields"] or "{}"),
            })
    return {"examples": examples, "count": len(examples)}

@app.get("/api/audit")
def get_all_audit(user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 100")
        return {"audit": [dict(r) for r in cur.fetchall()]}

@app.get("/api/audit/{doc_id}")
def get_doc_audit(doc_id: int, user: dict = Depends(get_current_user)):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM audit WHERE doc_id=? ORDER BY id DESC", (doc_id,))
        return {"audit": [dict(r) for r in cur.fetchall()]}

# ---------------------------------------------------------
# USER MANAGEMENT (ADMIN)
# ---------------------------------------------------------
@app.get("/api/users")
def get_users(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, full_name, email, role, is_active FROM users")
        return {"users": [dict(r) for r in cur.fetchall()]}

@app.post("/api/users")
def add_user(req: AddUserReq, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if len(req.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    h = hashlib.sha256(req.password.encode()).hexdigest()
    with get_db() as conn:
        try:
            conn.execute("INSERT INTO users (full_name, email, password_hash, role) VALUES (?, ?, ?, ?)",
                         (req.full_name, req.email, h, req.role))
            conn.execute("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?, ?, ?, ?, ?)",
                         (time.time(), user["full_name"], "CREATE_USER", f"नया अधिकारी जोड़ा गया: {req.email}", None))
            conn.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="User email already exists")
    return {"status": "ok"}

@app.patch("/api/users/{user_id}")
def update_user_status(user_id: int, req: UpdateUserReq, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    with get_db() as conn:
        if req.role is not None:
            conn.execute("UPDATE users SET role=? WHERE id=?", (req.role, user_id))
        if req.is_active is not None:
            conn.execute("UPDATE users SET is_active=? WHERE id=?", (1 if req.is_active else 0, user_id))
        conn.commit()
    return {"status": "ok"}

# ---------------------------------------------------------
# FRONTEND STATIC ROUTING
# ---------------------------------------------------------
css_dir = os.path.join(BASE_DIR, "css")
js_dir = os.path.join(BASE_DIR, "js")
if os.path.exists(css_dir):
    app.mount("/css", StaticFiles(directory=css_dir), name="css")
if os.path.exists(js_dir):
    app.mount("/js", StaticFiles(directory=js_dir), name="js")

@app.get("/favicon.svg", include_in_schema=False)
def serve_favicon():
    return FileResponse(os.path.join(BASE_DIR, "favicon.svg"), media_type="image/svg+xml")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)