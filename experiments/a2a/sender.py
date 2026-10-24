"""Runs a fixed sequence of connectivity checks and writes a JSON result.

Usage:
    python sender.py network   # separate processes, Almanac API resolution
    python sender.py bureau    # sender + receiver in one Bureau (local dispatch)
    python sender.py sync      # sender without endpoint, synchronous replies
"""

import json
import sys
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Bureau, Context
from uagents_core.identity import Identity

from common import (
    RECEIVER_ADDRESS,
    SENDER_PORT,
    SENDER_SEED,
    PingRequest,
    PingResponse,
    SlowRequest,
    UnknownRequest,
)

MODE = sys.argv[1] if len(sys.argv) > 1 else "network"
# "sync": sender has NO endpoint; replies must come back in the same HTTP exchange.
SYNC = MODE == "sync"
RESULT_PATH = Path(__file__).with_name(f"result_{MODE}.json")
UNREACHABLE_ADDRESS = Identity.from_seed(f"nobody-{uuid4()}", 0).address

results: dict = {"mode": MODE, "receiver": RECEIVER_ADDRESS, "checks": {}}


def record(name: str, ok: bool, detail: str) -> None:
    results["checks"][name] = {"ok": ok, "detail": detail}
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def build_sender(with_endpoint: bool) -> Agent:
    kwargs = {}
    if with_endpoint:
        kwargs = {
            "port": SENDER_PORT,
            "endpoint": [f"http://127.0.0.1:{SENDER_PORT}/submit"],
        }
    agent = Agent(name="a2a-test-sender", seed=SENDER_SEED, **kwargs)

    @agent.on_event("startup")
    async def run_checks(ctx: Context):
        results["sender"] = agent.address
        record("distinct_identities", agent.address != RECEIVER_ADDRESS,
               f"sender={agent.address[:16]}.. receiver={RECEIVER_ADDRESS[:16]}..")

        # 1. Request/response round trip, repeated to check reproducibility.
        for i in range(3):
            rid = f"ping-{i}-{uuid4().hex[:6]}"
            reply, status = await ctx.send_and_receive(
                RECEIVER_ADDRESS, PingRequest(request_id=rid, payload="hello"),
                response_type=PingResponse, timeout=20, sync=SYNC,
            )
            ok = isinstance(reply, PingResponse) and reply.request_id == rid
            record(f"round_trip_{i}", ok,
                   f"status={status.status.value} endpoint={status.endpoint!r} "
                   f"detail={status.detail!r} reply={reply!r}")

        # 2. Unreachable (never registered) recipient.
        reply, status = await ctx.send_and_receive(
            UNREACHABLE_ADDRESS, PingRequest(request_id="x", payload="x"),
            response_type=PingResponse, timeout=5,
        )
        record("unreachable_recipient_fails_cleanly", reply is None,
               f"status={status.status.value} detail={status.detail!r}")

        # 3. Receiver accepts the message but never answers -> timeout.
        reply, status = await ctx.send_and_receive(
            RECEIVER_ADDRESS, SlowRequest(request_id="slow"),
            response_type=PingResponse, timeout=4,
        )
        record("response_timeout_handled", reply is None,
               f"status={status.status.value} detail={status.detail!r}")

        # 4. Message type the receiver has no handler for.
        try:
            status = await ctx.send(RECEIVER_ADDRESS, UnknownRequest(junk=1))
            record("unsupported_message_no_crash", True,
                   f"status={status.status.value} detail={status.detail!r}")
        except Exception as exc:  # noqa: BLE001
            record("unsupported_message_no_crash", False, repr(exc))

        RESULT_PATH.write_text(json.dumps(results, indent=2))
        print(f"RESULT_WRITTEN {RESULT_PATH}", flush=True)

    return agent


if __name__ == "__main__":
    if MODE == "bureau":
        from receiver import build_receiver

        bureau = Bureau(port=8113)
        bureau.add(build_receiver(with_endpoint=False))
        bureau.add(build_sender(with_endpoint=False))
        bureau.run()
    else:
        build_sender(with_endpoint=MODE != "sync").run()
