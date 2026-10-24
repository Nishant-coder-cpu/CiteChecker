"""CiteCheck Verifier agent: independently re-checks audit reports.

Runs as its own process and identity. It receives the user's original input
plus the audit report, re-parses the input itself, recomputes all counts,
validates corrections/evidence against the stored records and re-fetches every
matched DOI from Crossref/OpenAlex.
"""

import asyncio
import os

from dotenv import load_dotenv
from uagents import Agent, Context

from citecheck.messages import VERIFIER_PORT, VerifyRequest, VerifyResponse, verifier_seed
from citecheck.models import AuditReport
from citecheck.verify import verify_report

load_dotenv()

seed = os.getenv("AGENT_SEED")
if not seed:
    raise RuntimeError("Add AGENT_SEED to your .env file.")

agent = Agent(
    name="citecheck-verifier",
    seed=verifier_seed(seed),
    port=VERIFIER_PORT,
    endpoint=[f"http://127.0.0.1:{VERIFIER_PORT}/submit"],
)


@agent.on_message(VerifyRequest, replies=VerifyResponse)
async def handle_verify(ctx: Context, sender: str, msg: VerifyRequest):
    ctx.logger.info(f"Verification request {msg.audit_id} from {sender}")
    try:
        report = AuditReport.model_validate_json(msg.report_json)
        result = await asyncio.to_thread(
            verify_report, msg.original_text, report, msg.live_recheck,
            f"Verifier agent {agent.address[:20]}… (separate process)",
        )
        response = VerifyResponse(audit_id=msg.audit_id, result_json=result.model_dump_json())
    except Exception as exc:  # noqa: BLE001 - malformed reports become an explicit error
        ctx.logger.exception("Verification failed")
        response = VerifyResponse(audit_id=msg.audit_id, result_json="",
                                  error=f"Could not verify report: {type(exc).__name__}")
    await ctx.send(sender, response)
    ctx.logger.info(f"Verification {msg.audit_id} answered")


if __name__ == "__main__":
    agent.run()
