"""Regression tests for the fast OCR pipeline, OCR cache, and bulk processing.

These exercise the OCR/Bulk-OCR work integrated onto the current mapping
architecture. The fast pipeline shares the canonical parcel resolver and Land
Intelligence context with the rest of the system; it does not introduce a second
mapping or worker system.
"""
import io
import os
import time
import zlib

os.environ.setdefault("ADMIN_INITIAL_PASSWORD", "Admin@123")
os.environ.setdefault("APP_ENV", "test")

from fastapi.testclient import TestClient
from PIL import Image

import server
import ocr_pipeline


def login(client):
    r = client.post("/api/auth/login", json={"email": "admin@landrec.gov.in", "password": "Admin@123"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def _make_png(color=(255, 255, 255), size=(200, 200)):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _make_text_pdf_bytes(text: str) -> bytes:
    """Build a minimal single-page PDF that carries a REAL extractable text
    layer (no external PDF writer required). pypdfium2 reads the text back, so
    the fast pipeline takes the embedded-text path instead of Tesseract."""
    parts = ["BT", "3 Tr", "/F1 11 Tf", "14 TL", "40 780 Td"]
    for line in text.split("\n"):
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        parts.append(f"({safe}) Tj")
        parts.append("T*")
    parts.append("ET")
    content = zlib.compress("\n".join(parts).encode("cp1252", "replace"), 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        f"<< /Length {len(content)} /Filter /FlateDecode >>\nstream\n".encode() + content + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    count = len(objects) + 1
    out.write(f"xref\n0 {count}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode())
    return out.getvalue()


def test_pdf_text_layer_bypasses_tesseract_when_embedded_text_present(monkeypatch, tmp_path):
    server.DB_PATH = str(tmp_path / "textlayer.db")
    server.init_db()
    client = TestClient(server.app)
    headers = login(client)
    pdf_bytes = _make_text_pdf_bytes(
        "Owner Name: Test Owner\nVillage: Testville\nDistrict: Pune\nSurvey Number: 45/2\nArea: 1.25 Acres"
    )

    # Spy on run_guided_ocr to ensure it is NOT called for text PDFs.
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
    orig_ocr = server.run_guided_ocr

    def counting(*a, **kw):
        counting.calls += 1
        return orig_ocr(*a, **kw)

    counting.calls = 0
    monkeypatch.setattr(server, "run_guided_ocr", counting)
    r2 = client.post("/api/process?mode=ocr_only", headers=headers, files={"file": ("a-copy.png", png, "image/png")})
    assert r2.status_code == 200
    assert counting.calls == 0, "Cache hit means Tesseract must not run again for identical content"


def test_cache_isolation_between_users(tmp_path):
    server.DB_PATH = str(tmp_path / "cache-iso.db")
    server.init_db()
    ocr_pipeline.CACHE_TABLE_READY = False
    ocr_pipeline.ensure_ocr_cache_table()
    client = TestClient(server.app)
    admin_h = login(client)
    png = _make_png()
    r = client.post("/api/process?mode=ocr_only", headers=admin_h, files={"file": ("x.png", png, "image/png")})
    assert r.status_code == 200
    doc_id_admin = r.json()["id"]

    client.post("/api/auth/signup", json={"full_name": "Test Officer", "email": "officer@example.test",
                                          "password": "Strong Pass 123!", "role": "DATA_OFFICER"})
    login_off = client.post("/api/auth/login", json={"email": "officer@example.test", "password": "Strong Pass 123!"})
    off_h = {"Authorization": "Bearer " + login_off.json()["token"]}

    admin_user = {"email": "admin@landrec.gov.in", "role": "ADMIN"}
    officer_user = {"email": "officer@example.test", "role": "DATA_OFFICER"}
    chash = ocr_pipeline.content_hash(png)
    # The admin's cache entry is under scope "admin" which requires admin role to
    # see, so a Data Officer lookup must not return it.
    entry = ocr_pipeline.cache_lookup(chash, officer_user)
    assert entry is None or entry.get("source_doc_id") != doc_id_admin
    # The admin can still see their own entry.
    assert ocr_pipeline.cache_lookup(chash, admin_user) is not None


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
    bad = client.post(f"/api/bulk/{bid}/files", headers=headers, files={"file": ("evil.exe", b"MZ")})
    assert bad.status_code == 200
    assert bad.json()["status"] == "FAILED"
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


def test_vectorflow_logo_path_is_correct():
    html = open(os.path.join(server.BASE_DIR, "index.html"), encoding="utf-8").read()
    assert "/assets/vectorflow-logo.png" in html
    assert "/assets/vectorflow.png" not in html


def test_downstream_does_not_block_ocr_response(monkeypatch, tmp_path):
    """Even when Land Intelligence runs, the OCR response returns promptly."""
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
    # doc_id is null (rejected type) so retry degrades gracefully instead of crashing.
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


def test_bulk_uses_same_pipeline_and_resolves_parcel():
    """A court document processed through Bulk OCR resolves to the same DEMO-LI
    parcel and surfaces ACTIVE litigation through the canonical Land
    Intelligence context (same pipeline as single-document OCR)."""
    from pathlib import Path
    from main import app  # canonical app with every router + demo seed

    client = TestClient(app)
    headers = login(client)
    seeded = client.post("/api/admin/demo/seed", json={"scenario": "LI"}, headers=headers)
    assert seeded.status_code == 200, seeded.text

    sample = Path("samples/demo-land-intel/DEMO-LI-COURT-001-court-order.pdf")
    try:
        cb = client.post("/api/bulk", headers=headers, json={"mode": "ocr_li"})
        bid = cb.json()["batch_id"]
        up = client.post(f"/api/bulk/{bid}/files", headers=headers,
                         files={"file": (sample.name, sample.read_bytes(), "application/pdf")})
        assert up.status_code == 200, up.text
        assert up.json()["status"] == "OK"
        doc_id = up.json()["doc_id"]

        detail = client.get(f"/api/documents/{doc_id}", headers=headers).json()
        assert detail["fields"]["survey_number"]["value"] == "131"
        assert detail["fields"]["village"]["value"] == "Shantiban"
        context = detail["land_context"]
        assert context.get("matched") is True
        banner = context.get("litigation_banner") or {}
        assert banner.get("text") == "Active litigation found for this property"
        assert banner.get("case_number") == "DEMO-CS-2025-0142"

        # The batch item carries the RBAC-scoped statuses through the same resolver.
        batch = client.get(f"/api/bulk/{bid}", headers=headers).json()
        item = batch["items"][0]
        assert item["parcel_match"]
        assert item["litigation_status"] == "ACTIVE_LITIGATION"
    finally:
        # Wipe the demo corpus (including the uploaded sample) so repeated test
        # runs never inherit stale demo documents.
        client.delete("/api/admin/demo/data", headers=headers)
