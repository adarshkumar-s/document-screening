import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
import server
import ai_governance


@pytest.fixture()
def client():
    return TestClient(main.app)


def test_land_intelligence_assets_and_types(client):
    checks = [
        ("/land-intelligence", 200, "text/html"),
        ("/land-intelligence.css", 200, "text/css"),
        ("/land-intelligence.js", 200, "javascript"),
    ]
    for path, status, content_type in checks:
        response = client.get(path)
        assert response.status_code == status
        assert content_type in response.headers.get("content-type", "").lower()
    assert client.get("/land-intelligence.css").text.startswith(":root")
    assert "LAND INTELLIGENCE" in client.get("/land-intelligence").text


def test_demo_land_endpoints_are_available(client):
    assert client.get("/api/demo-land/health").status_code == 200
    properties = client.get("/api/demo-land/properties")
    assert properties.status_code == 200
    assert "properties" in properties.json()


def test_ai_action_registry_is_closed():
    assert "REQUEST_REPROCESSING" in ai_governance.ACTION_REGISTRY
    assert "DROP_DATABASE" not in ai_governance.ACTION_REGISTRY


def test_ai_proposal_requires_registered_action():
    with pytest.raises(Exception):
        ai_governance.create_proposal({
            "action_type": "DROP_DATABASE",
            "target_type": "DOCUMENT",
            "target_ids": ["missing"],
            "before": {},
            "after": {},
            "reason": "test",
            "evidence": [],
            "confidence": 0.5,
            "risk": "HIGH",
        }, created_by="AI_ASSISTANT")


def test_legacy_ai_execution_endpoint_is_disabled():
    response = client.post("/api/admin/assistant/execute-action", json={"token":"anything"})
    assert response.status_code in (401, 403, 410)
    if response.status_code == 410:
        assert "approval" in response.json()["detail"].lower()


def test_create_verification_case_requires_admin_approval_and_executes_server_side(tmp_path):
    server.DB_PATH = str(tmp_path / "governed-case.db")
    server.init_db()
    from land_intelligence import _ensure_tables
    _ensure_tables()
    with server.get_db() as db:
        db.execute(
            "INSERT INTO documents (id,filename,doc_type,mean_conf,verdict,status,languages,pages,fields,validation,ai_decision_support,ocr_text,cleaned_ocr_text,detected_language,original_fields,uploaded_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("CASE-DOC","case.pdf","Land Record",60,"review","PENDING_VERIFICATION","[]",1,
             '{"owner_name":{"value":"A","confidence":0.6},"survey_number":{"value":"452","confidence":0.9},"village":{"value":"Sundarpur","confidence":0.9}}',
             '{}','{}','','','eng','{}','admin@landrec.gov.in',0,0)
        )
    admin = client.post("/api/auth/login", json={"email":"admin@landrec.gov.in","password":"Admin@123"})
    assert admin.status_code == 200
    headers = {"Authorization": f"Bearer {admin.json()['token']}"}
    proposal = client.post("/api/admin/ai-approval/proposals", headers=headers, json={
        "action_type":"CREATE_VERIFICATION_CASE",
        "target_type":"DOCUMENT",
        "target_ids":["CASE-DOC"],
        "before":{},
        "after":{"title":"Review ownership evidence"},
        "reason":"Ownership evidence requires human review.",
        "evidence":[{"kind":"FACT","field":"survey_number","value":"452"}],
        "confidence":0.9,
        "risk":"MEDIUM"
    })
    assert proposal.status_code == 200
    pid=proposal.json()["proposal"]["proposal_id"]
    approved = client.post(f"/api/admin/ai-approval/proposals/{pid}/approve", headers=headers, json={"note":"Approved for verification"})
    assert approved.status_code == 200
    assert approved.json()["proposal"]["status"] == "EXECUTED"
    assert approved.json()["proposal"]["execution_result"]["cases"]
    with server.get_db() as db:
        assert db.execute("SELECT 1 FROM verification_cases").fetchone() is not None


def test_location_actions_are_registered_and_admin_governed():
    assert "SET_PROPERTY_LOCATION" in ai_governance.ACTION_REGISTRY
    assert "CLEAR_PROPERTY_LOCATION" in ai_governance.ACTION_REGISTRY
    with pytest.raises(Exception):
        ai_governance.create_proposal({
            "action_type":"SET_PROPERTY_LOCATION","target_type":"PROPERTY",
            "target_ids":["DOES-NOT-EXIST"],"before":{},
            "after":{"latitude":28.62,"longitude":77.10},"reason":"test",
            "evidence":[],"confidence":0.9,"risk":"HIGH"
        }, created_by="AI_ASSISTANT")


def test_location_proposal_requires_approval_before_execution():
    import land_intelligence
    import server
    from fastapi.testclient import TestClient
    server.DB_PATH = str(Path("data") / "ai-location-test.db")
    server.init_db()
    land_intelligence._ensure_tables()
    with server.get_db() as db:
        admin = db.execute("SELECT id,full_name,email FROM users WHERE role='ADMIN' AND is_active=1 ORDER BY id LIMIT 1").fetchone()
    if not admin:
        pytest.skip("No test administrator is available")
    proposal = ai_governance.create_proposal({
        "action_type":"SET_PROPERTY_LOCATION","target_type":"PROPERTY",
        "target_ids":["DEMO-PROP-103-A"],
        "before":{"properties":{"DEMO-PROP-103-A":{"location_status":"PARCEL_GEOMETRY"}}},
        "after":{"latitude":28.6227,"longitude":77.1057,"reason":"Governed test pin"},
        "reason":"AI proposes a verified pin for administrator review.",
        "evidence":[{"type":"test"}],"confidence":0.95,"risk":"HIGH"
    }, created_by="AI_ASSISTANT")
    assert proposal["status"] == "PROPOSED"
    with server.get_db() as db:
        row=db.execute("SELECT latitude,longitude,location_status FROM properties WHERE property_id='DEMO-PROP-103-A'").fetchone()
    assert row["location_status"] == "PARCEL_GEOMETRY"
    assert row["latitude"] != 28.6227
    executed=ai_governance.approve_proposal(proposal["proposal_id"],dict(admin),"test approval")
    assert executed["status"] == "EXECUTED"
    assert executed["execution_result"]["location_status"] == "EXACT_PIN"
