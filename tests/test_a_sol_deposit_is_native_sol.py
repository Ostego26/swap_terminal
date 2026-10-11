"""A SOL deposit is NATIVE SOL, whatever SOL_SPL_MINT is set to.

Role: tests (chains/solana.SolanaAdapter._credits_in_transaction()'s reader choice)
Reads: nothing. Every RPC call is answered by a seeded transport in this file.
Writes: nothing
Can move funds: no -- the adapter is read-only and no socket is opened
Mainnet-safe: yes

=============================================================================
WITH SOL_SPL_MINT SET, A NATIVE SOL DEPOSIT WAS DROPPED IN COMPLETE SILENCE
=============================================================================

_credits_in_transaction() chose its reader on `self.is_spl`:

    credits = (self._spl_credits(...) if self.is_spl
               else self._native_credits(...))

So a customer who sent native SOL to SOL_DEPOSIT_ACCOUNT with a correct memo got
nothing. _spl_credits() found no matching pre/postTokenBalances entry and returned [],
and _attributable() returned [] on `if not credits` BEFORE the memo was ever read -- so
there was no deposit_events row, NO unattributable_deposits row either, and the scan's
INFO line read exactly like a healthy one, ending "Nothing was skipped: (none)." The
swap sat at awaiting_deposit forever with the money provably in the account.

MEASURED 2026-10-10 against a seeded getSignaturesForAddress/getTransaction pair
carrying a native 1.5 SOL credit and a valid spl-memo of '12': with mint='' it returned
one event (vout 12, amount 1.5); with the mint set it returned `events: []`,
`unattributable: []` and that same healthy-looking line.

=============================================================================
WHY NATIVE IS THE ANSWER, RATHER THAN READING BOTH OR REFUSING AT THE EDGE
=============================================================================

Operator, 2026-10-11: "sol should be devnet coins that we can swap into any other
chain", and "any other chain should be able to be swapped into any other as the entire
main idea of this fucking project".

And the arithmetic agrees with them: services/pricing.py prices the asset string 'SOL'
as NATIVE SOL either way, so anything credited as a SOL deposit must BE native SOL or
the quoted rate is wrong. Crediting a token as 'SOL' at the mint's own decimals against
a natively-priced quote is rule 11's wrong-precision case sitting on the gate that
releases a payout.

CLAUDE.md ALREADY SAID SO, which is what makes this a leak rather than a design
decision: "SPL/wGRC PAYOUT SUPPORT IN THE PYTHON TREE IS UNTOUCHED and is a different
thing -- chains/solana.py and SOL_SPL_MINT pay an SPL token OUT." Out. The file's three
other is_spl branches are all on the payout side (get_balance, describe_address, the
send path) and are correct; this was the one on the deposit side.

AN EARLIER REMEDY PROPOSED REFUSING SOL AS A FROM-ASSET while the adapter is
SPL-configured. That is recorded here because it is the wrong shape and was nearly
taken: it would have made a native SOL deposit impossible rather than possible, which
is the opposite of what the terminal is for.
"""

from __future__ import annotations

import pytest
import valid_addresses
from chains.solana import SolanaAdapter

DEPOSIT = valid_addresses.SOL_DEPOSIT_ACCOUNT
SENDER = valid_addresses.solana_address_for("test_a_sol_deposit_is_native_sol sender")

#: Any valid base58 pubkey serves as a mint identifier here -- _spl_credits() only ever
#: compares it for equality against the `mint` field of a token balance entry, and the
#: adapter's constructor validates that it is syntactically an address. Reusing an
#: existing fixture avoids inventing a 32-byte string by hand (rule 17: a hand-made
#: address that happens not to decode is a test that passes for the wrong reason).
MINT = valid_addresses.SOL_PAYOUT

#: The memo program id the cluster reports on a jsonParsed instruction.
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

#: The deposit tag a customer is told to put in the memo.
TAG = "12"

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_UNITS_PER_WHOLE = 1_000_000_000


def transaction(*, native_sol: float = 0.0, token_amount: float = 0.0, memo: str | None = TAG) -> dict:
    """One getTransaction response. NOT a mock of the adapter -- a stand-in for the cluster.

    The balance arrays are the real shape the RPC returns: index 0 is the sender, index 1
    is the deposit account, and _native_credits() takes post[1] - pre[1]. The token entry
    carries `owner` and `mint`, which is what _spl_credits() matches on.

    `memo=None` omits the instruction entirely, which is how a payment with no tag
    arrives.
    """
    # round() and no int(): round(float, 0) already returns an int, and ruff's RUF046
    # says so. The rounding is the load-bearing part -- 1234.567891234 * 1e9 is not
    # exactly representable, so truncating would seed a fixture one lamport short of
    # what the test then asserts.
    lamports = round(native_sol * LAMPORTS_PER_SOL)
    meta = {
        "err": None,
        "preBalances": [10 * LAMPORTS_PER_SOL, 0],
        "postBalances": [10 * LAMPORTS_PER_SOL - lamports, lamports],
        "preTokenBalances": [],
        "postTokenBalances": [],
    }
    if token_amount:
        meta["postTokenBalances"] = [{
            "accountIndex": 1, "owner": DEPOSIT, "mint": MINT,
            "uiTokenAmount": {"amount": str(round(token_amount * TOKEN_UNITS_PER_WHOLE)),
                              "decimals": 9},
        }]
    instructions = []
    if memo is not None:
        instructions.append({"program": "spl-memo", "programId": MEMO_PROGRAM, "parsed": memo})
    return {
        "transaction": {"message": {
            "accountKeys": [{"pubkey": SENDER}, {"pubkey": DEPOSIT}],
            "instructions": instructions,
        }},
        "meta": meta,
    }


class SeededCluster(SolanaAdapter):
    """The REAL adapter with one method replaced: the transport.

    SUBCLASSING AND OVERRIDING call() rather than monkeypatching a module, because the
    thing under test is which READER the adapter chooses -- so every line of
    _credits_in_transaction, _native_credits, _spl_credits, _attributable and
    deposit_tag_from has to be the real one. Only the bytes the cluster would have sent
    are substituted.
    """

    def __init__(self, mint: str, response: dict):
        super().__init__(url="http://seeded.invalid", mint=mint)
        self._response = response
        self.methods: list[str] = []

    def call(self, method, *_params):
        self.methods.append(method)
        if method == "getSignaturesForAddress":
            return [{"signature": "SIG1", "err": None, "slot": 1, "confirmationStatus": "finalized"}]
        if method == "getTransaction":
            return self._response
        raise AssertionError(f"the adapter made an unexpected RPC call: {method}")


def test_a_native_sol_deposit_is_credited_WITH_the_mint_set(request):
    """THE REGRESSION TEST. MUTATION: restore `self._spl_credits(...) if self.is_spl`.

    This is the exact case that was silently dropped: SOL_SPL_MINT configured (which is
    a PAYOUT setting), a customer sending native SOL, and a correct memo.
    """
    adapter = SeededCluster(MINT, transaction(native_sol=1.5))

    events = adapter.find_deposits_to_address(DEPOSIT)

    assert [(event["vout"], event["amount"]) for event in events] == [(int(TAG), 1.5)], (
        "a native SOL deposit with a correct memo was not credited while SOL_SPL_MINT was set"
    )
    assert adapter.unattributable_drops == [], "a credited deposit must not also be recorded as stranded"


def test_the_mint_makes_no_difference_to_the_deposit_leg():
    """Same transaction, both configurations, identical result.

    ASSERTED AS AN EQUALITY between the two runs rather than twice against a literal,
    because the property is that SOL_SPL_MINT does not reach this path at all -- not
    that each run happens to produce 1.5.
    """
    without = SeededCluster("", transaction(native_sol=0.25)).find_deposits_to_address(DEPOSIT)
    with_mint = SeededCluster(MINT, transaction(native_sol=0.25)).find_deposits_to_address(DEPOSIT)

    assert [(e["vout"], e["amount"]) for e in without] == [(e["vout"], e["amount"]) for e in with_mint]
    assert without, "the fixture credited nothing in either configuration; the test proves nothing"


def test_a_token_arriving_instead_is_RECORDED_rather_than_silent():
    """The other half, and without it the fix trades one quiet failure for another.

    Somebody sending the configured SPL token to the deposit account produces no native
    delta, so `credits` is empty -- which is how the original defect looked. The token
    read still happens, ONLY to record a drop, so the money is in a table that
    show_unattributable.py and /admin can both see rather than in nothing.
    """
    adapter = SeededCluster(MINT, transaction(native_sol=0.0, token_amount=2.0))

    events = adapter.find_deposits_to_address(DEPOSIT)

    assert events == [], "an SPL token was credited as a SOL deposit"
    assert len(adapter.unattributable_drops) == 1, (
        "a token arrived at the deposit account and was recorded nowhere, which is the "
        "defect this fix replaces wearing different clothes"
    )
    drop = adapter.unattributable_drops[0]
    assert drop.amount == 2.0
    assert drop.address == DEPOSIT
    assert MINT in drop.why, "the reason does not name the mint, so an operator cannot place the payment"
    assert "native SOL" in drop.why


def test_a_transaction_carrying_BOTH_credits_the_sol_and_records_no_drop():
    """One payment must not have two tables asserting different things about it.

    A transaction that moves native SOL to the deposit account AND a token to the same
    owner is a SOL deposit that happens to carry a token transfer. Crediting the SOL and
    also recording a drop would be the shape db.py's comment on unattributable_deposits
    refuses.
    """
    adapter = SeededCluster(MINT, transaction(native_sol=0.75, token_amount=5.0))

    events = adapter.find_deposits_to_address(DEPOSIT)

    assert [(e["vout"], e["amount"]) for e in events] == [(int(TAG), 0.75)]
    assert adapter.unattributable_drops == [], "the SOL was credited AND a drop was recorded"


def test_an_untagged_native_deposit_is_still_dropped_with_a_reason():
    """The pre-existing memo behavior is unchanged, which this fix must not disturb.

    A native credit with no memo is nobody's until a human matches it, and
    _attributable() already records that. Asserted here because the new token branch sits
    immediately beside it and a careless `return []` would have swallowed this case.
    """
    adapter = SeededCluster(MINT, transaction(native_sol=1.0, memo=None))

    events = adapter.find_deposits_to_address(DEPOSIT)

    assert events == []
    assert len(adapter.unattributable_drops) == 1
    assert adapter.unattributable_drops[0].amount == 1.0


def test_no_token_read_happens_when_no_mint_is_configured():
    """_spl_credits() matches on self.mint, so an unset mint matches nothing.

    ASSERTED SO THE GAP IS STATED RATHER THAN HIDDEN (rule 17): a token this terminal is
    not configured for is invisible on this path, and the remedy for that is a mint in
    the environment, not a change here. A reader who assumes "unattributable catches
    everything" needs to know where the edge is.
    """
    adapter = SeededCluster("", transaction(native_sol=0.0, token_amount=3.0))

    assert adapter.find_deposits_to_address(DEPOSIT) == []
    assert adapter.unattributable_drops == [], (
        "a drop was recorded for a mint this adapter is not configured for, which would be "
        "an amount read at a decimals nothing verified"
    )


@pytest.mark.parametrize("sol_amount", [0.000000001, 0.5, 1.0, 1234.567891234])
def test_the_credited_amount_goes_through_the_shared_conversion(sol_amount):
    """Rule 11: lamports to SOL is done once, in one function, not by a literal here.

    Several magnitudes including one lamport and a value with nine significant decimals,
    because a conversion that is wrong by a factor of a billion is the failure mode this
    rule exists for and it is invisible at 1.0.
    """
    adapter = SeededCluster(MINT, transaction(native_sol=sol_amount))
    events = adapter.find_deposits_to_address(DEPOSIT)

    assert len(events) == 1
    assert events[0]["amount"] == pytest.approx(sol_amount, rel=0, abs=1e-9)
