"""Deterministic parsing of pasted references into ParsedReference records.

Accepted input (one reference per line or numbered/bulleted block):

    1. LeCun, Y., Bengio, Y., & Hinton, G. (2015). Deep learning. Nature. doi:10.1038/nature14539
       Claim: Deep learning discovers structure in large data sets.
    [2] https://doi.org/10.1145/3065386
    - Smith, J. (2020). Some title. Journal. | claim: X causes Y

A "Claim:" line (or "| claim:" suffix) attaches to the reference before it.
"""

import re

from .models import ParsedReference

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
YEAR_RE = re.compile(r"\((\d{4})[a-z]?\)|\b((?:19|20)\d{2})\b")
MARKER_RE = re.compile(r"^\s*(?:\[\d+\]|\d+[.)]|[-*•])\s+")
CLAIM_LINE_RE = re.compile(r"^\s*claim\s*[:\-]\s*(.+)$", re.IGNORECASE)
INLINE_CLAIM_RE = re.compile(r"\s*(?:(?:\||—|--)\s*[Cc]laim|\sClaim)\s*[:\-]\s*(.+)$")
# Where a new reference starts inside run-together text: ". Surname, I." (chat apps may drop newlines)
RUN_TOGETHER_RE = re.compile(r"(?<=[.:\d])\s+(?=[A-Z][A-Za-zÀ-ſ'\-]+, (?:[A-Z]\.\s?)+[,(&]?)")
PAREN_YEAR_RE = re.compile(r"\((?:19|20)\d{2}[a-z]?\)")
QUOTED_TITLE_RE = re.compile(r"[\"“](.+?)[\"”]")

MAX_REFERENCES = 15


def normalize_doi(value: str) -> str | None:
    """Return a bare, lower-cased DOI from a DOI, doi: string or doi.org URL."""
    match = DOI_RE.search(value or "")
    if not match:
        return None
    doi = match.group(1).rstrip(".,;:)]}'\"")
    # Balance a trailing ')' that belongs to the DOI, e.g. 10.1016/S0140-6736(20)30183-5
    if doi.count("(") > doi.count(")") and match.group(1)[len(doi):].startswith(")"):
        doi += ")"
    return doi.lower()


def _extract_year(text: str) -> int | None:
    for match in YEAR_RE.finditer(text):
        value = int(match.group(1) or match.group(2))
        if 1800 <= value <= 2100:
            return value
    return None


def _extract_title(text: str) -> str | None:
    quoted = QUOTED_TITLE_RE.search(text)
    if quoted and len(quoted.group(1).split()) >= 2:
        return quoted.group(1).strip().rstrip(".,")
    # APA-style: "Authors (2015). Title. Venue."
    after_year = re.search(r"\(\d{4}[a-z]?\)\.?\s*(.+)", text)
    if after_year:
        title = re.split(r"(?<=[a-z0-9\)])[.?!]\s", after_year.group(1), maxsplit=1)[0]
        title = DOI_RE.sub("", title).strip().rstrip(".,")
        if len(title.split()) >= 2:
            return title
    # Fallback: longest period-delimited segment that is not author initials / DOI.
    cleaned = re.sub(r"https?://\S+", "", DOI_RE.sub("", text))
    segments = [s.strip() for s in re.split(r"\.\s+", cleaned) if s.strip()]
    candidates = [
        s for s in segments
        if len(s.split()) >= 3 and not re.fullmatch(r"[\w\s,.&'-]*\b[A-Z]\.?(,|$).*", s)
    ]
    return max(candidates, key=len).rstrip(".,") if candidates else None


def _extract_first_author(text: str) -> str | None:
    head = text.split("(")[0] if "(" in text else text.split(".")[0]
    match = re.match(r"\s*([A-Z][A-Za-zÀ-ſ'\-]+)(?:,|\s+et al|\s+and\s|\s*&)", head)
    return match.group(1) if match else None


def _split_blocks(text: str) -> list[tuple[str, str | None]]:
    """Group lines into (reference_text, claim) pairs."""
    blocks: list[list] = []  # each: [ref_text, claim]
    lines = []
    for line in text.splitlines():
        # A single line holding several "(Year)" citations was run together: split it.
        if len(PAREN_YEAR_RE.findall(line)) >= 2:
            lines.extend(RUN_TOGETHER_RE.split(line))
        else:
            lines.append(line)
    has_markers = any(MARKER_RE.match(line) for line in lines)
    for line in lines:
        if not line.strip():
            continue
        claim_match = CLAIM_LINE_RE.match(line)
        if claim_match:
            if blocks:
                blocks[-1][1] = claim_match.group(1).strip()
            continue
        if MARKER_RE.match(line):
            blocks.append([MARKER_RE.sub("", line).strip(), None])
        elif has_markers and blocks and line.startswith((" ", "\t")):
            blocks[-1][0] += " " + line.strip()          # wrapped continuation line
        elif not has_markers and (DOI_RE.search(line) or _extract_year(line)):
            blocks.append([line.strip(), None])
        # other unmarked lines (e.g. "Please verify these:") are treated as preamble
    pairs = []
    for ref, claim in blocks:
        inline = INLINE_CLAIM_RE.search(ref)
        if inline:
            claim = claim or inline.group(1).strip()
            ref = ref[: inline.start()].strip()
        pairs.append((ref, claim))
    return pairs


def parse_references(text: str) -> list[ParsedReference]:
    refs = []
    for i, (raw, claim) in enumerate(_split_blocks(text)[:MAX_REFERENCES], start=1):
        refs.append(
            ParsedReference(
                index=i,
                raw=raw,
                doi=normalize_doi(raw),
                year=_extract_year(DOI_RE.sub("", raw)),
                title=_extract_title(raw),
                first_author=_extract_first_author(raw),
                claim=claim,
            )
        )
    return refs


def count_references(text: str) -> int:
    """Return the number of reference blocks before applying the service cap."""
    return len(_split_blocks(text))
