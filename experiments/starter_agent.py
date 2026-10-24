
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
from openai import AsyncOpenAI
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

# Load configuration from .env
load_dotenv()

api_key = os.getenv("ASI_ONE_API_KEY")
seed = os.getenv("AGENT_SEED")

if not api_key or not seed:
    raise RuntimeError(
        "Add ASI_ONE_API_KEY and AGENT_SEED to your .env file."
    )

# Asynchronous ASI1 client
llm = AsyncOpenAI(
    base_url="https://api.asi1.ai/v1",
    api_key=api_key,
    timeout=30.0,
    max_retries=1,
)

# Fetch.ai agent configuration
agent = Agent(
    name="fetchathon-starter",
    seed=seed,
    port=8001,
    mailbox=True,
    publish_agent_details=True,
)

chat = Protocol(spec=chat_protocol_spec)


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
    user_text = " ".join(
        item.text
        for item in msg.content
        if isinstance(item, TextContent)
    ).strip()

    if not user_text:
        user_text = "Say hello."

    ctx.logger.info(f"Received message from {sender}")

    start = time.perf_counter()

    try:
        result = await llm.chat.completions.create(
            model="asi1",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a fast, helpful Fetch-A-Thon assistant. "
                        "Answer directly in 2-4 short sentences. "
                        "Be clear and useful. Avoid unnecessary detail."
                    ),
                },
                {
                    "role": "user",
                    "content": user_text,
                },
            ],
            max_tokens=160,
        )

        answer = result.choices[0].message.content
        answer = answer or "I couldn't generate a response."

        elapsed = time.perf_counter() - start
        ctx.logger.info(
            f"ASI1 response generated in {elapsed:.2f} seconds"
        )

    except Exception:
        ctx.logger.exception("ASI1 API request failed")
        answer = (
            "I received your message, but couldn't generate an AI "
            "response right now. Please try again."
        )

    # Send the response back to the user
    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[
                TextContent(type="text", text=answer),
                EndSessionContent(type="end-session"),
            ],
        ),
    )

    ctx.logger.info(f"Response sent to {sender}")


@chat.on_message(ChatAcknowledgement)
async def handle_ack(
    ctx: Context,
    sender: str,
    msg: ChatAcknowledgement,
):
    ctx.logger.info(f"Received chat acknowledgement from {sender}")


# Publish the chat protocol manifest
agent.include(chat, publish_manifest=True)


if __name__ == "__main__":
    agent.run()
