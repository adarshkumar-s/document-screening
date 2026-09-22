#!/usr/bin/env python3
"""Development-only performance probe for the document-screening hot paths.

Builds a deterministic synthetic dataset (documents with realistic OCR text
sizes, audit history, register rows, court cases) into a throwaway database,
then measures wall time, database query count / time and response size for the
endpoints the performance pass targets. Never enabled by the application; run
explicitly:

    python scripts/perf_probe.py --out /tmp/perf_before.json

The probe only reads/creates data tagged as benchmark data in its own database
(given via --db or DB_PATH); it never touches a deployed database.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

WORDS = ("khasra khatauni jamabandi mutation girdawari possession boundary cultivation irrigated "
         "barren holding tenant owner co-sharer partition demarcation survey village tehsil "
         "district record rights succession transfer mortgage lien release decree").split()

VILLAGES = ["Ambedarpur", "Barkheda", "Jayantipur", "Khetanpur", "Rampur Kalan", "Sonbarsa"]
OWNERS = ["Sita Devi", "Ram Swaroop Sharma", "Mahesh Verma", "Harish Chandra", "Jagdish Yadav",
          "Shyam Lal", "Om Prakash", "Dinesh Chand", "Kishan Lal Yadav", "Sushila Devi",
          "Chunri Lal", "Gayatri Devi", "Mohd. Irfan", "Virendra Pratap", "Amit Sharma", "Rekha Singh"]


def _ocr_text(seed: int, size: int = 5200) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(WORDS) for _ in range(size // 6))


def build_dataset(db_path: str) -> None:
    import sqlite3

    if os.path.exists(db_path):
        os.remove(db_path)

    os.environ["DB_PATH"] = db_path  # server reads the path at import time
    import server  # noqa: E402  (creates schema through the real init path)
    server.init_db()

    from land_intel import ensure_land_tables
    from court_cases import ensure_schema
    ensure_land_tables()
    ensure_schema()

    connection = sqlite3.connect(db_path)
    now = time.time()
    docs = []
    parcel = 0
    for village_index, village in enumerate(VILLAGES):
        for parcel_index in range(6):
            parcel += 1
            survey = f"{400 + parcel}"
            for doc_index in range(3):
                year = 2016 + doc_index * 3
                doc_id = f"BENCH-{parcel}-{doc_index}"
                owner = OWNERS[(parcel + doc_index) % len(OWNERS)]
                fields = {
                    key: {"value": value, "confidence": 0.95, "validation_status": "VALID", "validation_message": ""}
                    for key, value in {
                        "owner_name": owner, "father_name": "Father Name", "survey_number": survey,
                        "khasra_number": survey, "area": f"{1.0 + 0.1 * doc_index:.2f} ha",
                        "village": village, "tehsil": "Benchmark Tehsil", "district": "Benchmark District",
                        "state": "Benchmark Pradesh", "document_date": f"{year}-06-15",
                        "land_class": "Agricultural", "ownership_type": "Bhumidar", "khatauni_year": str(year),
                    }.items()
                }
                docs.append((
                    doc_id, f"bench-{parcel}-{doc_index}.pdf", "Land Record", 93, "review", "APPROVED",
                    '["eng"]', 1, json.dumps(fields), json.dumps({"status": "VALID", "issues": []}), "{}",
                    _ocr_text(parcel * 10 + doc_index), _ocr_text(parcel * 10 + doc_index, 3000), "eng",
                    json.dumps(fields), json.dumps({"benchmark": True}), "bench@example.test", "",
                    float(year), float(year),
                ))
    connection.executemany(
        """INSERT INTO documents (id, filename, doc_type, mean_conf, verdict, status, languages, pages,
           fields, validation, ai_decision_support, ocr_text, cleaned_ocr_text, detected_language,
           original_fields, metadata, uploaded_by, reviewer_comments, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        docs,
    )
    audit_rows = []
    actions = ["DOCUMENT_UPLOADED", "DOCUMENT_APPROVED", "LOCATION_SET", "MUTATION_CREATED",
               "DEMO_DATA_SEEDED", "LOGIN_SUCCEEDED", "REPORT_GENERATED"]
    for index in range(2500):
        audit_rows.append((now - index * 60, f"user{index % 17}@example.test", actions[index % len(actions)],
                           f"benchmark audit entry {index} for survey {400 + index % parcel}", None))
    connection.executemany("INSERT INTO audit (ts, username, action, detail, doc_id) VALUES (?,?,?,?,?)", audit_rows)
    for parcel_index in range(1, parcel + 1, 2):  # every other parcel carries registers
        survey = f"{400 + parcel_index}"
        village = VILLAGES[parcel_index % len(VILLAGES)]
        connection.execute(
            """INSERT INTO land_mutations (id, mutation_no, survey_number, khasra_number, village, tehsil,
               district, previous_owner, new_owner, reason_type, deed_no, deed_date, documents,
               document_checklist, status, risk_status, risk_payload, encumbrance_status, reviewer,
               reviewer_notes, decided_at, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"BENCH-MUT-{parcel_index}", f"BM-{parcel_index}", survey, survey, village, "Benchmark Tehsil",
             "Benchmark District", "Old Owner", "New Owner", "SALE", f"REG-{parcel_index}",
             "2022-02-02", "[]", "[]", "COMPLETED", "UNKNOWN", "{}", "UNKNOWN", "bench", "", now, "bench", now, now))
        if parcel_index % 4 == 1:
            connection.execute(
                """INSERT INTO land_encumbrances (id, survey_number, khasra_number, village, tehsil, district,
                   owner_name, lender, reference_no, amount, start_date, status, notes, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"BENCH-ENC-{parcel_index}", survey, survey, village, "Benchmark Tehsil", "Benchmark District",
                 "Owner", "Benchmark Bank", f"BB-{parcel_index}", 100000.0 + parcel_index, "2023-03-03",
                 "ACTIVE", "benchmark", "bench", now, now))
    for case_index in range(8):
        connection.execute(
            """INSERT INTO land_court_cases (id, survey_number, khasra_number, village, tehsil, district,
               case_number, case_type, court_name, filed_date, status, parties, relief_sought,
               decision_summary, evidence_doc_ids, notes, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"BENCH-CC-{case_index}", f"{400 + case_index * 3}", f"{400 + case_index * 3}",
             VILLAGES[case_index % len(VILLAGES)], "Benchmark Tehsil", "Benchmark District",
             f"BENCH-CR-{case_index}", "CIVIL", "Benchmark Court", "2022-05-05", "ACTIVE",
             "A v. B", "Declaration", "", "[]", "benchmark", "bench", now, now))
    connection.commit()
    connection.close()
    print(f"benchmark dataset ready: {len(docs)} documents, {parcel} parcels, 2500 audit rows -> {db_path}")


def measure(db_path: str, out_path: str) -> None:
    os.environ["DB_PATH"] = db_path
    import server
    from main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    email, password = "bench-admin@example.test", "Benchmark Password 1!"
    client.post("/api/auth/signup", json={"full_name": "Bench Admin", "email": email, "password": password})
    with server.get_db() as db:
        db.execute("UPDATE users SET role='ADMIN' WHERE email=?", (email,))
    token = client.post("/api/auth/login", json={"email": email, "password": password}).json()["token"]
    headers = {"Authorization": "Bearer " + token}

    # --- query instrumentation (wraps the app's own connection class) -------
    stats = {"queries": 0, "db_ms": 0.0, "ddl": 0}
    original_execute = server.DBConnection.execute

    def counting_execute(self, query, params=()):
        statement = " ".join(str(query).split()).upper()
        if statement.startswith(("CREATE ", "ALTER ")):
            stats["ddl"] += 1
        started = time.perf_counter()
        try:
            return original_execute(self, query, params)
        finally:
            stats["queries"] += 1
            stats["db_ms"] += (time.perf_counter() - started) * 1000

    server.DBConnection.execute = counting_execute

    def run(name: str, method: str, url: str, **kwargs):
        stats["queries"], stats["db_ms"], stats["ddl"] = 0, 0.0, 0
        started = time.perf_counter()
        response = getattr(client, method)(url, headers=headers, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        record = {
            "endpoint": f"{method.upper()} {url}",
            "status": response.status_code,
            "ms": round(elapsed, 1),
            "queries": stats["queries"],
            "db_ms": round(stats["db_ms"], 1),
            "response_kb": round(len(response.content) / 1024, 1),
            "ddl_statements": stats["ddl"],
        }
        print(json.dumps(record))
        return record, response

    results = []

    record, response = run("documents", "get", "/api/documents")
    first_doc = (response.json().get("documents") or [{}])[0]
    record["ocr_in_list"] = "ocr_text" in first_doc
    results.append(record)

    record, response = run("audit", "get", "/api/audit")
    record["audit_rows_returned"] = len(response.json().get("audit", []))
    results.append(record)

    record, response = run("land-records", "get", "/api/land-records?limit=100")
    lands = response.json().get("land_records") or []
    results.append(record)

    record, _ = run("land-records-filtered", "get", "/api/land-records?q=Ambedarpur&limit=100")
    results.append(record)

    record, _ = run("risk-review", "get", "/api/land-records/risk-review?limit=100")
    results.append(record)

    if lands:
        land_id = lands[len(lands) // 2]["land_id"]
        record, _ = run("land-detail", "get", f"/api/land-records/{land_id}")
        results.append(record)
        record, _ = run("due-diligence", "post", f"/api/land-records/{land_id}/due-diligence")
        results.append(record)
        record, _ = run("litigation", "get", f"/api/land-records/{land_id}/litigation")
        results.append(record)
        record, response = run("report-generate", "post", "/api/reports/land-verification",
                               json={"land_id": land_id})
        results.append(record)
        reference = (response.json() or {}).get("reference_no")
        if reference:
            record, _ = run("report-html", "get", f"/api/reports/land-verification/{reference}")
            results.append(record)

    # second pass on the cached endpoints to expose steady-state cost
    record, _ = run("land-records-2nd", "get", "/api/land-records?limit=100")
    results.append(record)
    record, _ = run("risk-review-2nd", "get", "/api/land-records/risk-review?limit=100")
    results.append(record)

    payload = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "database": db_path, "results": results}
    with open(out_path, "w") as handle:
        json.dump(payload, handle, indent=2)
    print(f"\nwrote {out_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/tmp/bench_documents.db")
    parser.add_argument("--out", default="/tmp/perf_results.json")
    parser.add_argument("--skip-build", action="store_true", help="reuse an existing benchmark database")
    args = parser.parse_args()
    if not args.skip_build:
        build_dataset(args.db)
    measure(args.db, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
