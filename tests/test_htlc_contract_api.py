#!/usr/bin/env python3
"""The kwargs bind to every REAL client, not to a stub's idea of one.

Role: tests (read-only)
Reads: the three client classes' signatures, by inspection. It CONSTRUCTS no
        client, opens no socket, and calls no method -- Signature.bind() checks
        that a call WOULD be accepted without making it.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHAT THIS WOULD HAVE CAUGHT, and what did not.

modules/script_leg.py called create_contract() positionally in the BTC/GRC
order. LTCClient takes its parameters in a DIFFERENT order, with secret_hash
last and defaulting to None. So the call handed LTC:

    participant_address  <- the secret hash
    refund_address       <- the claim address
    locktime             <- the refund address (a str)
    secret_hash          <- nothing at all, defaulting to None

It surfaced 2026-09-29 as

    TypeError: '<=' not supported between instances of 'str' and 'int'

on a --run that had ALREADY FUNDED THE XRP LEG. That TypeError is the refund
address landing where a locktime belongs, and it is the only thing that stopped
the call: every other misrouted argument was a string going where a string was
expected. With luckier types this would have built and funded an HTLC with NO
HASHLOCK, whose branches paid the wrong keys.

Fifteen tests covered script_leg and none of them saw it, because the test
double took its arguments positionally in the same wrong order -- so it accepted
the call and recorded what the call MEANT. A double that accepts what the real
thing rejects is a test asserting on a conversation that never happens. This
file binds against the real classes instead.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest
from modules.htlc_assets import script_client_classes
from modules.htlc_contract_api import (
    AMOUNT_KEYWORD,
    UnknownContractChain,
    create_contract_kwargs,
)
from source_tree import is_source

# THE THIRD COPY OF THIS DICT, UNTIL 2026-10-03. It was
# `{"BTC": BTCClient, "LTC": LTCClient, "GRC": GRCClient}` here, the same dict in a
# different key order at atomic_swap.py:148, and a third at atomic_swap_xrp.py:1224
# under a comment claiming the map was "imported rather than re-implemented". A test
# fixture is not exempt from rule 8: a test carrying its own copy of the vocabulary is a
# test that keeps passing on the day a fourth chain is added to the two real ones and
# not to it, which is precisely the chain nobody would have checked this signature for.
CLIENTS = script_client_classes()

ARGUMENTS = {
    "amount": 1,
    "secret_hash": "ab" * 32,
    "participant_address": "mParticipant",
    "refund_address": "mRefund",
    "locktime": 900,
}


@pytest.mark.parametrize("chain", sorted(CLIENTS))
def test_the_kwargs_BIND_to_every_real_clients_signature(chain):
    """Signature.bind() raises TypeError if the call would not be accepted.

    Nothing is constructed and nothing is called -- this asks the class what it
    would take. That is the whole difference between this and the stub-based
    tests that missed the defect.

    MUTATION: drop "secret_hash" from create_contract_kwargs' returned dict and
    BTC and GRC fail here, because both declare it as a required parameter; LTC
    does NOT fail, because its signature defaults it to None. That asymmetry is
    exactly why a missing hashlock is invisible on the one chain it can reach.
    Verified 2026-09-29.
    """
    kwargs = create_contract_kwargs(chain, **ARGUMENTS)
    signature = inspect.signature(CLIENTS[chain].create_contract)
    # `self` is supplied by the bound method at the real call site.
    signature.bind(object(), **kwargs)


@pytest.mark.parametrize("chain", sorted(CLIENTS))
def test_the_amount_keyword_is_the_one_that_clients_signature_actually_declares(chain):
    """The table is checked against the code rather than trusted.

    AMOUNT_KEYWORD existed in TWO other files before this one, both correct on
    the day they were written. A table that agrees with the signature by
    coincidence is rule 8's drift waiting to happen; this asserts the agreement.
    """
    parameters = inspect.signature(CLIENTS[chain].create_contract).parameters
    assert AMOUNT_KEYWORD[chain] in parameters, (
        f"{CLIENTS[chain].__name__}.create_contract() has no parameter named "
        f"{AMOUNT_KEYWORD[chain]!r}; it declares {sorted(parameters)}"
    )


def test_the_POSITIONAL_call_that_shipped_would_NOT_bind_to_LTC():
    """The defect itself, pinned so it cannot come back as a refactor.

    This is what modules/script_leg.py did until 2026-09-29. It binds fine to BTC
    and GRC -- which is why it ran on Gridcoin the day it was written -- and
    misroutes every argument on LTC.
    """
    positional = (ARGUMENTS["amount"], ARGUMENTS["secret_hash"],
                  ARGUMENTS["participant_address"], ARGUMENTS["refund_address"],
                  ARGUMENTS["locktime"])
    for chain in ("BTC", "GRC"):
        inspect.signature(CLIENTS[chain].create_contract).bind(object(), *positional)

    bound = inspect.signature(CLIENTS["LTC"].create_contract).bind(object(), *positional)
    # It BINDS -- five positional parameters accept five positional arguments --
    # and every one of them lands in the wrong place. That is the finding: arity
    # is not the check, and a stub with the right arity proves nothing.
    assert bound.arguments["participant_address"] == ARGUMENTS["secret_hash"], (
        "LTC's parameter order has changed. If the three clients now agree, this test has "
        "served its purpose and the divergence table can go with it (rule 19: a ratchet that "
        "reaches zero gets deleted)."
    )
    assert bound.arguments["locktime"] == ARGUMENTS["refund_address"]
    # AND THE SECRET HASH RECEIVES THE LOCKTIME. I wrote this assertion as
    # `"secret_hash" not in bound.arguments` -- expecting the default of None --
    # and the bind refuted it: all five positional arguments land, so secret_hash
    # gets the integer 900. That is worse than None. A hashlock built from an
    # arbitrary integer commits to a preimage NOBODY HOLDS, so the funded contract
    # could never be claimed by anyone, on either branch, ever -- the coins would
    # wait for a timelock that is itself a string. Rule 17: the claim was written
    # before it was bound, and binding it said otherwise.
    assert bound.arguments["secret_hash"] == ARGUMENTS["locktime"], (
        "LTC's parameter order has changed and this test's account of the defect is now wrong"
    )


def test_an_HTLC_WITHOUT_A_HASHLOCK_is_refused_rather_than_defaulted():
    """LTCClient defaults secret_hash to None. This path never lets that be reached.

    A contract with no hashlock is a timelocked gift: anyone may take it at
    expiry and the counterparty's leg is not bound to it. That the client permits
    it for its own reasons does not make it reachable through a swap.

    MUTATION: drop the `if not secret_hash` guard and this fails. Verified
    2026-09-29.
    """
    with pytest.raises(ValueError) as raised:
        create_contract_kwargs("LTC", **{**ARGUMENTS, "secret_hash": ""})
    assert "timelocked gift" in str(raised.value)


def test_an_unknown_chain_says_WHY_positional_is_not_the_answer():
    """The next person's instinct on a KeyError here is to call positionally."""
    with pytest.raises(UnknownContractChain) as raised:
        create_contract_kwargs("DOGE", **ARGUMENTS)
    assert "different orders" in str(raised.value)


def test_the_helpers_OWN_arguments_are_keyword_only():
    """Or it re-creates the bug one level up.

    participant_address and refund_address are both strings. Swapping them builds
    a contract whose branches are exchanged -- no type error, and the chain funds
    it happily.
    """
    parameters = inspect.signature(create_contract_kwargs).parameters
    positional = [name for name, p in parameters.items()
                  if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD]
    assert positional == ["chain"], (
        f"{positional} can be passed positionally. Only `chain` may be: the other five include two "
        f"interchangeable address strings, and swapping those is a funded contract with its "
        f"branches exchanged"
    )


def test_the_amount_keyword_table_has_exactly_ONE_definition_in_the_tree():
    """FOUR copies existed on 2026-09-29 and the defect landed in the gap.

        atomic_swap.AMOUNT_KEYWORD          BTC LTC GRC
        atomic_swapper._AMOUNT_KWARG        BTC LTC GRC
        regtest/steps.py, inline            BTC LTC        <- partial
        modules/script_leg.py               none: it called POSITIONALLY

    Every copy was correct where it stood. atomic_swapper's even carried the
    comment "passing positionally in the BTC/GRC order would hand LTC the secret
    hash as its participant address" -- the exact bug, written down, in a file
    the code that hit it does not import. The knowledge was in the tree; what was
    missing was a way to reach it.

    The partial one is its own hazard: regtest/steps.py knew BTC and LTC and not
    GRC, so that harness pointed at Gridcoin would have raised KeyError on the
    asset rather than building a call.

    MUTATION: re-inline the dict anywhere and this fails naming the file.
    Verified 2026-09-29.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    spellings = []
    # `is_source` RATHER THAN THE TWO NAMES THIS LINE USED TO TEST, 2026-10-10. A git
    # worktree at `.claude/worktrees/agent-<id>/` is a complete second copy of the tree,
    # so this sweep reported the one definition below as TWO and said so in those words:
    # "the amount-keyword table is spelled in ['.claude/worktrees/.../htlc_contract_api.py:64',
    # 'swap_terminal/modules/htlc_contract_api.py:64']". A uniqueness assertion over a tree
    # that contains a copy of itself cannot hold. tests/source_tree.py has the measurement.
    for path in sorted(root.rglob("*.py")):
        if not is_source(path) or path.parent.name == "tests":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if '"amount_btc"' in line and not line.lstrip().startswith("#"):
                spellings.append(f"{path.relative_to(root)}:{number}")
    assert spellings == ["swap_terminal/modules/htlc_contract_api.py:64"], (
        f"the amount-keyword table is spelled in {spellings}. One definition, imported -- four "
        f"copies is how a fifth caller came to pass positionally instead, and that call misroutes "
        f"every argument on LTC"
    )
