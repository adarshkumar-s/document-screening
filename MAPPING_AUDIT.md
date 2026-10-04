# GIS Mapping Module — Audit Report

**Scope:** `mapping.py`, `parcel_locator.py`, `map.html` / `map.js` / `map.css`,
mapping routes in `server.py` / `main.py`, mapping sections of `land_intel.py`,
`ai_governance.py` (shared `verification_findings` table), mapping tests, and the
schema/migration paths they touch.

**Method:** full source inspection of the mapping surface, plus empirical
reproduction against the real application (`TestClient` against `main.app` with an
isolated database) before any fix was written. Findings below are labelled
**[VERIFIED]** (reproduced with an executable test) or **[RISK]** (confirmed in
code, not yet user-reachable in the default deployment).

**Baseline test result (before changes):** `667 passed, 11 skipped` (`pytest -q`,
Python 3.11, repo at `9fd436f`, which includes merged PRs #36 and #37).

---

## 1. Data lifecycle as implemented

1. **Upload/OCR** → `documents.fields` JSON (survey/khasra/village/area/coordinate corners).
2. **Location** → three states computed in `_map_document_item`: exact pin
   (`documents.lat/lon`, reviewer-set), village-level (document village field,
   geocode on request), unresolved.
3. **Boundary geometry** → `documents.map_geometry` (officer digitised, area
   estimate, GeoJSON import) or OCR corner fields (`coordinate_1..5`) via
   `_document_boundary`.
4. **Reference parcels** → `properties` table (explicitly labelled synthetic /
   reference-only, seeded demo geometry), linked to documents via
   `property_documents`.
5. **Matching** → `mapping._resolve` scores candidates from the `properties`
   table; selection is human-confirmed (`PUT /records/{id}/parcel-selection`).
6. **API** → `/api/map/*` (mapping router) plus `/api/map/properties`,
   `/api/map/parcel-search` (parcel_locator router, mounted first and therefore
   shadowing mapping's own `/api/map/properties` — intentional per its comment).
7. **Rendering** → `map.js` (Leaflet + offline schematic fallback), client-side
   grid clustering, show-mode selected/all.
8. **Spatial checks / review queue** → `_reference_comparison`,
   `_refresh_review_findings` → `verification_findings` table.
9. **Audit** → `log_audit` on every location/boundary/selection write.

CRS assumption throughout: **WGS84 lon/lat (EPSG:4326)**, GeoJSON `[lon, lat]`
ordering on input/output, `[lat, lon]` only for Leaflet. Area maths use a local
equirectangular projection (`_boundary_area_m2`, metres) — acceptable for small
parcels; results are screening figures, never presented as legal area.

---

## 2. Verified defects (reproduced before fixing)

| # | Severity | Defect | Evidence |
|---|----------|--------|----------|
| D1 | **Critical** | `GET /api/map/review-queue` returns **500** whenever a detected finding's document has no linked property: `_refresh_review_findings` inserts `verification_findings.property_id = None` into a `NOT NULL` column → `sqlite3.IntegrityError` unhandled. Two same-pin documents (the simplest finding) are enough. No test covered this endpoint. | repro script + tracked regression test `test_review_queue_survives_findings_without_linked_property` |
| D2 | **Critical** | Findings never stay resolved: every queue GET marks all `OPEN` rows `SUPERSEDED` and re-inserts fresh rows with new IDs, so a **resolved finding reappears as a new open finding** on the next fetch. Resolution state and history are lost. | repro: resolve → next GET returns new id with same evidence |
| D3 | **High** | The queue GET **also supersedes unrelated rows** in the shared `verification_findings` table — including `AI_PROPOSAL` findings written by `ai_governance` verification cases (`case_id` set). Opening the map silently changes another module's workflow state. | repro: `AI-FOREIGN-1` status `OPEN → SUPERSEDED` |
| D4 | **High** | `POST /records/{id}/boundary/import` accepts **out-of-range coordinates** (stored `lat=999, lon=500`, HTTP 200) and **non-finite coordinates** (`NaN` in raw JSON): validation only checks ring closure/self-intersection, never numeric finiteness or ranges. The NaN path additionally **crashes the worker** (unhandled `ValueError` while serialising the response). | repro: import 200 + stored garbage; NaN → unhandled exception |
| D5 | **High** | **CSV formula injection**: `export.csv` writes attacker-controlled text fields (`owner`, `filename`, …) verbatim; `=cmd|' /C calc'!A0` is exported as-is and executes in spreadsheet software. | repro: payload present unescaped in CSV row |
| D6 | **High** | Dead area-mismatch finding: `_refresh_review_findings` reads `comparison["area_difference_percent"]`, a key `_reference_comparison` never produces → the recorded/boundary area review signal **can never fire**. | repro: comparison keys listed, key absent |
| D7 | **Medium** | `GET /records/{id}/parcel-candidates` derives `resolution_status` from `len(candidates)`: a single weak geography-only candidate (no strong identifier match, score ≈ 0.14) is reported as **`MATCH`**, overstating evidence; two candidates are `AMBIGUOUS` regardless of score separation. | code + `test_parcel_candidates_do_not_overstate_weak_matches` |
| D8 | **Medium** | **Geometry history absent**: boundary set/import/estimate/clear overwrite `map_geometry` without preserving the previous version or a reason column — no recovery, no before/after record (spec §13). Import ignores `reason` entirely. | repro: no history table/columns after two edits |
| D9 | **Medium** | **Inconsistent record caps across endpoints** — `/records` uses a 10 000 window, `/summary` silently uses 1 000, `export.csv` 1 000, `export.geojson`/`kml` 10 000, `/conflicts` 5 000. Counts, dashboard stats and exports therefore refer to *different* datasets once >1 000 records exist; CSV truncates silently. | code paths in `_map_visible_records` default `limit=1000` |
| D10 | **Medium** | **Non-deterministic ordering**: `ORDER BY created_at DESC` with no tiebreaker; records inserted in the same transaction share `created_at`, so pagination between pages can shift or duplicate rows. | code; regression test with equal timestamps |
| D11 | **Medium** | **O(N) full-dataset loads for single-record requests**: `/records/{id}/spatial-checks`, `/layers`, `/parcel-candidates` each call `_map_visible_records(limit=10000)` then filter in Python; `document_history` runs `SELECT * FROM documents` (including `ocr_text` blobs) for the whole table. `_map_location_audit_context` scans the entire location audit history per map load. `_refresh_review_findings` re-queries the full `properties` table once per record via `_resolve`. | code |
| D12 | **Low** | Latent wrong location SQL: `location=VILLAGE_LEVEL/UNRESOLVED` SQL branches match the literal substring `%village%` inside the fields JSON — nearly every record contains that key, so the SQL branch would classify everything as village-level. Currently bypassed (the only caller passes `location=""` and filters in Python), but unusable as written. | code |
| D13 | **Low** | No stale-write protection: a reviewer editing a record that another reviewer just changed silently overwrites (no version/`updated_at` check on location/boundary writes). | code |
| D14 | **Medium** | **Ambiguity over-firing**: `AMBIGUOUS_MATCH` / `AMBIGUOUS_PARCEL` were emitted whenever top candidates had close scores — *without requiring a strong cadastral identifier*. Documents whose only candidates agreed at district/village level (weakest-evidence crowds, spec §4.10 INSUFFICIENT_EVIDENCE territory) were flagged as needing human parcel selection, inflating the review queue (9,999 of 10,000 synthetic records) and overstating what the evidence supports. Found while profiling the queue recompute. | profiler + queue inspection; fixed |

## 3. Risks and gaps (code-confirmed, not yet user-reachable or lower impact)

- **R1** No `offset` on `/api/map/records`: `limit`/`has_more` exist but no safe
  second page (frontend downloads everything up to the cap).
- **R2** `/api/map/properties` implemented in `mapping.py` is *shadowed* by
  `parcel_locator`'s RBAC version (registration order in `main.py`). It remains
  unrestricted logic behind the secure route — defence-in-depth would apply the
  same visibility filter, but changing registration order could break the
  intentional security posture; documented instead.
- **R3** Export provenance: GeoJSON export carries `screening_only` metadata but
  no CRS member or per-feature accuracy/verification fields beyond
  `location_accuracy_m`; KML has no provenance extended data.
- **R4** Geocoder: no per-user rate limit (only a global 1.1 s spacing), TTL 7 d
  for positives / ~7 d minus 60 s for negatives, offline fallback returns
  `unavailable` — privacy markers block document/person text. Acceptable but
  worth documenting.
- **R5** `map_properties`' `total` is computed after SQL `LIMIT`, so `total` ≤
  `limit` (shadowed route anyway).
- **R6** Browser rendering / Playwright suite is opt-in (`MAP_BROWSER_URL`) and
  was **not** run in this environment.
- **R7** No PostGIS: deployment runs SQLite (local) or plain PostgreSQL (Render).
  All spatial ops are app-side with bbox prefilters; bbox filter only considers
  `lat/lon` pins, not polygon extents.

## 4. What already works (preserved)

- RBAC on pin/boundary/import/selection writes (`VERIFICATION_OFFICER`/`ADMIN`),
  viewer/data-officer read scoping, audit events on every location/boundary write.
- Self-intersection and zero-area rejection on drawn boundaries; coordinate range
  checks on the draw path; privacy-screened geocoder; offline schematic fallback;
  explicit non-authoritative disclaimers; show-mode selected/all; grid clustering;
  tile fallback chain; `parcel_locator` RBAC shadowing of reference properties;
  route-integrity guards from PR #37.

---

## 5. Fix status (implemented on this branch)

| Defect | Status | Fixed in |
|---|---|---|
| D1 review-queue 500 on unlinked findings | **Fixed** + regression test | `mapping.py` (`_refresh_review_findings`), `tests/test_spatial_findings.py` |
| D2 resolutions resurrect | **Fixed** + lifecycle tests | `mapping.py` (stable fingerprints, status persistence) |
| D3 foreign findings clobbered | **Fixed** + guard test | `mapping.py` (map-managed finding scope) |
| D4 import accepts NaN/out-of-range, crashes worker | **Fixed** + adversarial tests | `mapping.py` (`_polygon_ring_from_geojson`, `_ring_geometry_checks`) |
| D5 CSV formula injection | **Fixed** + test | `mapping.py` (`_csv_safe`) |
| D6 dead area-mismatch key | **Fixed** + positive/negative tests | `mapping.py` (`RECORDED_AREA_MISMATCH`, `MAP_AREA_MISMATCH_PERCENT`) |
| D7 parcel-candidate overstatement | **Fixed** + tests | `mapping.py` (`_resolve` resolution_status/evidence) |
| D8 no geometry history/reason | **Fixed** + tests | `mapping.py` (`document_geometry_history`, `/boundary/history`) |
| D9 inconsistent caps | **Fixed** + test | `mapping.py` (`MAP_RECORD_CAP`, export headers) |
| D10 non-deterministic ordering | **Fixed** + pagination test | `mapping.py` (`created_at DESC, id DESC`, `offset`) |
| D11 O(N) detail/history loads | **Fixed** + perf benchmark | `mapping.py` (`_map_record_by_id`, slim history columns, audit doc filter) |
| D12 broken location SQL clauses | **Fixed** + filter tests | `mapping.py` (Python-side location semantics) |
| D13 no stale-write protection | **Fixed** + tests | `mapping.py` (`expected_updated_at` → 409), `map.js` |
| D14 ambiguity over-firing | **Fixed** + tests | `mapping.py` (`_resolve` requires a strong-identifier match for `AMBIGUOUS_MATCH`; queue emission likewise — geography-only crowds resolve to `INSUFFICIENT_EVIDENCE` and raise no `AMBIGUOUS_PARCEL`) |

Frontend: stale-request guard, structured 409 messages, boundary provenance +
history panel, review-queue status views with counts, truncation hint, legend
addition (`tests/test_map_workspace_ui.py`).

**Measured performance (synthetic benchmark — TestClient, fresh local SQLite,
deterministic seed, p50 of 3 runs after warmup; `scripts/map_perf_bench.py`;
synthetic data only, not production capacity claims):**

Review-queue recompute (`GET /api/map/review-queue`, full finding recomputation):

| Records | Original | Intermediate | **Final** | Speed-up |
|--------:|---------:|-------------:|----------:|---------:|
| 1,000   | 24,780 ms | 2,693 ms | **517 ms** | **47.9×** |
| 10,000  | 516,751 ms | 60,383 ms | **9,449 ms** | **54.7×** |

Other operations at 10,000 records (final): records listing (limit 5000) 945 ms ·
bbox viewport query 11 ms · summary 459 ms · CSV export 584 ms · GeoJSON export 531 ms ·
parcel-candidates (single doc) 39 ms · spatial-checks (single doc) 25 ms ·
29 DB statements per listing (O(1) per request; was 5 pre-index, none are per-row).

Fix chain (all `mapping.py`, behavior held by tests): (1) shared inverted property
index — one index build per queue recompute instead of a parcel scan per record,
deterministic bucket ordering; (2) lazy candidate materialisation — flat tuples while
scoring, dicts/geometry parsed only for the returned top-10 (`materialize_properties=False`
on the queue path); (3) document/parcel normalisation keys computed once (profiler had
recorded 32 M `_key_for` calls per 10k recompute); (4) `doc_property_link` cached once
per recompute; (5) reconciliation skips the UPDATE when the stored finding is identical.
Profiler before: ~101 s cumulative in `_key_for` alone; after: hottest helper is
`dict.get` at ~2.7 s (profiler overhead included).
