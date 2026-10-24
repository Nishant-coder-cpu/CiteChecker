from citecheck import payment
from citecheck.payment import check_transfer, verify_payment_on_chain

US, BUYER, FEE = "fetch1agentwallet", "fetch1buyer", "fetch17xpfvakm2amg962yls6f84z3kell8c5lserqta"


def ev(sender, recipient, afet):
    return ("transfer", {"sender": sender, "recipient": recipient, "amount": f"{afet}atestfet"})


def test_quote_and_free_tier(monkeypatch):
    monkeypatch.setattr(payment, "PAYMENT_MODE", "testnet_fet")
    assert not payment.requires_payment(payment.FREE_REFERENCES)
    assert payment.requires_payment(payment.FREE_REFERENCES + 1)
    assert payment.quote_fet(8) == "0.08"
    assert "no real money" in payment.quote_text(8)
    monkeypatch.setattr(payment, "PAYMENT_MODE", "off")
    assert not payment.requires_payment(100)


def test_transfer_must_match_recipient_sender_and_amount():
    fee = ev(BUYER, FEE, 5 * 10**15)
    good = ev(BUYER, US, 8 * 10**16)
    assert check_transfer([fee, good], US, BUYER, "0.08").ok
    assert not check_transfer([fee, good], US, BUYER, "0.09").ok           # too little
    assert not check_transfer([fee, good], US, "fetch1other", "0.08").ok   # wrong payer
    assert not check_transfer([fee], US, BUYER, "0.01").ok                 # only the fee
    assert not check_transfer([("transfer", {"sender": BUYER, "recipient": US, "amount": "80000000000000000afet"})],
                              US, BUYER, "0.08").ok                         # mainnet denom, not testnet


def test_unverifiable_payments_are_refused():
    assert not verify_payment_on_chain("", US, BUYER, "0.08").ok
    assert not verify_payment_on_chain("abc", US, "", "0.08").ok
    assert not verify_payment_on_chain("abc", US, BUYER, "0.08", fetch=lambda h: (False, [], "not found")).ok
    failed_tx = lambda h: (False, [ev(BUYER, US, 10**18)], "")
    assert verify_payment_on_chain("abc", US, BUYER, "0.08", fetch=failed_tx).reason == "transaction failed on-chain"
    ok_tx = lambda h: (True, [ev(BUYER, US, 10**18)], "")
    assert verify_payment_on_chain("abc", US, BUYER, "0.08", fetch=ok_tx).ok
