# DEMO-LI — Synthetic Land Intelligence Demo Dataset

> **SYNTHETIC DEMO DATA — NOT REAL RECORDS.** Every person, parcel, bank, court,
> case number, deed number and loan number below is fictional. No real Aadhaar/PAN,
> land record, court case or government identifier is used. Persons are bare
> fictional names (Gautam, Saurav, Adarsh, Shivangi, Kiran, Manohar, Sarita, Bhavesh,
> Yashoda, Pramila, Devendra, Lakhan, Vrinda, Omkar, Girija, Shyam, Devaki,
> Nandkishor, Raghav, Bhola, Bhagwati, Madho).

- **Dataset namespace:** `DEMO-LI-`
- **Geography:** fictional district *Kishandham*, tehsil *Hariharpur*, villages *Devnapur* and *Shantiban* (state *Demo Pradesh*)
- **Seed (admin):** `POST /api/admin/demo/seed` with `{"scenario": "LI"}` (or `"all"` to include the original S1-S10 fixtures)
- **Wipe (admin):** `DELETE /api/admin/demo/data` — removes only demo-tagged artifacts
- **Machine-readable index:** `GET /api/admin/demo/land-intel/index`
- **Seeded by:** `land_demo_data.py`; documents rendered by `tools/generate_demo_documents.py` from `land_demo_docs.py`

## Scenario index

| Scenario ID | Parcel (survey · village) | Scenario type | Expected Land Intelligence result | Expected risk | Documents to upload | Expected alert | Related mutation | Related encumbrance | Related court case |
|---|---|---|---|---|---|---|---|---|---|
| **DEMO-LI-MUT-001** | 71/2 · Devnapur | MUTATION — approved transfer chain | Owner change Gautam -> Saurav is bridged by a COMPLETED mutation; no adverse signal. | **CLEAR** | `DEMO-LI-MUT-001-sale-deed.png`, `DEMO-LI-MUT-001-mutation-order.png` | No alert; land matched; verdict CLEAR. | DEMO-LI-MUT-001-M1 (M/DEMO/2024/0041, COMPLETED) | — | — |
| **DEMO-LI-MUT-002** | 71/8 · Devnapur | MUTATION — transfer application pending | Transfer deed on record; mutation M/DEMO/2025/0011 UNDER_REVIEW surfaces as pending work. | **CLEAR** | `DEMO-LI-MUT-002-mutation-application.png`, `DEMO-LI-MUT-002-transfer-deed.pdf` | Pending-mutation info on the land detail; no high-risk alert. | DEMO-LI-MUT-002-M1 (M/DEMO/2025/0011, UNDER_REVIEW) | — | — |
| **DEMO-LI-MUT-003** | 72/4 · Devnapur | MUTATION — rejected for incomplete documentation | Owner changed but the only mutation on file was REJECTED (missing certified deed copy and ID proof). | **REVIEW** | `DEMO-LI-MUT-003-rejection-order.png` | Review flag: owner change not validly mutated. | DEMO-LI-MUT-003-M1 (M/DEMO/2023/0087, REJECTED) | — | — |
| **DEMO-LI-MUT-004** | 118 · Shantiban | MUTATION — applicant name mismatches source deed | Application filed by Omkar while the source deed names Vrinda; the engine sees an unbridged owner change and reviewers see the mismatch note. | **REVIEW** | `DEMO-LI-MUT-004-mutation-application.png` | Review flag; mutation detail records the name mismatch. | DEMO-LI-MUT-004-M1 (M/DEMO/2024/0152, UNDER_REVIEW, mismatch noted) | — | — |
| **DEMO-LI-MUT-005** | 72/14 · Devnapur | MUTATION — multiple historical owners | Three-owner chain Lakhan -> Kiran -> Yashoda, each hop bridged by a completed mutation; rich ownership history for reports. | **CLEAR** | `DEMO-LI-MUT-005-mutation-order.png` | No alert; ownership history shows the full chain. | DEMO-LI-MUT-005-M1 (2019, COMPLETED); DEMO-LI-MUT-005-M2 (2024, COMPLETED) | — | — |
| **DEMO-LI-ENC-001** | 121 · Shantiban | ENCUMBRANCE — no known encumbrance (clean) | Encumbrance status NONE; no litigation; single consistent record. | **CLEAR** | `DEMO-LI-ENC-001-ownership-record.png` | No alert (false-positive check). | — | — | — |
| **DEMO-LI-ENC-002** | 122 · Shantiban | ENCUMBRANCE — existing bank mortgage (active) | Demo Gramin Bank mortgage DEMO-LN-2025-014 (Rs. 6,50,000) is live; mutation completion on this land is blocked behind the 409 safety gate. | **HIGH_RISK** | `DEMO-LI-ENC-002-mortgage-deed.png`, `DEMO-LI-ENC-002-encumbrance-certificate.pdf` | Red encumbrance banner; HIGH risk verdict. | — | DEMO-LI-ENC-002-E1 (ACTIVE) | — |
| **DEMO-LI-ENC-003** | 123 · Shantiban | ENCUMBRANCE — registered lease against the property | Registered lease DEMO-LI-LEASE-003 (term to 2026-10-31) recorded as a live encumbrance. | **HIGH_RISK** | `DEMO-LI-ENC-003-lease-deed.pdf` | Red encumbrance banner naming the lease. | — | DEMO-LI-ENC-003-E1 (ACTIVE lease) | — |
| **DEMO-LI-ENC-004** | 124 · Shantiban | ENCUMBRANCE — multiple encumbrances | Two simultaneous live charges (bank + society) plus one RELEASED historic charge for contrast. | **HIGH_RISK** | `DEMO-LI-ENC-004-encumbrance-certificate.png` | Two HIGH flags; register shows all three entries. | — | DEMO-LI-ENC-004-E1 (ACTIVE); DEMO-LI-ENC-004-E2 (ACTIVE); DEMO-LI-ENC-004-E3 (RELEASED) | — |
| **DEMO-LI-ENC-005** | 125 · Shantiban | ENCUMBRANCE — old encumbrance released | Historic loan DEMO-LN-2019-021 fully released on 2023-10-05; banner reports released status, no risk flag. | **CLEAR** | `DEMO-LI-ENC-005-release-letter.png` | Green banner 'loans registered but all released' (no risk flag). | — | DEMO-LI-ENC-005-E1 (RELEASED) | — |
| **DEMO-LI-ENC-006** | 126 · Shantiban | ENCUMBRANCE — conflicting/unclear information | Owner claims NOC but the register still shows ACTIVE; a second older entry is deliberately UNKNOWN. The register stance (ACTIVE stands until verified release) drives the risk. | **HIGH_RISK** | `DEMO-LI-ENC-006-encumbrance-certificate.png` | High risk from the standing ACTIVE entry; UNKNOWN entry visible in the register. | — | DEMO-LI-ENC-006-E1 (ACTIVE, conflicting); DEMO-LI-ENC-006-E2 (UNKNOWN) | — |
| **DEMO-LI-COURT-001** | 131 · Shantiban | COURT — civil title/boundary suit, active (Adarsh v. Shivangi) | Ordinary civil dispute: Adarsh claims ownership under the earlier 2018 registered deed; Shivangi disputes the boundary and relies on a later 2021 document; suit pending with a survey commissioner. | **HIGH_RISK** | `DEMO-LI-COURT-001-court-filing.png`, `DEMO-LI-COURT-001-court-order.pdf`, `DEMO-LI-COURT-001-sale-deed-2018.png`, `DEMO-LI-COURT-001-site-inspection-memo.png` | 'Active litigation found for this property' on upload; click through to the land record and court case DEMO-CS-2025-0142; risk HIGH. | — | — | DEMO-LI-COURT-001-C1 (DEMO-CS-2025-0142, PENDING, next hearing 2026-10-19) |
| **DEMO-LI-COURT-002** | 132 · Shantiban | COURT — boundary dispute, active, single cause | Documents are consistent (same owner, stable area); the ONLY adverse signal is the pending demarcation suit DEMO-CS-2025-0198. | **HIGH_RISK** | `DEMO-LI-COURT-002-court-filing.png` | 'Active litigation found for this property'; HIGH risk solely from litigation. | — | — | DEMO-LI-COURT-002-C1 (DEMO-CS-2025-0198, PENDING, next hearing 2026-11-05) |
| **DEMO-LI-COURT-003** | 133 · Shantiban | COURT — inheritance dispute with stay affecting transfer | Succession dispute over the late Shyam's holding; interim injunction keeps status quo; inheritance mutation held in abeyance. | **HIGH_RISK** | `DEMO-LI-COURT-003-injunction-order.pdf` | Active litigation + explicit transfer-stay warning; mutation completion is deferred. | DEMO-LI-COURT-003-M1 (M/DEMO/2024/0201, UNDER_REVIEW, held in abeyance) | — | DEMO-LI-COURT-003-C1 (DEMO-CIVIL-2024-0087, STAYED, injunction 2024-12-12) |
| **DEMO-LI-COURT-004** | 134 · Shantiban | COURT — case closed/resolved (false-positive check) | The only case on record is DISPOSED (withdrawn with liberty, 2022-08-19); the parcel must NOT raise an active-litigation alert. | **CLEAR** | `DEMO-LI-COURT-004-disposal-order.pdf` | No active-litigation alert; detail shows the disposed case for transparency. | — | — | DEMO-LI-COURT-004-C1 (DEMO-CIVIL-2021-0064, DISPOSED) |
| **DEMO-LI-COURT-005** | 33/2 · Devnapur | COURT — decided suit -> mutation by court decree | Title suit decreed for Nandkishor (2023-01-27); mutation M/DEMO/2023/0015 of type COURT_DECREE completed; reports show the decree-driven owner change. | **CLEAR** | `DEMO-LI-COURT-005-decree.pdf` | No active alert; ownership history shows the COURT_DECREE hop; disposed case listed. | DEMO-LI-COURT-005-M1 (M/DEMO/2023/0015, COURT_DECREE, COMPLETED) | — | DEMO-LI-COURT-005-C1 (DEMO-CIVIL-2022-0031, DISPOSED, decree 2023-01-27) |
| **DEMO-LI-RISK-001** | 141 · Shantiban | RISK — medium (area jump) | Area moved 2.10 ha (2021) -> 3.40 ha (2024) with no partition/merger mutation; medium-risk review signal. | **REVIEW** | — | Review flag AREA_JUMP; no high-risk alert. | — | — | — |
| **DEMO-LI-RISK-002** | 142 · Shantiban | RISK — high (ownership/document mismatch) | Two live 2024 deeds for the same holding name different buyers (Omkar vs Vrinda); one instrument must be fictitious. | **HIGH_RISK** | `DEMO-LI-RISK-002-deed-A.png`, `DEMO-LI-RISK-002-deed-B.png` | HIGH verdict with the conflicting-document evidence pair. | — | — | — |
| **DEMO-LI-RISK-003** | 143 · Shantiban | RISK — high by combination (several ordinary records compound) | A routine loan (2025-01-10) plus a registered sale (deed 2025-03-05) plus a blurry newest scan: individually ordinary, together a high-risk transfer during a live charge. | **HIGH_RISK** | `DEMO-LI-RISK-003-encumbrance-certificate.png` | HIGH verdict; evidence pairs the mutation with the live encumbrance. | DEMO-LI-RISK-003-M1 (M/DEMO/2025/0066, COMPLETED) | DEMO-LI-RISK-003-E1 (ACTIVE) | — |

## End-to-end litigation demo (the flagship flow)

1. Sign in as admin, seed: `POST /api/admin/demo/seed` `{"scenario": "LI"}`.
2. Upload **`DEMO-LI-COURT-001-court-filing.png`** through the existing document upload (doc type: `Court Case Filing`).
3. The existing extraction pipeline reads: Survey No `131`, Village `Shantiban`, parties *Adarsh* / *Shivangi*, case `DEMO-CS-2025-0142`.
4. The document detail's Land Intelligence panel matches parcel `LR-…` (survey 131 · Shantiban) and shows:
   **'⚠ Active litigation found for this property' — DEMO-CS-2025-0142**, next hearing 2026-10-19, verdict chip `HIGH_RISK`.
5. Click **⚖ Open court case** (or **Open land record**) -> the existing Land Intelligence land-record detail renders the court-case card,
   the litigation gate banner, and the `ACTIVE_LITIGATION` risk flag with evidence linking to case `DEMO-LI-COURT-001-C1`.

## Sample document inventory

| # | Filename | Scenario | Document type | Parcel (survey · village) | Expected matching result |
|---|---|---|---|---|---|
| 1 | `DEMO-LI-MUT-001-sale-deed.png` | DEMO-LI-MUT-001 | Sale Deed | 71/2 · Devnapur | No alert; land matched; verdict CLEAR. |
| 2 | `DEMO-LI-MUT-001-mutation-order.png` | DEMO-LI-MUT-001 | Mutation Order | 71/2 · Devnapur | No alert; land matched; verdict CLEAR. |
| 3 | `DEMO-LI-MUT-002-mutation-application.png` | DEMO-LI-MUT-002 | Mutation Application | 71/8 · Devnapur | Pending-mutation info on the land detail; no high-risk alert. |
| 4 | `DEMO-LI-MUT-002-transfer-deed.pdf` | DEMO-LI-MUT-002 | Sale Deed | 71/8 · Devnapur | Pending-mutation info on the land detail; no high-risk alert. |
| 5 | `DEMO-LI-MUT-003-rejection-order.png` | DEMO-LI-MUT-003 | Mutation Order | 72/4 · Devnapur | Review flag: owner change not validly mutated. |
| 6 | `DEMO-LI-MUT-004-mutation-application.png` | DEMO-LI-MUT-004 | Mutation Application | 118 · Shantiban | Review flag; mutation detail records the name mismatch. |
| 7 | `DEMO-LI-MUT-005-mutation-order.png` | DEMO-LI-MUT-005 | Mutation Order | 72/14 · Devnapur | No alert; ownership history shows the full chain. |
| 8 | `DEMO-LI-ENC-001-ownership-record.png` | DEMO-LI-ENC-001 | Land Record | 121 · Shantiban | No alert (false-positive check). |
| 9 | `DEMO-LI-ENC-002-mortgage-deed.png` | DEMO-LI-ENC-002 | Mortgage Deed | 122 · Shantiban | Red encumbrance banner; HIGH risk verdict. |
| 10 | `DEMO-LI-ENC-002-encumbrance-certificate.pdf` | DEMO-LI-ENC-002 | Encumbrance Certificate | 122 · Shantiban | Red encumbrance banner; HIGH risk verdict. |
| 11 | `DEMO-LI-ENC-003-lease-deed.pdf` | DEMO-LI-ENC-003 | Lease Deed | 123 · Shantiban | Red encumbrance banner naming the lease. |
| 12 | `DEMO-LI-ENC-004-encumbrance-certificate.png` | DEMO-LI-ENC-004 | Encumbrance Certificate | 124 · Shantiban | Two HIGH flags; register shows all three entries. |
| 13 | `DEMO-LI-ENC-005-release-letter.png` | DEMO-LI-ENC-005 | Release Letter | 125 · Shantiban | Green banner 'loans registered but all released' (no risk flag). |
| 14 | `DEMO-LI-ENC-006-encumbrance-certificate.png` | DEMO-LI-ENC-006 | Encumbrance Certificate | 126 · Shantiban | High risk from the standing ACTIVE entry; UNKNOWN entry visible in the register. |
| 15 | `DEMO-LI-COURT-001-sale-deed-2018.png` | DEMO-LI-COURT-001 | Registered Sale Deed | 131 · Shantiban | 'Active litigation found for this property' on upload; click through to the land record and court case DEMO-CS-2025-0142; risk HIGH. |
| 16 | `DEMO-LI-COURT-001-court-filing.png` | DEMO-LI-COURT-001 | Court Case Filing | 131 · Shantiban | 'Active litigation found for this property' on upload; click through to the land record and court case DEMO-CS-2025-0142; risk HIGH. |
| 17 | `DEMO-LI-COURT-001-court-order.pdf` | DEMO-LI-COURT-001 | Court Order | 131 · Shantiban | 'Active litigation found for this property' on upload; click through to the land record and court case DEMO-CS-2025-0142; risk HIGH. |
| 18 | `DEMO-LI-COURT-001-site-inspection-memo.png` | DEMO-LI-COURT-001 | Boundary Site Memo | 131 · Shantiban | 'Active litigation found for this property' on upload; click through to the land record and court case DEMO-CS-2025-0142; risk HIGH. |
| 19 | `DEMO-LI-COURT-002-court-filing.png` | DEMO-LI-COURT-002 | Court Case Filing | 132 · Shantiban | 'Active litigation found for this property'; HIGH risk solely from litigation. |
| 20 | `DEMO-LI-COURT-003-injunction-order.pdf` | DEMO-LI-COURT-003 | Court Order | 133 · Shantiban | Active litigation + explicit transfer-stay warning; mutation completion is deferred. |
| 21 | `DEMO-LI-COURT-004-disposal-order.pdf` | DEMO-LI-COURT-004 | Court Order | 134 · Shantiban | No active-litigation alert; detail shows the disposed case for transparency. |
| 22 | `DEMO-LI-COURT-005-decree.pdf` | DEMO-LI-COURT-005 | Court Decree | 33/2 · Devnapur | No active alert; ownership history shows the COURT_DECREE hop; disposed case listed. |
| 23 | `DEMO-LI-RISK-002-deed-A.png` | DEMO-LI-RISK-002 | Registered Sale Deed | 142 · Shantiban | HIGH verdict with the conflicting-document evidence pair. |
| 24 | `DEMO-LI-RISK-002-deed-B.png` | DEMO-LI-RISK-002 | Registered Sale Deed | 142 · Shantiban | HIGH verdict with the conflicting-document evidence pair. |
| 25 | `DEMO-LI-RISK-003-encumbrance-certificate.png` | DEMO-LI-RISK-003 | Encumbrance Certificate | 143 · Shantiban | HIGH verdict; evidence pairs the mutation with the live encumbrance. |

## Regenerating the documents

```bash
python tools/generate_demo_documents.py            # render into samples/demo-land-intel
python tools/generate_demo_documents.py --force    # re-render
python tools/generate_demo_documents.py --verify   # check all files present
```

## Automated tests

`tests/test_land_demo.py` covers: idempotent seeding, relationship validity,
expected risk verdicts per scenario, the register-document join, extraction of
every sample document's text through the real deterministic extractor,
upload->match->litigation-alert flow for the active-litigation parcel, and no
false litigation alert on the clean/disposed parcels.
