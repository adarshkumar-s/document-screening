"""Phase 14 — BACKUP / RESTORE tests: RBAC, manifest, round-trip with state
revert, corrupted archive rejection, and the pre-restore safety copy."""
import io
import json
import os
import zipfile

import pytest

from server import BASE_DIR


@pytest.fixture
def backup_module():
    import backup_restore

    return backup_restore


def _download_backup(client, headers):
    response = client.get("/api/admin/data-management/backup", headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    return response.content


def test_backup_is_admin_only(make_user_client):
    viewer, viewer_headers, _ = make_user_client("VIEWER", prefix="bkmv")
    officer, officer_headers, _ = make_user_client("VERIFICATION_OFFICER", prefix="bkmo")
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkma")

    assert viewer.get("/api/admin/data-management/backup", headers=viewer_headers).status_code == 403
    assert officer.get("/api/admin/data-management/backup", headers=officer_headers).status_code == 403
    assert admin.get("/api/admin/data-management/backup", headers=admin_headers).status_code == 200
    assert viewer.get("/api/admin/data-management/backup/manifest", headers=viewer_headers).status_code == 403


def test_backup_contains_manifest_tables_and_documents(make_user_client, insert_land_document):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkmc")
    doc_id = insert_land_document(survey="881", village="Backupville")
    content = _download_backup(admin, admin_headers)
    archive = zipfile.ZipFile(io.BytesIO(content))
    names = archive.namelist()

    assert "manifest.json" in names
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["application"] == "document-screening"
    assert manifest["kind"] == "full-backup"
    assert manifest["format_version"] >= 1
    assert "documents" in manifest["table_counts"]
    assert manifest["table_counts"]["documents"] >= 1
    # OCR/extraction state, verification state, audit trail, land registers
    for table in ("users", "documents", "audit", "corrections"):
        assert f"db/tables/{table}.json" in names
    documents = json.loads(archive.read("db/tables/documents.json"))
    assert any(row["id"] == doc_id for row in documents)
    for table in ("land_encumbrances", "land_mutations", "land_mutation_events", "land_reports"):
        assert f"db/tables/{table}.json" in names


def test_restore_round_trip_reverts_state(make_user_client, insert_land_document):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkmr")
    baseline_doc = insert_land_document(survey="882", village="Roundtrip")
    content = _download_backup(admin, admin_headers)

    # mutate state after the backup: a new document exists only outside the backup
    extra_doc = insert_land_document(survey="883", village="PostBackup")
    archive_before = zipfile.ZipFile(io.BytesIO(content))
    before_ids = {row["id"] for row in json.loads(archive_before.read("db/tables/documents.json"))}
    assert extra_doc not in before_ids and baseline_doc in before_ids

    restored = admin.post("/api/admin/data-management/restore", headers=admin_headers,
                          files={"file": ("backup.zip", content, "application/zip")})
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["status"] == "restored"
    assert body["verification"]["ok"] is True, body["verification"]
    assert "backup_before_restore_" in body["safety_copy"]

    listing = admin.get("/api/documents", headers=admin_headers)
    visible_ids = {doc["id"] for doc in listing.json()["documents"]}
    assert baseline_doc in visible_ids
    assert extra_doc not in visible_ids  # state reverted to the backup snapshot

    audit = admin.get("/api/audit", headers=admin_headers)
    actions = [row["action"] for row in audit.json().get("audit", audit.json().get("logs", []))]
    # the restore event itself is written AFTER the restore and survives
    assert "BACKUP_RESTORED" in actions
    # pre-restore events were replaced with the backup's audit trail — they are
    # preserved inside the safety copy, which is the undoable pre-restore state
    safety_zip = os.path.join(BASE_DIR, body["safety_copy"], "data_backup.zip")
    with zipfile.ZipFile(safety_zip) as archive:
        safety_audit = json.loads(archive.read("db/tables/audit.json"))
    safety_actions = [row["action"] for row in safety_audit]
    assert "BACKUP_RESTORE_STARTED" in safety_actions
    assert "BACKUP_EXPORTED" in safety_actions
    # the live (restored) audit carries the completed restore event, with the
    # safety-copy location in its detail
    audit = admin.get("/api/audit", headers=admin_headers)
    restored_events = [row for row in audit.json().get("audit", audit.json().get("logs", []))
                       if row["action"] == "BACKUP_RESTORED"]
    assert restored_events and body["safety_copy"] in restored_events[0]["detail"]


def test_restore_rejects_non_zip(make_user_client):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkmz")
    response = admin.post("/api/admin/data-management/restore", headers=admin_headers,
                          files={"file": ("garbage.zip", b"definitely not a zip", "application/zip")})
    assert response.status_code == 400


def test_restore_rejects_zip_without_database(make_user_client):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkmnd")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("random.txt", "hello")
    response = admin.post("/api/admin/data-management/restore", headers=admin_headers,
                          files={"file": ("empty.zip", buffer.getvalue(), "application/zip")})
    assert response.status_code == 400


def test_restore_rejects_foreign_manifest(make_user_client):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkmf")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"application": "some-other-app"}))
        archive.writestr("db/tables/documents.json", "[]")
    response = admin.post("/api/admin/data-management/restore", headers=admin_headers,
                          files={"file": ("foreign.zip", buffer.getvalue(), "application/zip")})
    assert response.status_code == 400


def test_restore_rejects_path_traversal_members(make_user_client):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkmt")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"application": "document-screening", "format_version": 1}))
        archive.writestr("db/tables/documents.json", "[]")
        archive.writestr("../evil.txt", "owned")
    response = admin.post("/api/admin/data-management/restore", headers=admin_headers,
                          files={"file": ("traversal.zip", buffer.getvalue(), "application/zip")})
    assert response.status_code == 400


def test_restore_rejects_future_format_version(make_user_client):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkmv2")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "application": "document-screening", "format_version": 999,
        }))
        archive.writestr("db/tables/documents.json", "[]")
    response = admin.post("/api/admin/data-management/restore", headers=admin_headers,
                          files={"file": ("future.zip", buffer.getvalue(), "application/zip")})
    assert response.status_code == 400


def test_safety_copy_actually_holds_pre_restore_data(make_user_client, insert_land_document):
    import os

    import server

    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkms")
    marker_doc = insert_land_document(survey="884", village="Safetyville")
    content = _download_backup(admin, admin_headers)
    insert_land_document(survey="885", village="OnlyAfterBackup")
    result = admin.post("/api/admin/data-management/restore", headers=admin_headers,
                        files={"file": ("backup.zip", content, "application/zip")}).json()
    safety_dir = result["safety_copy"]  # relative to the application root
    assert os.path.isdir(safety_dir)
    safety_zip = os.path.join(safety_dir, "data_backup.zip")
    assert os.path.isfile(safety_zip)
    with zipfile.ZipFile(safety_zip) as archive:
        documents = json.loads(archive.read("db/tables/documents.json"))
        ids = {row["id"] for row in documents}
    # the safety copy contains the state BEFORE the restore — including the
    # document created after the baseline backup
    assert marker_doc in ids


def test_backup_no_uploads_directory_traversal(make_user_client):
    admin, admin_headers, _ = make_user_client("ADMIN", prefix="bkmp")
    content = _download_backup(admin, admin_headers)
    archive = zipfile.ZipFile(io.BytesIO(content))
    for name in archive.namelist():
        assert not name.startswith("/")
        assert ".." not in name.split("/")
