import asyncio
import json

from citecheck import sources
from citecheck.audit import audit_text
from citecheck.models import MetadataStatus, VerificationResult
from citecheck.service import handle_request
from citecheck.verify import verify_report

from conftest import ALEXNET

# Fast path fails for this one: no DOI, title misspelt ("convolutionl").
TEXT = "1. Krizhevsky, A. (2017). ImageNet classification with deep convolutionl neural networks. CACM."


class ScriptedAgentLLM:
    """Plays a ReAct episode: one search, then conclude with a given evidence id."""

    def __init__(self, conclude_with="E1"):
        self.conclude_with, self.turn = conclude_with, 0
        self.chat = self.completions = self

    async def create(self, **kw):
        self.turn += 1
        if "tools" not in kw:                      # claim-assessment call (not used here)
            return _resp(content="{}")
        if self.turn == 1:
            return _resp(tool=("crossref_search", {"query": "ImageNet classification deep convolutional",
                                                   "thought": "typo in title; search loosely"}))
        return _resp(tool=("conclude", {"evidence_id": self.conclude_with, "thought": "E1 is the work"}))


def _resp(content=None, tool=None):
    class Fn: pass
    class Call: pass
    class Msg: pass
    class Choice: pass
    class R: pass
    m = Msg(); m.content = content; m.tool_calls = None
    if tool:
        c = Call(); c.id = "call_1"; c.function = Fn()
        c.function.name, c.function.arguments = tool[0], json.dumps(tool[1])
        m.tool_calls = [c]
    ch = Choice(); ch.message = m; r = R(); r.choices = [ch]
    return r


def _patch_search(monkeypatch, record):
    monkeypatch.setattr(sources, "crossref_search", lambda q, rows=3, session=None:
                        sources.Lookup("crossref", records=[record]) if "deep convolutional" in q
                        else sources.Lookup("crossref", not_found=True))
    monkeypatch.setattr(sources, "openalex_search", lambda q, rows=3, session=None:
                        sources.Lookup("openalex", not_found=True))


def test_agent_identifies_work_and_verifier_checks_provenance(monkeypatch):
    _patch_search(monkeypatch, ALEXNET)
    report = asyncio.run(audit_text(TEXT, ScriptedAgentLLM("E1")))
    r = report.results[0]
    assert r.metadata_status == MetadataStatus.VERIFIED          # one-letter typo is within tolerance
    assert r.matched.doi == "10.1145/3065386"
    assert any("Action: crossref_search" in s for s in r.agent_trace)
    assert verify_report(TEXT, report).passed

    tampered = report.model_copy(deep=True)
    tampered.results[0].agent_trace = ["Step 1: Thought: trust me → Conclude: E1."]
    assert "does not appear in its tool observations" in " ".join(verify_report(TEXT, tampered).issues)


def test_agent_cannot_use_unknown_evidence(monkeypatch):
    _patch_search(monkeypatch, ALEXNET)
    r = asyncio.run(audit_text(TEXT, ScriptedAgentLLM("E99"))).results[0]
    assert r.metadata_status == MetadataStatus.NOT_FOUND and r.matched is None


def test_agent_pick_with_wrong_year_is_rejected(monkeypatch):
    _patch_search(monkeypatch, ALEXNET.model_copy(update={"year": 2025}))
    r = asyncio.run(audit_text(TEXT, ScriptedAgentLLM("E1"))).results[0]
    assert r.matched is None and "not accepted" in r.notes[-1]


def test_reflection_reaudits_flagged_references(fake_registries):
    calls = []

    async def flaky_verifier(text, report):
        calls.append(1)
        passed = len(calls) > 1
        return VerificationResult(passed=passed, total_checks=10, failed_checks=0 if passed else 1,
                                  unresolved_references=[], issues=[] if passed else ["#1: something off"],
                                  summary="x", verifier="Verifier agent test")

    md = asyncio.run(handle_request(
        "1. LeCun, Y. (2015). Deep learning. Nature. doi:10.1038/nature14539", None, flaky_verifier))
    assert len(calls) == 2
    assert "Round 1: Verifier agent test flagged 1 issue(s); re-audited reference(s) #1. Round 2: passed." in md
