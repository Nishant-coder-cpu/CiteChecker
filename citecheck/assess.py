"""Semantic claim-support assessment with ASI1, guarded by deterministic checks.

The LLM only sees the claim and the retrieved abstract. Its answer is accepted
only if it is valid JSON with an allowed verdict, and any "plausible" or
"contradicted" verdict must quote a passage that literally occurs in the
abstract. Otherwise the verdict is downgraded to "uncertain".
"""

import json
import re

from .compare import norm
from .models import ClaimAssessment, ClaimStatus, SourceRecord

METHOD = "asi1-llm-on-abstract"

SYSTEM_PROMPT = (
    "You check whether a paper's ABSTRACT supports a CLAIM attributed to it. "
    "Use only the abstract text; do not use outside knowledge. "
    "Reply with a single JSON object and nothing else: "
    '{"verdict": "plausible" | "uncertain" | "contradicted", '
    '"quote": "<exact sentence copied from the abstract that supports or contradicts the claim, or empty>", '
    '"rationale": "<one short sentence>"}. '
    "Use plausible only if the abstract clearly states something consistent with the claim. "
    "Use contradicted only if the abstract clearly states the opposite. Otherwise uncertain."
)


def parse_llm_verdict(text: str, abstract: str, evidence_url: str) -> ClaimAssessment:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict) or data.get("verdict") not in ("plausible", "uncertain", "contradicted"):
        return ClaimAssessment(
            status=ClaimStatus.UNCERTAIN, method=METHOD, evidence_source=evidence_url,
            rationale="Model output was malformed; claim support could not be judged.",
        )
    verdict = ClaimStatus(data["verdict"])
    quote = str(data.get("quote") or "").strip()
    rationale = str(data.get("rationale") or "").strip()[:300] or "No rationale given."
    if verdict != ClaimStatus.UNCERTAIN:
        if not quote or norm(quote) not in norm(abstract):
            return ClaimAssessment(
                status=ClaimStatus.UNCERTAIN, method=METHOD, evidence_source=evidence_url,
                rationale=f"Model said '{verdict.value}' but its quote was not found in the abstract; downgraded.",
            )
    return ClaimAssessment(status=verdict, method=METHOD, evidence_source=evidence_url,
                           rationale=rationale, quote=quote or None)


async def assess_claim(llm, claim: str, record: SourceRecord) -> ClaimAssessment:
    if not record.abstract:
        return ClaimAssessment(
            status=ClaimStatus.NOT_ASSESSED, method="none", evidence_source=record.url,
            rationale="No abstract available from Crossref/OpenAlex; support cannot be judged from metadata alone.",
        )
    if llm is None:
        return ClaimAssessment(
            status=ClaimStatus.NOT_ASSESSED, method="none", evidence_source=record.url,
            rationale="Semantic assessment unavailable (no ASI1 client configured).",
        )
    try:
        result = await llm.chat.completions.create(
            model="asi1",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"CLAIM: {claim}\n\nABSTRACT: {record.abstract[:4000]}"},
            ],
            max_tokens=300,
            temperature=0,
        )
        text = result.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001 - any API failure becomes an explicit status
        return ClaimAssessment(
            status=ClaimStatus.NOT_ASSESSED, method="none", evidence_source=record.url,
            rationale=f"ASI1 request failed ({type(exc).__name__}); claim not assessed.",
        )
    return parse_llm_verdict(text, record.abstract, record.url)
