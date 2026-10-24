"""Crossref and OpenAlex clients.

Every call returns a Lookup describing exactly what happened (found / not found /
error) so the audit can report partial failures instead of guessing.
"""

import html
import os
import re
import time
from dataclasses import dataclass, field

import requests

from .models import SourceRecord

CROSSREF = "https://api.crossref.org"
OPENALEX = "https://api.openalex.org"
TIMEOUT = float(os.getenv("CITECHECK_HTTP_TIMEOUT", "10"))
# 504 is not retried: the upstream already waited for its own timeout.
RETRY_STATUSES = {429, 500, 502, 503}
# OpenAlex is the secondary source and often slow; cap it so it cannot dominate latency.
OPENALEX_TIMEOUT = float(os.getenv("OPENALEX_HTTP_TIMEOUT", "6"))


@dataclass
class Lookup:
    source: str
    records: list[SourceRecord] = field(default_factory=list)
    not_found: bool = False
    error: str | None = None


def _contact() -> dict:
    mailto = os.getenv("CROSSREF_MAILTO")
    return {"mailto": mailto} if mailto else {}


def _get(url: str, params: dict, session: requests.Session | None = None) -> requests.Response:
    """GET with one retry on rate-limit / server errors. Raises on network failure."""
    http = session or requests
    headers = {"User-Agent": "CiteCheck/0.1 (Fetch-A-Thon citation audit agent)"}
    timeout = OPENALEX_TIMEOUT if url.startswith(OPENALEX) else TIMEOUT
    for attempt in range(2):
        resp = http.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code not in RETRY_STATUSES or attempt == 1:
            return resp
        wait = min(float(resp.headers.get("Retry-After", "1") or 1), 3.0)
        time.sleep(wait)
    return resp


def _strip_jats(text: str | None) -> str | None:
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return re.sub(r"^abstract\s*", "", text, flags=re.IGNORECASE) or None


def _crossref_record(item: dict) -> SourceRecord:
    issued = (item.get("issued") or item.get("published") or {}).get("date-parts") or [[None]]
    doi = (item.get("DOI") or "").lower() or None
    return SourceRecord(
        source="crossref",
        url=f"https://doi.org/{doi}" if doi else item.get("URL", CROSSREF),
        doi=doi,
        title=(item.get("title") or [None])[0],
        authors=[a.get("family") or a.get("name", "") for a in item.get("author", [])],
        year=issued[0][0] if issued and issued[0] else None,
        venue=(item.get("container-title") or [None])[0],
        abstract=_strip_jats(item.get("abstract")),
    )


def _openalex_abstract(index: dict | None) -> str | None:
    if not index:
        return None
    positions = [(pos, word) for word, poss in index.items() for pos in poss]
    return " ".join(word for _, word in sorted(positions)) or None


def _openalex_record(item: dict) -> SourceRecord:
    doi = (item.get("doi") or "").replace("https://doi.org/", "").lower() or None
    source = ((item.get("primary_location") or {}).get("source") or {})
    return SourceRecord(
        source="openalex",
        url=item.get("id") or OPENALEX,
        doi=doi,
        title=item.get("title"),
        authors=[
            (a.get("author") or {}).get("display_name", "").split()[-1]
            for a in item.get("authorships", [])
            if (a.get("author") or {}).get("display_name")
        ],
        year=item.get("publication_year"),
        venue=source.get("display_name"),
        abstract=_openalex_abstract(item.get("abstract_inverted_index")),
    )


def _safe(source: str, fn) -> Lookup:
    try:
        return fn()
    except requests.Timeout:
        return Lookup(source, error=f"{source} timed out after {TIMEOUT:.0f}s")
    except requests.RequestException as exc:
        return Lookup(source, error=f"{source} request failed: {type(exc).__name__}")
    except (ValueError, KeyError, TypeError) as exc:
        return Lookup(source, error=f"{source} returned an unexpected response: {type(exc).__name__}")


def _from_response(source: str, resp: requests.Response, parse) -> Lookup:
    if resp.status_code == 404:
        return Lookup(source, not_found=True)
    if resp.status_code != 200:
        return Lookup(source, error=f"{source} lookup failed (HTTP {resp.status_code}); used other sources")
    return Lookup(source, records=parse(resp.json()))


def crossref_by_doi(doi: str, session=None) -> Lookup:
    return _safe("crossref", lambda: _from_response(
        "crossref",
        _get(f"{CROSSREF}/works/{doi}", _contact(), session),
        lambda data: [_crossref_record(data["message"])],
    ))


def crossref_search(query: str, rows: int = 3, session=None) -> Lookup:
    params = {"query.bibliographic": query[:300], "rows": rows, **_contact()}

    def parse(data):
        items = data["message"]["items"]
        return [_crossref_record(i) for i in items]

    def run():
        lookup = _from_response("crossref", _get(f"{CROSSREF}/works", params, session), parse)
        lookup.not_found = lookup.error is None and not lookup.records
        return lookup

    return _safe("crossref", run)


def openalex_by_doi(doi: str, session=None) -> Lookup:
    return _safe("openalex", lambda: _from_response(
        "openalex",
        _get(f"{OPENALEX}/works/doi:{doi}", {}, session),
        lambda data: [_openalex_record(data)],
    ))


def openalex_search(title: str, rows: int = 3, session=None) -> Lookup:
    params = {"search": title[:200], "per-page": rows}

    def run():
        lookup = _from_response(
            "openalex", _get(f"{OPENALEX}/works", params, session),
            lambda data: [_openalex_record(i) for i in data["results"]],
        )
        lookup.not_found = lookup.error is None and not lookup.records
        return lookup

    return _safe("openalex", run)
