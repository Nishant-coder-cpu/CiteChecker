"""Send one ACP ChatMessage to a running CiteCheck agent and print the reply.

    python scripts/chat_client.py <agent_address> demo/demo_references.txt

The message is resolved through the Almanac API like any other agent's would be
(for a mailbox agent that means via Agentverse). This exercises the same chat
protocol handler ASI:One uses, but it is not a substitute for testing in ASI:One.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)
from uagents import Protocol

target, text_path = sys.argv[1], sys.argv[2]
timeout_s = int(os.getenv("CHAT_CLIENT_TIMEOUT", "120"))
client = Agent(name="citecheck-chat-client", seed="citecheck-chat-client-throwaway-v1",
               port=8120, endpoint=["http://127.0.0.1:8120/submit"])
chat = Protocol(spec=chat_protocol_spec)
started = {}


@chat.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                               acknowledged_msg_id=msg.msg_id))
    text = "\n".join(c.text for c in msg.content if isinstance(c, TextContent))
    elapsed = (datetime.now(timezone.utc) - started["t"]).total_seconds()
    print(f"REPLY_FROM {sender} after {elapsed:.1f}s\n{text}", flush=True)
    os._exit(0)


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    print(f"ACK_FROM {sender}", flush=True)


client.include(chat)


@client.on_event("startup")
async def send(ctx: Context):
    started["t"] = datetime.now(timezone.utc)
    status = await ctx.send(target, ChatMessage(
        timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
        content=[TextContent(type="text", text=Path(text_path).read_text())],
    ))
    print(f"SEND_STATUS {status.status.value} via {status.endpoint} ({status.detail})", flush=True)


@client.on_interval(period=float(timeout_s))
async def give_up(ctx: Context):
    if (datetime.now(timezone.utc) - started["t"]).total_seconds() >= timeout_s:
        print(f"NO_REPLY within {timeout_s}s", flush=True)
        os._exit(1)


client.run()
