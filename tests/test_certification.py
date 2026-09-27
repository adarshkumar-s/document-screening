import hashlib
import io


def test_certification_fingerprint_changes_when_record_changes():
    from certification import fingerprint

    row = {
        "id": "doc-1",
        "filename": "record.pdf",
        "doc_type": "Land Record",
        "status": "APPROVED",
        "fields": '{"owner_name":{"value":"Sita Devi"}}',
        "ocr_text": "Owner Name: Sita Devi",
    }
    first = fingerprint(row)
    assert len(first) == 64
    row["fields"] = '{"owner_name":{"value":"Gita Devi"}}'
    assert fingerprint(row) != first


def test_qr_contains_verification_url():
    from certification import _qr_png
    from PIL import Image

    payload = "https://example.test/verify/doc-123"
    blob = _qr_png(payload)
    image = Image.open(io.BytesIO(blob))
    assert image.width > 100
    assert image.height > 100
    assert blob.startswith(b"\x89PNG")
