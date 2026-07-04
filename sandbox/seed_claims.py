#!/usr/bin/env python3
"""
Reclaim — synthetic denied-claims dataset generator.

Produces two files:
  claims.json         -> what the AGENT sees (denied claims, FHIR-flavored)
  payer_secrets.json  -> what only the PAYER SANDBOX sees (hidden truth per claim)

The split is the whole trick: the visible denial reason (CARC code) is often
vague or misleading; the hidden truth is what the payer's rules engine
actually checks. The Denial Genome earns its demo by *learning* the hidden
rules from appeal outcomes.

Deterministic (seeded) so the demo is choreographed and repeatable.
"""

import json
import random
from datetime import date, timedelta

random.seed(42)  # deterministic: your demo runs the same every time

# ---------------------------------------------------------------------------
# Reference data (real-world CARC codes, CPT + ICD-10 pairs)
# ---------------------------------------------------------------------------

# denial_profile: (carc_code, visible_reason, hidden_truth, winning_fix)
# `hidden_truth` is what the payer ACTUALLY checks on appeal.
# `winning_fix` is the attachment/correction that satisfies it.
DENIAL_PROFILES = [
    {
        "carc": "CO-16",
        "visible_reason": "Claim/service lacks information needed for adjudication.",
        "hidden_truth": "requires_modifier_25",
        "winning_fix": {"modifier": "25"},
        "trap": "Attaching more documentation alone will FAIL. The payer "
                "actually wants modifier 25 on the E/M line.",
    },
    {
        "carc": "CO-11",
        "visible_reason": "The diagnosis is inconsistent with the procedure.",
        "hidden_truth": "requires_corrected_icd",
        "winning_fix": {"corrected_icd": True},
        "trap": None,
    },
    {
        "carc": "CO-50",
        "visible_reason": "Non-covered: not deemed a medical necessity.",
        "hidden_truth": "requires_clinical_notes_and_severity",
        "winning_fix": {"attachments": ["clinical_notes"], "severity_documented": True},
        "trap": "Clinical notes WITHOUT documented severity keyword still fail.",
    },
    {
        "carc": "CO-29",
        "visible_reason": "The time limit for filing has expired.",
        "hidden_truth": "requires_timely_filing_receipt",
        "winning_fix": {"attachments": ["submission_receipt"]},
        "trap": None,
    },
    {
        "carc": "CO-97",
        "visible_reason": "Payment adjusted: service bundled into another service.",
        "hidden_truth": "requires_modifier_59",
        "winning_fix": {"modifier": "59"},
        "trap": None,
    },
    {
        "carc": "PR-204",
        "visible_reason": "Service not covered under the patient's current plan.",
        "hidden_truth": "requires_medical_necessity_letter",
        "winning_fix": {"attachments": ["medical_necessity_letter"]},
        "trap": None,
    },
]

# (cpt, description, icd10_wrong_sometimes, icd10_correct, typical_charge_usd)
SERVICE_LINES = [
    ("99214", "Office visit, established patient, moderate complexity", "Z00.00", "E11.65", 180),
    ("99213", "Office visit, established patient, low complexity", "Z00.00", "I10", 120),
    ("93000", "Electrocardiogram, complete", "Z00.00", "R07.9", 95),
    ("80053", "Comprehensive metabolic panel", "Z00.00", "E11.9", 65),
    ("71046", "Chest X-ray, 2 views", "Z00.00", "J18.9", 210),
    ("97110", "Therapeutic exercise, 15 min", "Z00.00", "M54.5", 85),
    ("36415", "Routine venipuncture", "Z00.00", "D64.9", 25),
    ("99285", "Emergency dept visit, high severity", "R69", "I21.9", 950),
    ("29881", "Knee arthroscopy with meniscectomy", "M25.561", "M23.205", 3200),
    ("45378", "Colonoscopy, diagnostic", "Z12.11", "K57.30", 1150),
]

FIRST = ["Amina", "Brian", "Cynthia", "David", "Esther", "Felix", "Grace",
         "Hassan", "Irene", "James", "Faith", "Kevin", "Lydia", "Moses",
         "Nancy", "Otieno", "Peris", "Quincy", "Rehema", "Samuel"]
LAST = ["Mwangi", "Ochieng", "Kamau", "Wanjiru", "Kiptoo", "Njoroge",
        "Achieng", "Mutua", "Cherono", "Omondi", "Ndungu", "Barasa"]

PAYER = {"id": "PAYER-ALPHA", "name": "Alpha Assurance Group"}


def make_claim(i: int, profile: dict) -> tuple[dict, dict]:
    """Return (visible_claim, secret) pair."""
    cpt, desc, icd_wrong, icd_right, charge = random.choice(SERVICE_LINES)
    submitted = date(2026, 3, 1) + timedelta(days=random.randint(0, 60))
    denied = submitted + timedelta(days=random.randint(14, 30))

    # CO-11 claims genuinely carry the wrong ICD on the visible claim
    icd_on_claim = icd_wrong if profile["carc"] == "CO-11" else icd_right
    charge = round(charge * random.uniform(0.9, 1.35), 2)

    claim_id = f"CLM-2026-{i:04d}"
    visible = {
        "resourceType": "ClaimResponse",          # FHIR-flavored, simplified
        "claim_id": claim_id,
        "status": "denied",
        "payer": PAYER,
        "patient": {
            "name": f"{random.choice(FIRST)} {random.choice(LAST)}",
            "member_id": f"MBR-{random.randint(100000, 999999)}",
            "dob": f"19{random.randint(50, 99)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
        },
        "provider": {"npi": "1234567890", "name": "Riverside Medical Center"},
        "service_lines": [{
            "line": 1,
            "cpt": cpt,
            "description": desc,
            "icd10": icd_on_claim,
            "modifiers": [],
            "billed_amount_usd": charge,
        }],
        "dates": {"submitted": submitted.isoformat(), "denied": denied.isoformat()},
        "denial": {
            "carc_code": profile["carc"],
            "reason_text": profile["visible_reason"],
            # remark codes are deliberately unhelpful ~half the time
            "rarc_code": random.choice(["N/A", "N290", "M127", "N/A"]),
        },
        "appeal_deadline": (denied + timedelta(days=60)).isoformat(),
    }
    secret = {
        "claim_id": claim_id,
        "hidden_truth": profile["hidden_truth"],
        "winning_fix": profile["winning_fix"],
        "correct_icd10": icd_right,
        "trap": profile["trap"],
    }
    return visible, secret


def main(n: int = 50) -> None:
    claims, secrets = [], []

    # Choreography: fix which profile lands on the demo-critical claims.
    #   #3  -> CO-16 (the trap: adversarial reviewer catches "docs alone" appeal)
    #   #48 -> CO-16 again (genome predicts modifier-25 BEFORE appeal — payoff)
    forced = {3: DENIAL_PROFILES[0], 48: DENIAL_PROFILES[0]}

    for i in range(1, n + 1):
        profile = forced.get(i, random.choice(DENIAL_PROFILES))
        visible, secret = make_claim(i, profile)
        claims.append(visible)
        secrets.append(secret)

    total = round(sum(c["service_lines"][0]["billed_amount_usd"] for c in claims), 2)

    with open("claims.json", "w") as f:
        json.dump({"total_denied_usd": total, "count": n, "claims": claims}, f, indent=2)
    with open("payer_secrets.json", "w") as f:
        json.dump({s["claim_id"]: s for s in secrets}, f, indent=2)

    print(f"Generated {n} denied claims totalling ${total:,.2f}")
    print("  claims.json         -> feed this to the Reclaim agent")
    print("  payer_secrets.json  -> load ONLY into payer_sandbox.py (never the agent)")


if __name__ == "__main__":
    main()
