"""Deterministic document intelligence for the Document Map workspace.

All findings are derived from the application's stored document fields/OCR metadata.
No external geocoding, LLM calls, or invented land facts are used here.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException

from server import get_current_user, get_db
from document_mapping import _doc, _mapping, _value, _num

router = APIRouter(prefix="/api/land/document-insights", tags=["Document Intelligence"])

KEYS = {
    "owner": ("owner_name",),
    "father": ("father_name",),
    "survey": ("survey_number", "gat_number", "khasra_number", "plot_number"),
    "khasra": ("khasra_number",),
    "khata": ("khata_number",),
    "area": ("area", "land_area", "plot_area"),
    "village": ("village",),
    "tehsil": ("tehsil", "taluka"),
    "district": ("district",),
    "date": ("document_date", "date", "registration_date"),
    "mutation": ("mutation_no", "mutation_number"),
    "registration": ("registration_no", "registration_number"),
}

TRANSFER_TYPES = ("sale", "mutation", "transfer", "gift", "partition", "release", "conveyance")


def field(fields: Dict[str, Any], name: str):
    return _value(fields, *KEYS.get(name, (name,)))


def norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip().casefold())


def date_key(v: Any):
    if not v:
        return None
    s = str(v).strip()
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", s)
    if m:
        return (int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"\b(19\d{2}|20\d{2})\b", s)
    return (int(m.group(1)), 1, 1) if m else None


def confidence(document: Dict[str, Any]) -> Optional[float]:
    return _num(document.get("mean_conf"))


def risk_radar(document: Dict[str, Any], mapping: Dict[str, Any]) -> Dict[str, Any]:
    fields = document.get("fields") or {}
    issues: List[Dict[str, Any]] = []
    conf = confidence(document)
    if conf is not None and conf < 70:
        issues.append({"severity": "high" if conf < 50 else "medium", "code": "LOW_OCR_CONFIDENCE", "title": "Low OCR confidence", "evidence": f"Stored mean confidence is {conf:.0f}%"})
    required = (("owner", "Owner name"), ("survey", "Survey / land identifier"), ("village", "Village"), ("district", "District"), ("date", "Document date"))
    for key, label in required:
        if not field(fields, key):
            issues.append({"severity": "medium", "code": "MISSING_" + key.upper(), "title": f"Missing {label}", "evidence": "The extracted field is empty."})
    if mapping.get("available") is False:
        issues.append({"severity": "low", "code": "NO_GEO_EVIDENCE", "title": "Not georeferenced", "evidence": "No coordinates or GeoJSON geometry were found in the uploaded record."})
    status = str(document.get("status") or "").upper()
    if status in {"REJECTED", "RETURNED_TO_DATA_OFFICER"}:
        issues.append({"severity": "high", "code": "WORKFLOW_ATTENTION", "title": "Workflow attention required", "evidence": f"Current record status is {status}."})
    if not issues:
        level = "LOW"
    elif any(i["severity"] == "high" for i in issues):
        level = "HIGH"
    elif any(i["severity"] == "medium" for i in issues):
        level = "MEDIUM"
    else:
        level = "LOW"
    return {"level": level, "score": min(100, len(issues) * 18 + (35 if conf is not None and conf < 50 else 0)), "issues": issues}


def compare_fields(a: Dict[str, Any], b: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for key, label in (("owner", "Owner"), ("father", "Father name"), ("survey", "Survey / land identifier"), ("khasra", "Khasra"), ("khata", "Khata"), ("area", "Area"), ("village", "Village"), ("tehsil", "Tehsil / Taluka"), ("district", "District"), ("date", "Document date"), ("mutation", "Mutation number"), ("registration", "Registration number")):
        av, bv = field(a, key), field(b, key)
        if norm(av) == norm(bv):
            state = "match" if av not in (None, "") else "missing"
        elif av in (None, "") or bv in (None, ""):
            state = "uncertain"
        else:
            state = "mismatch"
        out.append({"key": key, "label": label, "a": av, "b": bv, "state": state})
    return out


def consistency_report(documents: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = []
    for i in range(len(documents)):
        for j in range(i + 1, len(documents)):
            a, b = documents[i], documents[j]
            af, bf = a.get("fields") or {}, b.get("fields") or {}
            av = compare_fields(af, bf)
            mismatches = [x for x in av if x["state"] == "mismatch"]
            shared_survey = field(af, "survey") and field(bf, "survey") and norm(field(af, "survey")) == norm(field(bf, "survey"))
            same_village = field(af, "village") and field(bf, "village") and norm(field(af, "village")) == norm(field(bf, "village"))
            transfer = any(x in str(a.get("doc_type") or "").lower() + " " + str(b.get("doc_type") or "").lower() for x in TRANSFER_TYPES)
            if shared_survey and same_village and norm(field(af, "owner")) == norm(field(bf, "owner")):
                relation = "possible_duplicate"
                headline = "Potential duplicate: same land identifier and owner"
            elif shared_survey and transfer:
                relation = "possible_transfer"
                headline = "Related transfer documents; differences may be legitimate"
            elif shared_survey and mismatches:
                relation = "discrepancy"
                headline = "Conflicting extracted fields for the same land identifier"
            elif shared_survey:
                relation = "consistent"
                headline = "Related records are consistent on available fields"
            else:
                relation = "coincidental_match"
                headline = "No strong relationship established"
            rows.append({"a_id": a["id"], "b_id": b["id"], "relation": relation, "headline": headline, "differences": mismatches, "shared_survey": bool(shared_survey), "same_village": bool(same_village)})
    return {"pairs": rows, "related_count": sum(1 for r in rows if r["relation"] in {"consistent", "possible_transfer", "discrepancy", "possible_duplicate"})}


def explanation(risk: Dict[str, Any], consistency: Dict[str, Any]) -> Dict[str, Any]:
    if any(x["relation"] in {"discrepancy", "possible_duplicate"} for x in consistency["pairs"]):
        recommendation, code = "CAUTION_DISCREPANCY", "CAUTION_DISCREPANCY"
    elif risk["level"] == "HIGH":
        recommendation, code = "REVIEW_REQUIRED", "REVIEW_REQUIRED"
    elif risk["level"] == "MEDIUM":
        recommendation, code = "REVIEW_REQUIRED", "REVIEW_REQUIRED"
    else:
        recommendation, code = "ROUTINE_CLEAR", "ROUTINE_CLEAR"
    reasons = [i["evidence"] for i in risk["issues"]]
    reasons += [p["headline"] for p in consistency["pairs"] if p["relation"] in {"discrepancy", "possible_duplicate"}]
    return {"code": code, "recommendation": recommendation, "reasons": reasons or ["No deterministic screening issue was detected from the available stored evidence."], "disclaimer": "This is screening assistance, not legal confirmation of ownership or title."}


def load_document(document_id: str) -> Dict[str, Any]:
    d = _doc(document_id)
    m = _mapping(d)
    d["mapping"] = m
    d["public"] = {"id": d["id"], "filename": d.get("filename"), "doc_type": d.get("doc_type"), "status": d.get("status"), "confidence": d.get("mean_conf"), "created_at": d.get("created_at"), "updated_at": d.get("updated_at")}
    return d


def related_documents(target: Dict[str, Any]) -> List[Dict[str, Any]]:
    tf = target.get("fields") or {}
    identifiers = [field(tf, x) for x in ("survey", "khasra", "khata", "village")]
    identifiers = [norm(x) for x in identifiers if x]
    with get_db() as db:
        rows = db.execute("SELECT id,filename,doc_type,status,fields,mean_conf,created_at,updated_at FROM documents ORDER BY id DESC").fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d["id"] == target["id"]:
            continue
        try: d["fields"] = json.loads(d.get("fields") or "{}")
        except Exception: d["fields"] = {}
        vals = [norm(field(d["fields"], x)) for x in ("survey", "khasra", "khata", "village")]
        shared = [v for v in vals if v and v in identifiers]
        if shared:
            out.append(d)
    return out


def ensure_audit_chain():
    """Maintain a separate tamper-evident projection without changing the existing audit writer/schema."""
    with get_db() as db:
        db.execute("CREATE TABLE IF NOT EXISTS audit_chain (id INTEGER PRIMARY KEY, ts REAL NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, doc_id TEXT, prev_hash TEXT NOT NULL, entry_hash TEXT NOT NULL)")
        audits = db.execute("SELECT id,ts,username,action,detail,doc_id FROM audit ORDER BY id ASC").fetchall()
        existing = {r[0]: r[6] for r in db.execute("SELECT id,entry_hash FROM audit_chain ORDER BY id ASC").fetchall()}
        prev = ""
        for r in audits:
            rid = int(r[0])
            payload = f"{prev}|{rid}|{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5] or ''}"
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if existing.get(rid) != digest:
                db.execute("INSERT OR REPLACE INTO audit_chain (id,ts,username,action,detail,doc_id,prev_hash,entry_hash) VALUES (?,?,?,?,?,?,?,?)", (rid,r[1],r[2],r[3],r[4],r[5],prev,digest))
            prev = digest


def audit_status(doc_id: Optional[str] = None):
    ensure_audit_chain()
    with get_db() as db:
        rows = db.execute("SELECT id,ts,username,action,detail,doc_id,prev_hash,entry_hash FROM audit_chain ORDER BY id ASC").fetchall()
    prev = ""
    valid = True
    result = []
    for r in rows:
        expected = hashlib.sha256(f"{prev}|{r[0]}|{r[1]}|{r[2]}|{r[3]}|{r[4]}|{r[5] or ''}".encode("utf-8")).hexdigest()
        ok = expected == r[7] and r[6] == prev
        valid = valid and ok
        if doc_id is None or str(r[5]) == str(doc_id):
            result.append(dict(r))
        prev = r[7]
    return {"chain_valid": valid, "entries": result}


@router.get("/{document_id}")
def document_insights(document_id: str, user=Depends(get_current_user)):
    d = load_document(document_id)
    related = related_documents(d)
    docs = [d] + related
    risk = risk_radar(d, d["mapping"])
    consistency = consistency_report(docs)
    return {"document": d["public"], "mapping": d["mapping"], "risk": risk, "consistency": consistency, "explanation": explanation(risk, consistency), "timeline": timeline(docs)}


def timeline(documents: List[Dict[str, Any]]):
    events = []
    for d in documents:
        f = d.get("fields") or {}
        events.append({"id": d["id"], "filename": d.get("filename"), "doc_type": d.get("doc_type"), "status": d.get("status"), "date": field(f, "date"), "date_sort": date_key(field(f, "date")), "owner": field(f, "owner"), "survey": field(f, "survey"), "area": field(f, "area")})
    events.sort(key=lambda x: (x["date_sort"] is None, x["date_sort"] or (9999,12,31), str(x["id"])))
    return events


@router.get("/{document_id}/audit")
def document_audit(document_id: str, user=Depends(get_current_user)):
    _doc(document_id)
    return audit_status(document_id)


@router.get("/{document_id}/compare/{other_id}")
def document_compare(document_id: str, other_id: str, user=Depends(get_current_user)):
    a, b = load_document(document_id), load_document(other_id)
    diffs = compare_fields(a.get("fields") or {}, b.get("fields") or {})
    return {"a": a["public"], "b": b["public"], "fields": diffs, "changed": [x for x in diffs if x["state"] == "mismatch"]}
