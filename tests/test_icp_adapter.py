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


def test_a_payout_refuses_and_names_the_gap_rather_than_sending():
    """send_to_address is NOT wired, and the refusal explains why rather than failing vaguely.

    icrc1_transfer takes an Account -- a principal plus an optional subaccount -- and
    a customer supplies a 64-hex ACCOUNT IDENTIFIER, which is a SHA224 hash that
    cannot be inverted to that pair. The payout needs the ICP ledger's legacy
    `transfer` method. That is a real gap and this asserts it is reported as one.
    """
    log: list = []
    a = adapter(log=log)
    with pytest.raises(ICPCallFailed, match="cannot be inverted"):
        a.send_to_address(a.deposit_address(1), 1.0)
    assert log == [], "a refused payout must not have called the ledger at all"


def test_a_payout_to_an_invalid_address_refuses_before_anything_else():
    log: list = []
    a = adapter(log=log)
    with pytest.raises(ICPCallFailed, match="NOTHING was sent"):
        a.send_to_address(OWNER, 1.0)
    assert log == []
