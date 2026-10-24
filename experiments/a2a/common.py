"""Shared message models and identities for the agent-to-agent connectivity test.

Seeds here are throwaway test identities (not secrets). The sender derives the
receiver's address from its seed, so no address needs to be pasted by hand.
"""

from uagents import Model
from uagents_core.identity import Identity

RECEIVER_SEED = "citecheck-a2a-experiment-receiver-v1"
SENDER_SEED = "citecheck-a2a-experiment-sender-v1"

RECEIVER_PORT = 8111
SENDER_PORT = 8112

RECEIVER_ADDRESS = Identity.from_seed(RECEIVER_SEED, 0).address


class PingRequest(Model):
    request_id: str
    payload: str


class PingResponse(Model):
    request_id: str
    echo: str
    receiver_note: str


class SlowRequest(Model):
    """Receiver deliberately never answers this, to exercise response timeouts."""

    request_id: str


class UnknownRequest(Model):
    """No handler is registered for this model on the receiver."""

    junk: int
