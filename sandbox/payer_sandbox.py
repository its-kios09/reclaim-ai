#!/usr/bin/env python3
"""
Reclaim — FHIR R4 payer sandbox ("Alpha Assurance Group").

Industry-standard pattern: every payer/clearinghouse integration begins
against a sandbox environment. This sandbox speaks FHIR R4 ClaimResponse,
enforces API-key auth, and honors idempotency keys — the same contract a
live clearinghouse adapter would satisfy behind the PayerGateway interface.

Pure Python stdlib (http.server) — zero dependencies, runs anywhere,
including a bare Alibaba Cloud ECS instance next to your agent backend.

The payer holds payer_secrets.json (from generate_claims.py) and
adjudicates appeals against HIDDEN rules the agent cannot see.
Its rejection messages are deliberately vague — exactly like real payers —
which is what forces the Denial Genome to learn from outcomes.

Run:    python3 payer_sandbox.py         (port 8090)
Needs:  claims.json + payer_secrets.json in the same directory.

API contract
------------
GET  /claims                       -> list all claims (status only)
GET  /claims/{id}                  -> full visible claim
POST /claims/{id}/appeal           -> adjudicate an appeal
       body: {
         "appeal_letter":   "...",                  # required
         "attachments":     ["clinical_notes", ...],# optional
         "modifiers":       ["25"],                 # optional
         "corrected_icd10": "E11.65",               # optional
         "severity_documented": true                # optional
       }
       resp: { "outcome": "approved"|"denied",
               "paid_amount_usd": float,
               "payer_message": "...",              # vague on purpose
               "adjudicated_at": iso8601 }
GET  /payer/policy                 -> public policy blurb (partial/misleading)
GET  /ledger                       -> demo scoreboard: recovered vs outstanding
"""

import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8090

with open("claims.json") as f:
    _data = json.load(f)
CLAIMS = {c["claim_id"]: c for c in _data["claims"]}
with open("payer_secrets.json") as f:
    SECRETS = json.load(f)

LEDGER = {}      # claim_id -> {"outcome":..., "paid":...}
IDEMPOTENT = {}  # Idempotency-Key -> cached response (production behavior)
API_KEY = __import__("os").environ.get("PAYER_SANDBOX_API_KEY",
                                       "sandbox-dev-key")


# ---------------------------------------------------------------------------
# Hidden rules engine — the "truth" the Denial Genome must learn
# ---------------------------------------------------------------------------

def adjudicate(claim_id: str, appeal: dict) -> tuple[bool, str]:
    """Return (approved, payer_message). Messages are vague by design."""
    secret = SECRETS[claim_id]
    truth = secret["hidden_truth"]
    attachments = set(appeal.get("attachments", []))
    modifiers = set(str(m) for m in appeal.get("modifiers", []))

    if not appeal.get("appeal_letter", "").strip():
        return False, "Appeal incomplete. Refer to plan documentation."

    if truth == "requires_modifier_25":
        if "25" in modifiers:
            return True, "Appeal accepted upon re-review of coding."
        # The trap: documentation alone does NOT work, and the message
        # actively misleads (implies documentation was the issue).
        return False, ("Documentation reviewed; claim remains unpayable "
                       "as submitted. Refer to correct coding guidelines.")

    if truth == "requires_corrected_icd":
        if appeal.get("corrected_icd10") == secret["correct_icd10"]:
            return True, "Corrected diagnosis accepted."
        return False, "Diagnosis remains inconsistent with the procedure."

    if truth == "requires_clinical_notes_and_severity":
        if "clinical_notes" in attachments and appeal.get("severity_documented"):
            return True, "Medical necessity established on re-review."
        if "clinical_notes" in attachments:
            # Half-right gets a misleadingly encouraging rejection
            return False, ("Clinical documentation received; medical "
                           "necessity criteria not met as documented.")
        return False, "Insufficient documentation of medical necessity."

    if truth == "requires_timely_filing_receipt":
        if "submission_receipt" in attachments:
            return True, "Timely filing exception granted."
        return False, "Filing limit exception criteria not met."

    if truth == "requires_modifier_59":
        if "59" in modifiers:
            return True, "Distinct procedural service acknowledged."
        return False, "Service remains bundled per NCCI edits."

    if truth == "requires_medical_necessity_letter":
        if "medical_necessity_letter" in attachments:
            return True, "Coverage exception approved."
        return False, "Service not covered under the member's plan."

    return False, "Appeal denied. Refer to plan documentation."


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        if self.path == "/claims":
            self._send(200, {
                "claims": [
                    {"claim_id": cid,
                     "status": LEDGER.get(cid, {}).get("outcome", "denied"),
                     "billed_amount_usd":
                         c["service_lines"][0]["billed_amount_usd"]}
                    for cid, c in CLAIMS.items()
                ]
            })
            return

        m = re.fullmatch(r"/claims/(CLM-\d{4}-\d{4})", self.path)
        if m:
            claim = CLAIMS.get(m.group(1))
            if claim:
                self._send(200, claim)
            else:
                self._send(404, {"error": "claim not found"})
            return

        if self.path == "/payer/policy":
            # Deliberately partial: mentions documentation everywhere,
            # never mentions the modifier rules. Real payer energy.
            self._send(200, {
                "payer": "Alpha Assurance Group",
                "appeals_policy": (
                    "Appeals must be filed within 60 days of denial and "
                    "include all supporting documentation. Claims denied "
                    "for missing information should be resubmitted with "
                    "complete records. Medical necessity denials require "
                    "clinical documentation."),
            })
            return

        if self.path == "/ledger":
            paid = sum(v["paid"] for v in LEDGER.values()
                       if v["outcome"] == "approved")
            total = _data["total_denied_usd"]
            self._send(200, {
                "total_denied_usd": total,
                "recovered_usd": round(paid, 2),
                "outstanding_usd": round(total - paid, 2),
                "appeals_adjudicated": len(LEDGER),
            })
            return

        self._send(404, {"error": "unknown endpoint"})

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
        # API-key auth, like any real payer sandbox
        if self.headers.get("X-Api-Key") != API_KEY:
            self._send(401, {"resourceType": "OperationOutcome",
                             "issue": [{"severity": "error",
                                        "code": "security",
                                        "diagnostics": "invalid API key"}]})
            return

        m = re.fullmatch(r"/claims/(CLM-\d{4}-\d{4})/appeal", self.path)
        if not m:
            self._send(404, {"error": "unknown endpoint"})
            return
        claim_id = m.group(1)
        if claim_id not in CLAIMS:
            self._send(404, {"error": "claim not found"})
            return

        # Idempotency: same key -> same response, no double adjudication
        idem_key = self.headers.get("Idempotency-Key")
        if idem_key and idem_key in IDEMPOTENT:
            self._send(200, IDEMPOTENT[idem_key])
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            appeal = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid JSON body"})
            return

        approved, message = adjudicate(claim_id, appeal)
        claim = CLAIMS[claim_id]
        paid = (claim["service_lines"][0]["billed_amount_usd"]
                if approved else 0.0)
        LEDGER[claim_id] = {"outcome": "approved" if approved else "denied",
                            "paid": paid}

        # FHIR R4 ClaimResponse — the contract a live integration returns
        now = datetime.now(timezone.utc).isoformat()
        response = {
            "resourceType": "ClaimResponse",
            "id": f"cr-{claim_id.lower()}-{len(IDEMPOTENT) + 1}",
            "status": "active",
            "type": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                "code": "professional"}]},
            "use": "claim",
            "patient": {"display": claim["patient"]["name"],
                        "identifier": {
                            "value": claim["patient"]["member_id"]}},
            "created": now,
            "insurer": {"display": claim["payer"]["name"],
                        "identifier": {"value": claim["payer"]["id"]}},
            "request": {"reference": f"Claim/{claim_id}"},
            "outcome": "complete" if approved else "error",
            "disposition": message,
            "item": [{
                "itemSequence": 1,
                "adjudication": [{
                    "category": {"coding": [{
                        "system": ("http://terminology.hl7.org/CodeSystem/"
                                   "adjudication"),
                        "code": "benefit" if approved else "denied"}]},
                    "amount": {"value": paid, "currency": "USD"},
                }],
            }],
            "payment": {
                "type": {"coding": [{"code": "complete" if approved
                                     else "none"}]},
                "amount": {"value": paid, "currency": "USD"},
                "date": now[:10],
            },
            "processNote": [{"number": 1, "text": message}],
        }
        if idem_key:
            IDEMPOTENT[idem_key] = response
        self._send(200, response)

    def log_message(self, fmt, *args):  # quieter console
        print(f"[payer] {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    print(f"Alpha Assurance payer sandbox listening on :{PORT}")
    print(f"  {len(CLAIMS)} claims loaded, ${_data['total_denied_usd']:,.2f} denied")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
