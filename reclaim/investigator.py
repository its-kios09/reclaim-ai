"""
Reclaim — the investigator. The agent's first thinking step.

What this file does, in one sentence:
    Give it one denied claim, and it asks Qwen to act as a claims
    specialist — figure out what the denial code really means, what
    the payer is probably hiding, and which fix to try — returning
    a structured JSON verdict we can act on.

Run it:
    export DASHSCOPE_API_KEY=sk-...
    python3 -m reclaim.investigator            # investigates claim #3
    python3 -m reclaim.investigator CLM-2026-0011
"""

from __future__ import annotations

import json
import os
import sys

import requests

QWEN_URL = ("https://dashscope-intl.aliyuncs.com"
            "/compatible-mode/v1/chat/completions")
MODEL = "qwen-max"

STRATEGIES = (
    "ATTACH_DOCUMENTATION",
    "ADD_MODIFIER_25",
    "ADD_MODIFIER_59",
    "CORRECT_DIAGNOSIS",
    "ATTACH_CLINICAL_NOTES_WITH_SEVERITY",
    "ATTACH_SUBMISSION_RECEIPT",
    "ATTACH_MEDICAL_NECESSITY_LETTER",
)

SYSTEM_PROMPT = f"""You are a senior medical claims appeals specialist.
You are given one denied claim. Payer denial messages are often vague or
misleading; reason about what the denial code actually implies and what
evidence or coding correction is most likely to overturn it.

Respond with ONLY a JSON object - no markdown, no commentary:
{{
  "denial_analysis": "2-3 sentences: what this denial really means",
  "likely_root_cause": "one sentence",
  "recommended_strategy": "<one of: {', '.join(STRATEGIES)}>",
  "fallback_strategy": "<a different one from the same list>",
  "confidence": <float 0-1>,
  "reasoning": "2-3 sentences: why this strategy over the fallback"
}}"""


def ask_qwen(messages: list[dict]) -> str:
    """One POST to Qwen. Returns the model's text reply."""
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        sys.exit("Set DASHSCOPE_API_KEY first: export DASHSCOPE_API_KEY=sk-...")
    resp = requests.post(
        QWEN_URL,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "temperature": 0.3},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def investigate(claim: dict) -> dict:
    """The thinking step: claim in, structured verdict out."""
    line = claim["service_lines"][0]
    facts = {
        "claim_id": claim["claim_id"],
        "payer": claim["payer"]["name"],
        "service": {"cpt": line["cpt"], "description": line["description"],
                    "icd10": line["icd10"],
                    "billed_usd": line["billed_amount_usd"]},
        "denial": claim["denial"],
        "dates": claim["dates"],
    }
    reply = ask_qwen([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(facts, indent=2)},
    ])

    cleaned = reply.strip().removeprefix("```json").removeprefix("```")
    cleaned = cleaned.removesuffix("```").strip()
    verdict = json.loads(cleaned)

    if verdict.get("recommended_strategy") not in STRATEGIES:
        raise ValueError(
            f"Qwen chose an unknown strategy: {verdict.get('recommended_strategy')}")
    return verdict


if __name__ == "__main__":
    wanted = sys.argv[1] if len(sys.argv) > 1 else "CLM-2026-0003"
    claims = json.load(open("claims.json"))["claims"]
    claim = next(c for c in claims if c["claim_id"] == wanted)

    print(f"Investigating {claim['claim_id']} - denied "
          f"{claim['denial']['carc_code']}: "
          f"\"{claim['denial']['reason_text']}\"\n")
    verdict = investigate(claim)
    print(json.dumps(verdict, indent=2))
