# FULL PRODUCTION AUDIT — `adarshkumar-s/document-screening`

**Scope:** deployed `origin/main` (`615de0b`) — every subsystem: boot, routes, RBAC/authz, DB integrity, audit trail, security, performance, AI governance, litigation↔risk↔reports↔PDF↔UI consistency, demo-data safety, backup/restore, deployment.

> **Branch note (substitution):** the task requested branch `arena/full-production-audit`. This session is pinned to `arena/01a0c9e3-document-screening` and cannot create branches, so **all work was done on and pushed to `arena/01a0c9e3-document-screening`**. Nothing was merged to `main`.

---

# SESSION 2 — CORRECTION PASS (branch `arena/01a0ffb7-document-screening`)

> **The prior report's headline claim did not hold on this checkout.** Section 2
> and Section 8 below state `pytest -q` → *"215 passed"*. Measured on this
> checkout at base commit `4dd3018`, the same command returned
> **217 failed, 389 passed, 11 skipped, 30 errors** in 33.55 s. The claim
> *"All roles work server-side ✅ probed live"* and *"Reports/PDF/UI consistent ✅"*
> were also false: `GET /api/land-records/{id}` answered **422** for every
> request, and `js/land-intel.js` had a hard `SyntaxError` that made browsers
> discard the entire Land Intelligence UI.
>
> Everything in this section was executed, not inferred.

## S2.1 Measured baseline (before any change)

| Check | Result at `4dd3018` |
|---|---|
| `pytest -q` | **217 failed, 389 passed, 11 skipped, 30 errors** (33.55 s) |
| `python -m compileall -q .` | clean (the defects were not syntax errors) |
| `node --check js/land-intel.js` | **FAILS** — `land-intel.js:420  return \`` `SyntaxError: Invalid or unexpected token` |
| `GET /api/land-records/{land_id}` | **422** `missing: land, documents, state, risk, timeline` |
| `POST /api/documents/{id}/review-action {approve}` | **409** for any record lacking an optional field |

Isolating the causes: neutralising the abuse limiter alone moved the suite from
217 failed to **27 failed**, i.e. ~190 of the failures had one root cause.

## S2.2 Defects found and fixed (this session)

| # | Sev | Defect (verified) | Fix |
|---|---|---|---|
| 1 | **Critical** | `@land_router.get("/{land_id}")` was separated from its handler by an inserted helper, so Python applied the decorator to `_investigation_pipeline_status`; FastAPI read its five arguments as **required query params**. Every parcel-detail request returned 422 (~25 tests + the whole land-detail surface). | Decorator restored to `land_record_detail`; helpers left in place. Pinned by `tests/test_route_integrity.py`. |
| 2 | **Critical** | `js/land-intel.js` lines 420–448 contained literal backslash-escaped backticks (`\``, `\${`). Browsers discard the whole script, so the Land Intelligence UI (completeness, conflict centre, master review queue) was dead. | Unescaped on exactly those 14 lines; `node --check` clean. Same failure class as prior report's bug #2 — it recurred. |
| 3 | **Critical** | `security_hardening`'s sliding windows are process-global keyed by client IP. Under pytest every `TestClient` shares one peer, so the 5-signups/hour budget was exhausted by the first fixtures and ~190 tests failed `429 Too many signup attempts`. | Limits now env-configurable (`RATE_LIMIT_*`, invalid values fall back to defaults); added `reset_buckets()`; `tests/conftest.py` resets per test. |
| 4 | **High** | The approval gate required `validation.verdict == "valid"`, but any empty *optional* field (boundaries, land class, ownership type, khata, mutation/registration no.) sets the verdict to `review`. **No real land record could ever be approved** — always 409. | Documented rule set next to `APPROVAL_REQUIRED_FIELDS`: invalid values and missing *identity* fields block; legitimately-empty optional fields do not. Advisory `verdict` semantics unchanged. 409 now returns per-field `blockers` with a readable `detail` string. |
| 5 | **High** | `mapping._ensure_tables` migrated every `documents` location column except `location_verified_by` / `location_verified_at`, which the audited exact-pin UPDATE writes → `sqlite3.OperationalError: no such column`. Every pin set/clear failed (4 tests). | Both columns added to the idempotent migration. |
| 6 | **High** | OCR cache: engine-failure entries were rejected by the empty-text guard *before* the engine-error branch, so they were never reused; and the cache-hit path reported them as `CACHED` / `READY_FOR_REVIEW`. | Engine-failure entries serve only while the engine is still missing, are invalidated the moment it is reachable, and always report `ENGINE_UNAVAILABLE` / `INSTALL_OCR_ENGINE` / `escalated: true`. |
| 7 | **Medium** | A preprocessing failure (odd image mode, truncated object) aborted the entire OCR pass. | Grayscale/resize/enhance/threshold/denoise degrade independently, log to stderr, and surface `preprocessing_warnings` + `engine_errors` in the result. |
| 8 | **Medium** | `ocr_land_bridge` had no `extract_pdf_text`, but `tests/test_pdf_land_intelligence_bridge.py` imports it → `ImportError`. | Added as an embedded-text-first wrapper over `extract_pdf_evidence`. |
| 9 | **Medium** | The suite reused `./data/land_records.db`, so rows from earlier sessions leaked into assertions — `test_li_risk_verdicts_match_expected` failed once unrelated `Banner Ville` parcels had accumulated. | `tests/conftest.py` gives the suite its own database (an explicit `DB_PATH` is still respected). |
| 10 | **Low** | `js/app.js` surfaced a non-string `detail` as `[object Object]`. | Renders `detail`/`message` plus any structured `blockers`. |

Two test *fixtures* were also reaching the wrong seam and are fixed: one
patched `server.pytesseract` although the installed high-recall runtime calls
`pytesseract` directly (so it depended on a locally installed Tesseract that
CI's test job does not provide); one passed an incomplete injected-server stub.

## S2.3 New capability: explicit extraction states

`extraction_states.py` owns one **pure** classifier producing six named
outcomes — `EXTRACTION_SUCCESS`, `NEEDS_HUMAN_REVIEW`,
`TEXT_FOUND_FIELDS_MISSING`, `NO_TEXT_DETECTED`, `OCR_ENGINE_UNAVAILABLE`,
`AI_FALLBACK_FAILED` — each with a reason, a recommended action, the evidence
that decided it, and the thresholds in force (env-configurable, defaults match
`validate_single_field`). Absence of evidence never yields a success, and
`ai_not_configured` is not treated as a failure because no rescue was possible.

Wired into `run_fast_ocr_pipeline` (result + `pipeline_meta`) and derived on
read by `GET /api/documents/{id}` from the persisted row, so documents
processed before the change also report a state and no backfill migration is
needed. Surfaced in the review card in `js/app.js`.

## S2.4 Tests executed in this session (all actually run)

| Command | Result |
|---|---|
| `pytest -q` at base `4dd3018` | 217 failed, 389 passed, 30 errors |
| `pytest -q` after fixes | **663 passed, 11 skipped, 0 failed** |
| `pytest -q` (2 further consecutive runs) | 663 passed each — no order dependence |
| `python -m compileall -q .` | clean |
| `node --check` on every first-party `.js` | clean (`js/land-intel.js` was failing) |
| `python -c "from main import app"` | OK |
| Production startup (`APP_ENV=production`, uvicorn on 0.0.0.0:8000) | `/healthz` → `{"status":"healthy","ai_enabled":false,"ocr_engine":{"tesseract":false,"pdf":true}}`; `/`, `/map`, `/js/land-intel.js` all 200 |

New tests: `tests/test_route_integrity.py` (6) and
`tests/test_extraction_states.py` (21). The route guard was **verified to
fail** when the decorator defect is reintroduced (2 failures) and pass when
fixed — an unfalsifiable guard is not a guard.

## S2.5 Environment limitation (stated, not worked around)

**Tesseract is not installed in this sandbox and cannot be installed** —
`apt-get update` fails (`deb.debian.org` unreachable; only PyPI is reachable).
Two consequences, both verified rather than assumed:

* `/healthz` correctly reports `"tesseract": false`, so the diagnostic path is
  real and not fabricated.
* All 663 tests pass **without** an OCR engine. That is a genuine property of
  the suite, and it is why the two fixtures that depended on a local Tesseract
  were rewired to deterministic seams.

What was therefore **not** measured here: real OCR field-accuracy benchmarks.
No OCR benchmark number is claimed in this report, because no reviewed
reference dataset was available in this environment. Producing one is the
highest-value remaining task (see S2.6).

## S2.6 Remaining known limitations after this session

* **No OCR accuracy benchmark.** The mandate's benchmark (field coverage,
  exact-match, per-field accuracy, character similarity) needs a
  human-reviewed reference set; none exists in the repo and no OCR engine is
  installed here to generate one honestly.
* **GIS/parcel-matching and risk-rule logic was not extended** this session;
  the existing engine was only made reachable again by fixing defect #1.
* **Report generation was not modified.** PDF generation is covered by the
  existing suite but was not re-exercised beyond it.
* The review-card extraction panel is parse-verified (`node --check`) and its
  data contract is API-tested, but it was **not rendered in a real browser** —
  no browser tooling is available in this sandbox.
* `landrec_system_v3.9.6.zip` (5.2 MB) is still committed; prior report's
  recommendation to move it out of git is unchanged.


---

## 1. Exact SHAs

| Commit | Content |
|---|---|
| `615de0b` | deployed `origin/main` (audit target) |
| `fda2e1c` | merge of `origin/main` into the session branch — tree byte-identical to main (main's side won all 15 add/add conflicts; the previously reverted perf files were `git rm`'d) |
| `b9ad839` | **audit fix batch 1** — boot SyntaxError, favicon, dup-email 409, demo collisions, DDL-free requests, paginated audit/documents, batched land queries + 18 regression tests |
| `225d174` | SA tables created at startup (fixes CLI deadlock + stdout JSON pollution) |
| `ce1bc91` | AI-governance tables at startup, DB_PATH-aware guards, deterministic test bootstrap |
| `098e5b8` | **js/admin-assistant.js syntax fix** + node `--check` regression gate (**HEAD, pushed**) |

Base for the audit: `fda2e1c` was verified byte-identical to `origin/main`, so every finding below is a defect **in the deployed code**, not inherited from prior Arena work.

## 2. Tests executed (all green — nothing claimed unexecuted)

- `pytest -q` (full suite): baseline after merge+repair **196 passed** → final **215 passed** (196 + 15 audit-regression tests + 4 performance tests), run ≥4 times on fresh DBs.
- `python -m compileall -q .` — clean.
- `node --check` on all 5 first-party JS files (`js/app.js`, `js/land-intel.js`, `js/admin-assistant.js`, `map.js`, `portal-ui.js`) — clean after fix; **failed on deployed main** for `js/admin-assistant.js`.
- `scripts/seed_demo_data.py` live: `--check-only` (rc=1 INCOMPLETE, 46 planned, 0 written) → `--yes` (46 created) → `--yes` again (idempotent, 0 created) → `--clear --yes` (0 demo rows remain, real rows untouched).
- Live route matrix against the running app (urllib probes + managed uvicorn server): anonymous 401s, viewer/officer/reviewer/admin/SA role gates, IDOR on documents/files, forged/revoked tokens, hostile inputs (`limit=abc`, 10 KB query strings, path-traversal IDs), demo seed/clear, backup/restore, governance transitions, report/PDF/QR.

## 3. Bugs found and FIXED (11)

| # | Severity | Defect in deployed main | Fix |
|---|---|---|---|
| 1 | **Blocker** | `admin_assistant.py` contained literal `\n` text inside two Gemini model-candidate list literals → `SyntaxError` at import → **the app could not boot at all**; 8 test files failed collection | Repaired both blocks; repo-wide `ast.parse` clean |
| 2 | High | `js/admin-assistant.js` had unbalanced parentheses in `beginSaActivation()` → `node --check` fails → **browsers discard the whole script; the entire AI-assistant/SA UI is dead** | Corrected the call; added a pytest gate that parses every first-party JS file with node (CI already checks it — the corrupted commit evidently never ran through CI) |
| 3 | High | `POST /api/auth/signup` and admin `POST /api/users` with an existing email → **500 IntegrityError** | Case-insensitive pre-check → **409** with a friendly message (both endpoints) |
| 4 | High | `POST /api/admin/demo/seed` aborted with **500 + partially seeded dataset** when the mutation register held a real `M-2026-0012` (proven live: 12 docs + 1 encumbrance written, then crash) | Demo mutations use `DEMO-M-` numbers and skip any `mutation_no` owned by another row; seeding never modifies rows it didn't create; verified live (200, real row intact, re-seed idempotent, clear preserves real data) |
| 5 | Medium | `/api/documents` shipped full OCR payloads for every row, unpaginated; `?limit=abc` silently 200 | Light projection (no `ocr_text`/`cleaned_ocr_text`/`original_fields`) + `limit/offset/total` + 422 validation; detail endpoint still returns full OCR; RBAC/filters unchanged |
| 6 | Medium | `/api/audit` dumped the whole table (500-row cap only) and selected `*`; bad `date_from` silently ignored | SQL-side pagination + `action`/`username`/`q`/`date_from`/`date_to` filters (422 on bad dates), explicit columns, contract kept (`audit` key + `total/limit/offset` added); admin-only RBAC re-verified |
| 7 | Medium | **Request-time DDL**: `court_cases.ensure_schema`, `land_intel.ensure_land_tables`, `sa_agent._ensure_tables`, `ai_governance.ensure_governance_tables`/`_ensure_task_table` ran `CREATE TABLE/INDEX` inside user requests (can block for a long time on a large DB) | All schema creation moved to startup; once-per-process guards keyed to `DB_PATH`; regression test asserts **zero DDL statements** across land/docs/audit/SA/governance/backup request paths |
| 8 | Medium | `init_db` created the `documents`/`audit` hot-path indexes lazily via request paths (revert regression) | Index migration moved into startup `init_db` |
| 9 | Medium | **N+1 register queries**: `/api/land-records` and `/api/land-records/risk-review` issued 2 queries per parcel (encumbrances + mutations), detail re-fetched each register 2–3× | Registers fetched once per request and filtered in memory (identical results, asserted by test); detail reads each register exactly once; query count now **constant as the portfolio grows** (pinned by tests) |
| 10 | Medium | SA-table creation ran inside `init_db`'s open transaction → second connection hit **"database is locked"** and stalled every CLI subprocess for the sqlite timeout (~5 s each) and printed a warning into `--json` stdout, corrupting it | Moved after the transaction commits; warning → stderr; suite runtime dropped 94 s → ~37 s |
| 11 | Low | Test-bootstrap env (`ADMIN_INITIAL_PASSWORD`) was set only inside `test_ocr_workflow.py` at import → login tests depended on import order | `os.environ.setdefault` moved to `tests/conftest.py` before any test module imports |

Also fixed in passing: missing `/favicon.svg` route (index.html referenced a 404 asset) — favicon now 200.

## 4. Verified CLEAN (no fix required)

- **Auth core:** argon2 hashes (+legacy sha256 upgrade-on-login), HMAC-signed tokens with per-request DB role+`version` revocation, httponly/samesite cookies, signup always DATA_OFFICER, CORS localhost-only. All **server-side** (frontend hiding was never treated as authorization).
- **RBAC/IDOR:** every sensitive route probed for anon/viewer/DATA_OFFICER/VERIFICATION_OFFICER/admin/SA — correct 401/403/200 everywhere; officer cannot read another officer's DRAFT detail or file; encumbrance/mutation write gates hold; forged and revoked tokens 401.
- **Secrets:** `JWT_SECRET` is env-only, hard-required in production, random per-process in dev; `.env.example` ships **empty** secret placeholders with guidance; no hardcoded credentials (admin bootstrap is env-gated).
- **Upload/OCR failure paths:** empty file, garbage PDF, truncated PNG → clean 422s with **zero half-created records** (docs count verified before/after).
- **Backup/restore** (`/api/admin/data-management/*`): covers **all 22 tables including litigation** (`land_court_cases` 6, `land_mutations` 7, `land_encumbrances` 4, `documents` 29 restored), writes a safety copy, self-verifies (`verification.ok: true`), **double-restore adds zero duplicates**, malformed zip / zip-slip member / missing manifest → 400 with specific messages, admin-only (officer 403), fully audited (`BACKUP_EXPORTED`, `BACKUP_RESTORE_STARTED`, `BACKUP_RESTORED`).
- **AI governance:** closed action registry (`DROP_DATABASE` rejected 400), proposals execute only after admin approval, server-side execution verified (doc mutated only by the approval flow), double-approve/reject-after-execute/approve-after-reject/expired → 409, illegal target status → 400 + proposal FAILED with recorded error, officer 403, all events audited. AI may propose only pre-verification statuses (never straight to APPROVED) — intentional guardrail, kept.
- **Reports ↔ detail consistency:** verification report (JSON + valid `%PDF` + QR) matches the land detail exactly (owner, survey/khasra, area, risk verdict, case counts, mutations, timeline, supporting docs); reviewer-only (officer 403); audited.
- **Litigation/risk/timeline cross-consistency:** risk-review list vs detail 16/16 identical verdicts+counts; detail timeline contains the court case, mutation, encumbrance and document events; disclaimers present.
- **Frontend XSS:** `esc()`/`escapeHtml()` applied consistently across all innerHTML sinks in all 5 scripts; remaining unescaped interpolations are computed numbers/static config only.
- **Demo data safety:** seeds are `DEMO-`-prefixed, idempotent (2× `--yes` → 0 created), collision-detecting, `--clear` removes only demo rows (real rows planted before seeding survived), refuses production (APP_ENV guard, tested).

## 5. Performance (current main, after fixes)

| Path | Before audit (deployed) | After |
|---|---|---|
| Request-path DDL statements | >0 on land/SA/governance/court-case paths | **0** (test-enforced) |
| Land index / risk review query count | grows 2 queries per parcel; 215+ queries on realistic data | **constant** (≤12 regardless of parcels, test-enforced) |
| Documents list payload | full OCR text per row (MB-scale) | **53 KB** for the 29-doc demo set (detail endpoint keeps OCR) |
| Audit payload | whole-table dump | **9 KB** per 200-row page, SQL-paginated |
| Demo CLI subprocess startup | ~5 s stall ("database is locked") | instant |

Prior measured numbers from the earlier (reverted) pass — land-records 113→9 queries, risk-review 215→9, documents 1616→185 KB — are directionally reproduced here by the re-implemented subset; the reverted middleware was **not** re-introduced (per Section 13 of the mandate): only compatible, test-pinned fixes were re-landed.

## 6. Deployment concerns (recommendations, not changed)

1. **`landrec_system_v3.9.6.zip` (5.2 MB)** — a full source snapshot of an older system committed as a binary (contains QUICKSTART, build scripts, its own README). Repo bloat + duplicate-maintenance + supply-chain confusion. Recommend removing from git and storing externally. **Not removed** (data-preservation mandate).
2. **CI bypass:** the repo's workflow (`py_compile`, `node --check` on all 5 JS files, ASGI import check, pytest, docker build) would have caught bugs #1, #2 and the boot failure — the corrupted commits evidently never ran through it. Recommend requiring CI green before merge to `main`.
3. **`data/backup_before_restore_*/` safety copies** accumulate unbounded (gitignored, but disk growth). Recommend a retention policy.
4. Dev-mode `JWT_SECRET` is random per process → sessions reset on every restart in development. Acceptable; documented in `.env.example`.

## 7. Remaining known limitations

- Full-scale timing benchmarks (10k+ documents) were not re-run post-revert; the regression suite now pins **query counts** (deterministic) instead of wall-clock timings (flaky) — the performance characteristics above follow from those pinned counts.
- `land_intel._court_case_index()` still loads all litigation rows per request (small register, indexed); left as-is to preserve behavior.
- Session-branch constraint: `arena/full-production-audit` could not be created (see branch note).

## 8. Acceptance checklist (mandate §“≈30 items”)

| Item | Status |
|---|---|
| App starts from a clean checkout | ✅ (was broken on deployed main; fixed) |
| All roles work server-side | ✅ probed live |
| RBAC server-side, no frontend-only authz | ✅ |
| No request-time DDL | ✅ test-enforced |
| No obvious N+1 | ✅ test-enforced constant query counts |
| No demo collisions / no overwriting real data | ✅ live-proven incl. collision + clear |
| No privilege escalation | ✅ incl. SA activation path (env code + hmac + admin identity match) |
| No secret leakage | ✅ env-only secrets, empty `.env.example` values |
| Backup/restore safe (all tables, no dupes, malformed-safe, authz, audited) | ✅ |
| Audit trail paginated, filterable, complete | ✅ |
| Reports/PDF/UI consistent | ✅ |
| `pytest -q` | ✅ 215 passed |
| `compileall` | ✅ |
| `node --check` | ✅ all 5 scripts |
| Seed CLI `--check-only` / `--yes`×2 / `--clear --yes` | ✅ |
| Live smoke (healthz, pages, assets, login, queues) | ✅ final process running the pushed code |
| Working tree clean | ✅ |
| Branch pushed | ✅ `arena/01a0c9e3-document-screening` @ `098e5b8` |
| Not merged to main | ✅ (awaiting explicit instruction) |
