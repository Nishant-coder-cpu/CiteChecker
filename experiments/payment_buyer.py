"""Live Payment Protocol check against a local CiteCheck test instance.

    PAYMENT_MODE=testnet_fet AGENT_SEED=<test seed> AGENT_PORT=8031 \
      AGENT_ENDPOINT=http://127.0.0.1:8031/submit python agent.py
    python experiments/payment_buyer.py <instance_address> reject|fake_commit

No funds are moved: 'fake_commit' sends a made-up transaction id, which the
seller must refuse after checking the Dorado testnet.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment, CommitPayment, CompletePayment, RejectPayment, RequestPayment, payment_protocol_spec,
)

target, mode = sys.argv[1], sys.argv[2]
buyer = Agent(name="payment-test-buyer", seed="citecheck-payment-test-buyer-v1",
              port=8032, endpoint=["http://127.0.0.1:8032/submit"])
chat = Protocol(spec=chat_protocol_spec)
pay = Protocol(spec=payment_protocol_spec, role="buyer")
seen = []


def done(code=0):
    print("SEEN", seen, flush=True)
    os._exit(code)


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
    seen.append("ChatMessage")
    print(f"CHAT: {text[:260]!r}", flush=True)
    if "Payment declined" in text or "could not verify" in text:
        done(0)
    if "CiteCheck audit" in text and "##" in text:
        print("UNEXPECTED: audit ran", flush=True)
        done(1)


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    seen.append("Ack")


@pay.on_message(RequestPayment)
async def on_request(ctx: Context, sender: str, msg: RequestPayment):
    seen.append("RequestPayment")
    f = msg.accepted_funds[0]
    print(f"REQUEST: {f.amount} {f.currency} via {f.payment_method} to {msg.recipient} "
          f"deadline={msg.deadline_seconds}s network={msg.metadata.get('fet_network')}", flush=True)
    if mode == "reject":
        await ctx.send(sender, RejectPayment(reason="test: buyer declines"))
    else:
        await ctx.send(sender, CommitPayment(
            funds=f, recipient=msg.recipient, transaction_id="F" * 64, reference=msg.reference,
            metadata={"buyer_fet_wallet": "fetch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"}))


@pay.on_message(CancelPayment)
async def on_cancel(ctx: Context, sender: str, msg: CancelPayment):
    seen.append("CancelPayment")
    print(f"CANCEL: {msg.reason}", flush=True)


@pay.on_message(CompletePayment)
async def on_complete(ctx: Context, sender: str, msg: CompletePayment):
    seen.append("CompletePayment")
    print("UNEXPECTED: CompletePayment for a fake transaction", flush=True)
    done(1)


buyer.include(chat)
buyer.include(pay)


@buyer.on_event("startup")
async def start(ctx: Context):
    text = (Path(__file__).resolve().parents[1] / "demo" / "demo_references.txt").read_text()
    st = await ctx.send(target, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                            content=[TextContent(type="text", text=text)]))
    print(f"SENT {st.status.value} ({st.detail})", flush=True)


@buyer.on_interval(period=90.0)
async def give_up(ctx: Context):
    if seen:
        print("TIMEOUT", flush=True)
        done(1)


buyer.run()
