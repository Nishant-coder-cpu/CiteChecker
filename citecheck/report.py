"""Render an AuditReport (+ verification result) as chat-friendly markdown."""

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

    lines += ["", "### Details"]
    for r in report.results:
        lines.append(f"**#{r.index} {META_LABEL[r.metadata_status]}**")
        for f in r.field_checks:
            if not f.matches:
                lines.append(f"- `{f.field}`: cited *{f.cited}* → registry *{f.retrieved or 'not listed'}*")
        for note in r.notes:
            lines.append(f"- {note}")
        if r.matched:
            via = ", ".join(r.corroborating_sources) or r.matched.source
            lines.append(f"- Source ({via}): {r.matched.url}")
        if r.corrected_entry:
            lines.append(f"- Suggested entry (built only from {r.matched.source} metadata): `{r.corrected_entry}`")
        if r.claim.status != ClaimStatus.NO_CLAIM:
            lines.append(f"- Claim: *\"{_short(r.parsed.claim or '', 140)}\"* → **{CLAIM_LABEL[r.claim.status]}**. "
                         f"{r.claim.rationale}")
            if r.claim.quote:
                lines.append(f"  - Abstract says: \"{_short(r.claim.quote, 300)}\"")
        if r.metadata_status == MetadataStatus.VERIFIED and not r.notes and r.claim.status == ClaimStatus.NO_CLAIM:
            lines.append("- Title, year, author and DOI (where cited) agree with the registry record.")
        lines.append("")

    lines.append("### Quality verification")
    if verification is None:
        lines.append("- Verification did not run.")
    else:
        icon = "✅" if verification.passed else "❌"
        lines.append(f"- {icon} **{verification.verifier}**: {verification.summary}")
        for issue in verification.issues[:8]:
            lines.append(f"  - {issue}")

    lines += [
        "",
        "<sub>Metadata checks are deterministic comparisons against Crossref/OpenAlex records. "
        "Claim verdicts come from ASI1 reading the **abstract only** (not the full paper) and are "
        "kept only if the quoted passage really appears in the abstract. A missing record does not "
        "prove a citation is fake, and a valid DOI does not prove the paper supports your claim.</sub>",
    ]
    return "\n".join(lines)
