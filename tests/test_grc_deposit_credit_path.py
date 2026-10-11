"""GRC as the DEPOSIT side: which scriptPubKey spelling matches, and on which route.

Role: test (read-only; no socket, no subprocess, no chain, no database)
Reads: swap_terminal/chains/gridcoin.py, chains/base.py, script_pub_key.py
Writes: nothing
Can move funds: no -- the adapter is the REAL GridcoinAdapter with its `call`
      replaced by a seeded script. Nothing opens a socket and no wallet method
      is reachable.
Mainnet-safe: yes

=============================================================================
WHY GRIDCOIN NEEDS ITS OWN COPY OF THE VOUT-MATCHING MEASUREMENT
=============================================================================

tests/test_deposit_vout_matching.py already covers both field shapes, and it
covers them against a bare stub class. That is the right test for
script_pub_key.py and it is NOT the question asked here, which is whether the
GRIDCOIN adapter -- the one `chains/registry.ADAPTERS["GRC"]` builds, with
`asset = "GRC"` and nothing else -- reaches that shared decision at all.

The distinction is not academic on this chain. chains/base.py has TWO roads to
a transaction's outputs and they were added for different daemons:

    _raw_tx_for_vouts()          `getrawtransaction txid True`, direct
    _vouts_through_the_wallet()  `gettransaction` then decoderawtransaction or
                                 `getrawtransaction txid True <blockhash>`

and that second road CANNOT EXIST ON GRIDCOIN in one of its two forms. Measured
on the operator's Gridcoin testnet daemon 2026-09-27 and recorded in
docs/atomic_swap_runs_2026_09_27.md:235 --

    getrawtransaction            getrawtransaction <txid> [verbose=bool]
    gettransaction               gettransaction "txid" ( includeWatchonly )

TWO parameters. The block-hash argument arrived in Bitcoin Core 0.16, and
Gridcoin is on the pre-0.17 RPC surface: it has neither `getaddressinfo` nor
`gettxout` (both measured on that daemon, rpc -32601). So the GRC assertions
below are about which road is taken as much as about what is found on it.

WHICH SPELLING gridcoinresearchd ACTUALLY RETURNS IS NOT ESTABLISHED HERE, and
saying so is the point (rule 17). No Gridcoin daemon is reachable from the
container these tests were written in, `gridcoinresearchd` is not installed,
and no GRC `scriptPubKey` key list appears anywhere in this repository's records
-- grepped 2026-10-11 across every .py, .md and .json: the only recorded key
lists are Bitcoin Core 28.1.0's and Litecoin Core 0.21.4's.

What a reader can carry instead of a guess: `address` (singular) replaced
`addresses` (plural) in Bitcoin Core 0.20 and the plural was REMOVED in 22.0,
both of which are far later than the 0.17 surface Gridcoin was measured to be
on -- so the plural is the likely shape. THAT IS A HYPOTHESIS, it is labeled
one, and these tests are built so that it does not matter: the adapter is
driven with each shape in turn and must find the same output either way.

A TEST THAT PINNED ONLY THE LIKELY SHAPE WOULD BE THE DEFECT IT IS TESTING FOR.
The original bug was one field name read and the other ignored; a test that
picked one field name would pass with half the fix in place.
"""

from __future__ import annotations

import pytest
import valid_addresses

# THE CREDENTIAL NAMES COME FROM conftest RATHER THAN BEING SPELLED HERE, which is
# ruff's S106 answered rather than suppressed (rule 19: a noqa is a claim you checked).
# chains/registry.build_adapters() requires a non-empty RPC user and password for a
# Bitcoin-derived chain, so a fixture adapter has to supply both, and conftest's two
# constants are named to say they are fixtures and not secrets -- the same move
# tests/test_gridcoin_wallet_lock.py makes with LEAK_SENTINEL. No test in this file
# opens a socket, so neither value is ever sent anywhere.
from chains.base import RPCAdapter, RPCError
from chains.gridcoin import GridcoinAdapter
from conftest import RPC_FIXTURE_AUTH, RPC_FIXTURE_USER

#: The desk's own GRC deposit address, derived rather than typed. Its ROLE matters:
#: tests/valid_addresses.py keeps the deposit and payout fixtures separate so a test
#: cannot accidentally assert a guard against itself.
DEPOSIT = valid_addresses.GRC_DESK_DEPOSIT

#: Somebody else's GRC address, for the change output every real `sendtoaddress`
#: produces. Four of the five atomic swaps on 2026-09-27 landed the contract at vout
#: 1 and not 0 (docs/atomic_swap_runs_2026_09_27.md, "ONE VOUT WAS NEVER 0 BY
#: DEFAULT"), so a deposit sharing its transaction with a change output at index 0 is
#: the ordinary case rather than an edge one.
SOMEBODY_ELSE = valid_addresses.GRC_SECOND_PAYOUT

TXID = "ec793912564f9f09" + "0" * 48
AMOUNT = 310.72
#: The P2PKH scriptPubKey hex is carried on every output because a real daemon
#: carries it, and because chains/base.py matching on the ADDRESS rather than the hex
#: is the fallback road -- a fixture with no hex could not tell the two apart.
HEX = "76a914" + "11" * 20 + "88ac"


class SeededGridcoin(GridcoinAdapter):
    """The REAL GridcoinAdapter with one seeded `call`. Nothing else is overridden.

    SUBCLASSING RATHER THAN DUCK-TYPING, deliberately: the thing under test is
    `find_deposits_to_address` and `_extract_matching_vouts` AS THE GRC ADAPTER
    INHERITS THEM, and a hand-written stub with the same method names would pass
    even if chains/gridcoin.py overrode one of them tomorrow.

    `asset` is NOT set here. It comes from GridcoinAdapter, so a test that reads it
    back is reading the production value.
    """

    def __init__(self, answers: dict):
        super().__init__(user=RPC_FIXTURE_USER, password=RPC_FIXTURE_AUTH,
                         host="127.0.0.1", port=25715)
        self.answers = answers
        self.calls: list[str] = []

    def call(self, method: str, *params):
        self.calls.append(method)
        if method not in self.answers:
            raise AssertionError(
                f"the GRC adapter called {method!r}, which this fixture does not seed. "
                f"Seeded: {sorted(self.answers)}"
            )
        answer = self.answers[method]
        if isinstance(answer, Exception):
            raise answer
        return answer(*params) if callable(answer) else answer


def wallet_listing(amount: float = AMOUNT) -> list[dict]:
    """One `listtransactions` receive, the shape chains/base.py filters on."""
    return [{"category": "receive", "address": DEPOSIT, "amount": amount, "txid": TXID}]


def two_outputs(deposit_script_pub_key: dict) -> dict:
    """A funding transaction whose change is at vout 0 and the deposit at vout 1."""
    return {
        "confirmations": 7,
        "vout": [
            {"n": 0, "value": 12.5, "scriptPubKey": {"addresses": [SOMEBODY_ELSE], "hex": HEX}},
            {"n": 1, "value": AMOUNT, "scriptPubKey": deposit_script_pub_key},
        ],
    }


#: The two shapes, named by the daemon that produces each, so a failure says which.
#:
#: `reqSigs` and `type` ride along on the plural shape and `desc` on the singular one
#: because the real answers carry them, and a fixture reduced to the one key under
#: test cannot catch a matcher that reads a key it should not.
SCRIPT_PUB_KEY_SHAPES = {
    "addresses-plural (Core <=0.19, Litecoin 0.21.4, and the shape Gridcoin's pre-0.17 "
    "surface is expected to return -- HYPOTHESIS, see the module docstring)": {
        "addresses": [DEPOSIT], "reqSigs": 1, "type": "pubkeyhash", "hex": HEX,
        "asm": f"OP_DUP OP_HASH160 {'11' * 20} OP_EQUALVERIFY OP_CHECKSIG",
    },
    "address-singular (Core 22.0 and later)": {
        "address": DEPOSIT, "type": "pubkeyhash", "hex": HEX,
        "asm": f"OP_DUP OP_HASH160 {'11' * 20} OP_EQUALVERIFY OP_CHECKSIG",
        "desc": "pkh([00000000]0200)#checksum",
    },
}


@pytest.mark.parametrize("shape", list(SCRIPT_PUB_KEY_SHAPES), ids=lambda name: name.split()[0])
def test_the_grc_adapter_finds_the_real_vout_under_either_spelling(shape):
    """THE MEASUREMENT. vout 1 must be recorded as vout 1, whichever field names it.

    MUTATION, run 2026-10-11: in swap_terminal/script_pub_key.addresses_of(), delete
    the `addresses` (plural) branch -- the four lines from `plural =` to the
    `found.extend(...)`. The plural case then fails with vout 0 and the fabricated
    amount, and the singular case still passes. Deleting the `address` (singular)
    branch instead inverts which one fails. Both halves confirmed, which is what
    makes this a test of the union rather than of whichever field the fixture happens
    to prefer.
    """
    adapter = SeededGridcoin({
        "listtransactions": wallet_listing(),
        "getrawtransaction": two_outputs(SCRIPT_PUB_KEY_SHAPES[shape]),
    })

    found = adapter.find_deposits_to_address(DEPOSIT)

    assert len(found) == 1, f"{shape}: expected exactly one deposit event, got {found}"
    event = found[0]
    assert event["vout"] == 1, (
        f"{shape}: the deposit is at output 1 and was recorded at output {event['vout']}. "
        f"vout 0 is the value chains.base.fabricated_deposit_events() INVENTS, so this is the "
        f"fabricated fallback reading as a real event"
    )
    assert event["amount"] == AMOUNT
    assert event["confirmations"] == 7
    assert event["address"] == DEPOSIT


def test_gridcoin_takes_the_direct_route_and_never_needs_the_wallet_fallback():
    """GRC must be answered by `getrawtransaction` alone -- two calls, no third.

    WHY THIS IS AN ASSERTION AND NOT A PERFORMANCE NOTE. The wallet fallback's second
    form passes a BLOCK HASH as `getrawtransaction`'s third argument, and Gridcoin's
    `getrawtransaction` takes TWO parameters -- measured on the operator's daemon
    2026-09-27, docs/atomic_swap_runs_2026_09_27.md:235. So if GRC ever started
    reaching that road, one of its two branches would call a signature the daemon does
    not have, and chains/base.vout_read_route()'s own docstring says as much: "GRC does
    not reach this function today -- see _raw_tx_for_vouts".

    That sentence is an invariant nothing was holding. This holds it.

    MUTATION, run 2026-10-11: make the seeded `getrawtransaction` raise
    chains.base.RPCError (the -5 a node without -txindex answers) and this test fails
    on the call list, having taken `gettransaction` -- which is precisely the
    regression it exists to catch.
    """
    adapter = SeededGridcoin({
        "listtransactions": wallet_listing(),
        "getrawtransaction": two_outputs(SCRIPT_PUB_KEY_SHAPES[
            "address-singular (Core 22.0 and later)"]),
    })

    adapter.find_deposits_to_address(DEPOSIT)

    assert adapter.calls == ["listtransactions", "getrawtransaction"], (
        f"GRC read a deposit through {adapter.calls}. Gridcoin indexes every transaction "
        f"(measured 2026-09-27: route 4 of modules/htlc_rpc.lookup_contract_output() answered "
        f"for a CONFIRMED GRC contract on a daemon with no txindex setting), so the direct "
        f"call must answer and the wallet fallback must not be reached"
    )


def test_the_listtransactions_retry_is_what_a_pre_0_17_daemon_needs():
    """The 5-argument `listtransactions` must fall back to the 2-argument form.

    GRIDCOIN IS THE CHAIN THIS ARM EXISTS FOR, even though chains/base.py does not say
    so. `listtransactions "*" <count> <from> <include_watchonly>` is four arguments
    plus the account wildcard; `include_watchonly` arrived in Bitcoin Core 0.10 and the
    wildcard account in 0.9, and a pre-0.17 fork is exactly where that signature is
    least safe to assume. The retry is unwrapped on purpose -- chains/base.py's comment
    says a second failure must propagate rather than become an empty list that reads as
    "no deposit has arrived".

    NOT A CLAIM THAT GRIDCOIN REFUSES THE LONG FORM (rule 17). No daemon was asked.
    What is asserted is that the adapter survives a daemon that does, and still finds
    the deposit at its real vout rather than fabricating one -- which is the outcome
    that would otherwise be indistinguishable from a working poll.

    MUTATION, run 2026-10-11: remove the try/except around the first
    `listtransactions` in chains/base.find_deposits_to_address() and this test fails
    with the seeded RPCError propagating out of find_deposits_to_address().
    """
    long_form_calls: list[int] = []

    def listtransactions(*params):
        long_form_calls.append(len(params))
        if len(params) > 2:
            raise RPCError("Error: Invalid parameter count (rpc code -1)")
        return wallet_listing()

    adapter = SeededGridcoin({
        "listtransactions": listtransactions,
        "getrawtransaction": two_outputs(SCRIPT_PUB_KEY_SHAPES[
            "address-singular (Core 22.0 and later)"]),
    })

    found = adapter.find_deposits_to_address(DEPOSIT)

    assert long_form_calls == [4, 2], (
        f"expected the 4-parameter form then the 2-parameter retry, got {long_form_calls}"
    )
    assert len(found) == 1 and found[0]["vout"] == 1, (
        f"the retry found no real output: {found}. A deposit fabricated at vout 0 after a "
        f"signature refusal is the silent half of this failure"
    )


def test_an_output_that_pays_somebody_else_is_not_credited_to_this_swap():
    """The non-vacuity floor: the matcher must still be able to say NO.

    WITHOUT THIS, EVERY TEST ABOVE WOULD PASS AGAINST A MATCHER THAT RETURNED TRUE for
    any output. That is not hypothetical here: the whole family of defects this file is
    about is a comparison that silently stopped discriminating, and the first version
    of the shared matcher returned an empty list for every output on a Core 28.1 node
    without raising anything.

    THE FABRICATED EVENT IS WHAT IS ASSERTED, not an exception, because that is what
    chains/base.py does and changing it is a fund decision (rule 16). What matters is
    that it carries the INVENTED vout 0 -- so a reader comparing it against the tests
    above can see that "no real output matched" and "the output is at index 0" are
    different outcomes that happen to share a number.
    """
    singular = dict(SCRIPT_PUB_KEY_SHAPES["address-singular (Core 22.0 and later)"])
    singular["address"] = SOMEBODY_ELSE
    adapter = SeededGridcoin({
        "listtransactions": wallet_listing(),
        "getrawtransaction": {
            "confirmations": 7,
            "vout": [{"n": 1, "value": AMOUNT, "scriptPubKey": singular}],
        },
    })

    found = adapter.find_deposits_to_address(DEPOSIT)

    assert len(found) == 1
    assert found[0]["vout"] == 0, (
        "an output paying another address was matched as this swap's deposit, at its own index"
    )


def test_the_adapter_under_test_is_the_production_gridcoin_one():
    """A floor on the fixture itself, because the fixture is the subject.

    Every test above asserts behavior INHERITED from chains/base.py. If
    chains/gridcoin.py ever stopped being an RPCAdapter -- or stopped carrying
    `asset = "GRC"`, which is what reaches deposit_events.asset and therefore which
    swap a credit is attributed to -- the tests above would keep passing while
    measuring a class the terminal no longer uses.
    """
    assert issubclass(GridcoinAdapter, RPCAdapter)
    assert GridcoinAdapter.asset == "GRC"
    assert GridcoinAdapter.can_spend is True, (
        "GRC pays out through the daemon's own wallet; can_spend False would make "
        "chains/registry.why_cannot_pay_out() disable every *->GRC pair"
    )
