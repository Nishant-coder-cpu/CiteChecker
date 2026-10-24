import asyncio
import json

from citecheck import trace
from citecheck.service import handle_request

from test_audit_verify import GOOD_VERDICT, MIXED
from conftest import FakeLLM


def test_trace_collects_spans_across_threads_and_writes_jsonl(fake_registries, tmp_path):
    async def run():
        tr = trace.Trace("test").activate()
        md = await handle_request(MIXED, FakeLLM(GOOD_VERDICT))
        return md, tr.finish(path=tmp_path / "t.jsonl")

    md, record = asyncio.run(run())
    names = [s["name"] for s in record["spans"]]
    sources = {s.get("source") for s in record["spans"] if s["name"].startswith("lookup.")}
    assert sources == {"crossref", "openalex"}                      # recorded from worker threads
    assert "llm.claim" in names and "verify.local" in names
    assert record["references"] == 5 and record["verification_passed"] is True
    assert f"trace `{record['trace_id']}`" in md and "Run details:" in md
    saved = json.loads((tmp_path / "t.jsonl").read_text())
    assert saved["trace_id"] == record["trace_id"]


def test_failed_lookups_are_counted(down_registries, tmp_path):
    async def run():
        tr = trace.Trace("test").activate()
        await handle_request(MIXED)
        return tr

    tr = asyncio.run(run())
    assert "failed" in tr.summary_line()
    assert all(s.status == "error" for s in tr.spans if s.name.startswith("lookup."))


def test_span_without_trace_is_noop():
    with trace.span("x") as s:
        s.set("ok")
    assert trace.current() is None
