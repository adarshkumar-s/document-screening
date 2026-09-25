# DOCUMENT SCREENING

**Document-grounded land-record mapping for the existing screening portal**

This repository is a FastAPI document-screening application with a narrow GIS workspace adapted from the mapping workflow in `adarshkumar-s/Portfolio`. The map is an index of screened document records; it is not a cadastral authority, legal opinion, or ownership determination.

## What is preserved

The canonical `server.app` remains the source of truth for:

- authentication, roles, and session access;
- document upload and validation;
- OCR and AI-assisted extraction;
- document comparisons, queues, tasks, and administration;
- AI governance, approvals, and the audit trail.

The replacement is intentionally limited to the mapping surface and mapping-only document coordinates/history. The map never edits OCR fields or document identity fields.

## Portfolio-derived map workflow

Open `/map` from the portal after signing in. The replacement map provides:

- **Village Sheet** — cascading district, tehsil/taluka, and village selectors; plot/survey grouping; plot search; source-record browsing; neighbouring-plot navigation;
- **Real Map** — Leaflet record map with keyless global OpenStreetMap Humanitarian tiles by default, Esri imagery/OpenTopoMap fallbacks, native-zoom safeguards, and a fully offline schematic mode;
- **document-grounded positions** — exact reviewer pins, cached village-level geocodes, and explicit unresolved locations instead of fabricated coordinates;
- **record navigation** — filtered record sidebar, exact marker selection, popup actions, plot-to-map jumps, refresh, and stable local Leaflet assets;
- **coverage dashboard** — exact-pin, village-approximate, unresolved, survey, village, district, and review-queue metrics;
- **review filters and export** — filter by location quality/status and download a role-scoped CSV map register;
- **year-wise history** — survey-and-village passbook containing the current record, earlier records, ownership-chain changes, transfer evidence, and deterministic review signals;
- **audited exact pins** — Verification Officers and Administrators can set or clear a document pin; every change is written to the existing audit table;
- **privacy-aware visibility** — viewer and data-officer access follows the same document visibility rules as the canonical document API.

External tile and geocoding services are optional. Tile failures leave the record list and schematic mode available. Nominatim lookups are cached and throttled; unresolved lookups never create coordinates.

## Mapping files

- `mapping.py` — replacement mapping/support layer. It exposes the Portfolio-derived document map and history APIs, and retains only the compatibility helpers required by existing AI/OCR/audit-adjacent flows (`_resolve`, ownership reasoning, governed property-location actions, and idempotent schema support).
- `map.html`, `map.css`, `map.js` — replacement mapping UI.
- `static/vendor/leaflet/` — locally served Leaflet assets; the map does not depend on a CDN to start.
- `main.py` / `site.py` — ASGI entrypoints that add only map, history, and AI-approval routers to the canonical app.
- `tests/test_mapping.py` — replacement map smoke and compatibility tests.

The old Land Intelligence UI, credential-free demo router/assets, and old mapping route registration are intentionally absent.

## OCR language coverage

The upload and staff OCR selectors expose 21 Tesseract language packs: English, Hindi, Telugu, Tamil, Bengali, Marathi, Gujarati, Punjabi, Kannada, Odia, Urdu, Assamese, Malayalam, Nepali, Sanskrit, Sindhi, Sinhala, Arabic, Persian, Burmese, and Tibetan. Auto-detection retains the existing multilingual fallback and never changes the document validation or approval rules.

## Land Intelligence extension (encumbrances, mutations, land risk, reports)

The following extend the canonical screening flow **without replacing it**. OCR,
AI governance, RBAC, the audit trail, the verification queue and the map are
untouched; the extension reuses their helpers (document visibility, audit,
property resolution).

- **Encumbrance register** — loans/mortgages recorded against a parcel
  (survey + village key, optional khasra) with `ACTIVE / RELEASED / UNKNOWN`
  status, lender, reference number, amount, dates, evidence document and full
  audit coverage. Verifier/Admin write; every authenticated user reads.
- **Mutation workflow** — a governed namantaran/ferfar application queue
  (`RECEIVED → UNDER_REVIEW → VERIFIED → COMPLETED` or `REJECTED`) with an
  event log per mutation, document checklist, reviewer notes, and completion
  as a **human, Administrator-only** action. Completion records an audited
  register event; it never rewrites OCR document fields.
- **Deterministic land-risk engine** — rule-based signals computed from local
  data only: `ACTIVE_ENCUMBRANCE`, `SALE_DURING_ENCUMBRANCE`,
  `OWNER_CONFLICT_YEAR`, `OWNER_CHANGE_NO_MUTATION`, `PENDING_MUTATION`,
  `REJECTED_MUTATION`, `AREA_JUMP`, `DUPLICATE_CONFLICT`,
  `REJECTED_CONFLICT_COPY`, `CHAIN_GAP`, `SUSPICIOUS_MUTATION_SEQUENCE`,
  `LOW_QUALITY_EXTRACTION`. Verdicts map to `CLEAR / REVIEW / HIGH_RISK` and
  always carry evidence (documents, encumbrances, mutations). This is a
  workflow signal — never a legally authoritative fraud determination.
- **Approval safety gate** — completing a mutation on land with an active
  encumbrance returns `409` with the lender/reference/amount evidence and is
  blocked in the UI behind an explicit human confirmation. RBAC is never
  bypassed and blocked attempts are audited.
- **Verification integration** — the existing document detail API carries a
  `land_context` panel (encumbrance banner + risk verdict) rendered inside the
  existing review card; approving a document on encumbered/high-risk land
  returns a `land_risk_warning` workflow signal.
- **Land record details** — one aggregated view per parcel: property, current
  owner, ownership history (documents + completed mutations), mutations,
  encumbrances, risk, documents, map link and (admin-only) the audit trail.
- **Verification reports** — `LAND RECORD VERIFICATION REPORT` (printable HTML
  + JSON) with a machine-readable QR that identifies the report reference only.
  The report explicitly states it is **not** an official government title
  certificate. Reviewer/Admin generate; reviewer/admin view; audited.
- **Map show mode** — the existing map gains a `Show: Selected record`
  (default, only the selected record is emphasized) / `Show: All records`
  toggle. Filters, neighbouring plots, exact/approximate locations and the
  non-authoritative geometry disclaimers are unchanged.
- **Tile resilience** — fallback sequence is now OSM Humanitarian → OSM mirror
  (openstreetmap.de) → Esri → OpenTopoMap → OSM → offline schematic. No new
  external dependency.
- **Data Management (Administration)** — admin-only full backup (ZIP with
  `manifest.json`, portable logical export of every application table —
  documents/OCR state, verification state, AI governance, land records, map
  data, mutations, encumbrances, risk registers, audit trail, users — plus
  every uploaded scan) and a safe restore that validates the archive and
  manifest, writes an automatic safety copy of the current data first, then
  restores, verifies counts, and audits every step. Member sanitisation
  rejects path traversal; RBAC rejects non-admins with 403.
- **Demo scenarios** — ten deterministic, admin-governed fixtures
  (`POST /api/admin/demo/seed`) covering clean records, valid mutations,
  active encumbrances, ownership changes without mutations, conflicts, area
  jumps, pending/rejected mutations, low-quality OCR and duplicates. All demo
  artifacts are tagged and removable via `DELETE /api/admin/demo/data`
  without touching production data. Several demo parcels carry reference
  geometry or stored coordinates (clean, mutation, encumbered, conflicting,
  point-only cases) so the Locate workflow can be exercised on real demo data;
  one demo record intentionally has no spatial data and demonstrates the
  explicit “location unavailable” state.
- **Locate workflow (All Records → parcel → map)** — every record row in
  *All Records* (and in the viewer catalog and the Land Intelligence records
  list) has a **Locate** action. It resolves the record through the canonical
  parcel resolver and opens the existing map view on that exact parcel
  (`/map?locate=1&land_id=…&parcel=…&document_id=…`): polygons are drawn and
  fitted, point-only parcels are centred with a marker, the selected parcel is
  highlighted with a popup (survey/khasra, village, owner, land ID) and the
  panel offers **Open Land Intelligence** and **View record**. Selecting
  another record deselects and redraws the previous parcel. Records with no
  stored geometry or coordinates fall back **once** to the existing village
  geocoder (`POST /api/map/geocode`, cached and throttled), which is stored on
  the parcel as `GEOCODED_ADDRESS` and always shown as *“Location resolved from
  address — verify”*; if that fails, the record stays in the explicit
  *“Location unavailable — no verified coordinates”* state. Location data is
  never silently invented or upgraded to a verified/cadastral location.

### Canonical parcel response (stable shape)

`POST /api/parcels/resolve` and `GET /api/land-records/{land_id}/parcel` return
one canonical spatial object per land record. The map page, the Locate action,
the land-record detail view and the Land Intelligence list all consume this
single shape:

```jsonc
{
  "matched": true, "located": true, "match_method": "survey_village",
  "land_id": "LR-…", "parcel_id": "…", "property_id": "…",
  "survey_number": "103", "khasra_number": "103",
  "village": "…", "tehsil": "…", "district": "…", "owner": "…",
  "geometry": { "type": "Polygon", "coordinates": [] },   // null when only a point exists
  "bounds": [[28.64, 77.11], [28.64, 77.12]],              // Leaflet [[south, west], [north, east]]
  "centroid": { "latitude": 28.6434, "longitude": 77.11825 },
  "location": {
    "status": "PARCEL_GEOMETRY",          // VERIFIED_LOCATION | PARCEL_GEOMETRY | GEOCODED_ADDRESS | UNRESOLVED
    "state": "REFERENCE_GEOMETRY",        // VERIFIED | STORED_POINT | REFERENCE_GEOMETRY | GEOCODED | UNRESOLVED
    "label": "Located on map",            // or "Location resolved from address — verify"
                                          // or "Location unavailable — no verified coordinates"
    "note": "…", "latitude": 28.6434, "longitude": 77.11825,
    "source": "…", "confidence": 0.9, "verified": false, "verified_by": null,
    "resolved_from_address": false, "authoritative": false,
    "can_geocode": false, "geocode_query": null
  },
  "geometry_authoritative": false, "reference_only": true,
  "documents": [], "focus_document_id": "…", "candidates": [],
  "quality": { "ambiguous": false, "survey_scope": "village-scoped", "records": 1, "issues": [] },
  "urls": { "map": "/map?locate=1&…", "land_intelligence": "/?land_id=…", "record": "/?open_document=…" },
  "disclaimer": "…"
}
```

Matching order (strongest identifier first, owner names are **never** used):
`parcel_id`/`property_id` → `land_id` → linked document (`property_documents`)
→ survey/khasra **plus village** (village-scoped; a survey number alone that
matches several villages returns `ambiguous_survey` with candidates instead of
guessing) → document/village identity → the reviewer's exact document pin →
persisted address geocode → explicit `UNRESOLVED`. Approximate, geocoded and
reference geometry is never presented as an authoritative cadastral boundary;
`authoritative` is only ever true when the stored source says so.

### New API surface

```
GET/POST            /api/encumbrances
GET/PUT             /api/encumbrances/{id}
POST                /api/encumbrances/{id}/release
GET/POST            /api/mutations
GET                 /api/mutations/{id}            (+ /events)
POST                /api/mutations/{id}/review     (verifier/admin)
POST                /api/mutations/{id}/complete   (admin only; safety gate)
GET                 /api/land-records              (paginated, role-scoped)
GET                 /api/land-records/{land_id}    (detail; includes `parcel` + `map`)
GET                 /api/land-records/{land_id}/parcel   (canonical spatial object)
GET                 /api/land-records/risk-review  (verifier/admin)
GET                 /api/land-records/{land_id}/encumbrances | /mutations
POST                /api/parcels/resolve           (canonical parcel for Locate)
POST                /api/reports/land-verification
GET                 /api/reports/land-verification/{ref}(.qr.png)
GET/POST/DELETE     /api/admin/demo/scenarios | /seed | /data   (admin only)
GET                 /api/admin/data-management/backup(/manifest) (admin only)
POST                /api/admin/data-management/restore            (admin only)
```

`documents` gaining `land_context` and `review-action` returning an optional
`land_risk_warning` are the only changes to existing APIs; both are additive.

## Routes

### Mapping UI

- `GET /map` — replacement village-sheet and real-map workspace.
- `GET /map.css` — explicit CSS asset route.
- `GET /map.js` — explicit JavaScript asset route.

### Authenticated map API

- `GET /api/map/records` — visible uploaded-document records and extracted land fields; supports `q`, `district`, `tehsil`, `village`, `status`, `location`, and `limit` filters.
- `GET /api/map/summary` — role-scoped coverage and review metrics.
- `GET /api/map/export.csv` — role-scoped CSV map register for review/reporting.
- `PUT /api/map/records/{doc_id}/location` — audited verifier/admin exact pin update; send `{"lat":null,"lon":null}` to clear.
- `POST /api/map/geocode` — cached, throttled village/district lookup for approximate markers with a seven-day cache policy.
- `GET /api/documents/{doc_id}/history` — year-ordered survey/village passbook with ownership-chain and transfer review signals.

### Existing application API

All existing `/api/auth`, `/api/documents`, OCR, validation, task, administrator, AI-governance, comparison, and audit routes remain owned by `server.py` and continue to be available through `main:app`.

The compatibility property/workflow APIs used by existing governed paths remain available inside the mapping support layer; they are not the replacement map UI or its data source.

## Data boundary and safety

The map consumes uploaded document fields and mapping-only coordinates. It does not scrape a government portal, redistribute government cadastral data, or send uploaded documents to tile/geocoding providers. A map position is labelled as exact, village-level approximate, or unresolved. Approximate coordinates never represent a legal parcel boundary.

Ownership changes are signals for human review. AI and deterministic checks can recommend review but cannot approve ownership, declare fraud, or change a consequential record without the existing governed approval flow.

Do not commit secrets, credentials, or real sensitive documents. Set `JWT_SECRET` and `ADMIN_INITIAL_PASSWORD` privately in deployments. The default local database path is controlled by the existing application configuration.

## Local run

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/`, sign in, and choose **Land Records Map**. For OCR, install Tesseract locally; the existing Docker setup includes the project’s OCR dependencies.

## Docker

```bash
docker build -t document-screening .
docker run --rm -p 10000:10000 document-screening
```

The container runs `main:app`.

## Testing

```bash
python -m pytest -q
```

The suite covers the existing application regression paths plus map asset loading, document-grounded records, visibility, exact-pin RBAC/audit, cached geocoding, history navigation, and compatibility helpers. JavaScript syntax checks use `node --check map.js` and the existing portal scripts.
