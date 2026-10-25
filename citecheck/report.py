"""Render an AuditReport (+ verification result) as chat-friendly markdown."""

import os

from .models import AuditReport, ClaimStatus, MetadataStatus, VerificationResult

META_LABEL = {
    MetadataStatus.VERIFIED: "✅ Verified",
    MetadataStatus.MISMATCH: "⚠️ Metadata mismatch",
    MetadataStatus.DOI_NOT_FOUND: "❌ DOI not found",
    MetadataStatus.NOT_FOUND: "❓ No confident match",
    MetadataStatus.INSUFFICIENT_INFO: "❓ Insufficient information",
    MetadataStatus.LOOKUP_ERROR: "🔌 Lookup failed",
}
CLAIM_LABEL = {
    ClaimStatus.NO_CLAIM: "—",
    ClaimStatus.NOT_ASSESSED: "not assessed",
    ClaimStatus.PLAUSIBLE: "plausible (abstract)",
    ClaimStatus.UNCERTAIN: "uncertain",
    ClaimStatus.CONTRADICTED: "contradicted (abstract)",
}

# Tool-call transcripts are useful in logs, but can be several thousand
# characters long and cause ASI:One to display only the end of a report.
SHOW_AGENT_TRACE = os.getenv("REPORT_SHOW_AGENT_TRACE", "off") == "on"


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def render_report(report: AuditReport, verification: VerificationResult | None = None) -> str:
    s = report.summary
    verified = s.by_metadata_status.get("verified", 0)
    lines = [
        f"## CiteCheck audit `{report.audit_id}` — {s.total} reference(s)",
        f"**{verified} verified · {s.needs_attention} need attention · {s.unresolved} unresolved**",
        "",
        "| # | Reference | Metadata | Claim |",
        "|---|---|---|---|",
    ]
    for r in report.results:
        lines.append(f"| {r.index} | {_short(r.raw)} | {META_LABEL[r.metadata_status]} | "
                     f"{CLAIM_LABEL[r.claim.status]} |")

    # ASI:One has a fairly small visible-message budget.  The table establishes
    # a verdict for every submitted reference; the details below carry only the
    # decision-changing evidence, rather than repeating the entire input and
    # every registry note.
    lines += ["", "### Findings"]
    for r in report.results:
        mismatches = [f for f in r.field_checks if not f.matches]
        metadata_needs_detail = r.metadata_status != MetadataStatus.VERIFIED
        claim_needs_detail = r.claim.status in {
            ClaimStatus.NOT_ASSESSED, ClaimStatus.UNCERTAIN, ClaimStatus.CONTRADICTED,
        }
        if not metadata_needs_detail and not claim_needs_detail:
            continue
        lines.append(f"**#{r.index} {META_LABEL[r.metadata_status]} · claim: {CLAIM_LABEL[r.claim.status]}**")
        if mismatches:
            changes = "; ".join(
                f"`{f.field}` {f.cited} → {f.retrieved or 'not listed'}" for f in mismatches
            )
            lines.append(f"- Metadata: {_short(changes, 360)}")
        # The first note describes the lookup outcome; later notes are usually
        # implementation detail or a duplicate of a mismatch shown above.
        if metadata_needs_detail and r.notes:
            lines.append(f"- {_short(r.notes[0], 220)}")
        if r.corrected_entry:
            lines.append(f"- Corrected citation: `{_short(r.corrected_entry, 360)}`")
        if claim_needs_detail:
            evidence = r.claim.quote or r.claim.rationale
            lines.append(f"- Evidence: {_short(evidence, 180)}")
        if r.agent_trace and SHOW_AGENT_TRACE:
            lines.append(f"- 🧠 ReAct investigator ({len(r.agent_trace)} step(s)):")
            for step in r.agent_trace:
                lines.append(f"  - {_short(step, 480)}")
        lines.append("")

    lines.append("### Quality verification")
    for round_note in report.reflection:
        lines.append(f"- 🔁 {round_note}")
    if verification is None:
        lines.append("- Verification did not run.")
    else:
        icon = "✅" if verification.passed else "❌"
        lines.append(f"- {icon} **{verification.verifier}**: {verification.summary}")
        for issue in verification.issues[:8]:
            lines.append(f"  - {issue}")

    lines += [
        "",
        "<sub>Metadata: Crossref/OpenAlex. Claims: abstract-only evidence; a missing record does not prove a citation is fake.</sub>",
    ]
    return "\n".join(lines)
