import asyncio
import os
import time
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
from openai import AsyncOpenAI
from uagents import Agent, Context, Protocol
from uagents_core.types import DeliveryStatus
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

from citecheck import payment
from citecheck.messages import VERIFIER_PORT, verifier_address
from citecheck.parsing import parse_references
from citecheck.remote import make_remote_verify
from citecheck.resolver import ResilientResolver
from citecheck.service import HELP_TEXT, handle_request

# Load configuration from .env
load_dotenv()

api_key = os.getenv("ASI_ONE_API_KEY")
seed = os.getenv("AGENT_SEED")

if not api_key or not seed:
    raise RuntimeError(
        "Add ASI_ONE_API_KEY and AGENT_SEED to your .env file."
    )

# Asynchronous ASI1 client (used only for claim-support judgments)
llm = AsyncOpenAI(
    base_url="https://api.asi1.ai/v1",
    api_key=api_key,
    # api.asi1.ai is reachable only intermittently from some networks: fail fast
    # (the claim is then reported as "not assessed") rather than stall the reply.
    timeout=float(os.getenv("ASI1_TIMEOUT", "15")),
    max_retries=1,
)


VERIFIER_ADDRESS = verifier_address(seed)
# Retries transient Almanac API stalls (replies to ASI:One depend on it) and
# reaches the co-located Verifier agent at its known local endpoint.
RESOLVER = ResilientResolver(
    static={VERIFIER_ADDRESS: f"http://127.0.0.1:{VERIFIER_PORT}/submit"}
)

# Fetch.ai agent configuration
agent = Agent(
    name="citecheck",
    seed=seed,
    port=int(os.getenv("AGENT_PORT", "8001")),
    # AGENT_ENDPOINT is only for local testing; the deployed agent uses its Agentverse mailbox.
    **(
        {"endpoint": [os.environ["AGENT_ENDPOINT"]]}
        if os.getenv("AGENT_ENDPOINT")
        else {"mailbox": True}
    ),
    publish_agent_details=not os.getenv("AGENT_ENDPOINT"),
    readme_path=str(Path(__file__).with_name("AGENT_README.md")),
    description=(
        "Verifies academic references against Crossref and OpenAlex, flags wrong DOIs, "
        "years and authors, checks claims against abstracts, and independently verifies its report."
    ),
    handle_messages_concurrently=True,
    resolve=RESOLVER,
)


chat = Protocol(spec=chat_protocol_spec)
IN_PROGRESS: set[tuple[str, int]] = set()


@chat.on_message(ChatMessage)
async def handle_chat(
    ctx: Context,
    sender: str,
    msg: ChatMessage,
):
    # Acknowledge the incoming message
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )

    # Extract text from the incoming chat message
    user_text = "\n".join(
        item.text
        for item in msg.content
        if isinstance(item, TextContent)
    ).strip()

    ctx.logger.info(f"Received message from {sender}")

    if not user_text:
        if any(isinstance(item, StartSessionContent) for item in msg.content):
            return
        await send_text(ctx, sender, HELP_TEXT)
        return

    request_key = (sender, hash(user_text))
    if request_key in IN_PROGRESS:
        ctx.logger.info(f"Duplicate request from {sender} while the audit is running; skipped")
        return

    n_refs = len(parse_references(user_text))
    if payment.requires_payment(n_refs):
        await request_payment(ctx, sender, user_text, n_refs)
        return

    IN_PROGRESS.add(request_key)
    try:
        await run_and_reply(ctx, sender, user_text)
    finally:
        IN_PROGRESS.discard(request_key)


async def send_text(ctx: Context, recipient: str, text: str, end_session: bool = True):
    content = [TextContent(type="text", text=text)]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    message = ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)
    for attempt in range(2):
        status = await ctx.send(recipient, message)
        if status.status != DeliveryStatus.FAILED:
            ctx.logger.info(f"Reply to {recipient}: {status.status.value} ({status.detail})")
            return status
        if "timeout" in (status.detail or "").lower() or "429" in (status.detail or ""):
            # After a send timeout the envelope may still go out (a retry could
            # deliver it twice); after a 429, retrying only adds load.
            ctx.logger.warning(f"Reply to {recipient}: status unknown ({status.detail})")
            return status
        ctx.logger.warning(f"Reply attempt {attempt + 1} to {recipient} not sent: {status.detail}")
        await asyncio.sleep(2)
    ctx.logger.error(f"Reply to {recipient} could not be delivered")
    return status


async def run_and_reply(ctx: Context, sender: str, user_text: str, preamble: str | None = None):
    start = time.perf_counter()
    try:
        answer = await handle_request(
            user_text, llm, make_remote_verify(ctx, VERIFIER_ADDRESS, RESOLVER), preamble
        )
    except Exception:
        ctx.logger.exception("Audit failed")
        answer = (preamble + "\n\n" if preamble else "") + (
            "Something went wrong while auditing these references, so no "
            "results are reported. Please try again in a moment."
        )

    elapsed = time.perf_counter() - start
    ctx.logger.info(f"Audit response generated in {elapsed:.2f} seconds")

    # Send the response back to the user
    await send_text(ctx, sender, answer)


# ---------------------------------------------------------------------------
# AgentPay (optional): Payment Protocol, seller role, testnet FET only.
# ---------------------------------------------------------------------------
pay = Protocol(spec=payment_protocol_spec, role="seller")
USED_TX_KEY = "used_transaction_ids"


def _pending_key(sender: str) -> str:
    return f"pending_audit:{sender}"


async def request_payment(ctx: Context, sender: str, user_text: str, n_refs: int):
    amount = payment.quote_fet(n_refs)
    reference = uuid4().hex[:12]
    ctx.storage.set(_pending_key(sender), {
        "text": user_text, "amount": amount, "reference": reference, "created": time.time(),
    })
    await send_text(ctx, sender, payment.quote_text(n_refs), end_session=False)
    await ctx.send(sender, RequestPayment(
        accepted_funds=[Funds(currency="FET", amount=amount, payment_method="fet_direct")],
        recipient=str(agent.wallet.address()),
        deadline_seconds=payment.DEADLINE_SECONDS,
        reference=reference,
        description=f"CiteCheck audit of {n_refs} references (testnet FET, no real money)",
        metadata={
            "provider_agent_wallet": str(agent.wallet.address()),
            "fet_network": "stable-testnet",
            "mainnet": "false",
            "content": f"CiteCheck audit of {n_refs} references: {amount} testnet FET.",
        },
    ))
    ctx.logger.info(f"RequestPayment {reference} for {amount} testnet FET sent to {sender}")


@pay.on_message(CommitPayment)
async def handle_commit(ctx: Context, sender: str, msg: CommitPayment):
    pending = ctx.storage.get(_pending_key(sender))
    used = ctx.storage.get(USED_TX_KEY) or []
    buyer = (msg.metadata or {}).get("buyer_fet_wallet") or (msg.metadata or {}).get("buyer_fet_address")
    ctx.logger.info(f"CommitPayment from {sender}: tx={msg.transaction_id}")

    if not pending or time.time() - pending["created"] > payment.DEADLINE_SECONDS:
        reason = "No open payment request (it may have expired)."
        check = payment.TransferCheck(False, reason)
    elif msg.transaction_id in used:
        check = payment.TransferCheck(False, "This transaction was already used for an audit.")
    elif msg.funds.currency != "FET" or msg.funds.payment_method != "fet_direct":
        check = payment.TransferCheck(False, "Unsupported payment method.")
    else:
        check = await asyncio.to_thread(
            payment.verify_payment_on_chain, msg.transaction_id,
            str(agent.wallet.address()), str(buyer or ""), pending["amount"],
        )

    if not check.ok:
        ctx.logger.warning(f"Payment not verified: {check.reason}")
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason=check.reason))
        await send_text(ctx, sender, (
            f"I could not verify this payment on the Dorado testnet ({check.reason}), so the audit "
            "was **not** run. If testnet FET was transferred anyway, it was not matched to this "
            f"request; quote transaction `{msg.transaction_id}` to the operator. You can send up to "
            f"{payment.FREE_REFERENCES} references for a free audit."
        ))
        return

    ctx.storage.set(USED_TX_KEY, used + [msg.transaction_id])
    ctx.storage.remove(_pending_key(sender))
    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    preamble = (
        f"💳 **Payment verified on-chain:** {pending['amount']} testnet FET (Dorado test tokens, "
        f"no real money) received in transaction `{msg.transaction_id}`."
    )
    await run_and_reply(ctx, sender, pending["text"], preamble)


@pay.on_message(RejectPayment)
async def handle_reject(ctx: Context, sender: str, msg: RejectPayment):
    ctx.storage.remove(_pending_key(sender))
    ctx.logger.info(f"Payment rejected by {sender}: {msg.reason}")
    await send_text(ctx, sender, (
        "Payment declined, so no audit was run and nothing was charged. "
        f"You can send up to {payment.FREE_REFERENCES} references for a free audit."
    ))


@chat.on_message(ChatAcknowledgement)
async def handle_ack(
    ctx: Context,
    sender: str,
    msg: ChatAcknowledgement,
):
    ctx.logger.info(f"Received chat acknowledgement from {sender}")


# Publish the chat protocol manifest (and the payment protocol when enabled)
agent.include(chat, publish_manifest=True)
if payment.payments_enabled():
    agent.include(pay, publish_manifest=True)


if __name__ == "__main__":
    agent.run()
