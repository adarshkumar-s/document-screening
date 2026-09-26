"""Explicit production runtime compatibility fixes loaded before FastAPI starts."""
import json
import os
import re
import time


def _json(value):
    try:
        return json.dumps(value or {}, ensure_ascii=False)
    except Exception:
        return "{}"


def _rv(row, key, index):
    try:
        return row[key]
    except Exception:
        try:
            return row[index]
        except Exception:
            return None


def _backfill_demo_parcels():
    """Backfill reference geometry and property-document links for existing DEMO-LI data."""
    try:
        from server import get_db
        with get_db() as db:
            props = db.execute(
                """SELECT property_id, parcel_id, village, survey_number, geometry, centroid
                   FROM properties WHERE property_id LIKE 'DEMO-LI-%'
                   ORDER BY village, property_id"""
            ).fetchall()
            village_seen = {}
            for row in props:
                property_id = _rv(row, "property_id", 0)
                village = str(_rv(row, "village", 2) or "DEMO")
                geometry = _rv(row, "geometry", 4)
                if geometry:
                    village_seen[village] = village_seen.get(village, 0) + 1
                    continue
                index = village_seen.get(village, 0)
                village_seen[village] = index + 1
                base_lon, base_lat = ((77.1000, 28.6200) if village.casefold() == "devnapur" else (77.1150, 28.6350))
                col, ring = index % 5, index // 5
                lon, lat = base_lon + col * 0.0038, base_lat + ring * 0.0038
                w, h = 0.0030, 0.0028
                geometry_json = json.dumps({"type":"Polygon","coordinates":[[[lon,lat],[lon+w,lat],[lon+w,lat+h],[lon,lat+h],[lon,lat]]]})
                centroid_json = json.dumps([lon+w/2, lat+h/2])
                db.execute(
                    """UPDATE properties SET geometry=?, centroid=?, crs='EPSG:4326',
                       georeferenced=0, geometry_source='Synthetic DEMO-LI reference geometry',
                       geometry_confidence=0.70, data_source='Synthetic DEMO-LI dataset (reference only)',
                       source_confidence=0.70, location_status='PARCEL_GEOMETRY',
                       location_source='Synthetic DEMO-LI reference geometry', location_confidence=0.70,
                       updated_at=? WHERE property_id=? AND geometry IS NULL""",
                    (geometry_json, centroid_json, time.time(), property_id),
                )

            docs = db.execute("SELECT id, fields FROM documents WHERE id LIKE 'DEMO-LI-%'").fetchall()
            for doc in docs:
                doc_id, raw_fields = _rv(doc, "id", 0), _rv(doc, "fields", 1)
                try:
                    fields = json.loads(raw_fields or "{}")
                except Exception:
                    fields = {}
                def fv(*keys):
                    for key in keys:
                        value = fields.get(key)
                        if isinstance(value, dict): value = value.get("value")
                        if str(value or "").strip(): return str(value).strip()
                    return ""
                survey, village = fv("survey_number", "khasra_number", "plot_number"), fv("village")
                if not survey or not village:
                    continue
                prop = db.execute(
                    """SELECT property_id FROM properties WHERE property_id LIKE 'DEMO-LI-%'
                       AND LOWER(COALESCE(survey_number,''))=LOWER(?)
                       AND LOWER(COALESCE(village,''))=LOWER(?) LIMIT 1""",
                    (survey, village),
                ).fetchone()
                if prop:
                    property_id = _rv(prop, "property_id", 0)
                    db.execute(
                        """INSERT INTO property_documents(property_id, document_id, source_type, linked_at)
                           VALUES (?,?,?,?) ON CONFLICT(property_id, document_id) DO NOTHING""",
                        (property_id, doc_id, "DEMO-LI-SEED-LINK", time.time()),
                    )
    except Exception as exc:
        print("[RUNTIME PATCH] DEMO-LI parcel backfill skipped:", type(exc).__name__, str(exc))


def apply():
    import ocr_pipeline as mod
    import pytesseract
    from PIL import ImageOps

    def cache_store(chash, user, source_doc_id, filename, lang, ocr_result, fields, validation, pages, metadata):
        mod.ensure_ocr_cache_table(); scope=mod._visibility_scope_for(user); dbmod=mod.get_server()
        with dbmod.get_db() as db:
            db.execute("""INSERT INTO ocr_cache (content_hash,owner_email,owner_role,visibility_scope,source_doc_id,filename,lang,ocr_text,cleaned_text,detected_language,confidence,fields,validation,ocr_method,pages,word_count,metadata_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (content_hash) DO UPDATE SET owner_email=excluded.owner_email,owner_role=excluded.owner_role,visibility_scope=excluded.visibility_scope,source_doc_id=excluded.source_doc_id,filename=excluded.filename,lang=excluded.lang,ocr_text=excluded.ocr_text,cleaned_text=excluded.cleaned_text,detected_language=excluded.detected_language,confidence=excluded.confidence,fields=excluded.fields,validation=excluded.validation,ocr_method=excluded.ocr_method,pages=excluded.pages,word_count=excluded.word_count,metadata_json=excluded.metadata_json""",
                (chash,str((user or {}).get("email") or "").lower(),str((user or {}).get("role") or "").upper(),scope,source_doc_id,os.path.basename(filename or "upload"),lang or "auto",ocr_result.get("text","") or "",ocr_result.get("cleaned_text",ocr_result.get("text","") ) or "",ocr_result.get("detected_language","eng") or "eng",float(ocr_result.get("confidence",0) or 0),_json(fields),_json(validation),ocr_result.get("method","tesseract_fast") or "tesseract_fast",int(pages or 1),int(ocr_result.get("word_count",0) or 0),_json(metadata),time.time()))

    def fast_ocr(image, requested_lang="auto"):
        requested=(requested_lang or "auto").strip().lower()
        try: candidates=mod.get_server()._ocr_languages(requested) if requested not in ("", "auto") else ["eng","hin"]
        except Exception: candidates=["eng","hin"]
        primary=candidates[0] if candidates else "eng"
        gray=ImageOps.autocontrast(ImageOps.grayscale(image))
        if max(gray.size)>3000:
            scale=3000/max(gray.size); gray=gray.resize((max(1,int(gray.width*scale)),max(1,int(gray.height*scale))))
        def recognize(img,language,psm):
            try: return (pytesseract.image_to_string(img,lang=language,config=f"--oem 1 --psm {psm}") or "").strip()
            except Exception: return ""
        text=recognize(gray,primary,6)
        if len(re.findall(r"\S+",text))<4:
            alt=recognize(gray,primary,11)
            if len(re.findall(r"\S+",alt))>len(re.findall(r"\S+",text)): text=alt
        if len(re.findall(r"\S+",text))<4 and len(candidates)>1:
            multi="+".join(candidates[:2]); alt=recognize(gray,multi,6)
            if len(re.findall(r"\S+",alt))>len(re.findall(r"\S+",text)): text,primary=alt,multi
        if len(re.findall(r"\S+",text))<2:
            alt=recognize(gray.point(lambda p:255 if p>180 else 0),primary,6)
            if len(re.findall(r"\S+",alt))>len(re.findall(r"\S+",text)): text=alt
        words=re.findall(r"\S+",text); detected=mod.get_server().detect_primary_script(text) or "eng"
        return {"text":text,"confidence":0.82 if len(words)>=8 else (0.60 if words else 0.0),"word_count":len(words),"detected_language":detected,"method":"tesseract_fast","strategy":{"lang":primary}}

    mod.cache_store=cache_store; mod.run_fast_ocr=fast_ocr; _backfill_demo_parcels()

    path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"index.html")
    try:
        html=open(path,encoding="utf-8").read(); app_start=html.find('<div id="appShell" class="hidden">')
        if app_start>=0:
            utility_start=html.find('<div class="gov-utility-bar">',app_start); header_start=html.find('<div class="gov-header">',utility_start)
            if utility_start>=0 and header_start>utility_start: open(path,"w",encoding="utf-8").write(html[:utility_start]+html[header_start:])
    except Exception as exc: print("[RUNTIME PATCH] logo cleanup skipped:",type(exc).__name__)
