"""Deterministic quality verification of an AuditReport.

Independent of the audit pipeline's decisions: it re-parses the user's original
input, recomputes all counts, checks every correction and evidence claim against
the stored source records and, optionally, re-fetches each matched DOI from the
registry itself. Passing means these defined checks passed - not that the cited
science is correct.
"""

from collections import Counter

from . import sources
from .compare import compare_fields, format_entry, norm
from .models import AuditReport, ClaimStatus, MetadataStatus, VerificationResult
from .parsing import parse_references

UNRESOLVED = {"doi_not_found", "not_found", "insufficient_info", "lookup_error"}


class _Checks:
    def __init__(self):
        self.total = self.failed = self.skipped = 0
        self.issues: list[str] = []

    def check(self, ok: bool, issue: str) -> None:
        self.total += 1
        if not ok:
            self.failed += 1
            self.issues.append(issue)

    def skip(self, issue: str) -> None:
        self.total += 1
        self.skipped += 1
        self.issues.append(f"[skipped] {issue}")


def _recheck_live(c: _Checks, idx: int, rec) -> None:
    fetch = sources.openalex_by_doi if rec.source == "openalex" else sources.crossref_by_doi
    lookup = fetch(rec.doi)
    if lookup.error or not lookup.records:
        c.skip(f"#{idx}: could not re-fetch {rec.doi} from {rec.source} ({lookup.error or 'not found'})")
        return
    fresh = lookup.records[0]
    c.check(norm(fresh.title) == norm(rec.title) and fresh.year == rec.year
            and fresh.authors[:1] == rec.authors[:1],
            f"#{idx}: report metadata for {rec.doi} differs from a fresh {rec.source} lookup")


def verify_report(original_text: str, report: AuditReport, live_recheck: bool = False,
                  verifier: str = "local-deterministic") -> VerificationResult:
    c = _Checks()
    expected = parse_references(original_text)
    results = report.results

    # 1. Completeness and binding to the right input reference.
    c.check(len(results) == len(expected),
            f"Report has {len(results)} results but input contains {len(expected)} references")
    c.check(sorted(r.index for r in results) == list(range(1, len(results) + 1)),
            "Result indices are not exactly 1..N")
    by_index = {e.index: e for e in expected}
    for r in results:
        exp = by_index.get(r.index)
        c.check(exp is not None and exp.raw == r.raw,
                f"#{r.index}: result is not bound to the matching input reference")

    # 2. Summary counts recomputed from the records.
    s = report.summary
    c.check(s.total == len(results), "summary.total does not match number of results")
    c.check(s.by_metadata_status == dict(Counter(r.metadata_status.value for r in results)),
            "summary metadata counts do not match the individual records")
    c.check(s.by_claim_status == dict(Counter(r.claim.status.value for r in results)),
            "summary claim counts do not match the individual records")
    unresolved = [r.index for r in results if r.metadata_status.value in UNRESOLVED]
    c.check(s.unresolved == len(unresolved), "summary.unresolved does not match the records")

    for r in results:
        idx, rec, status = r.index, r.matched, r.metadata_status

        # 3. Unresolved cases must say why.
        if status.value in UNRESOLVED:
            c.check(bool(r.notes), f"#{idx}: unresolved ({status.value}) without an explanation")

        # 4. Status must agree with the field-level evidence.
        if status in (MetadataStatus.VERIFIED, MetadataStatus.MISMATCH):
            c.check(rec is not None and bool(r.corroborating_sources),
                    f"#{idx}: '{status.value}' without a linked source record")
        if status == MetadataStatus.VERIFIED:
            c.check(all(f.matches for f in r.field_checks), f"#{idx}: 'verified' but a field check failed")
        if status == MetadataStatus.MISMATCH:
            c.check(any(not f.matches for f in r.field_checks), f"#{idx}: 'mismatch' with no failing field")
        if rec is not None and r.field_checks:
            recomputed = {(f.field, f.retrieved, f.matches) for f in compare_fields(r.parsed, rec)}
            reported = {(f.field, f.retrieved, f.matches) for f in r.field_checks}
            c.check(recomputed == reported, f"#{idx}: field checks do not match the stored source record")

        # 5. Corrections only from retrieved metadata.
        if r.corrected_entry is not None:
            c.check(rec is not None and r.corrected_entry == format_entry(rec),
                    f"#{idx}: corrected entry is not derived from the retrieved record")
            c.check(status in (MetadataStatus.MISMATCH, MetadataStatus.DOI_NOT_FOUND),
                    f"#{idx}: correction offered for a '{status.value}' reference")

        # 6. Claim judgments need a source and quoted evidence.
        cl = r.claim
        c.check((cl.status == ClaimStatus.NO_CLAIM) == (not r.parsed.claim),
                f"#{idx}: claim status inconsistent with whether a claim was submitted")
        if cl.status in (ClaimStatus.PLAUSIBLE, ClaimStatus.CONTRADICTED):
            c.check(bool(cl.evidence_source) and rec is not None and bool(rec.abstract)
                    and bool(cl.quote) and norm(cl.quote) in norm(rec.abstract),
                    f"#{idx}: '{cl.status.value}' claim verdict without verifiable quoted evidence")

        # 7. A record identified by the ReAct investigator must be one its tools returned.
        if r.agent_trace and rec is not None and any("ReAct investigator" in n for n in r.notes):
            observed = " ".join(r.agent_trace).lower()
            c.check(bool(rec.doi) and rec.doi.lower() in observed,
                    f"#{idx}: investigator-identified DOI {rec.doi} does not appear in its tool observations")

        # 8. Independent live re-fetch of the matched record.
        if live_recheck and rec is not None and rec.doi:
            _recheck_live(c, idx, rec)

    passed = c.failed == 0
    summary = (
        f"{c.total - c.failed - c.skipped}/{c.total} checks passed"
        + (f", {c.skipped} skipped" if c.skipped else "")
        + (f", {c.failed} FAILED" if c.failed else "")
        + f". {len(unresolved)} reference(s) unresolved. Structural/evidence checks only; "
        "this does not certify the scholarship itself."
    )
    return VerificationResult(
        passed=passed, total_checks=c.total, failed_checks=c.failed, skipped_checks=c.skipped,
        unresolved_references=unresolved, issues=c.issues, summary=summary, verifier=verifier,
    )
