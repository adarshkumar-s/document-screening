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
- **Real Map** — Leaflet record map with keyless OpenStreetMap France tiles by default, Esri imagery/OpenTopoMap fallbacks, native-zoom safeguards, and a fully offline schematic mode;
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
