                status, risk_status, risk_payload, encumbrance_status, reviewer, reviewer_notes, decided_at,
                created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mut_id, mutation_no, None, survey, survey, village, DEMO_TEHSIL, DEMO_DISTRICT,
             previous_owner, new_owner, reason, deed_no, deed_date, "[]", "[]", status, "UNKNOWN", "{}",
             "UNKNOWN", "demo-reviewer@landrec.gov.in", notes, now if status in {"COMPLETED", "REJECTED"} else None,
             DEMO_ACTOR, now, now),
        )
        db.execute(
            """INSERT INTO land_mutation_events
               (id, mutation_id, status, note, actor, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (id) DO UPDATE SET
                 mutation_id=EXCLUDED.mutation_id,
                 status=EXCLUDED.status,
                 note=EXCLUDED.note,
                 actor=EXCLUDED.actor,
                 created_at=EXCLUDED.created_at""",
            (f"DEMO-EV-{mut_id}", mut_id, status, notes or f"Demo scenario {scenario}", DEMO_ACTOR, now),
        )
    return True


def _demo_court_case(case_id: str, scenario: str, *, survey: str, village: str, case_number: str,
                     case_type: str, court_name: str, filed_date: str, parties: str, relief: str,
                     status: str = "ACTIVE", closed_date: str = "", decision: str = "",
                     evidence_doc_ids: Optional[List[str]] = None, notes: str = "") -> bool:
    """Register a synthetic court case against a demo parcel.