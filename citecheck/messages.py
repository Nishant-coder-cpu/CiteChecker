"""Messages exchanged between the CiteCheck agent and the Verifier agent.

The report travels as JSON text so both sides validate it with the same
pydantic models rather than relying on nested uAgents schemas.
"""

import hashlib
import os

from uagents import Model
from uagents_core.identity import Identity

VERIFIER_PORT = int(os.getenv("VERIFIER_PORT", "8002"))


class VerifyRequest(Model):
    audit_id: str
    original_text: str
    report_json: str
    live_recheck: bool = True


class VerifyResponse(Model):
    audit_id: str
    result_json: str            # VerificationResult JSON
    error: str | None = None


def verifier_seed(agent_seed: str) -> str:
    """Verifier identity derived from the main seed, so no second secret is needed."""
    return os.getenv("VERIFIER_SEED") or hashlib.sha256(f"{agent_seed}:citecheck-verifier".encode()).hexdigest()


def verifier_address(agent_seed: str) -> str:
    return os.getenv("VERIFIER_ADDRESS") or Identity.from_seed(verifier_seed(agent_seed), 0).address
