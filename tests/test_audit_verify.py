import asyncio
import json

import requests

from citecheck import sources
from citecheck.assess import parse_llm_verdict
from citecheck.audit import audit_text
from citecheck.models import ClaimStatus, MetadataStatus
from citecheck.service import handle_request
from citecheck.verify import verify_report

from conftest import ALEXNET, FakeLLM

MIXED = """1. LeCun, Y., Bengio, Y., & Hinton, G. (2015). Deep learning. Nature. doi:10.1038/nature14539
2. He, K., Zhang, X., Ren, S., & Sun, J. (2014). Deep residual learning for image recognition. CVPR. doi:10.1109/CVPR.2016.90
3. Smith, J. (2021). Quantum blockchain methods for citation integrity. J. Imag. doi:10.9999/fake.2021.001
4. Krizhevsky, A., Sutskever, I., & Hinton, G. (2017). ImageNet classification with deep convolutional neural networks. CACM. doi:10.1038/nature14539
   Claim: The network achieved top-5 error of 17.0% on ImageNet test data.
5. Some paper about cells."""

GOOD_VERDICT = json.dumps({
    "verdict": "plausible",
    "quote": "On the test data, we achieved top-1 and top-5 error rates of 37.5% and 17.0%",
    "rationale": "The abstract reports this error rate.",
})


def audit(text=MIXED, llm=None):
    return asyncio.run(audit_text(text, llm))


def test_mixed_batch_statuses(fake_registries):
    report = audit(llm=FakeLLM(GOOD_VERDICT))
    status = [r.metadata_status for r in report.results]
    assert status == [MetadataStatus.VERIFIED, MetadataStatus.MISMATCH, MetadataStatus.DOI_NOT_FOUND,
                      MetadataStatus.MISMATCH, MetadataStatus.NOT_FOUND]
    year = next(f for f in report.results[1].field_checks if f.field == "year")
    assert (year.cited, year.retrieved, year.matches) == ("2014", "2016", False)
    wrong_doi = report.results[3]
    assert wrong_doi.matched.doi == "10.1145/3065386"           # intended work found by title
    assert "10.1145/3065386" in wrong_doi.corrected_entry
    assert wrong_doi.claim.status == ClaimStatus.PLAUSIBLE
    assert report.results[2].corrected_entry is None             # nothing invented
    assert report.summary.total == 5 and report.summary.unresolved == 2
    assert report.summary.by_metadata_status == {"verified": 1, "mismatch": 2, "doi_not_found": 1,
                                                 "not_found": 1}


def test_verifier_passes_clean_report_and_is_stable(fake_registries):
    report = audit(llm=FakeLLM(GOOD_VERDICT))
    first = verify_report(MIXED, report)
    second = verify_report(MIXED, report)
    assert first.passed and first.failed_checks == 0
    assert first.unresolved_references == [3, 5]
    assert first.model_dump() == second.model_dump()


def test_verifier_detects_tampering(fake_registries):
    report = audit(llm=FakeLLM(GOOD_VERDICT))

    def issues_after(mutate):
        bad = report.model_copy(deep=True)
        mutate(bad)
        v = verify_report(MIXED, bad)
        assert not v.passed
        return " ".join(v.issues)

    assert "input contains" in issues_after(lambda r: r.results.pop())
    assert "metadata counts" in issues_after(
        lambda r: r.summary.by_metadata_status.update(verified=4))
    assert "not derived" in issues_after(
        lambda r: setattr(r.results[1], "corrected_entry", "He (2016). Made-up title. CVPR."))
    assert "field checks do not match" in issues_after(
        lambda r: setattr(r.results[1].field_checks[-1], "retrieved", "1999"))
    assert "without an explanation" in issues_after(lambda r: r.results[2].notes.clear())
    assert "not bound" in issues_after(lambda r: setattr(r.results[0], "raw", "something else"))

    def fake_quote(r):
        r.results[3].claim.quote = "The network achieved 2% error."
    assert "quoted evidence" in issues_after(fake_quote)


def test_llm_quote_not_in_abstract_is_downgraded():
    out = parse_llm_verdict(json.dumps({"verdict": "plausible", "quote": "invented sentence", "rationale": "x"}),
                            ALEXNET.abstract, ALEXNET.url)
    assert out.status == ClaimStatus.UNCERTAIN and "downgraded" in out.rationale


def test_malformed_llm_output_is_uncertain():
    for text in ["", "sure, looks good!", '{"verdict": "definitely"}', "{broken json"]:
        assert parse_llm_verdict(text, ALEXNET.abstract, ALEXNET.url).status == ClaimStatus.UNCERTAIN


def test_contradicted_requires_quote():
    reply = json.dumps({"verdict": "contradicted", "quote": "", "rationale": "nope"})
    assert parse_llm_verdict(reply, ALEXNET.abstract, ALEXNET.url).status == ClaimStatus.UNCERTAIN


def test_llm_failure_marks_claim_not_assessed(fake_registries):
    report = audit(llm=FakeLLM(TimeoutError("asi1 down")))
    claim = report.results[3].claim
    assert claim.status == ClaimStatus.NOT_ASSESSED and "failed" in claim.rationale


def test_claim_without_abstract_not_assessed(fake_registries):
    text = "1. LeCun (2015). Deep learning. Nature. doi:10.1038/nature14539\n   Claim: Deep nets are great."
    llm = FakeLLM(GOOD_VERDICT)
    claim = audit(text, llm).results[0].claim
    assert claim.status == ClaimStatus.NOT_ASSESSED and "No abstract" in claim.rationale
    assert llm.calls == 0


def test_all_registries_down_reports_lookup_error(down_registries):
    report = audit()
    assert [r.metadata_status for r in report.results[:4]] == [MetadataStatus.LOOKUP_ERROR] * 4
    assert all(any("timed out" in n for n in r.notes) for r in report.results[:4])
    assert verify_report(MIXED, report).passed


def test_partial_failure_still_verifies(fake_registries, monkeypatch):
    monkeypatch.setattr(sources, "openalex_by_doi",
                        lambda doi, session=None: sources.Lookup("openalex", error="openalex lookup failed (HTTP 503)"))
    first = audit().results[0]
    assert first.metadata_status == MetadataStatus.VERIFIED
    assert first.corroborating_sources == ["crossref"] and "openalex lookup failed (HTTP 503)" in first.notes


def test_http_layer_timeout_and_rate_limit(monkeypatch):
    def timeout(*a, **k):
        raise requests.Timeout()
    monkeypatch.setattr(requests, "get", timeout)
    assert "timed out" in sources.crossref_by_doi("10.1/x").error

    class Resp:
        status_code, headers = 429, {"Retry-After": "0"}
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp())
    assert "HTTP 429" in sources.crossref_by_doi("10.1/x").error

    class NotFound:
        status_code, headers = 404, {}
    monkeypatch.setattr(requests, "get", lambda *a, **k: NotFound())
    assert sources.openalex_by_doi("10.1/x").not_found


def test_service_falls_back_when_verifier_agent_unreachable(fake_registries):
    async def unreachable(text, report):
        return None
    md = asyncio.run(handle_request(MIXED, None, unreachable))
    assert "Local deterministic verifier (Verifier agent unavailable)" in md


def test_service_help_on_no_references():
    md = asyncio.run(handle_request("hello!"))
    assert "couldn't find any references" in md


def test_wrong_doi_without_confirmable_work_offers_no_correction(fake_registries):
    text = "1. Doe, J. (2019). An unindexed study of something specific. J. X. doi:10.1038/nature14539"
    r = audit(text).results[0]
    assert r.metadata_status == MetadataStatus.MISMATCH
    assert r.corrected_entry is None
    assert any("could not be confirmed" in n for n in r.notes)
    assert r.claim.status == ClaimStatus.NO_CLAIM
    assert verify_report(text, audit(text)).passed


def test_doi_less_search_hit_is_not_accepted(fake_registries, monkeypatch):
    from conftest import ALEXNET
    stub = ALEXNET.model_copy(update={"doi": None, "source": "openalex", "year": 2012})
    monkeypatch.setattr(sources, "openalex_search",
                        lambda q, rows=3, session=None: sources.Lookup("openalex", records=[stub]))
    monkeypatch.setattr(sources, "crossref_search",
                        lambda q, rows=3, session=None: sources.Lookup("crossref", not_found=True))
    text = "1. Krizhevsky, A. (2012). ImageNet classification with deep convolutional neural networks. NeurIPS."
    assert audit(text).results[0].metadata_status == MetadataStatus.NOT_FOUND
