"""DEMO-LI scenario index and synthetic sample-document specifications.

Single source of truth for:
  * the demo-data index (scenario -> parcel -> expected Land Intelligence
    result -> documents to upload -> expected alert), surfaced through
    ``GET /api/admin/demo/land-intel/index`` and ``samples/demo-land-intel/INDEX.md``;
  * the generator (``tools/generate_demo_documents.py``) that renders every
    sample PNG/PDF from these specs;
  * the tests, which run the application's real deterministic text extractor
    over each spec's plain text to prove upload -> extraction -> matching.

Every document is fictional and watermarked. Document text is deliberately
consistent with the database rows seeded by ``land_demo_data.seed_all()``:
if a document says ``M/DEMO/2024/0041`` the mutation register uses the same
number, and so on.
"""
from __future__ import annotations

from typing import Any, Dict, List

STATE = "Demo Pradesh"
DISTRICT = "Kishandham"
TEHSIL = "Hariharpur"

_WM = "SYNTHETIC DEMO DOCUMENT — NOT A REAL GOVERNMENT RECORD"


def document_plain_text(spec: Dict[str, Any]) -> str:
    """Deterministic plain text for a spec — the exact content the renderer
    prints (minus layout), and the text the tests feed to the extractor."""
    lines: List[str] = [_WM, spec.get("department", ""), spec.get("title", ""), ""]
    if spec.get("ref_line"):
        lines += [spec["ref_line"], ""]
    for label, value in spec.get("fields", {}).items():
        lines.append(f"{label}: {value}")
    if spec.get("body"):
        lines.append("")
        lines += list(spec["body"])
    lines.append("")
    if spec.get("footer_note"):
        lines.append(spec["footer_note"])
    lines += ["", "(Seal: fictional 'DEMO' seal — no real authority implied)", "/sd/-(Demo Authorised Signatory)"]
    return "\n".join(line for line in lines)


def _geo(survey: str, village: str) -> Dict[str, str]:
    return {"Village": village, "Tehsil": TEHSIL, "District": DISTRICT, "State": STATE, "Survey No": survey}


DOCUMENT_SPECS: List[Dict[str, Any]] = [
    # -- mutation scenarios -------------------------------------------------
    {
        "filename": "DEMO-LI-MUT-001-sale-deed.png", "scenario": "LI-MUT-001", "doc_type": "Sale Deed",
        "kind": "deed", "department": "Office of the Sub-Registrar (Demo), Hariharpur",
        "title": "SALE DEED",
        "ref_line": "Document ID: DEMO-LI-DEED-2024-041  ·  Registration No: DEMO-REG-2024-041",
        "fields": {
            "Deed Date": "2024-01-18", "Transferor (Seller)": "Gautam", "Transferee (Buyer)": "Saurav",
            "Consideration": "Rs. 11,50,000 (synthetic)", "Khata No": "KH-71-2", "Plot No": "71/2",
            "Khasra No": "71/2", "Area": "2.50 ha", **_geo("71/2", "Devnapur"),
        },
        "body": [
            "This synthetic sale deed records that the transferor, GAUTAM, conveys to the transferee, SAURAV, "
            "all rights in the agricultural holding bearing survey (khasra) no. 71/2 of village Devnapur, "
            "tehsil Hariharpur, district Kishandham, admeasuring 2.50 hectares, for a fully-paid consideration.",
            "The transferee shall be recorded in the revenue papers upon registration. This document is a "
            "synthetic training artefact; the parties, numbers and amounts are fictional.",
        ],
        "footer_note": "Register her the mutation of the transferee. — presented and registered (demo).",
    },
    {
        "filename": "DEMO-LI-MUT-001-mutation-order.png", "scenario": "LI-MUT-001", "doc_type": "Mutation Order",
        "kind": "memo", "department": "Office of the Revenue Inspector (Demo), Hariharpur",
        "title": "MUTATION SANCTION ORDER (NAMANTARAN)",
        "ref_line": "Mutation Case No: M/DEMO/2024/0041  ·  Document ID: DEMO-LI-MUT-001-M1",
        "fields": {
            "Mutation No": "M/DEMO/2024/0041", "Date": "2024-02-15", "Owner Name": "Saurav",
            "Father's Name": "Gautam", "Previous Holder": "Gautam", "Khata No": "KH-71-2", "Area": "2.50 ha",
            **_geo("71/2", "Devnapur"),
        },
        "body": [
            "The mutation case M/DEMO/2024/0041 was examined. The registered sale deed DEMO-LI-DEED-2024-041 "
            "dated 2024-01-18 was found genuine after public notice; no objection was received.",
            "Sanctioned: the name of SAURAV is recorded in place of GAUTAM for survey 71/2 with effect from "
            "the date of the deed.",
        ],
        "footer_note": "Enter the change in the khatauni of the current period. (Synthetic demo order.)",
    },
    {
        "filename": "DEMO-LI-MUT-002-mutation-application.png", "scenario": "LI-MUT-002", "doc_type": "Mutation Application",
        "kind": "form", "department": "Office of the Revenue Inspector (Demo), Hariharpur",
        "title": "APPLICATION FOR MUTATION OF NAMES",
        "ref_line": "Mutation Case No: M/DEMO/2025/0011  ·  Application received: 2025-04-21",
        "fields": {
            "Mutation No": "M/DEMO/2025/0011", "Date": "2025-04-21", "Applicant": "Bhavesh",
            "Father's Name": "Devendra", "Previous Holder": "Devendra", "Owner Name": "Bhavesh",
            "Khata No": "KH-71-8", "Area": "1.60 ha", **_geo("71/8", "Devnapur"),
        },
        "body": [
            "The applicant applies for mutation of his name in place of the previous holder DEVENDRA on the "
            "strength of registered sale deed DEMO-LI-DEED-2025-011 dated 2025-04-02.",
            "Status note (synthetic): the application is UNDER REVIEW; the revenue inspector's field report "
            "is awaited.",
        ],
        "footer_note": "Application pending before the Revenue Inspector. (Synthetic demo form.)",
    },
    {
        "filename": "DEMO-LI-MUT-002-transfer-deed.pdf", "scenario": "LI-MUT-002", "doc_type": "Sale Deed",
        "kind": "deed", "department": "Office of the Sub-Registrar (Demo), Hariharpur",
        "title": "TRANSFER DEED (SALE)",
        "ref_line": "Document ID: DEMO-LI-DEED-2025-011  ·  Registration No: DEMO-REG-2025-011",
        "fields": {
            "Deed Date": "2025-04-02", "Transferor (Seller)": "Devendra", "Transferee (Buyer)": "Bhavesh",
            "Consideration (synthetic)": "Rs. 7,80,000", "Khata No": "KH-71-8", "Plot No": "71/8",
            "Khasra No": "71/8", "Area": "1.60 ha", **_geo("71/8", "Devnapur"),
        },
        "body": [
            "The transferor DEVENDRA conveys survey (khasra) no. 71/8 of village Devnapur, admeasuring "
            "1.60 hectares, to the transferee BHAVESH, a family member, by this synthetic transfer deed.",
            "This artefact supports mutation application M/DEMO/2025/0011, which remains pending review.",
        ],
        "footer_note": "Registration fee paid (demo). Not a real instrument.",
    },
    {
        "filename": "DEMO-LI-MUT-003-rejection-order.png", "scenario": "LI-MUT-003", "doc_type": "Mutation Order",
        "kind": "memo", "department": "Office of the Revenue Inspector (Demo), Hariharpur",
        "title": "ORDER OF REJECTION OF MUTATION CASE",
        "ref_line": "Mutation Case No: M/DEMO/2023/0087  ·  Order dated: 2023-10-02",
        "fields": {
            "Mutation No": "M/DEMO/2023/0087", "Date": "2023-10-02", "Applicant": "Pramila",
            "Previous Holder": "Manohar", "Khata No": "KH-72-4", "Area": "2.20 ha",
            **_geo("72/4", "Devnapur"),
        },
        "body": [
            "The mutation application filed by PRAMILA on the strength of sale deed DEMO-LI-DEED-2023-087 "
            "dated 2023-08-14 is REJECTED for the following reason:",
            "The supporting documentation is incomplete: the applicant failed to submit a certified copy of "
            "the sale deed and the required identity proof despite two reminders. The name of MANOHAR "
            "continues in the revenue record.",
            "The applicant may file a fresh application with complete documents. (Synthetic demo order.)",
        ],
        "footer_note": "No change effected in the record for survey 72/4.",
    },
    {
        "filename": "DEMO-LI-MUT-004-mutation-application.png", "scenario": "LI-MUT-004", "doc_type": "Mutation Application",
        "kind": "form", "department": "Office of the Revenue Inspector (Demo), Hariharpur",
        "title": "APPLICATION FOR MUTATION OF NAMES — NAME MISMATCH CASE",
        "ref_line": "Mutation Case No: M/DEMO/2024/0152  ·  Application received: 2024-11-20",
        "fields": {
            "Mutation No": "M/DEMO/2024/0152", "Date": "2024-11-20", "Applicant": "Omkar",
            "Previous Holder": "Manohar", "Khata No": "KH-118", "Area": "1.80 ha",
            **_geo("118", "Shantiban"),
        },
        "body": [
            "MISMATCH (deliberate demo inconsistency): this application was filed by OMKAR, but the source "
            "deed DEMO-LI-DEED-2024-152 dated 2024-11-06 — the only deed on record for this transaction — "
            "names VRINDA as the transferee of survey 118 from the previous holder MANOHAR.",
            "A clarification letter has been issued to the applicant; the case remains UNDER REVIEW until "
            "the mismatch is explained or corrected.",
        ],
        "footer_note": "Held pending clarification of the applicant name. (Synthetic demo form.)",
    },
    {
        "filename": "DEMO-LI-MUT-005-mutation-order.png", "scenario": "LI-MUT-005", "doc_type": "Mutation Order",
        "kind": "memo", "department": "Office of the Revenue Inspector (Demo), Hariharpur",
        "title": "MUTATION SANCTION ORDER (HISTORY EXTRACT)",
        "ref_line": "Mutation Case No: M/DEMO/2024/0063  ·  Order dated: 2024-03-10",
        "fields": {
            "Mutation No": "M/DEMO/2024/0063", "Date": "2024-03-10", "Owner Name": "Yashoda",
            "Previous Holder": "Kiran", "Khata No": "KH-72-14", "Area": "3.05 ha",
            **_geo("72/14", "Devnapur"),
        },
        "body": [
            "Ownership chain on record for survey 72/14: LAKHAN (2012 record) -> KIRAN by sale deed "
            "DEMO-LI-DEED-2019-044 dated 2019-05-21, mutation M/DEMO/2019/0044 completed on 2019-06-30 "
            "-> YASHODA by sale deed DEMO-LI-DEED-2024-063 dated 2024-02-08.",
            "Sanctioned: the name of YASHODA is recorded in place of KIRAN. (Synthetic demo order.)",
        ],
        "footer_note": "Chain of transfers verified against the register; both mutations stand completed.",
    },
    # -- encumbrance scenarios ----------------------------------------------
    {
        "filename": "DEMO-LI-ENC-001-ownership-record.png", "scenario": "LI-ENC-001", "doc_type": "Land Record",
        "kind": "land_record", "department": "Tehsil Office (Demo), Hariharpur — Revenue Records",
        "title": "RECORD OF RIGHTS (KHIRAJI KHATAUNI) — CURRENT PERIOD",
        "ref_line": "Record ID: DEMO-LI-ENC-001-DOC1  ·  Khatauni Year: 2023",
        "fields": {
            "Khatauni Year": "2023", "Owner Name": "Kiran", "Father's Name": "Lakhan",
            "Khata No": "KH-121", "Plot No": "121", "Khasra No": "121", "Area": "0.95 ha",
            "Land Class": "Agricultural", "Ownership Type": "Bhumidar", **_geo("121", "Shantiban"),
        },
        "body": [
            "The holding survey 121 is recorded in the name of KIRAN. No loan, lease, charge or other "
            "encumbrance is entered against this holding in the synthetic register. No litigation is pending.",
            "This is a clean-parcel demo record used to verify that no adverse signal is raised falsely.",
        ],
        "footer_note": "Extract of the record of rights (synthetic demo — not a government record).",
    },
    {
        "filename": "DEMO-LI-ENC-002-mortgage-deed.png", "scenario": "LI-ENC-002", "doc_type": "Mortgage Deed",
        "kind": "deed", "department": "Office of the Sub-Registrar (Demo), Hariharpur",
        "title": "MEMORANDUM OF MORTGAGE (REGISTERED CHARGE)",
        "ref_line": "Loan Reference: DEMO-LN-2025-014  ·  Registration No: DEMO-REG-2025-014",
        "fields": {
            "Date": "2025-02-10", "Mortgagor": "Manohar", "Mortgagee (Lender)": "Demo Gramin Bank (synthetic)",
            "Loan Amount (synthetic)": "Rs. 6,50,000", "Khata No": "KH-122", "Khasra No": "122",
            "Area": "2.75 ha", **_geo("122", "Shantiban"),
        },
        "body": [
            "MANOHAR has mortgaged survey 122 of village Shantiban (2.75 ha) in favour of DEMO GRAMIN BANK "
            "(a fictional lender) to secure a cash-credit facility of Rs. 6,50,000 under reference "
            "DEMO-LN-2025-014 dated 2025-02-10.",
            "The charge is LIVE as of the synthetic register date. Any transfer or mutation completion "
            "requires the lender's release (NOC) first.",
        ],
        "footer_note": "Charge registered in the synthetic encumbrance register.",
    },
    {
        "filename": "DEMO-LI-ENC-002-encumbrance-certificate.pdf", "scenario": "LI-ENC-002", "doc_type": "Encumbrance Certificate",
        "kind": "certificate", "department": "Sub-Registrar (Demo), Hariharpur — Encumbrance Register",
        "title": "ENCUMBRANCE REGISTER EXTRACT",
        "ref_line": "Certificate ID: DEMO-LI-EC-122-2026  ·  Issued: 2026-04-01",
        "fields": {
            "Date": "2026-04-01", "Owner Name": "Manohar", "Khata No": "KH-122", "Khasra No": "122",
            "Area": "2.75 ha", **_geo("122", "Shantiban"),
        },
        "body": [
            "Entries for survey 122 for the period 2020-2026:",
            "  (1) ACTIVE — Mortgage by way of deposit of title deeds, DEMO GRAMIN BANK (synthetic), ref "
            "DEMO-LN-2025-014, Rs. 6,50,000, commenced 2025-02-10. No release entry on record.",
            "Summary: ONE LIVE ENCUMBRANCE. The land is NOT encumbrance-free.",
        ],
        "footer_note": "Fictional extract for testing the active-mortgage risk path.",
    },
    {
        "filename": "DEMO-LI-ENC-003-lease-deed.pdf", "scenario": "LI-ENC-003", "doc_type": "Lease Deed",
        "kind": "deed", "department": "Office of the Sub-Registrar (Demo), Hariharpur",
        "title": "REGISTERED LEASE DEED",
        "ref_line": "Leed ID: DEMO-LI-LEASE-003  ·  Registration No: DEMO-REG-2023-033",
        "fields": {
            "Date": "2023-11-01", "Lessor (Owner)": "Sarita", "Lessee (Tenant)": "Lakhan",
            "Monthly Rent (synthetic)": "Rs. 8,000", "Khata No": "KH-123", "Khasra No": "123",
            "Area": "1.30 ha", **_geo("123", "Shantiban"),
        },
        "body": [
            "SARITA lets survey 123 of village Shantiban (1.30 ha) on agricultural lease to LAKHAN for the "
            "term 2023-11-01 to 2026-10-31 under this synthetic registered lease DEMO-LI-LEASE-003.",
            "The lease is a REGISTERED ENCUMBRANCE on the property while it subsists; it is recorded in the "
            "encumbrance register with status ACTIVE until expiry or surrender.",
        ],
        "footer_note": "Lease registered (demo). The tenant's possession is by consent of the owner.",
    },
    {
        "filename": "DEMO-LI-ENC-004-encumbrance-certificate.png", "scenario": "LI-ENC-004", "doc_type": "Encumbrance Certificate",
        "kind": "certificate", "department": "Sub-Registrar (Demo), Hariharpur — Encumbrance Register",
        "title": "ENCUMBRANCE REGISTER EXTRACT — MULTIPLE CHARGES",
        "ref_line": "Certificate ID: DEMO-LI-EC-124-2026  ·  Issued: 2026-04-01",
        "fields": {
            "Date": "2026-04-01", "Owner Name": "Bhavesh", "Khata No": "KH-124", "Khasra No": "124",
            "Area": "4.10 ha", **_geo("124", "Shantiban"),
        },
        "body": [
            "Entries for survey 124 for the period 2018-2026:",
            "  (1) ACTIVE — Term-loan mortgage, DEMO GRAMIN BANK (synthetic), ref DEMO-LN-2024-102, "
            "Rs. 4,00,000, commenced 2024-12-01.",
            "  (2) ACTIVE — Society charge, KISAN SAHKARI SAMITI (synthetic), ref DEMO-CH-2025-033, "
            "Rs. 1,25,000, commenced 2025-06-15.",
            "  (3) RELEASED — Loan, NAGAR FINANCE (synthetic), ref DEMO-LN-2019-054, Rs. 2,20,000, "
            "commenced 2019-08-01, fully released on 2023-03-30 (release letter DEMO-LI-NOC-2019-054).",
            "Summary: TWO LIVE ENCUMBRANCES exist simultaneously; one prior charge stands released.",
        ],
        "footer_note": "Fictional extract for testing the multiple-encumbrance risk path.",
    },
    {
        "filename": "DEMO-LI-ENC-005-release-letter.png", "scenario": "LI-ENC-005", "doc_type": "Release Letter",
        "kind": "memo", "department": "Purvi Vikas Bank (synthetic) — Hariharpur Branch",
        "title": "LETTER OF RELEASE / NO DUES (RE: MORTGAGE)",
        "ref_line": "Our Ref: DEMO-LN-2019-021  ·  Letter dated: 2023-10-05",
        "fields": {
            "Date": "2023-10-05", "Owner Name": "Yashoda", "Khata No": "KH-125", "Khasra No": "125",
            "Area": "2.40 ha", **_geo("125", "Shantiban"),
        },
        "body": [
            "The loan account DEMO-LN-2019-021 of SMT. YASHODA (sanctioned 2019-06-11, Rs. 3,00,000 against "
            "survey 125) has been FULLY REPAID. The bank releases its charge over survey 125 of village "
            "Shantiban and returns the title deeds.",
            "Nothing remains due. The register entry may be marked RELEASED with effect from 2023-10-05.",
        ],
        "footer_note": "Fictional bank letter for testing the released-encumbrance (clear) path.",
    },
    {
        "filename": "DEMO-LI-ENC-006-encumbrance-certificate.png", "scenario": "LI-ENC-006", "doc_type": "Encumbrance Certificate",
        "kind": "certificate", "department": "Sub-Registrar (Demo), Hariharpur — Encumbrance Register",
        "title": "ENCUMBRANCE REGISTER EXTRACT — CONFLICTING INFORMATION CASE",
        "ref_line": "Certificate ID: DEMO-LI-EC-126-2026  ·  Issued: 2026-04-01",
        "fields": {
            "Date": "2026-04-01", "Owner Name": "Girija", "Khata No": "KH-126", "Khasra No": "126",
            "Area": "1.10 ha", **_geo("126", "Shantiban"),
        },
        "body": [
            "Entries for survey 126 — NOTE: THIS EXTRACT DELIBERATELY CONTAINS CONFLICTING INFORMATION for "
            "testing the unclear-encumbrance path:",
            "  (1) ACTIVE — Cash-credit mortgage, DEMO GRAMIN BANK (synthetic), ref DEMO-LN-2023-091, "
            "Rs. 1,80,000, commenced 2023-05-20. The owner claims a no-dues letter was issued after "
            "repayment; the branch register still shows ACTIVE. The entry stands as registered until a "
            "verified release is produced.",
            "  (2) STATUS UNDER VERIFICATION (UNKNOWN) — older loan ref DEMO-LN-2018-147, Rs. 90,000, "
            "commenced 2018-12-01; the branch merged and the original ledger could not be traced.",
            "Summary: encumbrance position is UNCLEAR; at least one entry remains LIVE on the register.",
        ],
        "footer_note": "Fictional extract for testing conflicting/unclear encumbrance information.",
    },
    # -- court case scenarios ------------------------------------------------
    {
        "filename": "DEMO-LI-COURT-001-sale-deed-2018.png", "scenario": "LI-COURT-001", "doc_type": "Registered Sale Deed",
        "kind": "deed", "department": "Office of the Sub-Registrar (Demo), Hariharpur",
        "title": "REGISTERED SALE DEED (EARLIER TITLE OF THE DISPUTED PARCEL)",
        "ref_line": "Document ID: DEMO-LI-DEED-2018-131A  ·  Registration No: DEMO-REG-2018-131A",
        "fields": {
            "Deed Date": "2018-07-09", "Transferor (Seller)": "Madho", "Transferee (Buyer)": "Adarsh",
            "Khata No": "KH-131", "Plot No": "131", "Khasra No": "131", "Area": "2.60 ha",
            **_geo("131", "Shantiban"),
        },
        "body": [
            "MADHO conveys survey 131 of village Shantiban (2.60 ha) to ADARSH by this synthetic registered "
            "deed dated 2018-07-09. This is the EARLIER deed on which the plaintiff in DEMO-CS-2025-0142 "
            "bases his claim of ownership.",
            "A later document of 2021 records a different claimant over the same parcel; the civil suit is "
            "pending. This artefact is fictional.",
        ],
        "footer_note": "Registered (demo). Subject matter of pending synthetic suit DEMO-CS-2025-0142.",
    },
    {
        "filename": "DEMO-LI-COURT-001-court-filing.png", "scenario": "LI-COURT-001", "doc_type": "Court Case Filing",
        "kind": "court", "department": "IN THE COURT OF THE CIVIL JUDGE (SENIOR DIVISION), HARIHARPUR (FICTIONAL DEMO COURT)",
        "title": "PLAINT — SUIT FOR DECLARATION OF TITLE AND BOUNDARY DEMARCATION",
        "ref_line": "Civil Suit No: DEMO-CS-2025-0142  ·  Date of Filing: 2025-03-11",
        "fields": {
            "Case No": "DEMO-CS-2025-0142", "Plaintiff": "Adarsh", "Defendant": "Shivangi",
            "Suit Value (synthetic)": "Rs. 14,00,000", "Khata No": "KH-131", "Plot No": "131",
            "Khasra No": "131", "Area": "2.60 ha", **_geo("131", "Shantiban"),
            "Next Hearing Date": "2026-10-19",
        },
        "body": [
            "PARTIES: Adarsh, son of Hari, resident of village Shantiban (fictional) ... PLAINTIFF",
            "         Shivangi, daughter of Pramod, resident of village Shantiban (fictional) ... DEFENDANT",
            "SUBJECT: An ordinary civil property dispute. The plaintiff ADARSH claims ownership of survey 131 "
            "based on an earlier registered deed dated 2018-07-09. The defendant SHIVANGI disputes the "
            "claimed boundary and asserts rights under a later registered document dated 2021. A civil suit "
            "concerning title and boundary rights is currently pending.",
            "PRAYER: Declare the plaintiff's title; demarcate the correct boundary per the village map; "
            "permanent injunction against disturbance of possession. (Synthetic fictional pleading.)",
            "STATUS: ACTIVE (pending trial) — stage: commission evidence; a survey commissioner has been appointed.",
        ],
        "footer_note": "Fictional demo filing; no real court, parties or case number are involved.",
    },
    {
        "filename": "DEMO-LI-COURT-001-court-order.pdf", "scenario": "LI-COURT-001", "doc_type": "Court Order",
        "kind": "court", "department": "IN THE COURT OF THE CIVIL JUDGE (SENIOR DIVISION), HARIHARPUR (FICTIONAL DEMO COURT)",
        "title": "ORDER — APPOINTMENT OF SURVEY COMMISSIONER",
        "ref_line": "Civil Suit No: DEMO-CS-2025-0142  ·  Order dated: 2025-07-15",
        "fields": {
            "Case No": "DEMO-CS-2025-0142", "Order Date": "2025-07-15", "Plaintiff": "Adarsh",
            "Defendant": "Shivangi", "Next Hearing Date": "2026-10-19", **_geo("131", "Shantiban"),
        },
        "body": [
            "APPLICATION: For appointment of a survey commissioner to demarcate the boundary of survey 131, "
            "village Shantiban, which is the subject matter of the suit.",
            "ORDER: A court-appointed survey commissioner is directed to demarcate the disputed boundary "
            "between the claims of the parties in the presence of both, and to submit a report before the "
            "next hearing date of 2026-10-19. Either party may file objections to the report within four "
            "weeks of receipt.",
            "Stage recorded: COMMISSION EVIDENCE. Suit pending. (Synthetic fictional order.)",
        ],
        "footer_note": "Certified demo copy — fictional; issued for testing the litigation-alert path.",
    },
    {
        "filename": "DEMO-LI-COURT-001-site-inspection-memo.png", "scenario": "LI-COURT-001", "doc_type": "Boundary Site Memo",
        "kind": "memo", "department": "Demo Revenue Field Agency — Survey & Boundary Wing",
        "title": "SITE INSPECTION AND BOUNDARY MEMORANDUM",
        "ref_line": "Memo ID: DEMO-LI-SITE-131-2025  ·  Inspection date: 2025-07-02",
        "fields": {
            "Date": "2025-07-02", "Owner Name": "Adarsh / Shivangi (disputed)", "Khata No": "KH-131",
            "Plot No": "131", "Khasra No": "131", "Area": "2.60 ha", **_geo("131", "Shantiban"),
        },
        "body": [
            "The inspection team visited survey 131 following the direction in DEMO-CS-2025-0142. Findings "
            "(synthetic): the boundary pillar between the claimed portions has shifted from the position "
            "noted in the village map; the map measurement and the field book differ by about 1.5 metres.",
            "Recommendation: demarcation by the court-appointed survey commissioner; record to be corrected "
            "only after the court's decision.",
        ],
        "footer_note": "Boundary/site document for OCR extraction testing (synthetic).",
    },
    {
        "filename": "DEMO-LI-COURT-002-court-filing.png", "scenario": "LI-COURT-002", "doc_type": "Court Case Filing",
        "kind": "court", "department": "IN THE COURT OF THE CIVIL JUDGE (JUNIOR DIVISION), HARIHARPUR (FICTIONAL DEMO COURT)",
        "title": "PLAINT — SUIT FOR BOUNDARY DEMARCATION AND PERMANENT INJUNCTION",
        "ref_line": "Civil Suit No: DEMO-CS-2025-0198  ·  Date of Filing: 2025-06-09",
        "fields": {
            "Case No": "DEMO-CS-2025-0198", "Plaintiff": "Lakhan", "Defendant": "Raghav",
            "Khata No": "KH-132", "Plot No": "132", "Khasra No": "132", "Area": "0.85 ha",
            **_geo("132", "Shantiban"), "Next Hearing Date": "2026-11-05",
        },
        "body": [
            "PARTIES: Lakhan ... PLAINTIFF;  Raghav (owner of the adjoining parcel) ... DEFENDANT.",
            "SUBJECT: Boundary dispute between the owners of adjoining parcels. The plaintiff claims the "
            "boundary pillar has been noted at inconsistent positions in the village map and the field "
            "book; the defendant relies on the field book. A suit for demarcation and permanent injunction "
            "is pending.",
            "STATUS: PENDING — stage: framing of issues. (Synthetic fictional pleading.)",
        ],
        "footer_note": "Fictional demo filing; no real court, parties or case number are involved.",
    },
    {
        "filename": "DEMO-LI-COURT-003-injunction-order.pdf", "scenario": "LI-COURT-003", "doc_type": "Court Order",
        "kind": "court", "department": "DISTRICT JUDGE COURT, KISHANDHAM (FICTIONAL DEMO COURT)",
        "title": "INTERIM ORDER / INJUNCTION — STATUS QUO",
        "ref_line": "Civil Suit No: DEMO-CIVIL-2024-0087  ·  Order dated: 2024-12-12",
        "fields": {
            "Case No": "DEMO-CIVIL-2024-0087", "Order Date": "2024-12-12", "Petitioner": "Girija",
            "Respondent": "Kiran", "Next Hearing Date": "2026-10-08", **_geo("133", "Shantiban"),
        },
        "body": [
            "SUBJECT: Inheritance dispute over the ancestral holding of the late SHYAM (survey 133). Both "
            "parties claim succession; the inheritance mutation application M/DEMO/2024/0201 is on hold "
            "pending the suit.",
            "OPERATIVE ORDER: The parties are directed to maintain STATUS QUO. No transfer, lease, gift or "
            "mutation of the suit land shall be effected until further orders of the court. Any dealing in "
            "the interim is at the risk of the parties.",
            "Stage recorded: INTERIM INJUNCTION IN FORCE; suit stayed against alienation. (Synthetic order.)",
        ],
        "footer_note": "Fictional demo order for testing the transfer-stay risk path.",
    },
    {
        "filename": "DEMO-LI-COURT-004-disposal-order.pdf", "scenario": "LI-COURT-004", "doc_type": "Court Order",
        "kind": "court", "department": "IN THE COURT OF THE CIVIL JUDGE (JUNIOR DIVISION), HARIHARPUR (FICTIONAL DEMO COURT)",
        "title": "FINAL ORDER — SUIT DISMISSED AS WITHDRAWN",
        "ref_line": "Civil Suit No: DEMO-CIVIL-2021-0064  ·  Order dated: 2022-08-19",
        "fields": {
            "Case No": "DEMO-CIVIL-2021-0064", "Order Date": "2022-08-19", "Plaintiff": "Sarita",
            "Defendant": "Bhola", **_geo("134", "Shantiban"),
        },
        "body": [
            "The suit concerning a claimed occupation of part of survey 134 was amicably resolved between "
            "the parties outside the court.",
            "ORDER: The suit is DISMISSED AS WITHDRAWN with liberty; decree drawn accordingly. No claim "
            "remains pending between the parties over this parcel.",
            "STATUS: DISPOSED / CLOSED. (Synthetic fictional order.)",
        ],
        "footer_note": "Used to verify NO false litigation alert on a parcel with only a disposed case.",
    },
    {
        "filename": "DEMO-LI-COURT-005-decree.pdf", "scenario": "LI-COURT-005", "doc_type": "Court Decree",
        "kind": "court", "department": "DISTRICT JUDGE COURT, KISHANDHAM (FICTIONAL DEMO COURT)",
        "title": "DECREE SHEET",
        "ref_line": "Civil Suit No: DEMO-CIVIL-2022-0031  ·  Decree dated: 2023-01-27",
        "fields": {
            "Case No": "DEMO-CIVIL-2022-0031", "Decree Date": "2023-01-27", "Plaintiff": "Devaki",
            "Defendant": "Nandkishor", "Khata No": "KH-33-2", "Plot No": "33/2", "Khasra No": "33/2",
            "Area": "1.70 ha", **_geo("33/2", "Devnapur"),
        },
        "body": [
            "SUBJECT: Title suit concerning succession to the recorded holding of survey 33/2, village "
            "Devnapur.",
            "DECREE: The suit is decreed in favour of the defendant NANDKISHOR; his title to survey 33/2 is "
            "declared, and the revenue authorities are directed to record the change by mutation.",
            "Consequential mutation M/DEMO/2023/0015 stands COMPLETED on the strength of this decree. "
            "(Synthetic fictional decree.)",
        ],
        "footer_note": "Used to test the COURT_DECREE mutation path and disposed-case reporting.",
    },
    # -- risk review scenarios ----------------------------------------------
    {
        "filename": "DEMO-LI-RISK-002-deed-A.png", "scenario": "LI-RISK-002", "doc_type": "Registered Sale Deed",
        "kind": "deed", "department": "Office of the Sub-Registrar (Demo), Hariharpur",
        "title": "REGISTERED SALE DEED — DOCUMENT 'A' OF THE CONFLICT PAIR",
        "ref_line": "Document ID: DEMO-LI-DEED-2024-142A  ·  Registration No: DEMO-REG-2024-142A",
        "fields": {
            "Deed Date": "2024-03-19", "Transferor (Seller)": "Bhagwati", "Transferee (Buyer)": "Omkar",
            "Khata No": "KH-142", "Plot No": "142", "Khasra No": "142", "Area": "2.00 ha",
            **_geo("142", "Shantiban"),
        },
        "body": [
            "DELIBERATE INCONSISTENCY (demo): this deed claims to convey survey 142 from BHAGWATI to OMKAR "
            "for the 2024 period.",
            "A second deed of the SAME YEAR (document 'B') conveys the very same holding to a DIFFERENT "
            "buyer. Both cannot stand; the deterministic engine must flag OWNER_CONFLICT_YEAR and the "
            "documents require human verification.",
        ],
        "footer_note": "Conflicting instrument 'A' — synthetic, for the ownership-mismatch risk path.",
    },
    {
        "filename": "DEMO-LI-RISK-002-deed-B.png", "scenario": "LI-RISK-002", "doc_type": "Registered Sale Deed",
        "kind": "deed", "department": "Office of the Sub-Registrar (Demo), Hariharpur",
        "title": "REGISTERED SALE DEED — DOCUMENT 'B' OF THE CONFLICT PAIR",
        "ref_line": "Document ID: DEMO-LI-DEED-2024-142B  ·  Registration No: DEMO-REG-2024-142B",
        "fields": {
            "Deed Date": "2024-05-27", "Transferor (Seller)": "Bhagwati", "Transferee (Buyer)": "Vrinda",
            "Khata No": "KH-142", "Plot No": "142", "Khasra No": "142", "Area": "2.00 ha",
            **_geo("142", "Shantiban"),
        },
        "body": [
            "DELIBERATE INCONSISTENCY (demo): this deed claims to convey the SAME survey 142 from BHAGWATI "
            "to VRINDA two months after document 'A' conveyed it to a different buyer.",
            "One of the two instruments is necessarily fictitious; the case is reserved for the reviewer.",
        ],
        "footer_note": "Conflicting instrument 'B' — synthetic, for the ownership-mismatch risk path.",
    },
    {
        "filename": "DEMO-LI-RISK-003-encumbrance-certificate.png", "scenario": "LI-RISK-003", "doc_type": "Encumbrance Certificate",
        "kind": "certificate", "department": "Sub-Registrar (Demo), Hariharpur — Encumbrance Register",
        "title": "ENCUMBRANCE REGISTER EXTRACT — COMPOUNDING RISK CASE",
        "ref_line": "Certificate ID: DEMO-LI-EC-143-2026  ·  Issued: 2026-04-01",
        "fields": {
            "Date": "2026-04-01", "Owner Name": "Pramila", "Khata No": "KH-143", "Khasra No": "143",
            "Area": "1.45 ha", **_geo("143", "Shantiban"),
        },
        "body": [
            "Entry for survey 143: ACTIVE — Working-capital loan, NAGAR FINANCE (synthetic), ref "
            "DEMO-LN-2025-006, Rs. 1,50,000, commenced 2025-01-10. No release entry.",
            "REGISTER NOTE: a sale deed DEMO-LI-DEED-2025-066 dated 2025-03-05 was registered while the "
            "above charge was live; mutation M/DEMO/2025/0066 was completed without a lender release on "
            "file. Each record is individually ordinary; together they constitute a high-risk transfer "
            "during a live encumbrance.",
        ],
        "footer_note": "Fictional extract for the compounding-risk (sale during encumbrance) path.",
    },
]


def _scenario_index() -> List[Dict[str, Any]]:
    """One row per scenario: id -> parcel -> expectation -> uploads -> alerts."""
    D, S = "Devnapur", "Shantiban"
    rows: List[Dict[str, Any]] = [
        {
            "scenario_id": "DEMO-LI-MUT-001", "scenario_type": "MUTATION — approved transfer chain",
            "parcel": {"survey": "71/2", "village": D}, "people": ["Gautam", "Saurav"],
            "expected_verdict": "CLEAR",
            "expected_flags": [],
            "expected_result": "Owner change Gautam -> Saurav is bridged by a COMPLETED mutation; no adverse signal.",
            "documents_to_upload": ["DEMO-LI-MUT-001-sale-deed.png", "DEMO-LI-MUT-001-mutation-order.png"],
            "expected_alert": "No alert; land matched; verdict CLEAR.",
            "related_mutations": ["DEMO-LI-MUT-001-M1 (M/DEMO/2024/0041, COMPLETED)"],
            "related_encumbrances": [], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-MUT-002", "scenario_type": "MUTATION — transfer application pending",
            "parcel": {"survey": "71/8", "village": D}, "people": ["Devendra", "Bhavesh"],
            "expected_verdict": "CLEAR",
            "expected_flags": ["PENDING_MUTATION (INFO)"],
            "expected_result": "Transfer deed on record; mutation M/DEMO/2025/0011 UNDER_REVIEW surfaces as pending work.",
            "documents_to_upload": ["DEMO-LI-MUT-002-mutation-application.png", "DEMO-LI-MUT-002-transfer-deed.pdf"],
            "expected_alert": "Pending-mutation info on the land detail; no high-risk alert.",
            "related_mutations": ["DEMO-LI-MUT-002-M1 (M/DEMO/2025/0011, UNDER_REVIEW)"],
            "related_encumbrances": [], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-MUT-003", "scenario_type": "MUTATION — rejected for incomplete documentation",
            "parcel": {"survey": "72/4", "village": D}, "people": ["Manohar", "Pramila"],
            "expected_verdict": "REVIEW",
            "expected_flags": ["OWNER_CHANGE_NO_MUTATION (REVIEW)", "REJECTED_MUTATION (INFO)"],
            "expected_result": "Owner changed but the only mutation on file was REJECTED (missing certified deed copy and ID proof).",
            "documents_to_upload": ["DEMO-LI-MUT-003-rejection-order.png"],
            "expected_alert": "Review flag: owner change not validly mutated.",
            "related_mutations": ["DEMO-LI-MUT-003-M1 (M/DEMO/2023/0087, REJECTED)"],
            "related_encumbrances": [], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-MUT-004", "scenario_type": "MUTATION — applicant name mismatches source deed",
            "parcel": {"survey": "118", "village": S}, "people": ["Manohar", "Omkar", "Vrinda"],
            "expected_verdict": "REVIEW",
            "expected_flags": ["OWNER_CHANGE_NO_MUTATION (REVIEW)"],
            "expected_result": "Application filed by Omkar while the source deed names Vrinda; the engine sees an unbridged owner change and reviewers see the mismatch note.",
            "documents_to_upload": ["DEMO-LI-MUT-004-mutation-application.png"],
            "expected_alert": "Review flag; mutation detail records the name mismatch.",
            "related_mutations": ["DEMO-LI-MUT-004-M1 (M/DEMO/2024/0152, UNDER_REVIEW, mismatch noted)"],
            "related_encumbrances": [], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-MUT-005", "scenario_type": "MUTATION — multiple historical owners",
            "parcel": {"survey": "72/14", "village": D}, "people": ["Lakhan", "Kiran", "Yashoda"],
            "expected_verdict": "CLEAR",
            "expected_flags": [],
            "expected_result": "Three-owner chain Lakhan -> Kiran -> Yashoda, each hop bridged by a completed mutation; rich ownership history for reports.",
            "documents_to_upload": ["DEMO-LI-MUT-005-mutation-order.png"],
            "expected_alert": "No alert; ownership history shows the full chain.",
            "related_mutations": ["DEMO-LI-MUT-005-M1 (2019, COMPLETED)", "DEMO-LI-MUT-005-M2 (2024, COMPLETED)"],
            "related_encumbrances": [], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-ENC-001", "scenario_type": "ENCUMBRANCE — no known encumbrance (clean)",
            "parcel": {"survey": "121", "village": S}, "people": ["Kiran"],
            "expected_verdict": "CLEAR",
            "expected_flags": [],
            "expected_result": "Encumbrance status NONE; no litigation; single consistent record.",
            "documents_to_upload": ["DEMO-LI-ENC-001-ownership-record.png"],
            "expected_alert": "No alert (false-positive check).",
            "related_mutations": [], "related_encumbrances": [], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-ENC-002", "scenario_type": "ENCUMBRANCE — existing bank mortgage (active)",
            "parcel": {"survey": "122", "village": S}, "people": ["Manohar"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["ACTIVE_ENCUMBRANCE (HIGH)"],
            "expected_result": "Demo Gramin Bank mortgage DEMO-LN-2025-014 (Rs. 6,50,000) is live; mutation completion on this land is blocked behind the 409 safety gate.",
            "documents_to_upload": ["DEMO-LI-ENC-002-mortgage-deed.png", "DEMO-LI-ENC-002-encumbrance-certificate.pdf"],
            "expected_alert": "Red encumbrance banner; HIGH risk verdict.",
            "related_mutations": [], "related_encumbrances": ["DEMO-LI-ENC-002-E1 (ACTIVE)"], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-ENC-003", "scenario_type": "ENCUMBRANCE — registered lease against the property",
            "parcel": {"survey": "123", "village": S}, "people": ["Sarita", "Lakhan"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["ACTIVE_ENCUMBRANCE (HIGH)"],
            "expected_result": "Registered lease DEMO-LI-LEASE-003 (term to 2026-10-31) recorded as a live encumbrance.",
            "documents_to_upload": ["DEMO-LI-ENC-003-lease-deed.pdf"],
            "expected_alert": "Red encumbrance banner naming the lease.",
            "related_mutations": [], "related_encumbrances": ["DEMO-LI-ENC-003-E1 (ACTIVE lease)"], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-ENC-004", "scenario_type": "ENCUMBRANCE — multiple encumbrances",
            "parcel": {"survey": "124", "village": S}, "people": ["Bhavesh"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["ACTIVE_ENCUMBRANCE x2 (HIGH)"],
            "expected_result": "Two simultaneous live charges (bank + society) plus one RELEASED historic charge for contrast.",
            "documents_to_upload": ["DEMO-LI-ENC-004-encumbrance-certificate.png"],
            "expected_alert": "Two HIGH flags; register shows all three entries.",
            "related_mutations": [],
            "related_encumbrances": ["DEMO-LI-ENC-004-E1 (ACTIVE)", "DEMO-LI-ENC-004-E2 (ACTIVE)", "DEMO-LI-ENC-004-E3 (RELEASED)"],
            "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-ENC-005", "scenario_type": "ENCUMBRANCE — old encumbrance released",
            "parcel": {"survey": "125", "village": S}, "people": ["Yashoda"],
            "expected_verdict": "CLEAR",
            "expected_flags": [],
            "expected_result": "Historic loan DEMO-LN-2019-021 fully released on 2023-10-05; banner reports released status, no risk flag.",
            "documents_to_upload": ["DEMO-LI-ENC-005-release-letter.png"],
            "expected_alert": "Green banner 'loans registered but all released' (no risk flag).",
            "related_mutations": [], "related_encumbrances": ["DEMO-LI-ENC-005-E1 (RELEASED)"], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-ENC-006", "scenario_type": "ENCUMBRANCE — conflicting/unclear information",
            "parcel": {"survey": "126", "village": S}, "people": ["Girija"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["ACTIVE_ENCUMBRANCE (HIGH)"],
            "expected_result": "Owner claims NOC but the register still shows ACTIVE; a second older entry is deliberately UNKNOWN. The register stance (ACTIVE stands until verified release) drives the risk.",
            "documents_to_upload": ["DEMO-LI-ENC-006-encumbrance-certificate.png"],
            "expected_alert": "High risk from the standing ACTIVE entry; UNKNOWN entry visible in the register.",
            "related_mutations": [],
            "related_encumbrances": ["DEMO-LI-ENC-006-E1 (ACTIVE, conflicting)", "DEMO-LI-ENC-006-E2 (UNKNOWN)"],
            "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-COURT-001", "scenario_type": "COURT — civil title/boundary suit, active (Adarsh v. Shivangi)",
            "parcel": {"survey": "131", "village": S}, "people": ["Adarsh", "Shivangi"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["ACTIVE_LITIGATION (HIGH)", "OWNER_CHANGE_NO_MUTATION (REVIEW)"],
            "expected_result": "Ordinary civil dispute: Adarsh claims ownership under the earlier 2018 registered deed; Shivangi disputes the boundary and relies on a later 2021 document; suit pending with a survey commissioner.",
            "documents_to_upload": ["DEMO-LI-COURT-001-court-filing.png", "DEMO-LI-COURT-001-court-order.pdf", "DEMO-LI-COURT-001-sale-deed-2018.png", "DEMO-LI-COURT-001-site-inspection-memo.png"],
            "expected_alert": "'Active litigation found for this property' on upload; click through to the land record and court case DEMO-CS-2025-0142; risk HIGH.",
            "related_mutations": [],
            "related_encumbrances": [],
            "related_court_cases": ["DEMO-LI-COURT-001-C1 (DEMO-CS-2025-0142, ACTIVE, next hearing 2026-10-19)"],
        },
        {
            "scenario_id": "DEMO-LI-COURT-002", "scenario_type": "COURT — boundary dispute, active, single cause",
            "parcel": {"survey": "132", "village": S}, "people": ["Lakhan", "Raghav"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["ACTIVE_LITIGATION (HIGH)"],
            "expected_result": "Documents are consistent (same owner, stable area); the ONLY adverse signal is the pending demarcation suit DEMO-CS-2025-0198.",
            "documents_to_upload": ["DEMO-LI-COURT-002-court-filing.png"],
            "expected_alert": "'Active litigation found for this property'; HIGH risk solely from litigation.",
            "related_mutations": [], "related_encumbrances": [],
            "related_court_cases": ["DEMO-LI-COURT-002-C1 (DEMO-CS-2025-0198, ACTIVE, next hearing 2026-11-05)"],
        },
        {
            "scenario_id": "DEMO-LI-COURT-003", "scenario_type": "COURT — inheritance dispute with stay affecting transfer",
            "parcel": {"survey": "133", "village": S}, "people": ["Girija", "Kiran", "Shyam"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["ACTIVE_LITIGATION (HIGH)", "TRANSFER_STAYED (HIGH)", "PENDING_MUTATION (INFO)"],
            "expected_result": "Succession dispute over the late Shyam's holding; interim injunction keeps status quo; inheritance mutation held in abeyance.",
            "documents_to_upload": ["DEMO-LI-COURT-003-injunction-order.pdf"],
            "expected_alert": "Active litigation + explicit transfer-stay warning; mutation completion is deferred.",
            "related_mutations": ["DEMO-LI-COURT-003-M1 (M/DEMO/2024/0201, UNDER_REVIEW, held in abeyance)"],
            "related_encumbrances": [],
            "related_court_cases": ["DEMO-LI-COURT-003-C1 (DEMO-CIVIL-2024-0087, ACTIVE + transfer stayed, injunction 2024-12-12)"],
        },
        {
            "scenario_id": "DEMO-LI-COURT-004", "scenario_type": "COURT — case closed/resolved: withdrawn suit (false-positive check)",
            "parcel": {"survey": "134", "village": S}, "people": ["Sarita", "Bhola"],
            "expected_verdict": "CLEAR",
            "expected_flags": [],
            "expected_result": "The only case on record is WITHDRAWN (dismissed as withdrawn with liberty, closed 2022-08-19); the parcel must NOT raise an active-litigation alert.",
            "documents_to_upload": ["DEMO-LI-COURT-004-disposal-order.pdf"],
            "expected_alert": "No active-litigation alert; detail shows the closed case for transparency.",
            "related_mutations": [], "related_encumbrances": [],
            "related_court_cases": ["DEMO-LI-COURT-004-C1 (DEMO-CIVIL-2021-0064, WITHDRAWN, closed 2022-08-19)"],
        },
        {
            "scenario_id": "DEMO-LI-COURT-005", "scenario_type": "COURT — decided suit -> mutation by court decree",
            "parcel": {"survey": "33/2", "village": D}, "people": ["Devaki", "Nandkishor"],
            "expected_verdict": "CLEAR",
            "expected_flags": [],
            "expected_result": "Title suit decreed for Nandkishor (2023-01-27); mutation M/DEMO/2023/0015 of type COURT_DECREE completed; reports show the decree-driven owner change.",
            "documents_to_upload": ["DEMO-LI-COURT-005-decree.pdf"],
            "expected_alert": "No active alert; ownership history shows the COURT_DECREE hop; closed (decided) case listed.",
            "related_mutations": ["DEMO-LI-COURT-005-M1 (M/DEMO/2023/0015, COURT_DECREE, COMPLETED)"],
            "related_encumbrances": [],
            "related_court_cases": ["DEMO-LI-COURT-005-C1 (DEMO-CIVIL-2022-0031, DECIDED, decree 2023-01-27)"],
        },
        {
            "scenario_id": "DEMO-LI-RISK-001", "scenario_type": "RISK — medium (area jump)",
            "parcel": {"survey": "141", "village": S}, "people": ["Lakhan"],
            "expected_verdict": "REVIEW",
            "expected_flags": ["AREA_JUMP (REVIEW)"],
            "expected_result": "Area moved 2.10 ha (2021) -> 3.40 ha (2024) with no partition/merger mutation; medium-risk review signal.",
            "documents_to_upload": [],
            "expected_alert": "Review flag AREA_JUMP; no high-risk alert.",
            "related_mutations": [], "related_encumbrances": [], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-RISK-002", "scenario_type": "RISK — high (ownership/document mismatch)",
            "parcel": {"survey": "142", "village": S}, "people": ["Omkar", "Vrinda", "Bhagwati"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["OWNER_CONFLICT_YEAR (HIGH)"],
            "expected_result": "Two live 2024 deeds for the same holding name different buyers (Omkar vs Vrinda); one instrument must be fictitious.",
            "documents_to_upload": ["DEMO-LI-RISK-002-deed-A.png", "DEMO-LI-RISK-002-deed-B.png"],
            "expected_alert": "HIGH verdict with the conflicting-document evidence pair.",
            "related_mutations": [], "related_encumbrances": [], "related_court_cases": [],
        },
        {
            "scenario_id": "DEMO-LI-RISK-003", "scenario_type": "RISK — high by combination (several ordinary records compound)",
            "parcel": {"survey": "143", "village": S}, "people": ["Manohar", "Pramila"],
            "expected_verdict": "HIGH_RISK",
            "expected_flags": ["SALE_DURING_ENCUMBRANCE (HIGH)", "ACTIVE_ENCUMBRANCE (HIGH)", "LOW_QUALITY_EXTRACTION (INFO)"],
            "expected_result": "A routine loan (2025-01-10) plus a registered sale (deed 2025-03-05) plus a blurry newest scan: individually ordinary, together a high-risk transfer during a live charge.",
            "documents_to_upload": ["DEMO-LI-RISK-003-encumbrance-certificate.png"],
            "expected_alert": "HIGH verdict; evidence pairs the mutation with the live encumbrance.",
            "related_mutations": ["DEMO-LI-RISK-003-M1 (M/DEMO/2025/0066, COMPLETED)"],
            "related_encumbrances": ["DEMO-LI-RISK-003-E1 (ACTIVE)"],
            "related_court_cases": [],
        },
    ]
    return rows


SCENARIO_INDEX = _scenario_index()


def document_manifest() -> List[Dict[str, Any]]:
    """Inventory of every generated sample document (filename, scenario, type,
    parcel, expected matching result) — mirrors the rendered files on disk."""
    scenario_by_id = {row["scenario_id"]: row for row in SCENARIO_INDEX}
    manifest = []
    for spec in DOCUMENT_SPECS:
        row = scenario_by_id.get("DEMO-LI-" + spec["scenario"], {})
        manifest.append({
            "filename": spec["filename"],
            "scenario_id": "DEMO-" + spec["scenario"],
            "document_type": spec["doc_type"],
            "parcel": {"survey": spec["fields"].get("Survey No", ""), "village": spec["fields"].get("Village", "")},
            "doc_kind": spec["kind"],
            "expected_matching_result": row.get("expected_alert", ""),
            "expected_verdict": row.get("expected_verdict", ""),
        })
    return manifest
