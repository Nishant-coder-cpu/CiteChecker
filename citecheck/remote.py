"""Client side of the CiteCheck -> Verifier agent exchange."""

import os

from uagents import Context

from .messages import VerifyRequest, VerifyResponse
from .models import AuditReport, VerificationResult

VERIFIER_TIMEOUT = int(os.getenv("VERIFIER_TIMEOUT", "45"))


def make_remote_verify(ctx: Context, verifier_address: str):
    async def remote_verify(text: str, report: AuditReport) -> VerificationResult | None:
        request = VerifyRequest(
            audit_id=report.audit_id,
            original_text=text,
            report_json=report.model_dump_json(),
        )
        # sync=True: the Verifier answers within the same HTTP exchange, so the
        # reply does not depend on the caller's mailbox being reachable.
        # One retry covers transient Almanac API resolution timeouts.
        for attempt in range(2):
            reply, status = await ctx.send_and_receive(
                verifier_address,
                request,
                response_type=VerifyResponse,
                sync=True,
                timeout=VERIFIER_TIMEOUT,
            )
            if isinstance(reply, VerifyResponse) or "Timeout waiting" in (status.detail or ""):
                break
            ctx.logger.warning(f"Verifier attempt {attempt + 1} failed ({status.detail})")
        if not isinstance(reply, VerifyResponse) or reply.error or reply.audit_id != report.audit_id:
            ctx.logger.warning(
                f"Verifier agent unavailable ({status.detail}"
                f"{'; ' + reply.error if isinstance(reply, VerifyResponse) and reply.error else ''}); "
                "using local fallback"
            )
            return None
        ctx.logger.info(f"Verifier agent answered via {status.endpoint}")
        return VerificationResult.model_validate_json(reply.result_json)

    return remote_verify
