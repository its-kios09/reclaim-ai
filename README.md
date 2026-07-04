# Reclaim

**The autonomous agent that fights denied medical claims — and learns each
payer's behavior to prevent the next denial.**

Built on Qwen (qwen-max / qwen-vl-max) · Alibaba Cloud · FHIR R4

*Global AI Hackathon Series with Qwen Cloud — Track 4: Autopilot Agent*

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)

---

## The problem

Healthcare providers lose staggering amounts of earned revenue to claim
denials — not because the claims are invalid, but because fighting denials
requires skilled human labor that doesn't scale:

- **~$262 billion** in medical claims are denied annually in the US alone
  (~15% of all claims submitted to private payers)
- **More than half (54.3%)** of denied claims are ultimately overturned when
  providers appeal — meaning most denials are wrong or fixable
- Yet **~65% of denied claims are never resubmitted**, because each appeal
  costs ~$44 in administrative labor and takes an average of three review
  rounds of 45–60 days each

The pattern repeats in every insured health system worldwide. Appeal work is
high-volume, evidence-driven, and payer-rule-bound — work that is currently
rationed by labor cost. Reclaim removes the ration.

## What Reclaim does

Given a denied claim (structured FHIR resource **or a photographed denial
letter**), Reclaim autonomously:

1. **Investigates** — parses the denial reason (CARC/RARC codes) and consults
   its learned model of this payer's actual behavior
2. **Selects a strategy** — via Thompson sampling over per-payer success
   posteriors (see [The Denial Genome](#the-denial-genome))
3. **Drafts the appeal** — qwen-max writes the appeal letter, evidence list,
   and coding corrections
4. **Battle-tests it** — a second, adversarial qwen-max instance plays the
   payer's adjudicator and attacks the draft; the writer revises until the
   draft survives (max 3 rounds)
5. **Pauses for a human** — the appeal, its rationale, and the adversary's
   verdict are presented at an approval gate. Nothing is submitted without
   sign-off
6. **Submits and learns** — the adjudication outcome (approve *or* deny)
   updates the payer's behavioral model, making the next claim smarter

Over a batch of claims, recovery visibly turns into **prevention**: the agent
begins predicting winning strategies — and flagging likely denials — before
appeals are drafted.

## The Denial Genome

The core innovation. Payer rejection messages are systematically vague
("claim lacks information") while the *actual* adjudication rules are hidden.
Reclaim treats appeal-strategy selection as a **contextual bandit problem**:

- Each `(payer, denial-code)` context holds a Beta posterior per candidate
  strategy (add modifier, attach clinical notes, correct diagnosis code, ...)
- Strategy selection is **Thompson sampling**: draw from each posterior, play
  the winner — principled exploration/exploitation, not greedy replay
- Every outcome (success and failure) updates the posterior; confidence
  shown in the UI is the real posterior probability, not a heuristic

Failures teach as much as successes: a rejected appeal narrows the posterior
just as an accepted one does.

## Architecture

```
                       ┌─────────────────────────────────────────────┐
                       │        RECLAIM CORE (Alibaba Cloud ECS)     │
  FHIR R4 Claim ──────>│                                             │
  Denial letter ──────>│  Intake ─── qwen-vl-max (vision extraction) │
  (photo/scan)         │     │                                       │
                       │  Investigator ── Denial Genome              │
                       │     │            (Thompson sampling)        │
                       │  Appeal Writer ⇄ Adversarial Adjudicator    │
                       │     │            (qwen-max × 2, ≤3 rounds)  │
                       │  Human Approval Gate ── dashboard UI        │
                       │     │                                       │
                       │  PayerGateway (adapter interface)           │
                       └─────┼───────────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │ SandboxPayerAdapter (active)│   ClearinghouseAdapter
              │ FHIR R4 payer sandbox       │   (documented prod path:
              │ auth · idempotency · retry  │    Availity / Change-Optum)
              └─────────────────────────────┘
```

Every claim's lifecycle is **event-sourced**: an append-only log
(`ClaimIngested → StrategySampled → DraftCreated → AdversaryChallenged →
HumanApproved → AdjudicationReceived → PosteriorUpdated`). State is a fold
over events — giving crash-resumable batch processing, a complete audit
trail (a domain requirement in claims processing), and a replayable
per-claim timeline in the UI.

### Engineering notes

- **Model routing** — qwen-turbo for extraction/classification, qwen-max
  only where reasoning pays (drafting, adversarial review). Inference cost
  is tracked per call and reported as **$ spent per $ recovered**
- **Schema-guarded LLM I/O** — all model outputs validate against pydantic
  schemas with a bounded repair loop; malformed output emits an event and
  retries, never crashes a claim
- **Adversary calibration** — the adjudicator verdict is sampled with
  self-consistency; high-variance verdicts route to the human gate flagged
  low-confidence
- **Production behaviors at the payer boundary** — API-key auth, idempotency
  keys, bounded retries with exponential backoff, full FHIR ClaimResponse
  retained for audit

## Honest scope

The payer's *decision logic* is a sandbox — as it is in every real payer
integration's first phase (Availity, Change Healthcare, and payer APIs all
onboard integrators against sandboxes). The sandbox enforces hidden
adjudication rules the agent must genuinely learn; the agent never sees
them. Everything else is real: real Qwen inference on Alibaba Cloud, real
FHIR R4 resources, real learning from real outcomes. Going live is an
adapter swap behind `PayerGateway`, not a rewrite.

The claims dataset is synthetic (generated, seeded, no real patient data
anywhere) using real-world CARC/RARC denial codes and CPT/ICD-10 coding.

## Quick start

```bash
git clone https://github.com/its-kios09/reclaim-ai
cd reclaim-ai

# 1. Generate the claims world (50 denied claims, ~$27K)
python sandbox/seed_claims.py

# 2. Start the payer sandbox
python sandbox/payer_sandbox.py &            # :8090

# 3. Configure Qwen access (Alibaba Cloud Model Studio)
export DASHSCOPE_API_KEY=sk-...

# 4. Run the agent against the batch
python -m reclaim.batch --claims claims.json

# 5. Open the dashboard
python -m reclaim.ui                            # :8000
```

## Repository layout

```
reclaim/            agent core: events, genome, writer, adversary, gateway
sandbox/            payer sandbox (FHIR R4) + claims seed tooling
ui/                 dashboard: queue, ledger, approval gate, genome panel
docs/               architecture diagram, deployment proof, eval results
```

## Evaluation

Run the 50-claim benchmark and report recovery rate, mean adversary rounds,
mean rounds-to-approval, and inference cost per recovered dollar:

```bash
python -m reclaim.eval --claims claims.json --fresh-genome
```

## Status

- [x] Claims world: generator + FHIR R4 payer sandbox (auth, idempotency)
- [x] PayerGateway adapter interface + sandbox adapter
- [ ] Denial Genome (Thompson sampling)
- [ ] Event-sourced claim lifecycle
- [ ] Appeal writer + adversarial adjudicator loop
- [ ] Human approval gate + dashboard
- [ ] Qwen-VL denial-letter intake
- [ ] Eval harness + benchmark results
- [ ] Alibaba Cloud deployment + proof recording
- [ ] Demo video

## License

MIT — see [LICENSE](LICENSE).
