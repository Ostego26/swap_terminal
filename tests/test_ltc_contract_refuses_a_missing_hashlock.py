"""LTCClient.create_contract() says "no hashlock" rather than dying inside a push encoder.

Role: test (one refusal, called with seeded inputs; nothing is built and nothing is sent)
Reads: swap_terminal/modules/atomic_ltc_client.py
Writes: nothing
Can move funds: no. The refusal under test is the FIRST statement in create_contract(),
        three steps before the sendtoaddress, and the client here is constructed with a
        loopback URL that is never contacted because nothing gets that far.
Mainnet-safe: yes

WHAT THIS PINS, AND WHAT IT DELIBERATELY DOES NOT CHANGE.

The LTC client is the only one of the three whose create_contract() defaults
`secret_hash` to None -- the divergence table in its own module header records it, row
"secret_hash required?: yes / NO, defaults None / yes". That default STAYS. The
application never reaches it: modules/htlc_contract_api.create_contract_kwargs() makes
secret_hash keyword-only and required and refuses a falsy one with the sentence that
matters, "an HTLC without a hashlock is a timelocked gift".

What was missing was the refusal on a DIRECT call. Measured 2026-10-09 by calling it:
with secret_hash=None, build_htlc_redeem_script() reached push_data(None) and raised

    TypeError: object of type 'NoneType' has no len()

five frames from the cause, naming a push encoder instead of the hashlock. Nothing was
broadcast then and nothing is broadcast now -- the only thing that changed is which
sentence the caller gets, which is the whole content of rule 14 applied to an exception.

Rule 8 is why the refusal is asserted to NAME the thing: there are now two copies of this
guard, this one and create_contract_kwargs()'s, and a reader who finds one has to be able
to tell it is the same rule.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from modules.atomic_ltc_client import LTCClient
from modules.htlc_contract_api import create_contract_kwargs
from valid_addresses import LTC_PARTICIPANT, LTC_REFUND

# Not credentials. A loopback URL on a port nothing listens on, and two fixture strings,
# so that LTCClient.__init__'s own "Missing LTC RPC credentials" refusal is not what this
# test measures. No socket is opened: the refusal under test precedes every RPC.
PROBE_URL = "http://127.0.0.1:1"
PROBE_RPC_USER = "fixture-rpc-user"
PROBE_RPC_AUTH = "fixture-rpc-auth-value"

# DERIVED, NOT SPELLED, and the gate that says so is a real one. A first draft of this file
# wrote two `tltc1...` literals and tests/test_address_literals_are_valid.py's
# test_the_literal_count_does_not_climb_back went from 60 to 62 over its ceiling of 60 --
# which is the correct outcome and rule 19's exact case: "never add a baseline line for code
# you are writing now; if your change needs a new entry to pass, your change is the defect."
#
# tests/valid_addresses.py already carries these two, derived from a described purpose, so
# they cannot be mistyped and they say what they are for. They must also be DISTINCT, because
# build_htlc_redeem_script() refuses a script whose two branches hash to one key -- a refusal
# that would otherwise mask the one this file is about.
PARTICIPANT = LTC_PARTICIPANT
REFUND = LTC_REFUND
LOCKTIME = 3_298_078


def _client() -> LTCClient:
    return LTCClient(PROBE_URL, PROBE_RPC_USER, PROBE_RPC_AUTH)


@pytest.mark.parametrize("missing", [None, ""])
def test_a_contract_asked_for_without_a_hashlock_is_refused_by_name(missing):
    """ValueError naming the hashlock, not a TypeError from the push encoder.

    Both shapes of missing, because the default is None and an empty string is what a
    caller reading a blank environment variable would pass. `if not secret_hash` covers
    both, and asserting only None would let a fix that checked `is None` pass while the
    empty string still reached push_data.
    """
    with pytest.raises(ValueError, match="no secret hash") as caught:
        _client().create_contract(
            amount_ltc=Decimal("0.1"),
            participant_address=PARTICIPANT,
            refund_address=REFUND,
            locktime=LOCKTIME,
            secret_hash=missing,
        )
    message = str(caught.value)
    assert "timelocked gift" in message, (
        f"the refusal has to say WHY, which is the sentence create_contract_kwargs() "
        f"already uses for the same rule: {message}"
    )
    assert "create_contract_kwargs" in message, (
        f"rule 8: the other copy of this guard must be named here, or a reader who finds "
        f"this one cannot know the application path is already covered: {message}"
    )


def test_a_contract_with_a_hashlock_gets_past_the_refusal():
    """The guard must not be the whole function.

    It builds the real redeem script and then fails at the first RPC -- which is the
    proof that the refusal above is about the hashlock and not about everything.
    `decodescript` is the first call create_contract() makes, and nothing listens on
    port 1, so the error is a transport error rather than a ValueError.
    """
    # Not a credential: a placeholder SHA-256 DIGEST. No preimage hashes to it.
    secret_hash = "ff" * 32
    # Exception rather than a narrow class on purpose, and NOT suppressed: ruff's B017 does
    # not fire because the raise is bound and asserted on below, which is the property B017
    # exists to require. What this test says is that the failure is NOT the hashlock refusal;
    # which transport exception requests raises against a closed port is not its subject.
    with pytest.raises(Exception) as caught:
        _client().create_contract(
            amount_ltc=Decimal("0.1"),
            participant_address=PARTICIPANT,
            refund_address=REFUND,
            locktime=LOCKTIME,
            secret_hash=secret_hash,
        )
    assert "no secret hash" not in str(caught.value), (
        "a contract WITH a hashlock must get past the hashlock guard"
    )


def test_the_other_copy_of_this_rule_still_refuses_too():
    """create_contract_kwargs() is the path the application uses, and it guards first.

    Asserted here rather than taken on trust, because this file's whole argument for
    leaving the LTC default in place is that this second guard covers the live path. If
    it ever stopped, the argument would be gone and only this test would say so.
    """
    with pytest.raises(ValueError, match="timelocked gift"):
        create_contract_kwargs(
            "LTC",
            amount="0.1",
            secret_hash="",
            participant_address=PARTICIPANT,
            refund_address=REFUND,
            locktime=LOCKTIME,
        )
