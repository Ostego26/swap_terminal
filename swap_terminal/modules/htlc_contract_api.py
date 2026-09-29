#!/usr/bin/env python3
"""The one way to call a chain client's create_contract(). Keywords, always.

Role: submodule (a decision -- how to address three clients that disagree)
Reads: nothing. It builds a dict.
Writes: nothing
Can move funds: no. The CALLER funds; this only shapes the arguments.
Mainnet-safe: yes

THE THREE CLIENTS DISAGREE ABOUT THEIR OWN SIGNATURE, and the disagreement is
not cosmetic:

    BTCClient.create_contract(amount_btc, secret_hash, participant_address,
                              refund_address, locktime)
    GRCClient.create_contract(amount_grc, secret_hash, participant_address,
                              refund_address, locktime)
    LTCClient.create_contract(amount_ltc, participant_address, refund_address,
                              locktime, secret_hash=None)

LTC takes its parameters in a different ORDER and its secret_hash LAST WITH A
DEFAULT OF None. So a positional call written against BTC's order hands LTC the
secret hash as its participant address, the claim address as its refund address,
the refund address as its locktime -- and no secret hash at all.

MEASURED, ON A --run THAT HAD ALREADY FUNDED THE XRP LEG, 2026-09-29:

    step 7/10  B funds the LTC leg: 0.02226164 LTC, same hash, expiring FIRST
    FAIL  the LTC leg: got=TypeError: '<=' not supported between instances of 'str' and 'int'

That TypeError is the refund address arriving where a locktime belongs. IT IS
ALSO THE ONLY THING THAT STOPPED IT: every other misrouted argument was a string
going where a string was expected. Had the types lined up, the call would have
built and FUNDED an HTLC whose hashlock was absent (secret_hash defaulting to
None), whose claim branch paid an address derived from a hash, and whose refund
branch paid the counterparty's claim key. On a real swap that is coins sent to a
contract nobody can open by the intended route.

modules/script_leg.py was written on 2026-09-29 against the GRC client, run
against GRC the same day, and its positional call was correct for two of the
three chains it advertises.

TWO TABLES OF THIS ALREADY EXISTED and neither was reachable from the third
caller: atomic_swapper._AMOUNT_KWARG and atomic_swap.AMOUNT_KEYWORD, both
mapping the same three assets to the same three keywords, both with a comment
explaining the divergence. atomic_swapper's even says out loud what would happen
otherwise -- "passing positionally in the BTC/GRC order would hand LTC the secret
hash as its participant address" -- which is exactly the bug, written down, in a
file the code that hit it does not import. That is rule 8's complaint in its
purest form: the knowledge was in the tree and the defect shipped anyway, because
nothing pointed from one copy to the place that needed it.

So: ONE table, ONE function that builds the call, and every caller goes through
it. Normalizing the three clients' signatures would be better still and is NOT
done here -- it changes the public shape of three modules with four callers on a
live-money tree, and the argument for it belongs to the operator (rule 16). What
this removes is the ability to get the call wrong.
"""

from __future__ import annotations

#: Which keyword each client takes its amount under. The divergence is real and
#: predates every caller; it is mapped here so adding a chain is a row rather
#: than an `if` at each call site.
AMOUNT_KEYWORD = {"BTC": "amount_btc", "LTC": "amount_ltc", "GRC": "amount_grc"}


class UnknownContractChain(KeyError):
    """A chain with no create_contract() keyword mapping."""


def create_contract_kwargs(chain: str, *, amount, secret_hash: str,  # noqa: PLR0913 -- checked: these six ARE the contract. Every one is a distinct fact the chain needs and none is derivable from another, so bundling them into an object would add a type without removing a parameter -- and would let the whole contract be built positionally in one place instead of five, which is the failure this function exists to remove. Same judgment recorded at chains/xrp.py:735 and chains/base.py:68. PLR0917 is deliberately NOT suppressed beside it: it does not fire, because every parameter after `chain` is keyword-only, and that is the property the safety rests on.
                           participant_address: str, refund_address: str,
                           locktime: int) -> dict:
    """Every argument create_contract() needs, by keyword, for this chain.

    KEYWORD-ONLY ON THIS SIDE TOO, deliberately. A helper whose own arguments can
    be passed positionally would re-create the bug it exists to prevent, one
    level up -- participant_address and refund_address are both strings and
    swapping them builds a contract whose branches are exchanged, which no type
    error would catch and which the chain would happily fund.

    secret_hash is REQUIRED here even though LTC's own signature defaults it to
    None. A hashlock is not optional in an atomic swap: without it the contract
    is a timelocked gift. The default exists in that client for its own reasons
    and must never be reached through this path.
    """
    if chain not in AMOUNT_KEYWORD:
        raise UnknownContractChain(
            f"{chain} has no create_contract() amount keyword (known: "
            f"{', '.join(sorted(AMOUNT_KEYWORD))}). Add a row rather than calling positionally: "
            f"the three clients take their parameters in different orders and a positional call "
            f"written for one silently misroutes on another."
        )
    if not secret_hash:
        raise ValueError(
            f"{chain}: create_contract() was asked for with no secret hash. An HTLC without a "
            f"hashlock is a timelocked gift -- anyone may take it at expiry and the counterparty's "
            f"leg is not bound to it. LTCClient defaults secret_hash to None; this path never lets "
            f"that default be reached."
        )
    return {
        AMOUNT_KEYWORD[chain]: amount,
        "secret_hash": secret_hash,
        "participant_address": participant_address,
        "refund_address": refund_address,
        "locktime": locktime,
    }
