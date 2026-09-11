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
