"""Chat-facing service: text in, markdown report out.

`remote_verify` is an injected coroutine that asks the Verifier agent; if it is
missing or fails, the same deterministic checks run locally and the report says so.
If verification fails, the flagged references are re-audited once (reflection).
"""

import asyncio
import os
import re
from collections.abc import Awaitable, Callable

from . import trace
from .audit import reaudit, run_audit
from .models import AuditReport, VerificationResult
from .parsing import MAX_REFERENCES, parse_references
from .report import render_report
from .verify import verify_report

REFLECTION = os.getenv("AGENTIC_MODE", "on") == "on"
RemoteVerify = Callable[[str, AuditReport], Awaitable[VerificationResult | None]]
Progress = Callable[[str], Awaitable[None]]


async def _say(progress: Progress | None, text: str) -> None:
    if progress is None:
        return
    try:
        await progress(text)
    except Exception:  # noqa: BLE001 - progress updates must never break an audit
        pass


def _limited(progress: Progress | None, limit: int) -> Progress | None:
    """At most `limit` messages (keeps the chat readable and the endpoint unthrottled)."""
    if progress is None:
        return None
    sent = 0

    async def wrapper(text: str) -> None:
        nonlocal sent
        if sent < limit:
            sent += 1
            await _say(progress, text)
    return wrapper

HELP_TEXT = f"""**CiteCheck** verifies academic references against Crossref and OpenAlex.

Paste up to {MAX_REFERENCES} references, one per line (numbered or bulleted is fine). DOIs or doi.org links help.
Optionally add a `Claim:` line under a reference to check whether its abstract supports what you cite it for.

Example:
```
1. LeCun, Y., Bengio, Y., & Hinton, G. (2015). Deep learning. Nature. doi:10.1038/nature14539
2. Krizhevsky, A., Sutskever, I., & Hinton, G. (2017). ImageNet classification with deep convolutional neural networks. Communications of the ACM. doi:10.1145/3065386
   Claim: A deep convolutional network achieved record ImageNet error rates.
```
I'll report verified, mismatched and unresolved references, suggested corrections built only from registry metadata, and an independent quality check of the report."""


async def handle_request(text: str, llm=None, remote_verify: RemoteVerify | None = None,
                         preamble: str | None = None, progress: Progress | None = None) -> str:
    refs = parse_references(text)
    if not refs:
        return ("I couldn't find any references in that message (I look for lines with a DOI, "
                "a year, or a numbered/bulleted list).\n\n" + HELP_TEXT)
    claims = sum(1 for r in refs if r.claim)
    await _say(progress, f"🔎 Found **{len(refs)} reference(s)**"
                         + (f" and {claims} claim(s)" if claims else "")
                         + ". Checking each against Crossref and OpenAlex"
                         + (", and the claims against the papers' abstracts with ASI1" if claims else "") + "…")
    report = await run_audit(refs, llm, _limited(progress, 3))
    await _say(progress, "🛡️ Audit done. Sending the report to the independent **Verifier agent** "
                         "to recount and re-fetch every source…")
    verification = await _verify(text, report, remote_verify)

    # Reflection: feed the verifier's findings back once and re-audit what it flagged.
    if not verification.passed and REFLECTION:
        flagged = {int(i) for issue in verification.issues for i in re.findall(r"#(\d+)", issue)}
        flagged = flagged or {r.index for r in report.results}
        first = verification
        with trace.span("reflection", references=len(flagged)) as sp:
            report = await reaudit(report, flagged, llm)
            verification = await _verify(text, report, remote_verify)
            sp.set("ok" if verification.passed else "failed")
        report.reflection.append(
            f"Round 1: {first.verifier} flagged {first.failed_checks} issue(s); re-audited "
            f"reference(s) {', '.join('#' + str(i) for i in sorted(flagged))}. "
            f"Round 2: {'passed' if verification.passed else 'still failing'}."
        )

    rendered = render_report(report, verification)
    tr = trace.current()
    if tr is not None:
        tr.attrs.update(references=report.summary.total, audit_id=report.audit_id,
                        verification_passed=verification.passed, verifier=verification.verifier,
                        reflection_rounds=len(report.reflection))
        rendered += f"\n\n<sub>Run details: {tr.summary_line()} · trace `{tr.id}`</sub>"
    return f"{preamble}\n\n{rendered}" if preamble else rendered


async def _verify(text: str, report: AuditReport, remote_verify: RemoteVerify | None) -> VerificationResult:
    verification = None
    if remote_verify is not None:
        with trace.span("verify.remote") as sp:
            try:
                verification = await remote_verify(text, report)
            except Exception:  # noqa: BLE001 - fall back to local checks below
                verification = None
            sp.set("ok" if verification else "fallback")
    if verification is None:
        with trace.span("verify.local") as sp:
            verification = await asyncio.to_thread(
                verify_report, text, report, True,
                "Local deterministic verifier (Verifier agent unavailable)" if remote_verify else
                "Local deterministic verifier",
            )
            sp.set(passed=verification.passed)
    return verification
