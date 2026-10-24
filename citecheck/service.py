"""Chat-facing service: text in, markdown report out.

`remote_verify` is an injected coroutine that asks the Verifier agent; if it is
missing or fails, the same deterministic checks run locally and the report says so.
"""

import asyncio
from collections.abc import Awaitable, Callable

from .audit import run_audit
from .models import AuditReport, VerificationResult
from .parsing import MAX_REFERENCES, parse_references
from .report import render_report
from .verify import verify_report

RemoteVerify = Callable[[str, AuditReport], Awaitable[VerificationResult | None]]

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
                         preamble: str | None = None) -> str:
    refs = parse_references(text)
    if not refs:
        return ("I couldn't find any references in that message (I look for lines with a DOI, "
                "a year, or a numbered/bulleted list).\n\n" + HELP_TEXT)
    report = await run_audit(refs, llm)

    verification = None
    if remote_verify is not None:
        try:
            verification = await remote_verify(text, report)
        except Exception:  # noqa: BLE001 - fall back to local checks below
            verification = None
    if verification is None:
        verification = await asyncio.to_thread(
            verify_report, text, report, True,
            "Local deterministic verifier (Verifier agent unavailable)" if remote_verify else
            "Local deterministic verifier",
        )
    rendered = render_report(report, verification)
    return f"{preamble}\n\n{rendered}" if preamble else rendered
