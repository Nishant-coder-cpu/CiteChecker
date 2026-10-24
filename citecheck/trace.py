"""Per-request tracing: timed spans with outcomes, written as JSON lines.

A Trace is bound to the current context (contextvars), so spans opened inside
asyncio tasks and asyncio.to_thread workers attach to the request that started
them. Each finished trace is appended to logs/traces.jsonl.
"""

import contextvars
import json
import os
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

TRACE_FILE = Path(os.getenv("CITECHECK_TRACE_FILE", Path(__file__).resolve().parents[1] / "logs" / "traces.jsonl"))
_current: contextvars.ContextVar["Trace | None"] = contextvars.ContextVar("citecheck_trace", default=None)
_write_lock = Lock()


class Span:
    def __init__(self, name: str, attrs: dict):
        self.name, self.attrs = name, dict(attrs)
        self.status = "ok"
        self.start = time.perf_counter()
        self.duration_ms = 0.0

    def set(self, status: str | None = None, **attrs) -> None:
        if status:
            self.status = status
        self.attrs.update(attrs)


class Trace:
    def __init__(self, kind: str, **attrs):
        self.id = uuid4().hex[:12]
        self.kind = kind
        self.attrs = dict(attrs)
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._t0 = time.perf_counter()
        self.spans: list[Span] = []
        self._lock = Lock()
        self._token = None

    # -- lifecycle ---------------------------------------------------------
    def activate(self) -> "Trace":
        self._token = _current.set(self)
        return self

    def finish(self, status: str = "ok", path: Path | None = None) -> dict:
        record = self.to_dict(status)
        target = path or TRACE_FILE
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with _write_lock, target.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            pass  # tracing must never break a reply
        if self._token is not None:
            _current.reset(self._token)
            self._token = None
        return record

    # -- data --------------------------------------------------------------
    def add(self, span: Span) -> None:
        with self._lock:
            self.spans.append(span)

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def to_dict(self, status: str = "ok") -> dict:
        return {
            "trace_id": self.id, "kind": self.kind, "started_at": self.started_at,
            "status": status, "duration_ms": round(self.elapsed_ms(), 1), **self.attrs,
            "spans": [
                {"name": s.name, "status": s.status, "duration_ms": round(s.duration_ms, 1), **s.attrs}
                for s in self.spans
            ],
        }

    def summary_line(self) -> str:
        """One-line, human-readable run summary for the report footer."""
        groups: dict[str, Counter] = {}
        for s in self.spans:
            if s.name.startswith("lookup."):
                key = s.attrs.get("source", s.name)
            elif s.name in ("llm.claim", "verify.remote", "verify.local"):
                key = s.name
            else:
                continue
            groups.setdefault(key, Counter())[s.status] += 1
        labels = {"crossref": "Crossref", "openalex": "OpenAlex", "llm.claim": "ASI1",
                  "verify.remote": "Verifier agent", "verify.local": "Local verifier"}
        parts = []
        for key in ("crossref", "openalex", "llm.claim", "verify.remote", "verify.local"):
            if key in groups:
                c = groups[key]
                bad = sum(n for st, n in c.items() if st != "ok" and st != "not_found")
                parts.append(f"{labels[key]} {sum(c.values())}" + (f" ({bad} failed)" if bad else ""))
        agent = [s for s in self.spans if s.name == "agent.investigate"]
        if agent:
            steps = sum(1 for s in self.spans if s.name == "llm.agent_step")
            found = sum(1 for s in agent if s.status == "ok")
            parts.append(f"ReAct agent {len(agent)} investigation(s), {steps} step(s), {found} resolved")
        parts.append(f"total {self.elapsed_ms() / 1000:.1f} s")
        return " · ".join(parts)


def current() -> Trace | None:
    return _current.get()


@contextmanager
def span(name: str, **attrs):
    """Time a block. No-op (but still yields a Span) when no trace is active."""
    s = Span(name, attrs)
    try:
        yield s
    except Exception as exc:
        s.set("error", error=type(exc).__name__)
        raise
    finally:
        s.duration_ms = (time.perf_counter() - s.start) * 1000
        trace = _current.get()
        if trace is not None:
            trace.add(s)
