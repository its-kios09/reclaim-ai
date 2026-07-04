# Payer sandbox environment

Simulates a payer's adjudication endpoint (FHIR R4 ClaimResponse, API-key
auth, idempotency) with hidden adjudication rules — mirroring the sandbox
phase every live clearinghouse integration starts with.

`seed_claims.py` generates the claims world (seeded, synthetic, no real
patient data). `payer_secrets.json` is the payer's private rule state:
**the agent never reads it.**
