from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


STATE_OPTIONS = [
    "Andhra Pradesh", "Bihar", "Chhattisgarh", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Odisha", "Punjab", "Rajasthan", "Tamil Nadu", "Telangana",
    "Uttar Pradesh", "Uttarakhand", "West Bengal", "Delhi", "Other",
]


def test_staff_upload_has_state_selector_between_document_type_and_language():
    html = (ROOT / "index.html").read_text()
    upload = html.split('id="staff-tab-upload"', 1)[1].split('id="staff-tab-queue"', 1)[0]
    assert 'id="staffDocTypeSelect"' in upload
    assert 'id="staffStateSelect"' in upload
    assert 'id="staffLangSelect"' in upload
    assert upload.index('id="staffDocTypeSelect"') < upload.index('id="staffStateSelect"') < upload.index('id="staffLangSelect"')
    assert 'id="staffStateHelper"' in upload
    for state in STATE_OPTIONS:
        assert f'value="{state}"' in upload
    assert 'value="" selected>Select State</option>' in upload


def test_staff_upload_sends_state_without_changing_language_or_document_type():
    app_js = (ROOT / "js" / "app.js").read_text()
    assert "const state = $('#staffStateSelect')?.value || '';" in app_js
    assert "&state=${encodeURIComponent(state)}" in app_js
    assert "const lang = $('#staffLangSelect')?.value || 'auto';" in app_js
    assert "const docType = $('#staffDocTypeSelect')?.value || 'Land Record';" in app_js
    assert "State-specific screening rules and land-record terminology will be applied where available." in (ROOT / "index.html").read_text()
