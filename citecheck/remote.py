"""Client side of the CiteCheck -> Verifier agent exchange."""

import asyncio
import os

from uagents import Context
from uagents.communication import send_sync_message
from uagents.resolver import Resolver
from uagents_core.types import MsgStatus

from .messages import VerifyRequest, VerifyResponse
from .models import AuditReport, VerificationResult

VERIFIER_TIMEOUT = int(os.getenv("VERIFIER_TIMEOUT", "45"))
# The agent server tracks pending sync requests per *sender address*, so two
# concurrent requests from this agent would overwrite each other's reply slot.
_ONE_AT_A_TIME = asyncio.Lock()


def make_remote_verify(ctx: Context, verifier_address: str, resolver: Resolver | None = None):
    async def remote_verify(text: str, report: AuditReport) -> VerificationResult | None:
        request = VerifyRequest(
            audit_id=report.audit_id,
            original_text=text,
            report_json=report.model_dump_json(),
        )
        # A direct synchronous HTTP exchange, signed with this agent's identity.
        # Deliberately NOT ctx.send_and_receive: the SDK sends all outgoing
        # messages through one sequential queue, and a slow verification there
        # would delay replies to ASI:One past their send timeout.
        for attempt in range(2):
            try:
                async with _ONE_AT_A_TIME:
                    reply = await asyncio.wait_for(
                        send_sync_message(
                            verifier_address,
                            request,
                            response_type=VerifyResponse,
                            sender=ctx.agent.identity,
                            resolver=resolver,
                            timeout=VERIFIER_TIMEOUT,
                        ),
                        VERIFIER_TIMEOUT,
                    )
            except Exception as exc:  # noqa: BLE001 - timeout or connection error
                reply = None
                detail = f"{type(exc).__name__}"
            else:
                detail = reply.detail if isinstance(reply, MsgStatus) else "unexpected reply"
            if isinstance(reply, VerifyResponse):
                break
            ctx.logger.warning(f"Verifier attempt {attempt + 1} failed ({detail})")

        if not isinstance(reply, VerifyResponse) or reply.error or reply.audit_id != report.audit_id:
            error = reply.error if isinstance(reply, VerifyResponse) else None
            ctx.logger.warning(
                f"Verifier agent unavailable{' (' + error + ')' if error else ''}; using local fallback"
            )
            return None
        ctx.logger.info("Verifier agent answered (direct sync exchange)")
        return VerificationResult.model_validate_json(reply.result_json)

    return remote_verify
