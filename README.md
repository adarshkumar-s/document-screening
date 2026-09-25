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
  without touching production data.

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
GET                 /api/land-records/{land_id}    (detail; risk via /risk)
GET                 /api/land-records/risk-review  (verifier/admin)
GET                 /api/land-records/{land_id}/encumbrances | /mutations
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

## SA / Admin AI and the Verification Officer AI

The Admin panel's AI entry point is now `Admin AI 🔒` — the gateway to **SA**,
the privileged administrator assistant. The normal (non-SA) assistant lives in
the Verification Officer interface as a small AI icon next to the AI Task icon.

### Admin AI 🔒 (SA)

- SA is NOT accessible before authentication. Clicking `Admin AI 🔒` prompts
  `Enter AI access password`. After server-side verification the assistant
  greets `Welcome, <verified administrator name>` and SA is available.
- The password is verified **server-side** and resolves to an already
  configured administrator identity (`valid credential → administrator
  identity → SA session`). The browser cannot submit a name, `isAdmin`,
  `unlocked=true`, hidden form fields, or JS variables and become anyone.
- **Credential ownership is enforced**: an SA credential belongs to exactly
  one administrator and can only be created and used by that administrator.
  Administrator A can neither create nor use a credential of administrator B
  (the endpoint always binds to the calling administrator; unlock only ever
  considers the caller's own credentials).
- Credentials are stored argon2id-hashed (never plaintext, never logged,
  never returned) and can be seeded with `SA_ACCESS_PASSWORD` or
  rotated via `POST /api/sa/credentials` (Administrator only, self).
  **Rotation is real**: it invalidates and scrubs the previous credential and
  revokes every existing SA session for/by that identity — the new password
  must be used to re-authenticate.
- SA authorization is a **short-lived server-side session** bound to the
  logged-in administrator (user id + session version) and the verified
  identity. It expires (`SA_SESSION_TTL_SECONDS`, default 15 minutes), is
  revoked on logout / password change / role change, and requires
  re-authentication after expiry. The httpOnly cookie only carries an opaque
  token; the database stores its SHA-256 hash.
- All existing SA security controls are preserved: RBAC, tool allowlists,
  approval gates (proposals execute only after explicit Administrator
  approval in the AI Approval Center), mutation safety, audit logging, actor
  isolation (one administrator can never touch another's requests), and
  prompt-injection protections. The model can never grant itself tools and
  model output is never authorization.

### SA timeout / Try again (no duplicate mutations)

SA requests run through a safe task lifecycle (`assistant_tasks.py`):

- Every logical request carries a client-generated `request_id`; the original
  request is preserved server-side automatically.
- On timeout or a recoverable provider failure the UI shows
  `The assistant timed out.` with a real `Try again` button (and a
  Cancel/Dismiss path). Try again re-attaches to the SAME logical request -
  nothing has to be re-typed.
- **A timeout followed by Try Again can NEVER execute the same logical
  operation twice**: completed requests replay their stored result; running
  requests are re-attached (never started twice); and any re-executed runner
  is idempotent through proposal idempotency keys, so one logical request can
  create at most one proposal and an approved proposal executes exactly once
  (a retried approval replays the stored outcome).
- Running work is owned by a **worker lease with a real heartbeat**: a live
  worker can never be reclaimed, however long it runs. Only a provably dead
  lease (missed heartbeats past the lease timeout — the owning process is
  gone) may be re-claimed, and every execution owns a `run_id` so a zombie
  worker can never overwrite a reclaimed execution's state or result.
- Proposal approval is a **single-writer execution claim** (`PROPOSED ->
  EXECUTING` compare-and-set in SQL): of two concurrent approvals exactly one
  can acquire the claim and run the mutation, the other gets a safe
  conflict/re-attach response, and completion is ownership-guarded and
  **fail-closed** (`execution_claim`, exactly-one-row writes on
  `EXECUTING -> EXECUTED/FAILED`): a missing rowcount is never treated as
  success, a stale executor can never overwrite an outcome or emit a false
  audit event, and it receives a safe conflict/re-attach response instead of
  reporting its stale result. An already `EXECUTED` approval still replays
  its stored result. The assistant request claims (`_claim` /
  `_claim_stale_running` / `_mark_finished`) use the same fail-closed
  exactly-one-row ownership checks.
- Credential rotation commits **atomically in one transaction** (old
  credentials deactivated + hashes scrubbed + existing SA sessions revoked +
  new credential registered together): there is no committed state where the
  new password works while an old captured SA session is still alive.
- The SA unlock throttle is **database-backed and shared by every application
  worker/process** (atomic admission via a counter-row upsert): multiple
  workers or restarts cannot bypass the failed-attempt limit. Successful
  authentication clears the state; expired windows reset and old records are
  cleaned; no secret is ever stored in the throttle.
- Permanent authorization or validation errors never offer Try Again.

### Verification Officer AI (normal assistant)

- Appears as a small ✨ AI icon next to the AI Task icon in the Verification
  Officer interface (`js/officer-assistant.js`).
- It may only explain documents and fields, summarize evidence, analyze the
  information available to the officer, answer questions, explain land-record
  terminology, and suggest verification checks.
- It is an assistant only. Server-side it exposes exactly one read-only
  endpoint (`POST /api/officer/assistant/query`) with allowlisted read-only
  lookups: no mutations, no approvals/rejections, no administrative actions,
  no SA access, no admin tools, no impersonation, no authorization or session
  changes. Prompt-injection attempts and action requests are refused
  deterministically, and a Verification Officer calling the API manually or
  sending malicious prompts still cannot obtain any privileged capability.

New/changed API surface:

```
POST                /api/sa/unlock                 (admin login + AI access password)
POST                /api/sa/lock
GET                 /api/sa/session
POST                /api/sa/credentials            (admin only; configure/rotate)
POST                /api/admin/assistant/query     (SA session required; request_id idempotent)
POST                /api/admin/assistant/briefing  (SA session required; request_id idempotent)
GET                 /api/admin/assistant/requests/{request_id}
POST                /api/admin/assistant/requests/{request_id}/retry
POST                /api/admin/assistant/requests/{request_id}/cancel
POST                /api/officer/assistant/query   (Verification Officer only; read-only)
```

Files: `sa_gateway.py` (credential/session security lock),
`assistant_tasks.py` (idempotent request lifecycle),
`officer_assistant.py` (read-only normal AI), `js/officer-assistant.js` and the
SA parts of `js/admin-assistant.js` (UI). Tests live in
`tests/test_sa_gateway.py`, `tests/test_sa_lifecycle.py`, and
`tests/test_officer_assistant.py`.

The compatibility property/workflow APIs used by existing governed paths remain available inside the mapping support layer; they are not the replacement map UI or its data source.

## Data boundary and safety

The map consumes uploaded document fields and mapping-only coordinates. It does not scrape a government portal, redistribute government cadastral data, or send uploaded documents to tile/geocoding providers. A map position is labelled as exact, village-level approximate, or unresolved. Approximate coordinates never represent a legal parcel boundary.

Ownership changes are signals for human review. AI and deterministic checks can recommend review but cannot approve ownership, declare fraud, or change a consequential record without the existing governed approval flow.

Do not commit secrets, credentials, or real sensitive documents. Set `JWT_SECRET` and `ADMIN_INITIAL_PASSWORD` privately in deployments. The default local database path is controlled by the existing application configuration.


## AI Admin Assistant and Superior SA mode

The normal **AI Admin Assistant** remains the default assistant and keeps its existing authority. Its knowledge now covers the portal's document, verification, Land Intelligence, mapping, litigation, reporting, audit, administration, backup/restore, and AI-governance surfaces.

Typing the configured SA activation phrase in the assistant input opens the secure **SA** identity gate. The UI presents **Gautam**, **Adarsh**, and **Devi Cr**; the selected identity must match the authenticated administrator account, so the selector cannot be used for impersonation.

SA provides project-wide, multi-step administrator assistance: it can plan investigations across registered website capabilities, execute independent read-only checks in parallel, retain investigation context within the conversation, and prepare consequential operations. Consequential operations are never executed directly by SA. They become proposals in the existing AI Approval Center, where the administrator must approve them; the server re-validates the current target state before execution.

Every SA session is recorded with the authenticated administrator, admin.<identity> label, session ID, task, plan, evidence, proposals, completion events, timestamps, and errors. The **SA Activity Report** button exposes the current session report, and GET /api/admin/assistant/sa/report can retrieve an administrator's report.

### SA configuration

Set these deployment variables privately:

SA_ACTIVATION_CODE=<your private activation phrase>
SA_SESSION_TTL_SECONDS=3600
SA_MODEL=gemini-3.6-flash

Do not commit the real activation phrase. Production refuses to activate SA unless SA_ACTIVATION_CODE is configured.

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
