"""ReAct investigator: an ASI1 tool-calling agent for references the fast path
could not resolve.

The agent reasons about *how* to find the cited work (title variants, author +
year, preprint vs. published version, a mistyped DOI) and calls registry tools to
do it. Every record a tool returns is stored as evidence `E<n>`. To finish, the
agent must name one evidence id (or declare the work not found); it cannot
supply metadata itself. Its choice is then re-checked deterministically (DOI
present, title match, year within one) before it is used, and the Verifier
agent later checks that the matched DOI appears in the transcript.
"""

import asyncio
import json
import os

from . import sources, trace
from .compare import TITLE_MATCH, title_similarity
from .models import ParsedReference, SourceRecord

MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "5"))

SYSTEM_PROMPT = """You are CiteCheck's citation investigator. A reference could not be matched
automatically. Find the exact published work it refers to, using the tools.

Work in short steps: think about the most likely reason it failed (typo in DOI, title
abbreviated or reworded, preprint vs. conference/journal version, wrong year), then call
ONE tool. Every record a tool returns has an evidence id like E3.

Rules:
- Never invent a DOI, title, year or author. Only records returned by tools count.
- Finish by calling `conclude` with the evidence id of the record that IS the cited work,
  or evidence_id "none" if you cannot find it. Prefer the original version over re-uploads.
- You have at most {steps} tool calls. Put your reasoning in each tool's `thought` field."""

_THOUGHT = {"thought": {"type": "string", "description": "Your reasoning for this step."}}
TOOLS = [
    {"type": "function", "function": {
        "name": "crossref_search", "description": "Bibliographic search in Crossref (returns up to 5 records).",
        "parameters": {"type": "object", "required": ["query", "thought"],
                       "properties": {"query": {"type": "string"}, **_THOUGHT}}}},
    {"type": "function", "function": {
        "name": "openalex_search", "description": "Title search in OpenAlex (returns up to 5 records).",
        "parameters": {"type": "object", "required": ["query", "thought"],
                       "properties": {"query": {"type": "string"}, **_THOUGHT}}}},
    {"type": "function", "function": {
        "name": "lookup_doi", "description": "Resolve a DOI in Crossref, falling back to OpenAlex.",
        "parameters": {"type": "object", "required": ["doi", "thought"],
                       "properties": {"doi": {"type": "string"}, **_THOUGHT}}}},
    {"type": "function", "function": {
        "name": "conclude", "description": "Finish: name the evidence id of the cited work, or 'none'.",
        "parameters": {"type": "object", "required": ["evidence_id", "thought"],
                       "properties": {"evidence_id": {"type": "string"}, **_THOUGHT}}}},
]


class Investigation:
    def __init__(self, ref: ParsedReference, context: str):
        self.ref = ref
        self.context = context
        self.evidence: dict[str, SourceRecord] = {}
        self.transcript: list[str] = []

    def _store(self, records: list[SourceRecord]) -> list[dict]:
        out = []
        for rec in records[:5]:
            eid = f"E{len(self.evidence) + 1}"
            self.evidence[eid] = rec
            out.append({"id": eid, "source": rec.source, "doi": rec.doi, "title": rec.title,
                        "year": rec.year, "first_author": rec.authors[0] if rec.authors else None,
                        "venue": rec.venue})
        return out

    async def run_tool(self, name: str, args: dict) -> str:
        if name == "crossref_search":
            lookup = await asyncio.to_thread(sources.crossref_search, str(args.get("query", "")), 5)
        elif name == "openalex_search":
            lookup = await asyncio.to_thread(sources.openalex_search, str(args.get("query", "")), 5)
        elif name == "lookup_doi":
            doi = str(args.get("doi", "")).strip().lower().removeprefix("https://doi.org/")
            lookup = await asyncio.to_thread(sources.crossref_by_doi, doi)
            if not lookup.records:
                lookup = await asyncio.to_thread(sources.openalex_by_doi, doi)
        else:
            return json.dumps({"error": f"unknown tool {name}"})
        if lookup.error:
            return json.dumps({"error": lookup.error})
        records = self._store(lookup.records)
        return json.dumps({"records": records} if records else {"records": [], "note": "nothing found"})

    def accept(self, eid: str) -> tuple[SourceRecord | None, str]:
        """Deterministic gate on the agent's final choice."""
        rec = self.evidence.get(eid)
        if eid.lower() == "none" or rec is None:
            return None, "agent concluded the work could not be found"
        if not rec.doi:
            return None, f"agent chose {eid}, which has no DOI; not accepted"
        if title_similarity(self.ref.title or self.ref.raw, rec.title) < TITLE_MATCH:
            return None, f'agent chose {eid} ("{rec.title}") but its title does not match the citation; not accepted'
        if self.ref.year and rec.year and abs(self.ref.year - rec.year) > 1:
            return None, f"agent chose {eid} ({rec.year}) but the cited year is {self.ref.year}; not accepted"
        return rec, f"agent identified {eid} (doi {rec.doi})"


async def investigate(llm, ref: ParsedReference, context: str) -> tuple[SourceRecord | None, list[str], str]:
    """Run the ReAct loop. Returns (accepted record or None, transcript, outcome note)."""
    inv = Investigation(ref, context)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(steps=MAX_STEPS)},
        {"role": "user", "content": f"Cited reference:\n{ref.raw}\n\nParsed: title={ref.title!r}, "
                                    f"year={ref.year}, first_author={ref.first_author!r}, doi={ref.doi!r}\n\n"
                                    f"Automatic check result: {context}"},
    ]
    with trace.span("agent.investigate", reference=ref.index) as sp:
        for step in range(1, MAX_STEPS + 1):
            with trace.span("llm.agent_step", step=step) as lsp:
                try:
                    resp = await llm.chat.completions.create(
                        model="asi1", messages=messages, tools=TOOLS, temperature=0, max_tokens=400)
                except Exception as exc:  # noqa: BLE001
                    lsp.set("error", error=type(exc).__name__)
                    sp.set("error", steps=step)
                    inv.transcript.append(f"Step {step}: ASI1 unavailable ({type(exc).__name__}); investigation stopped.")
                    return None, inv.transcript, "investigator stopped: ASI1 unavailable"
            msg = resp.choices[0].message
            calls = list(getattr(msg, "tool_calls", None) or [])[:1]          # one action per step keeps the trace readable
            if not calls:
                # The model answered in prose: ask it to act (counts as a step).
                text = (msg.content or "").strip()
                inv.transcript.append(f"Step {step}: Thought: {text[:240] or '(empty)'} → no action; asked to call a tool.")
                messages.append({"role": "assistant", "content": text or "(no answer)"})
                messages.append({"role": "user", "content": (
                    "Respond only with a tool call. If one of the evidence records is the cited work, "
                    "call conclude with its evidence id; otherwise search again or conclude 'none'.")})
                continue
            call = calls[0]
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            thought = str(args.get("thought") or msg.content or "").strip()[:160]
            name = call.function.name
            if name == "conclude":
                eid = str(args.get("evidence_id", "none"))
                rec, note = inv.accept(eid)
                inv.transcript.append(f"Step {step}: Thought: {thought} → Conclude: {eid}. {note}.")
                sp.set("ok" if rec else "not_found", steps=step, evidence=len(inv.evidence))
                return rec, inv.transcript, note
            shown = {k: v for k, v in args.items() if k != "thought"}
            observation = await inv.run_tool(name, args)
            obs = json.loads(observation)
            summary = (obs.get("error") or ", ".join(
                f'{r["id"]} {r["doi"] or "no-doi"} "{(r["title"] or "")[:40]}" ({r["year"]})' for r in obs.get("records", [])
            ) or "nothing found")
            inv.transcript.append(f"Step {step}: Thought: {thought} → Action: {name}({json.dumps(shown)[:120]}) "
                                  f"→ Observation: {summary[:900]}")
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [{
                "id": call.id, "type": "function",
                "function": {"name": name, "arguments": call.function.arguments or "{}"}}]})
            messages.append({"role": "tool", "tool_call_id": call.id, "content": observation})
        sp.set("not_found", steps=len(inv.transcript), evidence=len(inv.evidence))
    return None, inv.transcript, f"investigator found no confirmable match within {MAX_STEPS} steps"
