"""Deterministic metadata comparison between a cited reference and a registry record."""

import re
import unicodedata
from difflib import SequenceMatcher

from .models import FieldCheck, ParsedReference, SourceRecord

TITLE_MATCH = 0.90        # cited title counts as correct
CANDIDATE_ACCEPT = 0.80   # a search hit is accepted as the intended work


def norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def title_similarity(a: str | None, b: str | None) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # Allow a cited title that omits / adds a subtitle ("Title: Subtitle", "Title - Subtitle").
    short, long_ = sorted([(a or "").strip(), (b or "").strip()], key=len)
    rest = long_[len(short):].lstrip() if norm(long_).startswith(norm(short)) else ""
    if rest[:1] in (":", "-", "–", "—", "?", "!", ".") and len(norm(short).split()) >= 3:
        return 0.92
    return SequenceMatcher(None, na, nb).ratio()


def candidate_score(ref: ParsedReference, rec: SourceRecord) -> float:
    """How confident we are that `rec` is the work `ref` intends to cite (may exceed 1)."""
    if ref.doi and rec.doi and ref.doi == rec.doi:
        return 1.2
    score = title_similarity(ref.title or ref.raw, rec.title)
    if ref.year and rec.year:
        score += 0.05 if abs(ref.year - rec.year) <= 1 else -0.05
    if ref.first_author and rec.authors and norm(ref.first_author) == norm(rec.authors[0]):
        score += 0.05
    return score


def best_candidate(ref: ParsedReference, records: list[SourceRecord]) -> tuple[SourceRecord | None, float, str | None]:
    """Pick the search hit that is the cited work, or explain why none qualifies.

    Search hits are only accepted when the title is a near-exact match and the
    year (if both known) is within one year, so a re-upload or a different paper
    with a similar title is never presented as "the" cited work.
    """
    scored = sorted(((candidate_score(ref, r), r) for r in records), key=lambda x: -x[0])
    if not scored:
        return None, 0.0, None
    for score, rec in scored:
        if score < CANDIDATE_ACCEPT:
            break
        if not rec.doi:   # DOI-less index entries are too often stubs or duplicates
            continue
        if title_similarity(ref.title or ref.raw, rec.title) < TITLE_MATCH:
            continue
        if ref.year and rec.year and abs(ref.year - rec.year) > 1:
            continue
        return rec, min(score, 1.0), None
    score, top = scored[0]
    if not top.doi:
        why = "it has no DOI in the index, so it cannot be confirmed as the cited work"
    elif (ref.year and top.year and abs(ref.year - top.year) > 1
          and title_similarity(ref.title or ref.raw, top.title) >= TITLE_MATCH):
        why = "publication year differs from the citation"
    else:
        why = "title is not a close enough match"
    reason = (f'closest record "{top.title}" ({top.year}, {top.source}'
              f'{", doi " + top.doi if top.doi else ""}) was not accepted: {why}')
    return None, min(score, 1.0), reason


def compare_fields(ref: ParsedReference, rec: SourceRecord) -> list[FieldCheck]:
    """Compare only the fields the user actually cited."""
    checks = []
    if ref.doi:
        checks.append(FieldCheck(field="doi", cited=ref.doi, retrieved=rec.doi,
                                 matches=bool(rec.doi) and ref.doi == rec.doi))
    if ref.title:
        checks.append(FieldCheck(field="title", cited=ref.title, retrieved=rec.title,
                                 matches=title_similarity(ref.title, rec.title) >= TITLE_MATCH))
    if ref.year:
        checks.append(FieldCheck(field="year", cited=str(ref.year),
                                 retrieved=str(rec.year) if rec.year else None,
                                 # online-first vs issue year commonly differ by one
                                 matches=rec.year is not None and abs(ref.year - rec.year) <= 1))
    if ref.first_author:
        retrieved = rec.authors[0] if rec.authors else None
        checks.append(FieldCheck(field="first_author", cited=ref.first_author, retrieved=retrieved,
                                 matches=bool(retrieved) and norm(ref.first_author) == norm(retrieved)))
    return checks


def format_entry(rec: SourceRecord) -> str:
    """Bibliography entry built strictly from retrieved metadata (no invented fields)."""
    authors = [a for a in rec.authors if a]
    if len(authors) > 3:
        author_text = f"{authors[0]} et al."
    elif len(authors) > 1:
        author_text = ", ".join(authors[:-1]) + f", & {authors[-1]}"
    else:
        author_text = authors[0] if authors else "[authors not listed]"
    parts = [f"{author_text} ({rec.year or 'n.d.'}).", f"{rec.title or '[title not listed]'}."]
    if rec.venue:
        parts.append(f"{rec.venue}.")
    if rec.doi:
        parts.append(f"https://doi.org/{rec.doi}")
    return " ".join(parts)
