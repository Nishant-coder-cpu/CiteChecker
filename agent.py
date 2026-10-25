import asyncio
import os
import time
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import httpx
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

from citecheck import net, payment, trace
from citecheck.messages import VERIFIER_PORT, verifier_address
from citecheck.parsing import parse_references
from citecheck.remote import make_remote_verify
from citecheck.resolver import ResilientResolver
from citecheck.service import HELP_TEXT, handle_request

# Load configuration from .env
load_dotenv()
net.apply()  # IPv4 + fast connect timeouts (see citecheck/net.py)

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
    # From some networks about half of all connections to api.asi1.ai stall while
    # answered calls take ~1 s, so abandon a stalled connect quickly and retry.
    # If every attempt fails, the claim is reported as "not assessed".
    timeout=httpx.Timeout(float(os.getenv("ASI1_TIMEOUT", "20")), connect=3.0),
    max_retries=4,
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
    # ASI:One renders only the final ACP message in some chats. Keep a normal
    # report in one message so the beginning is not hidden by a later chunk.
    # _message_parts remains as a safety valve for an exceptionally large reply.
    parts = _message_parts(text)
    status = None
    with trace.span("reply.deliver", parts=len(parts)) as sp:
        for index, part in enumerate(parts):
            content = [TextContent(type="text", text=part)]
            if end_session and index == len(parts) - 1:
                content.append(EndSessionContent(type="end-session"))
            message = ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)
            status = await _deliver(ctx, recipient, message)
            if status.status == DeliveryStatus.FAILED:
                break
        assert status is not None
        sp.set("ok" if status.status != DeliveryStatus.FAILED else "failed", detail=status.detail)
    return status


MAX_CHAT_MESSAGE_CHARS = int(os.getenv("MAX_CHAT_MESSAGE_CHARS", "50000"))


def _message_parts(text: str) -> list[str]:
    """Split a report at line boundaries without losing any text."""
    if len(text) <= MAX_CHAT_MESSAGE_CHARS:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > MAX_CHAT_MESSAGE_CHARS:
        cut = remaining.rfind("\n", 0, MAX_CHAT_MESSAGE_CHARS)
        if cut <= 0:
            cut = MAX_CHAT_MESSAGE_CHARS
        parts.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    parts.append(remaining)
    return parts


async def _deliver(ctx: Context, recipient: str, message: ChatMessage):
    for attempt in range(2):
        status = await ctx.send(recipient, message)
        if status.status != DeliveryStatus.FAILED:
            ctx.logger.info(f"Reply to {recipient}: {status.status.value} ({status.detail})")
            return status
        RESOLVER.invalidate(recipient)
        if "timeout" in (status.detail or "").lower() or "429" in (status.detail or ""):
            # After a send timeout the envelope may still go out (a retry could
            # deliver it twice); after a 429, retrying only adds load.
            ctx.logger.warning(f"Reply to {recipient}: status unknown ({status.detail})")
            return status
        ctx.logger.warning(f"Reply attempt {attempt + 1} to {recipient} not sent: {status.detail}")
        await asyncio.sleep(2)
    ctx.logger.error(f"Reply to {recipient} could not be delivered")
    return status


# Status updates use the same webhook as the final report.  They are disabled
# by default because a slow relay can queue them ahead of the actual result.
PROGRESS_UPDATES = os.getenv("PROGRESS_UPDATES", "off") == "on"
PROGRESS_HEARTBEAT_SECONDS = float(os.getenv("PROGRESS_HEARTBEAT_SECONDS", "0"))
PROGRESS_SEND_TIMEOUT = float(os.getenv("PROGRESS_SEND_TIMEOUT", "3"))


class ProgressStream:
    """Ordered activity updates for an ACP chat session.

    ACP has no message-edit operation, so the changing spinner is sent as a
    compact live feed. Each update is delivered in sequence, and close() waits
    for any in-flight update so the final report can never overtake it.
    """

    FRAMES = ("◐", "◓", "◑", "◒")

    def __init__(self, ctx: Context, recipient: str):
        self.ctx = ctx
        self.recipient = recipient
        self.started = time.perf_counter()
        self.closed = False
        self.send_lock = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.heartbeat: asyncio.Task | None = None

    async def start(self) -> None:
        if PROGRESS_HEARTBEAT_SECONDS > 0:
            self.heartbeat = asyncio.create_task(self._pulse(), name="citecheck-progress-heartbeat")

    async def __call__(self, text: str) -> None:
        if self.closed:
            return
        await self._send(text)

    async def _send(self, text: str) -> None:
        message = ChatMessage(
            timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
            content=[TextContent(type="text", text=text)],
        )
        async with self.send_lock:
            with trace.span("progress.send") as sp:
                try:
                    status = await asyncio.wait_for(
                        self.ctx.send(self.recipient, message), timeout=PROGRESS_SEND_TIMEOUT
                    )
                except TimeoutError:
                    sp.set("timeout", detail=f"progress send exceeded {PROGRESS_SEND_TIMEOUT:g}s")
                except Exception as exc:  # noqa: BLE001 - progress must never interrupt an audit
                    sp.set("error", error=type(exc).__name__)
                else:
                    detail = getattr(status, "detail", None)
                    failed = getattr(status, "status", None) == DeliveryStatus.FAILED
                    sp.set("failed" if failed else "ok", detail=detail)
                    if failed:
                        RESOLVER.invalidate(self.recipient)

    async def _pulse(self) -> None:
        frame = 0
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=PROGRESS_HEARTBEAT_SECONDS
                )
                break
            except asyncio.TimeoutError:
                pass
            elapsed = int(time.perf_counter() - self.started)
            await self._send(
                f"{self.FRAMES[frame % len(self.FRAMES)]} **Still working** · {elapsed}s elapsed. "
                "I am continuing the audit…"
            )
            frame += 1

    async def close(self) -> None:
        self.closed = True
        self.stop_event.set()
        if self.heartbeat is not None:
            await self.heartbeat
        # A heartbeat may already have entered _send(). Waiting for the lock
        # guarantees it finishes before send_text() emits the final report.
        async with self.send_lock:
            pass


def make_progress(ctx: Context, recipient: str) -> ProgressStream:
    return ProgressStream(ctx, recipient)


async def run_and_reply(ctx: Context, sender: str, user_text: str, preamble: str | None = None):
    tr = trace.Trace("chat_audit", sender=sender, paid=bool(preamble)).activate()
    start = time.perf_counter()
    progress = make_progress(ctx, sender) if PROGRESS_UPDATES else None
    if progress is not None:
        await progress.start()
    try:
        answer = await handle_request(
            user_text, llm, make_remote_verify(ctx, VERIFIER_ADDRESS, RESOLVER), preamble,
            progress=progress,
        )
    except Exception:
        ctx.logger.exception("Audit failed")
        answer = (preamble + "\n\n" if preamble else "") + (
            "Something went wrong while auditing these references, so no "
            "results are reported. Please try again in a moment."
        )

    finally:
        if progress is not None:
            await progress.close()

    elapsed = time.perf_counter() - start
    ctx.logger.info(f"Audit response generated in {elapsed:.2f} seconds")

    # Send the response back to the user
    status = await send_text(ctx, sender, answer)
    tr.finish("ok" if status.status != DeliveryStatus.FAILED else "reply_failed")


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
