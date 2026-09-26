"""Explicit production runtime compatibility fixes loaded before FastAPI starts."""
import io
import json
import math
import os
import re
import time


def _json(value):
    try:
        return json.dumps(value or {}, ensure_ascii=False)
    except Exception:
        return "{}"


def _backfill_demo_parcels():
    """Repair the existing DEMO-LI mapping rows without replacing the DB.

    The original DEMO-LI seed deliberately created properties with null
    geometry and did not link their documents to those properties. That makes
    the RBAC-safe parcel resolver correctly refuse to expose them. This
    migration derives clearly labelled *reference* rectangles from the
    existing synthetic DEMO-LI parcel identities and links existing demo
    documents by survey+village. No document/OCR data is changed.
    """
    try:
        from server import get_db
        with get_db() as db:
            props = db.execute(
                """SELECT property_id, parcel_id, village, survey_number, geometry, centroid
                   FROM properties
                   WHERE property_id LIKE 'DEMO-LI-%'
                   ORDER BY village, property_id"""
            ).fetchall()
            if not props:
                return

            village_counts = {}
            for row in props:
                village = str(row["village"] or "DEMO")
                village_counts.setdefault(village, 0)
                village_counts[village] += 1

            village_seen = {}
            for row in props:
                if row["geometry"]:
                    village_seen[str(row["village"] or "DEMO")] = village_seen.get(str(row["village"] or "DEMO"), 0) + 1
                    continue
                village = str(row["village"] or "DEMO")
                index = village_seen.get(village, 0)
                village_seen[village] = index + 1
                # Fictional reference-grid coordinates. They are intentionally
                # separate from exact document pins and are labelled as such.
                base_lon, base_lat = (77.1000, 28.6200) if village.casefold() == "devnapur".casefold() else (77.1150, 28.6350)
                col = index % 5
                ring = index // 5
                lon = base_lon + col * 0.0038
                lat = base_lat + ring * 0.0038
                w = 0.0030
                h = 0.0028
                geometry = json.dumps({
                    "type": "Polygon",
                    "coordinates": [[[lon, lat], [lon+w, lat], [lon+w, lat+h], [lon, lat+h], [lon, lat]]],
                }, ensure_ascii=False)
                centroid = json.dumps([lon + w/2, lat + h/2])
                db.execute(
                    """UPDATE properties SET geometry=?, centroid=?, crs='EPSG:4326',
                       georeferenced=0, geometry_source='Synthetic DEMO-LI reference geometry',
                       geometry_confidence=0.70, data_source='Synthetic DEMO-LI dataset (reference only)',
                       source_confidence=0.70, location_status='PARCEL_GEOMETRY',
                       location_source='Synthetic DEMO-LI reference geometry', location_confidence=0.70,
                       updated_at=? WHERE property_id=? AND geometry IS NULL""",
                    (geometry, centroid, time.time(), row["property_id"]),
                )

            # Link every existing DEMO-LI document to its canonical property.
            docs = db.execute(
                "SELECT id, fields FROM documents WHERE id LIKE 'DEMO-LI-%'"
            ).fetchall()
            for doc in docs:
                try:
                    fields = json.loads(doc["fields"] or "{}")
                except Exception:
                    fields = {}
                def fv(*keys):
                    for key in keys:
                        value = fields.get(key)
                        if isinstance(value, dict): value = value.get("value")
                        if str(value or "").strip(): return str(value).strip()
                    return ""
                survey = fv("survey_number", "khasra_number", "plot_number")
                village = fv("village")
                if not survey or not village:
                    continue
                prop = db.execute(
                    """SELECT property_id FROM properties
                       WHERE property_id LIKE 'DEMO-LI-%'
                         AND LOWER(COALESCE(survey_number,''))=LOWER(?)
                         AND LOWER(COALESCE(village,''))=LOWER(?)
                       LIMIT 1""",
                    (survey, village),
                ).fetchone()
                if prop:
                    db.execute(
                        """INSERT INTO property_documents(property_id, document_id, source_type, linked_at)
                           VALUES (?,?,?,?) ON CONFLICT(property_id, document_id) DO NOTHING""",
                        (prop["property_id"], doc["id"], "DEMO-LI-SEED-LINK", time.time()),
                    )
    except Exception as exc:
        print("[RUNTIME PATCH] DEMO-LI parcel backfill skipped:", type(exc).__name__, str(exc))


def apply():
    import ocr_pipeline as mod
    import pytesseract
    from PIL import Image, ImageOps, ImageFilter

    # ---------------------------------------------------------------
    # OCR: fast first pass, reliable fallback only when extraction is blank.
    # ---------------------------------------------------------------
    def cache_store(chash, user, source_doc_id, filename, lang, ocr_result, fields, validation, pages, metadata):
        mod.ensure_ocr_cache_table()
        scope = mod._visibility_scope_for(user)
        dbmod = mod.get_server()
        with dbmod.get_db() as db:
            db.execute(
                """
                INSERT INTO ocr_cache (
                    content_hash, owner_email, owner_role, visibility_scope, source_doc_id,
                    filename, lang, ocr_text, cleaned_text, detected_language, confidence,
                    fields, validation, ocr_method, pages, word_count, metadata_json, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (content_hash) DO UPDATE SET
                    owner_email=excluded.owner_email, owner_role=excluded.owner_role,
                    visibility_scope=excluded.visibility_scope, source_doc_id=excluded.source_doc_id,
                    filename=excluded.filename, lang=excluded.lang, ocr_text=excluded.ocr_text,
                    cleaned_text=excluded.cleaned_text, detected_language=excluded.detected_language,
                    confidence=excluded.confidence, fields=excluded.fields, validation=excluded.validation,
                    ocr_method=excluded.ocr_method, pages=excluded.pages, word_count=excluded.word_count,
                    metadata_json=excluded.metadata_json
                """,
                (
                    chash, str((user or {}).get("email") or "").lower(),
                    str((user or {}).get("role") or "").upper(), scope, source_doc_id,
                    os.path.basename(filename or "upload"), lang or "auto",
                    ocr_result.get("text", "") or "",
                    ocr_result.get("cleaned_text", ocr_result.get("text", "")) or "",
                    ocr_result.get("detected_language", "eng") or "eng",
                    float(ocr_result.get("confidence", 0) or 0), _json(fields), _json(validation),
                    ocr_result.get("method", "tesseract_fast") or "tesseract_fast",
                    int(pages or 1), int(ocr_result.get("word_count", 0) or 0), _json(metadata), time.time(),
                ),
            )

    def fast_ocr(image, requested_lang="auto"):
        requested = (requested_lang or "auto").strip().lower()
        try:
            candidates = mod.get_server()._ocr_languages(requested) if requested not in ("", "auto") else ["eng", "hin"]
        except Exception:
            candidates = ["eng", "hin"]
        primary = candidates[0] if candidates else "eng"

        gray = ImageOps.grayscale(image)
        if max(gray.size) > 3000:
            scale = 3000 / max(gray.size)
            gray = gray.resize((max(1, int(gray.width * scale)), max(1, int(gray.height * scale))))
        gray = ImageOps.autocontrast(gray)

        def recognize(img, language, psm):
            try:
                return (pytesseract.image_to_string(img, lang=language, config=f"--oem 1 --psm {psm}") or "").strip()
            except Exception:
                return ""

        # PSM 6 is fast and good for normal land-record scans. If it produces
        # little/no text, PSM 11 handles sparse forms without invoking Gemini.
        text = recognize(gray, primary, 6)
        if len(re.findall(r"\S+", text)) < 4:
            alt = recognize(gray, primary, 11)
            if len(re.findall(r"\S+", alt)) > len(re.findall(r"\S+", text)):
                text = alt
        # Hindi/English fallback only when the primary language produced no
        # useful result. This keeps ordinary English scans fast.
        if len(re.findall(r"\S+", text)) < 4 and len(candidates) > 1:
            multi = "+".join(candidates[:2])
            alt = recognize(gray, multi, 6)
            if len(re.findall(r"\S+", alt)) > len(re.findall(r"\S+", text)):
                text, primary = alt, multi
        # A final adaptive threshold is cheap and rescues faint/grey scans.
        if len(re.findall(r"\S+", text)) < 2:
            bw = gray.point(lambda p: 255 if p > 180 else 0)
            alt = recognize(bw, primary, 6)
            if len(re.findall(r"\S+", alt)) > len(re.findall(r"\S+", text)):
                text = alt

        words = re.findall(r"\S+", text)
        detected = mod.get_server().detect_primary_script(text) or "eng"
        confidence = 0.82 if len(words) >= 8 else (0.60 if words else 0.0)
        return {"text": text, "confidence": confidence, "word_count": len(words),
                "detected_language": detected, "method": "tesseract_fast", "strategy": {"lang": primary}}

    mod.cache_store = cache_store
    mod.run_fast_ocr = fast_ocr

    # ---------------------------------------------------------------
    # Existing DEMO-LI database repair: reference geometry + document links.
    # ---------------------------------------------------------------
    _backfill_demo_parcels()

    # ---------------------------------------------------------------
    # Remove only the duplicate logged-in utility-bar logo. Keep the main
    # portal header logo and the authentication screen logo.
    # ---------------------------------------------------------------
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    try:
        html = open(path, encoding="utf-8").read()
        app_start = html.find('<div id="appShell" class="hidden">')
        if app_start >= 0:
            utility_start = html.find('<div class="gov-utility-bar">', app_start)
            header_start = html.find('<div class="gov-header">', utility_start)
            if utility_start >= 0 and header_start > utility_start:
                html = html[:utility_start] + html[header_start:]
                open(path, "w", encoding="utf-8").write(html)
    except Exception as exc:
        print("[RUNTIME PATCH] logo cleanup skipped:", type(exc).__name__)
