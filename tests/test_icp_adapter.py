"""chains/icp.ICPAdapter, exercised through an injected transport.

Role: tests (read-only)
Reads: swap_terminal.chains.icp
Writes: nothing
Can move funds: no -- every call goes to a function defined in this file
Mainnet-safe: yes. No container, no replica, no network, no dfx.

WHY A SEEDED TRANSPORT RATHER THAN A RUNNING LEDGER. The adapter's transport is a
parameter (see its module docstring for why it is dfx and not an HTTP client), so
the parsing, the refusals and the amount arithmetic can be called with seeded
inputs -- which is what rule 10 asks for and what the behavioral-verification
principle means by running the real code. The READ paths were additionally
exercised against the real released ICP ledger on the local replica on 2026-10-06
and agreed; what this file guards is that they keep agreeing, and that every
failure mode produces a refusal rather than a plausible number.

THE ASSERTION THAT MATTERS MOST is
test_output_this_adapter_cannot_read_raises_instead_of_returning_zero. On a payout
path, "this account holds nothing" and "the question could not be asked" lead to
opposite actions, and a regex that silently fails to match would make them the
same value -- which is rule 12's BLE001 defect with no except clause in sight.

No address literals: every principal and account identifier is derived by calling
chains/icp_account.
"""

from __future__ import annotations

import pytest

from swap_terminal.chains.icp import ICP_DECIMALS, ICPAdapter, ICPCallFailed
from swap_terminal.chains.icp_account import account_identifier, principal_to_text, subaccount_from_index

LEDGER = "bkyz2-fmaaa-aaaaa-qaaaq-cai"
OWNER = principal_to_text(bytes.fromhex("00000000000000020101"))

#: What the real ledger answered on the local replica, 2026-10-06. Seeded here so
#: the arithmetic is checked against values a ledger actually produced.
REAL_BALANCE_E8S = 100_000_000_000
REAL_FEE_E8S = 10_000


def transport(responses: dict, log: list | None = None):
    """A `call` that returns seeded text per method, recording what it was asked."""

    def call(canister: str, method: str, argument: str) -> str:
        if log is not None:
            log.append((canister, method, argument))
        if method not in responses:
            raise AssertionError(f"the adapter called {method!r}, which this test did not seed")
        return responses[method]

    return call


def adapter(responses=None, log=None) -> ICPAdapter:
    seeded = responses if responses is not None else {
        "icrc1_balance_of": f"({REAL_BALANCE_E8S} : nat)",
        "icrc1_fee": f"({REAL_FEE_E8S} : nat)",
    }
    return ICPAdapter(LEDGER, OWNER, call=transport(seeded, log))


# -- construction -----------------------------------------------------------


def test_a_mistyped_owner_principal_refuses_construction():
    """Checked at construction because EVERY address depends on it.

    A mistyped principal makes every deposit address wrong in the same way, and the
    CRC32 inside a textual principal makes that detectable for free -- so the
    failure happens before anything can hand an address to a customer.
    """
    mistyped = OWNER[:-1] + ("b" if OWNER[-1] != "b" else "c")
    with pytest.raises(ValueError, match="canonical"):
        ICPAdapter(LEDGER, mistyped, call=transport({}))


def test_an_empty_ledger_id_refuses_construction():
    with pytest.raises(ValueError, match="no ledger"):
        ICPAdapter("", OWNER, call=transport({}))


# -- derivation -------------------------------------------------------------


def test_the_desks_own_address_is_its_default_subaccount():
    assert adapter().own_address() == account_identifier(OWNER)


@pytest.mark.parametrize("index", [0, -1])
def test_a_deposit_address_is_refused_for_index_below_one(index):
    """A second guard. db.py's CHECK is the guarantee; this catches a caller that
    derived an index some other way, and index 0 is the desk's own account."""
    with pytest.raises(ValueError, match="DESK'S OWN"):
        adapter().deposit_address(index)


def test_each_index_gives_a_distinct_address_and_none_is_the_desks_own():
    a = adapter()
    derived = {a.deposit_address(i) for i in (1, 2, 3)}
    assert len(derived) == 3
    assert a.own_address() not in derived
    assert a.deposit_address(1) == account_identifier(OWNER, subaccount_from_index(1))


def test_validate_address_checks_the_checksum_not_just_the_shape():
    """Any 64 hex characters pass a length-and-alphabet test, which is what a
    truncated copy-paste produces -- and on a payout that is unrecoverable."""
    a = adapter()
    good = a.own_address()
    assert a.validate_address(good)
    assert not a.validate_address(("0" if good[0] != "0" else "1") + good[1:])
    assert not a.validate_address(good[:-2])
    assert not a.validate_address(OWNER), "a principal is not an account identifier"


def test_owns_address_never_returns_false():
    """None, not False, matching SolanaAdapter for the same reason.

    This adapter cannot enumerate the desk's subaccounts, so it does not KNOW an
    arbitrary account is not the desk's -- and the payout-to-the-desk gate reads
    that answer. False would be a claim it cannot support.
    """
    a = adapter()
    assert a.owns_address(a.own_address()) is True
    assert a.owns_address(a.deposit_address(1)) is None
    assert a.owns_address(account_identifier(principal_to_text(bytes([4])))) is None


# -- reads ------------------------------------------------------------------


def test_the_balance_is_e8s_divided_by_the_ledgers_own_decimals():
    assert adapter().get_balance() == REAL_BALANCE_E8S / 10**ICP_DECIMALS == 1000.0


def test_the_default_account_and_a_subaccount_are_asked_as_different_arguments():
    """The argument is the whole difference between reading desk inventory and
    reading one customer's deposit, so it is asserted rather than assumed."""
    log: list = []
    a = adapter(log=log)
    a.get_balance()
    a.subaccount_balance(1)
    default_arg, subaccount_arg = log[0][2], log[1][2]
    assert "subaccount" not in default_arg
    assert "subaccount = opt vec" in subaccount_arg
    assert OWNER in default_arg and OWNER in subaccount_arg
    assert log[0][0] == log[1][0] == LEDGER


def test_the_fee_comes_from_the_ledger_and_is_not_a_constant():
    """Seeding a different fee must change the answer.

    That is the assertion that a hardcoded 10_000 would fail, and the reason the
    adapter reads icrc1_fee() on every call: agreement with mainnet today is not a
    licence to copy the number (rule 8).
    """
    assert adapter().chain_fee() == REAL_FEE_E8S / 10**ICP_DECIMALS == 0.0001
    moved = adapter({"icrc1_fee": "(25_000 : nat)"})
    assert moved.chain_fee() == 0.00025


def test_deposit_confirmations_is_one_and_says_it_is_a_compatibility_value():
    """ICP has no confirmation depth; the watcher asks anyway.

    Pinned so that nobody raises it to 6 by analogy with GRC, which would make
    every ICP deposit wait forever for blocks that carry no meaning.
    """
    a = adapter()
    assert a.deposit_confirmations() == 1
    assert "compatibility" in a.deposit_confirmations.__doc__.lower()


@pytest.mark.parametrize("junk", ["", "(variant { Err = 3 })", "nonsense", "( : nat)"])
def test_output_this_adapter_cannot_read_raises_instead_of_returning_zero(junk):
    """THE ONE THAT MATTERS. "Holds nothing" and "could not ask" are opposite actions.

    A regex that silently failed to match would make a broken call indistinguishable
    from an empty account: a payout gate would refuse a funded swap, or worse, a
    deposit watcher would report a paid customer as unpaid forever.
    """
    a = adapter({"icrc1_balance_of": junk, "icrc1_fee": junk})
    with pytest.raises(ICPCallFailed, match="not zero"):
        a.get_balance()
    with pytest.raises(ICPCallFailed, match="not zero"):
        a.chain_fee()


def test_underscores_in_the_candid_nat_are_parsed():
    """dfx prints 100_000_000_000, not 100000000000. Parsing the human form is the
    whole reason the regex exists rather than an int() call."""
    assert adapter({"icrc1_balance_of": "(1_234_567_890 : nat)"}).get_balance() == 12.3456789


# -- the path that moves money ---------------------------------------------


#: A fixed nanosecond timestamp, used because these tests never reach a ledger: the
#: same value twice is what dedup needs, and reading a clock here would test the
#: hazard rather than the fix.
#:
#: IT IS NOT A USABLE KEY AGAINST A REAL LEDGER, and that distinction cost a live
#: attempt. This exact constant is 2025-10-06 and the replica refused it with
#: TxTooOld / allowed_window_nanos = 86_400_000_000_000 -- 24 hours. A real caller
#: passes the swap's RECORDED CREATION TIME: a real timestamp, captured once, reused
#: on every retry. See send_to_address's docstring.
FIXED_NANOS = 1_759_700_000_000_000_000

OK_BLOCK = "(variant { Ok = 42 : nat64 })"


def sending_adapter(transfer_reply=OK_BLOCK, log=None) -> ICPAdapter:
    return adapter({"icrc1_fee": f"({REAL_FEE_E8S} : nat)", "transfer": transfer_reply}, log=log)


def test_a_send_without_an_idempotency_key_refuses_and_calls_the_ledger_ZERO_TIMES():
    """THE DEFAULT IS THE DANGEROUS ONE, so the default refuses.

    The ICP ledger deduplicates on (from, to, amount, fee, memo, created_at_time)
    for 24 hours and answers a repeat with TxDuplicate carrying the ORIGINAL block
    index -- but only when created_at_time is given. With it null the ledger stamps
    its own time, every retry is a NEW transaction, and a payout worker that times
    out and retries PAYS TWICE.

    Read from the deployed ledger's interface (rs/ledger_suite/icp/ledger.did), and
    the assertion that matters is the second one: a refusal that had already called
    the ledger would be a refusal reported over a transfer that happened.
    """
    log: list = []
    a = sending_adapter(log=log)
    with pytest.raises(ICPCallFailed, match="created_at_time unset"):
        a.send_to_address(a.deposit_address(1), 0.5)
    assert log == [], "a refused send must not have called the ledger at all"


def test_a_send_passes_the_fee_it_read_from_the_ledger_not_a_constant():
    """Legacy `transfer` REQUIRES the fee and rejects a mismatch with BadFee.

    So the fee is read first and seeded differently here: if the adapter carried a
    hardcoded 10_000 this would still pass against the real ledger today and fail
    the day a ledger changes its fee, which is rule 8's drift with money attached.
    """
    log: list = []
    a = adapter({"icrc1_fee": "(25_000 : nat)", "transfer": OK_BLOCK}, log=log)
    a.send_to_address(a.deposit_address(1), 0.5, created_at_time_nanos=FIXED_NANOS)
    methods = [m for _, m, _ in log]
    assert methods == ["icrc1_fee", "transfer"], "the fee must be read BEFORE the transfer"
    sent = log[1][2]
    assert "fee = record { e8s = 25000 : nat64 }" in sent


def test_the_destination_goes_out_as_a_32_BYTE_BLOB_not_as_text():
    # RAW DOCSTRING, because the quoted dfx error below contains \ee and \cc and a
    # non-raw string makes those invalid escape sequences. That surfaced as a
    # DeprecationWarning from tests/test_xrp_balances.py, which ast.parse()s every
    # .py in the tree -- so a bad escape anywhere becomes a warning in an unrelated
    # test with only "<unknown>:249" to locate it.
    r"""`type AccountIdentifier = blob` in the ledger's own interface.

    THIS TEST PASSED WHILE THE CODE WAS WRONG, and that is the lesson in it. The
    first version asserted the escapes and the byte count and never the `blob`
    KEYWORD, so it agreed with an argument that said `to = "\ee\cc..."` -- which
    candid reads as TEXT. dfx refused it on the live replica with "Not valid unicode
    text", after the test had reported the property in its own name.

    A test named for a property that does not assert the property is worse than no
    test, because the name is what a reader trusts. The keyword is asserted first
    here, before anything else about the literal.
    """
    log: list = []
    a = sending_adapter(log=log)
    destination = a.deposit_address(1)
    a.send_to_address(destination, 0.5, created_at_time_nanos=FIXED_NANOS)
    sent = log[1][2]
    expected = "".join(f"\\{b:02x}" for b in bytes.fromhex(destination))
    assert f'to = blob "{expected}"' in sent, (
        "the destination must carry the `blob` keyword -- a bare quoted string is candid `text` "
        "and dfx refuses 32 arbitrary bytes as UTF-8"
    )
    assert destination not in sent, "the 64-hex text must not appear; the blob does"
    assert sent.count("\\") == 32, "32 bytes, each escaped"


def test_the_amount_and_the_timestamp_are_carried_exactly():
    log: list = []
    a = sending_adapter(log=log)
    assert a.send_to_address(a.deposit_address(1), 0.5, created_at_time_nanos=FIXED_NANOS) == "42"
    sent = log[1][2]
    assert "amount = record { e8s = 50000000 : nat64 }" in sent
    assert f"created_at_time = opt record {{ timestamp_nanos = {FIXED_NANOS} : nat64 }}" in sent
    assert "from_subaccount = null" in sent, "payouts leave the desk's default account"


@pytest.mark.parametrize("reply,needle", [
    ("(variant { Err = variant { BadFee = record { expected_fee = record { e8s = 10_000 : nat64 } } } })", "BadFee"),
    ("(variant { Err = variant { InsufficientFunds = record { balance = record { e8s = 1 : nat64 } } } })", "NOT established"),
    ("(variant { Err = variant { TxDuplicate = record { duplicate_of = 7 : nat64 } } })", "TxDuplicate"),
    ("(variant { Err = variant { TxTooOld = record { allowed_window_nanos = 86_400_000_000_000 : nat64 } } })", "24h"),
    ("", "NOT established"),
])
def test_anything_but_a_block_index_raises_and_does_not_claim_nothing_moved(reply, needle):
    """A failed send must not assert that no funds moved, because it does not know.

    TxDuplicate in particular means an earlier identical transfer DID happen and the
    reply carries its block index. A message saying "nothing was sent" there would
    be false, and a BadFee is deliberately NOT retried at the expected figure --
    a retry is a second send, and this method must never make two where one was
    asked for.
    """
    a = sending_adapter(transfer_reply=reply)
    with pytest.raises(ICPCallFailed, match=needle):
        a.send_to_address(a.deposit_address(1), 0.5, created_at_time_nanos=FIXED_NANOS)


def test_a_payout_to_an_invalid_address_refuses_before_reading_the_fee():
    log: list = []
    a = sending_adapter(log=log)
    with pytest.raises(ICPCallFailed, match="NOTHING was sent"):
        a.send_to_address(OWNER, 0.5, created_at_time_nanos=FIXED_NANOS)
    assert log == []


# ---------------------------------------------------------------------------
# The ledger's own derivation, as a cross-check on ours
# ---------------------------------------------------------------------------


def ledger_blob(hex_text: str) -> str:
    return '(blob "' + "".join(f"\\{b:02x}" for b in bytes.fromhex(hex_text)) + '")'


def test_the_ledger_and_this_repository_derive_the_same_account():
    """The one assumption under every ICP address this terminal publishes.

    `account_identifier : (Account) -> (AccountIdentifier) query` makes the ledger do
    the derivation itself, so agreement is checkable for one query call. A
    disagreement would mean a customer pays an address the ledger credits to
    something else, and the watcher polls an account that stays at zero forever.
    """
    a = adapter({"account_identifier": ledger_blob(account_identifier(OWNER))})
    agrees, why = a.verify_derivation()
    assert agrees is True
    assert account_identifier(OWNER) in why


def test_a_disagreement_is_reported_as_total_rather_than_as_one_bad_address():
    """Both sides are printed, and the message says every address is wrong."""
    a = adapter({"account_identifier": ledger_blob(account_identifier(OWNER, subaccount_from_index(9)))})
    agrees, why = a.verify_derivation()
    assert agrees is False
    assert "DISAGREEMENT" in why
    assert "wrong" in why and "do not send" in why


def test_a_blob_this_adapter_cannot_read_raises_rather_than_returning_empty():
    a = adapter({"account_identifier": "(variant { Err = 1 })"})
    with pytest.raises(ICPCallFailed, match="not empty"):
        a.ledger_account_identifier()
