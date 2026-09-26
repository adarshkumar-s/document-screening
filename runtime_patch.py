"""Production compatibility fixes plus controlled idempotent demo seeding."""
import io, json, os, re, time


def _json(value):
    try: return json.dumps(value or {}, ensure_ascii=False)
    except Exception: return "{}"


def _rowdict(row):
    try: return {key: row[key] for key in row.keys()}
    except Exception:
        try: return dict(row)
        except Exception: return {}


def _seed_demo_once_if_requested():
    if os.getenv("SEED_DEMO_LI_ON_STARTUP", "").strip().lower() != "true":
        return
    try:
        import server
        import demo_scenarios
        original_execute = server.DBConnection.execute
        def demo_execute(self, query, params=()):
            if self.is_pg and isinstance(query, str) and re.match(r"^\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\b", query, re.I):
                query = re.sub(r"^\s*INSERT\s+OR\s+(REPLACE|IGNORE)\s+INTO\b", "INSERT INTO", query, count=1, flags=re.I)
                query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
            return original_execute(self, query, params)
        server.DBConnection.execute = demo_execute
        try:
            result = demo_scenarios.seed_all(dry_run=False)
            print(f"[DEMO SEED] canonical seeder completed: {result.get('created', {})}")
        finally:
            server.DBConnection.execute = original_execute
    except Exception as exc:
        import traceback
        print("[DEMO SEED] canonical seeder failed:", type(exc).__name__, str(exc))
        traceback.print_exc()


def _backfill_demo_parcels():
    try:
        from server import get_db
        with get_db() as db:
            props = db.execute("SELECT property_id,parcel_id,village,survey_number,geometry FROM properties WHERE property_id LIKE 'DEMO-LI-%' ORDER BY village,property_id").fetchall()
            seen = {}
            for raw in props:
                row = _rowdict(raw); pid = row.get("property_id"); village = str(row.get("village") or "DEMO")
                if row.get("geometry"):
                    seen[village] = seen.get(village, 0) + 1; continue
                idx = seen.get(village, 0); seen[village] = idx + 1
                base_lon, base_lat = ((77.1000,28.6200) if village.casefold()=="devnapur" else (77.1150,28.6350))
                col, ring = idx % 5, idx // 5; lon, lat, w, h = base_lon+col*0.0038, base_lat+ring*0.0038, 0.0030, 0.0028
                geom = json.dumps({"type":"Polygon","coordinates":[[[lon,lat],[lon+w,lat],[lon+w,lat+h],[lon,lat+h],[lon,lat]]]}); cent=json.dumps([lon+w/2,lat+h/2])
                db.execute("UPDATE properties SET geometry=?,centroid=?,crs='EPSG:4326',georeferenced=0,geometry_source='Synthetic DEMO-LI reference geometry',geometry_confidence=0.70,data_source='Synthetic DEMO-LI dataset (reference only)',source_confidence=0.70,location_status='PARCEL_GEOMETRY',location_source='Synthetic DEMO-LI reference geometry',location_confidence=0.70,updated_at=? WHERE property_id=? AND geometry IS NULL",(geom,cent,time.time(),pid))
            docs=db.execute("SELECT id,fields FROM documents WHERE id LIKE 'DEMO-LI-%'").fetchall()
            for raw in docs:
                doc=_rowdict(raw)
                try: fields=json.loads(doc.get("fields") or "{}")
                except Exception: fields={}
                def fv(*keys):
                    for key in keys:
                        value=fields.get(key); value=value.get("value") if isinstance(value,dict) else value
                        if str(value or "").strip(): return str(value).strip()
                    return ""
                survey,village=fv("survey_number","khasra_number","plot_number"),fv("village")
                if not survey or not village: continue
                prop=_rowdict(db.execute("SELECT property_id FROM properties WHERE property_id LIKE 'DEMO-LI-%' AND LOWER(COALESCE(survey_number,''))=LOWER(?) AND LOWER(COALESCE(village,''))=LOWER(?) LIMIT 1",(survey,village)).fetchone())
                if prop.get("property_id"):
                    db.execute("INSERT INTO property_documents(property_id,document_id,source_type,linked_at) VALUES (?,?,?,?) ON CONFLICT(property_id,document_id) DO NOTHING",(prop["property_id"],doc.get("id"),"DEMO-LI-SEED-LINK",time.time()))
    except Exception as exc:
        print("[RUNTIME PATCH] DEMO-LI parcel backfill skipped:",type(exc).__name__,str(exc))


def apply():
    import ocr_pipeline as mod
    import pytesseract
    from PIL import ImageEnhance, ImageOps

    def cache_store(chash,user,source_doc_id,filename,lang,ocr_result,fields,validation,pages,metadata):
        mod.ensure_ocr_cache_table(); scope=mod._visibility_scope_for(user); dbmod=mod.get_server()
        with dbmod.get_db() as db:
            db.execute("""INSERT INTO ocr_cache(content_hash,owner_email,owner_role,visibility_scope,source_doc_id,filename,lang,ocr_text,cleaned_text,detected_language,confidence,fields,validation,ocr_method,pages,word_count,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(content_hash) DO UPDATE SET owner_email=excluded.owner_email,owner_role=excluded.owner_role,visibility_scope=excluded.visibility_scope,source_doc_id=excluded.source_doc_id,filename=excluded.filename,lang=excluded.lang,ocr_text=excluded.ocr_text,cleaned_text=excluded.cleaned_text,detected_language=excluded.detected_language,confidence=excluded.confidence,fields=excluded.fields,validation=excluded.validation,ocr_method=excluded.ocr_method,pages=excluded.pages,word_count=excluded.word_count,metadata_json=excluded.metadata_json""",(chash,str((user or {}).get("email") or "").lower(),str((user or {}).get("role") or "").upper(),scope,source_doc_id,os.path.basename(filename or "upload"),lang or "auto",ocr_result.get("text","") or "",ocr_result.get("cleaned_text",ocr_result.get("text","") ) or "",ocr_result.get("detected_language","eng") or "eng",float(ocr_result.get("confidence",0) or 0),_json(fields),_json(validation),ocr_result.get("method","tesseract_fast") or "tesseract_fast",int(pages or 1),int(ocr_result.get("word_count",0) or 0),_json(metadata),time.time()))

    def fast_ocr(image,requested_lang="auto"):
        requested=(requested_lang or "auto").strip().lower()
        try: candidates=mod.get_server()._ocr_languages(requested) if requested not in ("","auto") else ["eng","hin"]
        except Exception: candidates=["eng","hin"]
        candidates=[c for c in candidates if c]; primary=candidates[0] if candidates else "eng"
        base=ImageOps.exif_transpose(image).convert("L")
        if max(base.size)>3200:
            scale=3200/max(base.size); base=base.resize((max(1,int(base.width*scale)),max(1,int(base.height*scale))))
        variants=[ImageOps.autocontrast(base), ImageEnhance.Sharpness(ImageOps.autocontrast(base)).enhance(1.8), ImageOps.autocontrast(base).point(lambda p:255 if p>175 else 0)]
        def words(text): return len(re.findall(r"\S+",text or ""))
        def recognize(img,language,psm):
            try: return (pytesseract.image_to_string(img,lang=language,config=f"--oem 3 --psm {psm}") or "").strip()
            except Exception as exc: print(f"[OCR] tesseract attempt failed: {type(exc).__name__}"); return ""
        best=""; method="tesseract_fast"
        for label,img in zip(("gray","sharp","threshold"),variants):
            for psm in (6,11):
                text=recognize(img,primary,psm)
                if words(text)>words(best): best=text; method=f"tesseract_{label}_psm{psm}"
                if words(best)>=8: break
            if words(best)>=8: break
        if words(best)<4 and len(candidates)>1:
            multi="+".join(candidates[:2])
            for img in variants[:2]:
                text=recognize(img,multi,6)
                if words(text)>words(best): best=text; primary=multi; method="tesseract_multi"
        if words(best)<2:
            try:
                fallback=mod.get_server().run_guided_ocr(base,{"lang":primary,"psm":6,"rotation":0,"enhance":True,"denoise":False})
                text=(fallback.get("text") or "").strip()
                if words(text)>words(best): best=text; method="tesseract_guided_fallback"
            except Exception as exc: print(f"[OCR] guided fallback failed: {type(exc).__name__}")
        detected=mod.get_server().detect_primary_script(best) or "eng"; wc=words(best)
        print(f"[OCR] result words={wc} method={method} lang={primary}")
        return {"text":best,"confidence":0.84 if wc>=8 else (0.62 if wc>=3 else 0.0),"word_count":wc,"detected_language":detected,"method":method,"strategy":{"lang":primary}}

    mod.cache_store=cache_store; mod.run_fast_ocr=fast_ocr
    _seed_demo_once_if_requested(); _backfill_demo_parcels()
    path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"index.html")
    try:
        html=open(path,encoding="utf-8").read(); app_start=html.find('<div id="appShell" class="hidden">')
        if app_start>=0:
            utility_start=html.find('<div class="gov-utility-bar">',app_start); header_start=html.find('<div class="gov-header">',utility_start)
            if utility_start>=0 and header_start>utility_start: open(path,"w",encoding="utf-8").write(html[:utility_start]+html[header_start:])
    except Exception as exc: print("[RUNTIME PATCH] logo cleanup skipped:",type(exc).__name__)
