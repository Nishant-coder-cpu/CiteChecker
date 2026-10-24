"""AgentPay: optional per-reference pricing on top of the free CiteCheck core.

Payments use the uAgents Payment Protocol with `fet_direct` FET on the Dorado
**testnet** (test tokens, no real money). A CommitPayment is only accepted after
the transaction is found on-chain with a transfer of at least the quoted amount
from the buyer's wallet to this agent's wallet. A protocol message alone is
never treated as proof of payment.
"""

import os
import time
from dataclasses import dataclass
from decimal import Decimal

PAYMENT_MODE = os.getenv("PAYMENT_MODE", "off")           # "off" | "testnet_fet"
FREE_REFERENCES = int(os.getenv("FREE_REFERENCES", "3"))
PRICE_PER_REFERENCE_FET = Decimal(os.getenv("PRICE_PER_REFERENCE_FET", "0.01"))
DEADLINE_SECONDS = 600
DENOM = "atestfet"
ATTO = Decimal(10) ** 18


def payments_enabled() -> bool:
    return PAYMENT_MODE == "testnet_fet"


def requires_payment(n_refs: int) -> bool:
    return payments_enabled() and n_refs > FREE_REFERENCES


def quote_fet(n_refs: int) -> str:
    """Price for a batch, as a plain decimal string (e.g. '0.08')."""
    return format((PRICE_PER_REFERENCE_FET * n_refs).normalize(), "f")


def quote_text(n_refs: int) -> str:
    return (
        f"This batch has **{n_refs} references**. Audits of up to {FREE_REFERENCES} references are free; "
        f"larger batches cost {PRICE_PER_REFERENCE_FET} FET per reference, so this one is "
        f"**{quote_fet(n_refs)} testnet FET** (Dorado test tokens, no real money).\n\n"
        "Approve the payment request to run the audit, or reject it and send up to "
        f"{FREE_REFERENCES} references for a free audit. Nothing runs until the transfer is confirmed on-chain."
    )


@dataclass
class TransferCheck:
    ok: bool
    reason: str


def check_transfer(events: list[tuple[str, dict[str, str]]], recipient: str, sender: str,
                   min_amount_fet: str) -> TransferCheck:
    """Look for ONE transfer event paying >= the quote from `sender` to `recipient`.

    `events` must be the un-merged per-event list (cosmpy's TxResponse.events
    merges same-type events, which can hide the payment behind the fee transfer).
    """
    needed = int(Decimal(min_amount_fet) * ATTO)
    saw_recipient = False
    for etype, attrs in events:
        if etype != "transfer" or attrs.get("recipient") != recipient:
            continue
        saw_recipient = True
        if attrs.get("sender") != sender:
            continue
        for coin in (attrs.get("amount") or "").split(","):
            if coin.endswith(DENOM) and coin[: -len(DENOM)].isdigit() and int(coin[: -len(DENOM)]) >= needed:
                return TransferCheck(True, "transfer found on-chain")
    if not saw_recipient:
        return TransferCheck(False, "transaction does not pay this agent's wallet")
    return TransferCheck(False, "no transfer from the buyer's wallet of at least the quoted amount")


def fetch_tx_events(tx_hash: str, attempts: int = 5, delay: float = 3.0) -> tuple[bool, list, str]:
    """Query Dorado for a tx. Returns (tx_succeeded, events, error). Retries while not yet indexed."""
    from cosmpy.aerial.client import LedgerClient, NetworkConfig
    from cosmpy.protos.cosmos.tx.v1beta1.service_pb2 import GetTxRequest

    ledger = LedgerClient(NetworkConfig.fetchai_stable_testnet())
    last_error = "not found"
    for attempt in range(attempts):
        try:
            resp = ledger.txs.GetTx(GetTxRequest(hash=tx_hash)).tx_response
            events = [
                (e.type, {_dec(a.key): _dec(a.value) for a in e.attributes})
                for e in resp.events
            ]
            return int(resp.code) == 0, events, ""
        except Exception as exc:  # noqa: BLE001 - grpc NotFound while the tx is being indexed
            last_error = f"{type(exc).__name__}"
            if attempt < attempts - 1:
                time.sleep(delay)
    return False, [], f"transaction not found on Dorado testnet ({last_error})"


def _dec(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def verify_payment_on_chain(tx_hash: str, recipient: str, sender: str, amount_fet: str,
                            fetch=fetch_tx_events) -> TransferCheck:
    if not tx_hash or not sender:
        return TransferCheck(False, "missing transaction id or buyer wallet")
    succeeded, events, error = fetch(tx_hash)
    if error:
        return TransferCheck(False, error)
    if not succeeded:
        return TransferCheck(False, "transaction failed on-chain")
    return check_transfer(events, recipient, sender, amount_fet)
