# Mapping Module — Data Provenance, Geometry, and Operations

This document covers the GIS mapping / land-intelligence surface of the
application: how location evidence is produced, stored, validated, displayed
and exported, and what the system deliberately does **not** claim.

## Scope and architecture

| Piece | File(s) | Role |
|---|---|---|
| Map API + document geometry | `mapping.py` | `/api/map/*` endpoints, geometry validation/history, review queue, exports |
| Reference parcel read API | `parcel_locator.py` | RBAC-filtered `/api/map/properties`, `/api/map/parcel-search`, `/api/parcels/*` (mounted first, shadowing the legacy properties route) |
| Map UI | `map.html`, `map.js`, `map.css` | Leaflet map + offline schematic fallback, record list, review queue, boundary editor |
| Land records (cross-module) | `land_intel.py` | `focus_record_id` deep links into `/map` |
| Shared findings table | `ai_governance.py` + `mapping.py` | `verification_findings` — map-managed rows (`case_id IS NULL`, map finding types) vs AI-governance rows (`case_id` set) |

Deployment: SQLite locally, PostgreSQL on Render (`DATABASE_URL`). **No
PostGIS** — all spatial operations are application-side with bounding-box
prefilters on `documents.lat/lon`. Database-specific SQL is confined to
`server.DBConnection`; advanced spatial joins would require a future optional
PostGIS upgrade.

## Coordinate reference system

* All stored coordinates are **WGS84 longitude/latitude (EPSG:4326)**.
* GeoJSON input/output uses `[lon, lat]` (CRS84); the draw API accepts
  `{lat, lon}` objects or `[lat, lon]` pairs and stores `[lon, lat]`.
* There is no projection transformation step; area maths use a local
  equirectangular approximation in metres (`_boundary_area_m2`), valid for the
  small parcels this app handles, and are always labelled screening figures.

## Geometry provenance

Document geometry carries a source string written only by the code path that
created it:

| Source | Written by | Meaning |
|---|---|---|
| `Officer-digitized boundary` | `PUT /records/{id}/boundary` | Reviewer traced corners manually |
| `Imported survey GeoJSON` | `POST /records/{id}/boundary/import` | Uploaded GeoJSON, validated |
| `Area-based estimate (screening only)` | `POST /records/{id}/boundary/estimate` | Square derived from recorded area — NOT a boundary |
| `OCR-extracted printed coordinates` | derived from `coordinate_N` OCR fields | Printed corners parsed from the document |
| Reference parcels (`properties.geometry_source`) | seed/demo data | Explicitly labelled synthetic/reference-only |

Rules enforced by code and tests:

* Provenance is never upgraded implicitly — opening a record does not make an
  approximate or document-derived shape "official".
* Every endpoint response and export that includes geometry is marked
  `screening_only` / `authoritative: false` with disclaimers.
* `map_geometry_status` (`VERIFIED_BOUNDARY`, `IMPORTED_BOUNDARY`,
  `ESTIMATED_BOUNDARY`) and `map_geometry_reason` (free-text reason) sit next
  to the geometry; the full before/after chain lives in
  `document_geometry_history` (actor, timestamp, action, reason, previous and
  new geometry + sources) and is exposed at
  `GET /api/map/records/{id}/boundary/history`.
* Document pins (`lat`/`lon`) are reviewer-set exact locations with audit
  events (`location_set` / `location_cleared`), verification actor/time, and
  optional accuracy in **metres** (`location_accuracy_m`).
* Village-level geocodes are labelled `VILLAGE_LEVEL` /
  `APPROXIMATE — VILLAGE LOCATION` and are never counted as mapped coverage.

## Geometry validation (all write paths)

`_ring_geometry_checks` + `_polygon_ring_from_geojson` enforce, with
field-specific errors:

* numeric, **finite** coordinates (NaN/Infinity rejected), WGS84 ranges;
* closed rings (auto-closed on import), ≥ 3 distinct corners, non-zero area;
* no self-intersections (draw + import);
* vertex cap `MAP_MAX_IMPORT_VERTICES` (500) and the global 25 MB request cap
  (`security_hardening.MAX_REQUEST_BYTES`);
* single outer ring only (holes/MultiPolygon rejected by the importer).

Malformed input returns HTTP 400 and never crashes a worker; verified by
adversarial tests (`tests/test_mapping_hardening.py`).

## Area calculations and comparisons

* Recorded area is parsed from the document's `area` field with explicit unit
  conversion (ha/acre/ft² → m²; bare numbers are treated as m²). Recorded area
  and calculated geometry area are always returned as **separate** values.
* Geometry area: equirectangular local projection in metres.
* Tolerance for mismatch findings is configurable via
  `MAP_AREA_MISMATCH_PERCENT` (default **25 %**) — local survey practice,
  map scale and source precision vary, so the threshold is an environment
  setting, not a hard-coded constant.
* Findings compare values with units and explain the comparison; they are
  screening signals, never title statements.

## Parcel matching (`mapping._resolve`)

Evidence per candidate is separated:

* `evidence.positive` / `evidence.contradictory` / `evidence.missing`
* `evidence.source_reliability` — always states the source is the
  project-owned reference register, not an official cadastral source.

`resolution_status` values: `MATCH` (strong identifier, no conflicts),
`AMBIGUOUS_MATCH` (close scores **and a strong identifier match in play** →
human selection required; geography-only crowds never count as ambiguous),
`POSSIBLE MATCH`, `INSUFFICIENT_EVIDENCE` (geography-only — never auto-selectable),
`NO MATCH`, `INSUFFICIENT DATA`. Normalisation handles Unicode digits,
whitespace and sibling identifier columns (`survey` ↔ `gat`) without ever
equating `45` with `45/1`. Legacy `status` vocabulary is preserved for
existing consumers (`server.py`, `sa_investigation.py`).

## Spatial findings / review queue

Finding types (map-managed): `PIN_OUTSIDE_BOUNDARY` (ERROR),
`AMBIGUOUS_PARCEL` (ERROR — only when a strong cadastral identifier matches
several close parcels; geography-only candidates do not raise it),
`DUPLICATE_EXACT_LOCATION` (WARNING),
`RECORDED_AREA_MISMATCH` (WARNING), `REFERENCE_AREA_MISMATCH` (WARNING),
`CONFLICTING_RECORDED_AREA` (WARNING), `PIN_OUTSIDE_REFERENCE` (WARNING),
`DUPLICATE_SURVEY` (INFO).

Lifecycle: findings are recomputed on each queue GET with a **stable
fingerprint** (type + sorted evidence ids). Resolution decisions
(`RESOLVED`/`DISMISSED` + note + actor) persist while the evidence set is
unchanged; conditions that disappear mark the row `SUPERSEDED` with a note —
rows are never silently deleted. Rows owned by `ai_governance` (non-null
`case_id`) are never touched by the map queue, and only map-managed finding
types can be resolved through the map endpoint.

`GET /api/map/review-queue` filters: `status=open|resolved|dismissed|superseded|all`,
`severity=INFO|WARNING|ERROR`, `type=<finding_type>`.

## Query caps, pagination, exports

One constant governs every dataset view — `mapping.MAP_RECORD_CAP` (10 000):
`/records`, `/summary`, `/conflicts`, `/geojson`, KML, CSV and the review
queue all read the same population. Responses disclose `metadata.record_cap`,
`metadata.truncated` and `total_is_lower_bound` when the candidate window
saturates; CSV additionally returns `X-Map-Record-Count`/`X-Map-Record-Cap`
headers. Ordering is deterministic (`created_at DESC, id DESC`) and
`offset` provides stable pagination. CSV cells starting with
`=`, `+`, `-`, `@`, tab or CR are quote-prefixed against formula injection.

## Privacy and geocoding

* Geocoding is optional (`MAP_GEOCODER_URL`, `MAP_GEOCODER_USER_AGENT`);
  offline failures return `unavailable` and the map keeps working.
* Free-form queries containing owner/document/case markers are rejected —
  only place-shaped queries reach the provider; structured
  village/tehsil/district/state fields are preferred.
* Cache: in-memory + `geocode_cache` table, 7-day TTL (short negative TTL);
  global 1.1 s request spacing; results are labelled approximate.
* Exports and listings apply role visibility *before* any limit is applied
  (viewer: approved only; data officer: own uploads only).

## Concurrency

Write endpoints accept optional `expected_updated_at` (taken from the record
the editor loaded). A mismatch returns **409** with `code: STALE_RECORD`, so a
reviewer cannot silently overwrite a newer edit. Omitting the field keeps the
legacy behaviour for older clients.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MAP_RECORD_CAP` (code constant) | 10000 | Unified dataset cap |
| `MAP_MAX_IMPORT_VERTICES` (code constant) | 500 | Import vertex limit |
| `MAP_AREA_MISMATCH_PERCENT` | 25 | Area mismatch tolerance (%) |
| `MAP_GEOCODER_URL` | Nominatim search endpoint | Geocoder provider |
| `MAP_GEOCODER_USER_AGENT` | project UA string | Provider etiquette |

## Performance

`scripts/map_perf_bench.py` seeds clearly-labelled synthetic data and
measures listing, bbox, summary, exports, review-queue recompute, single-record
detail endpoints, and validation latency. Measured results and known
bottlenecks are reported in `MAPPING_AUDIT.md`. Browser/Playwright rendering
is covered only by the opt-in suite (`MAP_BROWSER_URL`), which requires a
local Chromium and was not part of default CI runs.

## What this system does NOT claim

* No official cadastral boundaries, government integrations, or legal title
  determinations — OpenStreetMap/satellite/geocoder output is never treated
  as an authoritative parcel source.
* Geometric overlap, risk scores, and missing documents are review signals,
  not proof of fraud, encroachment, or ownership.
* Synthetic/demo parcels (`DEMO-*`, `BENCH-*`) are labelled as such in data
  and UI.
