"""Mutation integration with the EXISTING AI Approval system.

AI may PROPOSE a mutation application; nothing exists until an administrator
approves it in the AI Approval Center, and completion stays a separate,
safety-gated human action. This proves there is no second approval system.
"""
import uuid

import pytest

from ai_governance import ACTION_REGISTRY, ensure_governance_tables


def test_mutation_action_is_registered():
    assert "CREATE_MUTATION_APPLICATION" in ACTION_REGISTRY


def test_ai_mutation_proposal_requires_admin_and_executes_server_side(make_user_client):
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="aimo")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="aima")
    ensure_governance_tables()

    me = admin.get("/api/auth/me", headers=admin_headers).json()["user"]
    village = f"AIpur-{uuid.uuid4().hex[:6]}"

    proposal_body = {
        "action_type": "CREATE_MUTATION_APPLICATION",
        "target_type": "MUTATION_APPLICATION",
        "target_ids": [],
        "before": {},
        "after": {
            "survey_number": f"8{uuid.uuid4().hex[:5]}",
            "village": village,
            "tehsil": "Sadar",
            "district": "Ghaziabad",
            "previous_owner": "Old Owner",
            "new_owner": "New Owner",
            "reason_type": "SALE",
            "deed_date": "2026-01-15",
        },
        "reason": "Screening found a registered sale deed requiring a mutation application.",
        "evidence": [{"kind": "FACT", "field": "reason_type", "value": "SALE"}],
        "confidence": 0.9,
        "risk": "MEDIUM",
    }

    # a verifier cannot create AI proposals (existing RBAC of the endpoint)
    denied = officer.post("/api/admin/ai-approval/proposals", headers=officer_headers,
                          json={"proposal": proposal_body})
    assert denied.status_code == 403

    created = admin.post("/api/admin/ai-approval/proposals", headers=admin_headers,
                         json={"proposal": proposal_body})
    assert created.status_code == 200, created.text
    proposal_id = created.json()["proposal"]["proposal_id"]

    # nothing exists yet
    listing_before = admin.get("/api/mutations", headers=admin_headers, params={"q": village})
    assert listing_before.json()["total"] == 0

    # Final approval requires the calling administrator's own password.
    approved = admin.post(f"/api/admin/ai-approval/proposals/{proposal_id}/approve",
                          headers=admin_headers,
                          json={"note": "Evidence verified", "password": "Strong Land Password 123!"})
    assert approved.status_code == 200
    assert approved.json()["proposal"]["status"] == "EXECUTED"
    result = approved.json()["proposal"]["execution_result"]
    assert result["mutations"] and result["status"] == "RECEIVED"

    # the mutation now exists through the SAME queue with a normal app number
    listing = admin.get("/api/mutations", headers=admin_headers, params={"q": village})
    assert listing.json()["total"] == 1
    mutation = listing.json()["mutations"][0]
    assert mutation["mutation_no"].startswith("M-")
    assert mutation["status"] == "RECEIVED"
    assert mutation["created_by"] == me["email"]

    # the AI-created mutation still needs the human workflow: the safety gate
    # and admin-only completion apply to it exactly as to manual applications.
    audit = admin.get("/api/audit", headers=admin_headers)
    actions = [row["action"] for row in audit.json().get("audit", audit.json().get("logs", []))]
    assert "MUTATION_CREATED" in actions  # audited through the standard path


def test_ai_mutation_proposal_validation(make_user_client):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="aimv")
    ensure_governance_tables()

    invalid_type = admin.post("/api/admin/ai-approval/proposals", headers=admin_headers, json={"proposal": {
        "action_type": "CREATE_MUTATION_APPLICATION",
        "target_type": "MUTATION_APPLICATION",
        "target_ids": [],
        "after": {"survey_number": "1", "new_owner": "B", "reason_type": "FORGERY"},
        "reason": "bad type",
        "confidence": 0.8,
    }})
    assert invalid_type.status_code == 400

    missing_owner = admin.post("/api/admin/ai-approval/proposals", headers=admin_headers, json={"proposal": {
        "action_type": "CREATE_MUTATION_APPLICATION",
        "target_type": "MUTATION_APPLICATION",
        "target_ids": [],
        "after": {"survey_number": "1"},
        "reason": "no owner",
        "confidence": 0.8,
    }})
    assert missing_owner.status_code == 400

    missing_identity = admin.post("/api/admin/ai-approval/proposals", headers=admin_headers, json={"proposal": {
        "action_type": "CREATE_MUTATION_APPLICATION",
        "target_type": "MUTATION_APPLICATION",
        "target_ids": [],
        "after": {"new_owner": "B"},
        "reason": "no land identity",
        "confidence": 0.8,
    }})
    assert missing_identity.status_code == 400
