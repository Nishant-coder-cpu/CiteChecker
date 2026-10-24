from uagents import Agent, Context

from common import (
    RECEIVER_PORT,
    RECEIVER_SEED,
    PingRequest,
    PingResponse,
    SlowRequest,
)


def build_receiver(with_endpoint: bool = True) -> Agent:
    kwargs = {}
    if with_endpoint:
        kwargs = {
            "port": RECEIVER_PORT,
            "endpoint": [f"http://127.0.0.1:{RECEIVER_PORT}/submit"],
        }
    agent = Agent(name="a2a-test-receiver", seed=RECEIVER_SEED, **kwargs)

    @agent.on_message(PingRequest, replies=PingResponse)
    async def on_ping(ctx: Context, sender: str, msg: PingRequest):
        ctx.logger.info(f"RECEIVED ping {msg.request_id} from {sender}")
        await ctx.send(
            sender,
            PingResponse(
                request_id=msg.request_id,
                echo=msg.payload,
                receiver_note=f"handled-by:{agent.address}",
            ),
        )

    @agent.on_message(SlowRequest)
    async def on_slow(ctx: Context, sender: str, msg: SlowRequest):
        ctx.logger.info(f"RECEIVED slow request {msg.request_id}; not replying")

    return agent


if __name__ == "__main__":
    build_receiver().run()
