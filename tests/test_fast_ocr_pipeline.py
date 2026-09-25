"""Regression tests for the fast OCR pipeline, OCR cache, and bulk processing."""
import io
import json
import os
import time

from fastapi.testclient import TestClient
from PIL import Image

os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "Admin@123")
os.environ.setdefault("APP_ENV", "test")
import server
import ocr_pipeline


def login(client):
    r = client.post("/api/auth/login", json={"email": "admin@landrec.gov.in", "password": "Admin@123"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


def _make_png(color=(255, 255, 255), size=(200, 200)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_text_pdf_bytes(text: str = "Owner Name: Test Owner\nVillage: Testville\nDistrict: Pune\nSurvey Number: 45/2\nArea: 1.25 Acres"):
    """Create a minimal in-memory PDF using reportlab if available, else a fake PDF with text via pypdfium2...
    We simply write a small text-containing PDF using pypdfium2-free method via reportlab if present."""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import letter
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        y = 750
        for line in text.split("\n"):
            c.drawString(72, y, line)
            y -= 14
        c.save()
        return buf.getvalue()
    except Exception:
        return None


def test_pdf_text_layer_bypasses_tesseract_when_embedded_text_present(monkeypatch, tmp_path):
    server.DB_PATH = str(tmp_path / "textlayer.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    pdf_bytes = _make_text_pdf_bytes()
    if not pdf_bytes:
        import pytest
        pytest.skip("reportlab not available for PDF fixture")

    # Spy on run_guided_ocr to ensure it is NOT called for text PDFs
    called = {"n": 0}
    orig = server.run_guided_ocr
    def spy(*a, **kw):
        called["n"] += 1
        return orig(*a, **kw)
    monkeypatch.setattr(server, "run_guided_ocr", spy)

    r = client.post("/api/process?mode=ocr_only", headers=headers,
                    files={"file": ("text-doc.pdf", pdf_bytes, "application/pdf")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert called["n"] == 0, "Tesseract should NOT run when PDF has usable embedded text"
    assert j["pipeline_meta"]["ocr_method"] == "pdf_text_layer"
    assert "Test Owner" in (j["fields"].get("owner_name", {}).get("value", "") or "")


def test_image_requires_ocr(monkeypatch, tmp_path):
    server.DB_PATH = str(tmp_path / "image-ocr.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    called = {"n": 0}
    orig = server.run_guided_ocr
    def spy(*a, **kw):
        called["n"] += 1
        return orig(*a, **kw)
    monkeypatch.setattr(server, "run_guided_ocr", spy)
    png = _make_png()
    r = client.post("/api/process?mode=ocr_only", headers=headers, files={"file": ("img.png", png, "image/png")})
    assert r.status_code == 200
    assert called["n"] >= 1, "Images must be sent to OCR"


def test_ocr_cache_hit_returns_same_result(monkeypatch, tmp_path):
    server.DB_PATH = str(tmp_path / "cache1.db")
    server.init_db()
    ocr_pipeline.CACHE_TABLE_READY = False
    ocr_pipeline.ensure_ocr_cache_table()
    client = TestClient(server.app)
    headers = login(client)
    png = _make_png()
    r1 = client.post("/api/process?mode=ocr_only", headers=headers, files={"file": ("a.png", png, "image/png")})
    assert r1.status_code == 200
    ocr_count_1 = getattr(server, "_ocr_call_count", 0)
    orig_ocr = server.run_guided_ocr
    def counting(*a, **kw):
        counting.calls += 1
        return orig_ocr(*a, **kw)
    counting.calls = 0
    monkeypatch.setattr(server, "run_guided_ocr", counting)
    r2 = client.post("/api/process?mode=ocr_only", headers=headers, files={"file": ("a-copy.png", png, "image/png")})
    assert r2.status_code == 200
    # Cache hit means Tesseract should not be called again for identical content by admin
    assert counting.calls == 0


def test_cache_isolation_between_users(tmp_path):
    server.DB_PATH = str(tmp_path / "cache-iso.db")
    server.init_db()
    ocr_pipeline.CACHE_TABLE_READY = False
    ocr_pipeline.ensure_ocr_cache_table()
    client = TestClient(server.app)
    # login admin to create cache entry
    admin_h = login(client)
    png = _make_png()
    r = client.post("/api/process?mode=ocr_only", headers=admin_h, files={"file": ("x.png", png, "image/png")})
    assert r.status_code == 200
    doc_id_admin = r.json()["id"]

    # Create a DATA_OFFICER user
    client.post("/api/auth/signup", json={"full_name": "Test Officer", "email": "officer@example.test", "password": "Strong Pass 123!", "role": "DATA_OFFICER"})
    login_off = client.post("/api/auth/login", json={"email": "officer@example.test", "password": "Strong Pass 123!"})
    off_h = {"Authorization": "Bearer " + login_off.json()["token"]}
    # Data officer can upload same content - they will have their own cache; isolation prevents cross-user leak
    # but staff-level cache (data_officers get user-scoped) means they process fresh
    # Here we simply ensure the cache_lookup helper respects RBAC by role check.
    admin_user = {"email": "admin@landrec.gov.in", "role": "ADMIN"}
    officer_user = {"email": "officer@example.test", "role": "DATA_OFFICER"}
    chash = ocr_pipeline.content_hash(png)
    # Admin cached entry should not be returned to officer if it is user-scoped (admin uses 'admin' scope, officer can't access)
    # wait — admin scope is "admin" which any admin can see; officer is DATA_OFFICER which is not admin or staff but their own.
    # Simulate storing under officer scope first:
    entry = ocr_pipeline.cache_lookup(chash, officer_user)
    # The admin's entry is under scope "admin" which requires admin role to see.
    # Therefore officer lookup must return None until officer creates their own.
    # We already processed with admin first - officer lookup MUST return None because admin scope is not visible to officer.
    assert entry is None or entry.get("source_doc_id") != doc_id_admin


def test_failed_document_does_not_fail_batch(tmp_path):
    server.DB_PATH = str(tmp_path / "bulk-fail.db")
    server.init_db()
    ocr_pipeline.BATCH_TABLE_READY = False
    ocr_pipeline.ensure_batch_table()
    client = TestClient(server.app)
    headers = login(client)
    cb = client.post("/api/bulk", headers=headers, json={"mode": "ocr_only"})
    assert cb.status_code == 200
    bid = cb.json()["batch_id"]
    # First file bad (.exe)
    bad = client.post(f"/api/bulk/{bid}/files", headers=headers, files={"file": ("evil.exe", b"MZ")})
    assert bad.status_code == 200
    assert bad.json()["status"] == "FAILED"
    # Second valid
    png = _make_png()
    good = client.post(f"/api/bulk/{bid}/files", headers=headers, files={"file": ("ok.png", png, "image/png")})
    assert good.status_code == 200
    assert good.json()["status"] == "OK"
    final = client.post(f"/api/bulk/{bid}/finalize", headers=headers)
    assert final.status_code == 200
    status = client.get(f"/api/bulk/{bid}", headers=headers)
    assert status.json()["counts"]["failed"] >= 1
    assert status.json()["counts"]["completed"] + status.json()["counts"]["processing"] >= 1


def test_duplicate_content_upload_is_fast(monkeypatch, tmp_path):
    server.DB_PATH = str(tmp_path / "dup.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    png = _make_png()
    t0 = time.time()
    r1 = client.post("/api/process?mode=ocr_only", headers=headers, files={"file": ("a.png", png, "image/png")})
    t1 = time.time() - t0
    assert r1.status_code == 200
    # Count OCR calls
    calls = {"n": 0}
    orig = server.run_guided_ocr
    def spy(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)
    monkeypatch.setattr(server, "run_guided_ocr", spy)
    t0b = time.time()
    r2 = client.post("/api/process?mode=ocr_only", headers=headers, files={"file": ("b.png", png, "image/png")})
    t2 = time.time() - t0b
    assert r2.status_code == 200
    assert calls["n"] == 0, "Duplicate must not run Tesseract again"
    assert t2 < max(0.2, t1), "Cache hit must be substantially faster"


def test_land_intel_dashboard_has_no_land_intel_shortcut_card():
    html = open(os.path.join(os.path.dirname(server.BASE_DIR), "document-screening", "index.html"), encoding="utf-8").read()
    # The only mentions of Land Intelligence should be in the proper tab / workspace,
    # NOT a quick-action/shortcut card inside the dashboard tab div.
    import re
    dash = re.search(r'id="staff-tab-dashboard".*?(?=<div id="staff-tab-upload")', html, re.S)
    assert dash, "Dashboard panel not found"
    dash_html = dash.group(0).lower()
    assert "land intelligence" not in dash_html, "Dashboard must NOT contain a Land Intelligence shortcut card."
    assert "create mutation" not in dash_html, "Create Mutation must NOT appear on the dashboard."


def test_vectorflow_logo_path_is_correct():
    html = open(os.path.join(os.path.dirname(server.BASE_DIR), "document-screening", "index.html"), encoding="utf-8").read()
    assert "/assets/vectorflow-logo.png" in html
    assert "/assets/vectorflow.png" not in html


def test_downstream_does_not_block_ocr_response(monkeypatch, tmp_path):
    """Even if downstream (parcel match / land intel) is slow, OCR response returns quickly."""
    server.DB_PATH = str(tmp_path / "async.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    png = _make_png()
    t0 = time.time()
    r = client.post("/api/process?mode=ocr_li", headers=headers, files={"file": ("fast.png", png, "image/png")})
    elapsed = time.time() - t0
    assert r.status_code == 200
    assert elapsed < 60


def test_individual_retry_after_failure(tmp_path):
    server.DB_PATH = str(tmp_path / "retry.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    cb = client.post("/api/bulk", headers=headers, json={"mode": "ocr_only"})
    bid = cb.json()["batch_id"]
    bad = client.post(f"/api/bulk/{bid}/files", headers=headers, files={"file": ("bad.exe", b"MZ")})
    assert bad.json()["status"] == "FAILED"
    item_id = bad.json()["item_id"]
    fin = client.post(f"/api/bulk/{bid}/finalize", headers=headers)
    assert fin.status_code == 200
    # Retry: doc_id is null so we expect graceful response (not crash)
    rr = client.post(f"/api/bulk/items/{item_id}/retry", headers=headers)
    assert rr.status_code == 200


def test_batch_progress_counts(tmp_path):
    server.DB_PATH = str(tmp_path / "progress.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    cb = client.post("/api/bulk", headers=headers, json={"mode": "ocr_only"})
    bid = cb.json()["batch_id"]
    png = _make_png()
    for i in range(3):
        client.post(f"/api/bulk/{bid}/files", headers=headers, files={"file": (f"f{i}.png", png, "image/png")})
    client.post(f"/api/bulk/{bid}/files", headers=headers, files={"file": ("bad.exe", b"MZ")})
    client.post(f"/api/bulk/{bid}/finalize", headers=headers)
    st = client.get(f"/api/bulk/{bid}", headers=headers).json()
    assert st["total"] == 4
    assert st["counts"]["failed"] >= 1
    assert st["counts"]["completed"] + st["counts"]["processing"] >= 2
