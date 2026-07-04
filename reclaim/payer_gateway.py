"""
Reclaim — payer gateway layer.

Production pattern: the agent never talks to a payer directly. It talks to
a PayerGateway interface. Today that interface is backed by the FHIR R4
payer sandbox; in production it is backed by a clearinghouse adapter
(Availity, Change Healthcare / Optum, or a direct payer API). Swapping is
a configuration change, not a code change.

    gateway = build_gateway()          # reads RECLAIM_PAYER_ADAPTER env
    claim   = gateway.get_claim("CLM-2026-0048")
    result  = gateway.submit_appeal("CLM-2026-0048", appeal)
"""

from __future__ import annotations

import json
import os
import time
import uuid
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Domain types (FHIR-aligned, minimal)
# ---------------------------------------------------------------------------

@dataclass
class AppealSubmission:
    appeal_letter: str
    attachments: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    corrected_icd10: str | None = None
    severity_documented: bool = False

    def to_payload(self) -> dict:
        p = {
            "appeal_letter": self.appeal_letter,
            "attachments": self.attachments,
            "modifiers": self.modifiers,
            "severity_documented": self.severity_documented,
        }
        if self.corrected_icd10:
            p["corrected_icd10"] = self.corrected_icd10
        return p


@dataclass
class AdjudicationResult:
    claim_id: str
    approved: bool
    paid_amount_usd: float
    disposition: str                 # payer's message (often vague)
    raw_fhir: dict                   # full ClaimResponse for audit trail
    request_id: str                  # idempotency/audit key


# ---------------------------------------------------------------------------
# Gateway interface
# ---------------------------------------------------------------------------

class PayerGateway(ABC):
    """Every payer connection implements this. The agent depends on
    nothing else."""

    @abstractmethod
    def list_claims(self) -> list[dict]: ...

    @abstractmethod
    def get_claim(self, claim_id: str) -> dict: ...

    @abstractmethod
    def submit_appeal(self, claim_id: str,
                      appeal: AppealSubmission) -> AdjudicationResult: ...

    @abstractmethod
    def get_ledger(self) -> dict: ...


# ---------------------------------------------------------------------------
# Sandbox adapter (active today)
# ---------------------------------------------------------------------------

class SandboxPayerAdapter(PayerGateway):
    """Talks to the FHIR R4 payer sandbox over HTTP.

    Production behaviors included deliberately:
      - API-key auth header
      - Idempotency keys on submissions
      - Bounded retries with exponential backoff on transient failures
      - Full FHIR ClaimResponse retained for the audit trail
    """

    def __init__(self, base_url: str | None = None,
                 api_key: str | None = None,
                 max_retries: int = 3):
        self.base_url = (base_url or
                         os.environ.get("PAYER_SANDBOX_URL",
                                        "http://localhost:8090")).rstrip("/")
        self.api_key = api_key or os.environ.get("PAYER_SANDBOX_API_KEY",
                                                 "sandbox-dev-key")
        self.max_retries = max_retries

    # -- plumbing -----------------------------------------------------------
    def _request(self, method: str, path: str,
                 body: dict | None = None,
                 idempotency_key: str | None = None) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/fhir+json",
                   "X-Api-Key": self.api_key}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        data = json.dumps(body).encode() if body is not None else None
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503):   # transient -> retry
                    last_err = e
                    time.sleep(0.5 * 2 ** attempt)
                    continue
                raise                                 # 4xx -> caller's bug
            except urllib.error.URLError as e:
                last_err = e
                time.sleep(0.5 * 2 ** attempt)
        raise ConnectionError(
            f"payer unreachable after {self.max_retries} attempts: {last_err}")

    # -- interface ----------------------------------------------------------
    def list_claims(self) -> list[dict]:
        return self._request("GET", "/claims")["claims"]

    def get_claim(self, claim_id: str) -> dict:
        return self._request("GET", f"/claims/{claim_id}")

    def submit_appeal(self, claim_id: str,
                      appeal: AppealSubmission) -> AdjudicationResult:
        request_id = str(uuid.uuid4())
        fhir = self._request("POST", f"/claims/{claim_id}/appeal",
                             body=appeal.to_payload(),
                             idempotency_key=request_id)
        # Parse FHIR ClaimResponse
        approved = fhir.get("outcome") == "complete" and \
            (fhir.get("payment", {}).get("amount", {}).get("value", 0) > 0)
        return AdjudicationResult(
            claim_id=claim_id,
            approved=approved,
            paid_amount_usd=fhir.get("payment", {})
                                .get("amount", {}).get("value", 0.0),
            disposition=fhir.get("disposition", ""),
            raw_fhir=fhir,
            request_id=request_id,
        )

    def get_ledger(self) -> dict:
        return self._request("GET", "/ledger")


# ---------------------------------------------------------------------------
# Clearinghouse adapter (production path — documented, not active)
# ---------------------------------------------------------------------------

class ClearinghouseAdapter(PayerGateway):
    """Production integration point.

    A live deployment implements this against a clearinghouse:
      - Availity Essentials API (claim status: X12 276/277, appeals via
        payer portals or 275 attachments)
      - Change Healthcare / Optum APIs (X12 837 resubmission, 275)
      - Or a payer's direct FHIR endpoint where offered.

    The agent code above this interface does not change. That is the point.
    """

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "Live clearinghouse credentials required. "
            "Set RECLAIM_PAYER_ADAPTER=sandbox for the sandbox environment.")

    list_claims = get_claim = submit_appeal = get_ledger = None  # type: ignore


def build_gateway() -> PayerGateway:
    adapter = os.environ.get("RECLAIM_PAYER_ADAPTER", "sandbox").lower()
    if adapter == "sandbox":
        return SandboxPayerAdapter()
    if adapter == "clearinghouse":
        return ClearinghouseAdapter()
    raise ValueError(f"unknown payer adapter: {adapter}")
