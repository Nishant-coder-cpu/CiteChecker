"""Audit pipeline: parse -> look up -> compare -> assess -> structured report.

Counts and statuses are computed here from structured records; the LLM is only
used inside assess.assess_claim for the claim-support judgment.
"""

import asyncio
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from . import sources
from .assess import assess_claim
from .compare import best_candidate, compare_fields, format_entry, title_similarity, TITLE_MATCH
from .models import (
    AuditReport,
    AuditSummary,
    ClaimAssessment,
    ClaimStatus,
    MetadataStatus,
    ParsedReference,
    ReferenceResult,
    SourceRecord,
)
from .parsing import parse_references

UNRESOLVED = {
    MetadataStatus.DOI_NOT_FOUND,
    MetadataStatus.NOT_FOUND,
    MetadataStatus.INSUFFICIENT_INFO,
    MetadataStatus.LOOKUP_ERROR,
}
CONCURRENCY = 4


async def _run(fn, *args) -> sources.Lookup:
    return await asyncio.to_thread(fn, *args)


def _errors(*lookups: sources.Lookup) -> list[str]:
    return [lk.error for lk in lookups if lk.error]


def _with_abstract(rec: SourceRecord, others: list[SourceRecord]) -> SourceRecord:
    """Fill a missing abstract from another source describing the same DOI."""
    if rec.abstract or not rec.doi:
        return rec
    for other in others:
        if other.doi == rec.doi and other.abstract:
            return rec.model_copy(update={"abstract": other.abstract})
    return rec


async def _search(ref: ParsedReference) -> tuple[SourceRecord | None, float, list[sources.Lookup], str | None]:
    query = ref.title or ref.raw
    lookups = await asyncio.gather(
        _run(sources.crossref_search, ref.raw),
        _run(sources.openalex_search, query),
    )
    records = [r for lk in lookups for r in lk.records]
    rec, score, rejected = best_candidate(ref, records)
    if rec:
        rec = _with_abstract(rec, records)
    return rec, score, list(lookups), rejected


async def resolve_reference(ref: ParsedReference) -> ReferenceResult:
    notes: list[str] = []
    matched: SourceRecord | None = None
    corroborating: list[str] = []

    if not ref.doi and (not ref.title or len((ref.title or "").split()) < 3):
        return _result(ref, MetadataStatus.INSUFFICIENT_INFO, None, [],
                       ["No DOI and no recognisable title; nothing reliable to search on."])

    if ref.doi:
        cr, oa = await asyncio.gather(
            _run(sources.crossref_by_doi, ref.doi), _run(sources.openalex_by_doi, ref.doi)
        )
        notes += _errors(cr, oa)
        found = cr.records + oa.records
        corroborating = sorted({r.source for r in found})
        if found:
            matched = _with_abstract(found[0], found)
            if ref.title and title_similarity(ref.title, matched.title) < TITLE_MATCH:
                notes.append(f'DOI resolves to a different title: "{matched.title}".')
                alt, score, lks, rejected = await _search(ref)
                notes += _errors(*lks)
                if alt and alt.doi != ref.doi:
                    notes.append(f"Title search found the cited title under DOI {alt.doi} (score {score:.2f}).")
                    matched, corroborating = alt, [alt.source]
                else:
                    if rejected:
                        notes.append(rejected[0].upper() + rejected[1:] + ".")
                    notes.append("The intended work could not be confirmed, so no corrected entry is offered.")
                    checks = compare_fields(ref, matched)
                    return _result(ref, MetadataStatus.MISMATCH, matched, corroborating, notes, checks,
                                   allow_correction=False)
        elif cr.not_found and oa.not_found:
            notes.append("DOI not found in Crossref or OpenAlex. This alone does not prove the work is fake.")
            alt, score, lks, _ = await _search(ref) if ref.title else (None, 0.0, [], None)
            notes += _errors(*lks)
            if alt:
                notes.append(f"Title search suggests DOI {alt.doi} (score {score:.2f}); please confirm.")
            return _result(ref, MetadataStatus.DOI_NOT_FOUND, alt, [alt.source] if alt else [], notes)
        else:
            return _result(ref, MetadataStatus.LOOKUP_ERROR, None, [],
                           notes + ["Could not reach the metadata registries; retry later."])
    else:
        matched, score, lks, rejected = await _search(ref)
        notes += _errors(*lks)
        if rejected:
            notes.append(rejected[0].upper() + rejected[1:] + ".")
        if matched:
            corroborating = [matched.source]
            notes.append(f"Identified by bibliographic search (match score {score:.2f}); no DOI was cited.")
        elif all(lk.error for lk in lks):
            return _result(ref, MetadataStatus.LOOKUP_ERROR, None, [],
                           notes + ["Could not reach the metadata registries; retry later."])
        else:
            return _result(ref, MetadataStatus.NOT_FOUND, None, [], notes + [
                "No confident match in Crossref/OpenAlex"
                + ("" if rejected else f" (best score {score:.2f})")
                + ". Not being indexed does not prove the work is fake."
            ])

    checks = compare_fields(ref, matched)
    status = MetadataStatus.VERIFIED if all(c.matches for c in checks) else MetadataStatus.MISMATCH
    return _result(ref, status, matched, corroborating, notes, checks)


def _result(ref, status, matched, corroborating, notes, checks=None,
            allow_correction=True) -> ReferenceResult:
    corrected = format_entry(matched) if allow_correction and matched and status in (
        MetadataStatus.MISMATCH, MetadataStatus.DOI_NOT_FOUND) else None
    claim = ClaimAssessment(status=ClaimStatus.NO_CLAIM, method="none", rationale="No claim attached.")
    return ReferenceResult(
        index=ref.index, raw=ref.raw, parsed=ref, metadata_status=status, matched=matched,
        corroborating_sources=corroborating, field_checks=checks or [],
        corrected_entry=corrected, claim=claim, notes=notes,
    )


def _claim_target_confident(result: ReferenceResult) -> bool:
    if result.metadata_status not in (MetadataStatus.VERIFIED, MetadataStatus.MISMATCH):
        return False
    title_checks = [c for c in result.field_checks if c.field == "title"]
    return all(c.matches for c in title_checks)


async def _audit_one(ref: ParsedReference, llm, sem: asyncio.Semaphore) -> ReferenceResult:
    async with sem:
        result = await resolve_reference(ref)
    if ref.claim:
        if result.matched and _claim_target_confident(result):
            result.claim = await assess_claim(llm, ref.claim, result.matched)
        else:
            result.claim = ClaimAssessment(
                status=ClaimStatus.NOT_ASSESSED, method="none",
                rationale="The cited work could not be confidently identified, so the claim was not assessed.",
            )
    return result


def summarize(results: list[ReferenceResult]) -> AuditSummary:
    return AuditSummary(
        total=len(results),
        by_metadata_status=dict(Counter(r.metadata_status.value for r in results)),
        by_claim_status=dict(Counter(r.claim.status.value for r in results)),
        needs_attention=sum(r.needs_attention for r in results),
        unresolved=sum(r.metadata_status in UNRESOLVED for r in results),
    )


async def run_audit(refs: list[ParsedReference], llm=None) -> AuditReport:
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(_audit_one(r, llm, sem) for r in refs))
    results = sorted(results, key=lambda r: r.index)
    return AuditReport(
        audit_id=uuid4().hex[:10],
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        results=results,
        summary=summarize(results),
        sources_used=["Crossref REST API", "OpenAlex API"] + (["ASI1 (claim support only)"] if llm else []),
    )


async def audit_text(text: str, llm=None) -> AuditReport:
    return await run_audit(parse_references(text), llm)
