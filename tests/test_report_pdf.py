"""PDF Land Record Verification Report tests.

The PDF must be a real, parseable document carrying every required field and
a QR image; it is parsed with pypdfium2 (already a project dependency)."""
import io
import time
import uuid

import pytest

pypdfium2 = pytest.importorskip("pypdfium2", reason="pypdfium2 is required to parse generated PDFs")


def _survey(prefix="3"):
    return f"{prefix}{uuid.uuid4().hex[:5]}"


def _create_report(admin, headers, village="Pdfville"):
    survey = _survey()
    admin.post("/api/land-records", headers=headers)  # ensure tables exist
    from tests.test_reports import _survey as _  # noqa: F401  (keep helpers obvious)
    return survey


def test_pdf_report_contains_all_required_fields(make_user_client, insert_land_document):
    admin, headers, _ = make_user_client("ADMIN", prefix="pdfa")
    survey = _survey()
    insert_land_document(survey=survey, village="Pdfville", owner="Sita Devi", year="2022", area="2.75 ha")
    land_id = admin.get("/api/land-records", headers=headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    generated = admin.post("/api/reports/land-verification", headers=headers, json={"land_id": land_id})
    assert generated.status_code == 200
    reference = generated.json()["reference_no"]
    reviewer = generated.json()["report"]["reviewer"]

    pdf_response = admin.get(f"/api/reports/land-verification/{reference}/report.pdf", headers=headers)
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.content[:5] == b"%PDF-"

    document = pypdfium2.PdfDocument(io.BytesIO(pdf_response.content))
    text = " ".join(document[0].get_textpage().get_text_bounded().split())
    for expected in (
        "LAND RECORD VERIFICATION REPORT",
        reference,
        "Sita Devi",          # owner
        survey,               # survey/khasra
        "2.75 ha",            # area
        "Pdfville",           # village
        "Sadar",              # tehsil
        "Ghaziabad",          # district
        "APPROVED",           # verification status
        "CLEAR",              # risk status
        "NONE",               # encumbrance status
        reviewer,               # reviewer
        "NOT an official government land title certificate",
    ):
        assert expected in text, f"missing PDF field: {expected}"
    # the QR is embedded as an image XObject
    assert b"/Subtype /Image" in pdf_response.content

    # the download is audited
    audit = admin.get("/api/audit", headers=headers)
    actions = [row["action"] for row in audit.json().get("audit", audit.json().get("logs", []))]
    assert "REPORT_PDF_DOWNLOADED" in actions


def test_pdf_report_lists_risk_signals_and_encumbrance(make_user_client, insert_land_document):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="pdfb")
    survey = _survey()
    insert_land_document(survey=survey, village="Pdfriskville", owner="Mahesh Verma")
    officer.post("/api/encumbrances", headers=officer_headers, json={
        "survey_number": survey, "village": "Pdfriskville", "lender": "Example Bank",
        "reference_no": "LN-PDF-1", "amount": 850000,
    })
    land_id = officer.get("/api/land-records", headers=officer_headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    reference = officer.post("/api/reports/land-verification", headers=officer_headers,
                             json={"land_id": land_id}).json()["reference_no"]
    blob = officer.get(f"/api/reports/land-verification/{reference}/report.pdf", headers=officer_headers).content
    text = pypdfium2.PdfDocument(io.BytesIO(blob))[0].get_textpage().get_text_bounded()
    assert "ACTIVE_ENCUMBRANCE" in text
    assert "ACTIVE" in text  # encumbrance status


def test_pdf_report_requires_reviewer_role(make_user_client, insert_land_document):
    data_officer, do_headers, _ = make_user_client("DATA_OFFICER", prefix="pdfc")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="pdfca")
    survey = _survey()
    insert_land_document(survey=survey, village="Pdfrbville")
    land_id = admin.get("/api/land-records", headers=admin_headers, params={"q": survey}).json()["land_records"][0]["land_id"]
    reference = admin.post("/api/reports/land-verification", headers=admin_headers,
                           json={"land_id": land_id}).json()["reference_no"]
    assert data_officer.get(f"/api/reports/land-verification/{reference}/report.pdf",
                            headers=do_headers).status_code == 403


def test_pdf_report_unknown_reference_404(make_user_client):
    admin, headers, _ = make_user_client("ADMIN", prefix="pdfd")
    assert admin.get("/api/reports/land-verification/LVR-2026-999999/report.pdf",
                     headers=headers).status_code == 404


def test_pdf_renderer_handles_non_latin1_and_long_payloads():
    from report_pdf import render_verification_report_pdf

    payload = {
        "reference_no": "LVR-2026-000999",
        "document_id": "doc-1", "land_record_id": "LR-x",
        "owner": "राम स्वरूप शर्मा and Ram Swaroop",
        "survey": "१२३", "area": "₹2,50,000",
        "village": "Barkheda", "tehsil": "S", "district": "D",
        "verification_status": "APPROVED", "risk_status": "REVIEW",
        "encumbrance_status": "CLEAR", "mutation_status": "COMPLETED",
        "risk_flags": [{"severity": "REVIEW", "title": f"Signal {i}", "code": f"CODE_{i}"} for i in range(20)],
        "supporting_documents": [{"id": f"doc-{i}", "filename": f"file-{i}.pdf", "status": "APPROVED"} for i in range(24)],
        "generated_at": time.time(), "reviewer": "reviewer@example.test",
        "disclaimer": "Internal verification workflow report. NOT an official government land title certificate.",
    }
    from PIL import Image

    qr = io.BytesIO()
    Image.new("RGB", (33, 33), "white").save(qr, format="PNG")
    blob = render_verification_report_pdf(payload, qr.getvalue())
    document = pypdfium2.PdfDocument(io.BytesIO(blob))
    assert len(document) >= 1  # long payloads paginate instead of crashing
    text = document[0].get_textpage().get_text_bounded()
    assert "LVR-2026-000999" in text
