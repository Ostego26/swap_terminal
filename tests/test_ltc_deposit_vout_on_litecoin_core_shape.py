"""An LTC deposit is credited at its REAL vout, on Litecoin Core 0.21.4's field shape.

Role: test (read-only; no socket, no subprocess, no chain, no database)
Reads: chains/litecoin.py, chains/base.py, script_pub_key.py
Writes: nothing
Can move funds: no. The adapter here subclasses the REAL LitecoinAdapter and
      replaces `call` with a seeded script, so every method under test is the
      production one and nothing leaves the process. No socket is opened.
Mainnet-safe: yes

=============================================================================
THE QUESTION THIS FILE WAS WRITTEN TO ANSWER
=============================================================================

swap_terminal/script_pub_key.py exists because `scriptPubKey.addresses` was
removed in Bitcoin Core 22.0, a Core 28.1 node therefore matched nothing, and
every deposit_events row on the brokered path was written by the FABRICATED
fallback in chains/base.RPCAdapter._extract_matching_vouts() -- vout 0, with
the amount taken from the wallet's own `listtransactions` summary rather than
from the output that actually paid.

Litecoin Core is a different codebase at a different version, and the operator
measured the two shapes side by side on 2026-09-25:

    Bitcoin Core 28.1.0    scriptPubKey keys: ['address', 'asm', 'desc', 'hex', 'type']
    Litecoin Core 0.21.4   scriptPubKey keys: ['addresses', 'asm', 'hex', 'reqSigs', 'type']

So LTC is the chain on the OTHER side of that removal: it returns the plural
field and no singular one. The question is whether LTC can still reach the
fabricated fallback. Measured here, 2026-10-11: it cannot, by either route --
the deposit at output 1 is recorded as output 1, with the value read off that
output.

=============================================================================
WHY THIS IS NOT A SECOND COPY OF tests/test_deposit_vout_matching.py (rule 8)
=============================================================================

That file is the authority for the defect and for script_pub_key.py's
behavior, and it DOES seed Litecoin's plural shape. A reader who finds this
file must be told it exists, so: it is the other one, and the difference is
which object runs.

Every adapter in that file is `StubAdapter(RPCAdapter)` with `asset = "BTC"`,
and its Litecoin-shaped cases pass BTC regtest addresses through it. That
proves the FIELD-SHAPE union works; it does not exercise LitecoinAdapter, and
it cannot notice an LTC-specific regression -- a subclass that overrode
_extract_matching_vouts(), or an address form `pays_address()` compares
differently. This file runs the real LitecoinAdapter over real derived LTC
testnet addresses instead, and asserts the RPC LADDER as well as the event,
because on a node without -txindex the ladder is what keeps the fabricated
branch out of reach.

=============================================================================
NO ADDRESS HERE IS INVENTED
=============================================================================

Every address comes from tests/valid_addresses.py, derived from a described
purpose rather than typed, for the reason that file's own header records: 25
address-shaped literals in this tree once decoded as nothing at all, and two of
them sat beside stubs that modeled a daemon's validity check by a leading
letter, so a fake address and a fake check agreed with each other through 17
passing tests.
"""

from __future__ import annotations

import pytest
from chains.base import RPCError
from chains.litecoin import LitecoinAdapter
from deposit_vout_artifact import SUSPECT_VOUT
from valid_addresses import (
    LTC_P2SH_SCRIPT_ADDRESS2,
    LTC_PARTICIPANT,
    LTC_REFUND,
)

#: The swap's own deposit address, and the output index it is paid at. NOT 0 --
#: SUSPECT_VOUT is 0, so an event at index 0 cannot be told from a fabricated
#: one by looking at the row, which is the whole reason the fixture pays at 1.
DEPOSIT_ADDRESS = LTC_PARTICIPANT
DEPOSIT_VOUT = 1
SOMEBODY_ELSE = LTC_REFUND

#: The operator's own figure, from the LTC swap measured on their host
#: 2026-10-10 (deposit_payers.py records it: "a swap wanted 0.06030068 LTC").
#: A real amount rather than a round one, because the match is `abs(value -
#: amount) < SATOSHI` and a round number hides a rounding defect.
DEPOSIT_AMOUNT = 0.06030068
CHANGE_AMOUNT = 1.0

DEPOSIT_TXID = "cd" * 32
BLOCK_HASH = "ee" * 32

#: What litecoind answers when asked for a CONFIRMED transaction on a node with
#: no -txindex. Quoted from the operator's own daemon reading on 2026-10-04 (the
#: BTC wording; the message is Core's and is the same on both codebases), which
#: is the state that sent every confirmed deposit down the fabricated branch.
NO_TXINDEX_ERROR = (
    "No such mempool transaction. Use -txindex or provide a block hash to enable "
    "blockchain transaction queries. Use gettransaction for wallet transactions. (rpc code -5)"
)

CONFIRMATIONS = 3


def litecoin_core_scriptpubkey(address: str, script_hex: str) -> dict:
    """A decoded output the way LITECOIN CORE 0.21.4 spells it, and no other way.

    `addresses` (plural, a list) and `reqSigs`, with NO `address` key. That is
    the operator's measured key set, and leaving the singular key out is the
    whole point: a fixture carrying both would pass whether or not
    script_pub_key.addresses_of() reads the plural one, which is the defect
    this file is about.
    """
    return {
        "addresses": [address],
        "asm": "",
        "hex": script_hex,
        "reqSigs": 1,
        "type": "witness_v0_keyhash",
    }


def decoded_transaction(*, with_confirmations: bool) -> dict:
    """The deposit transaction, paying somebody else at vout 0 and us at vout 1.

    `with_confirmations` is what the two routes differ on and is not a
    convenience: a verbose `getrawtransaction` carries a confirmation count and
    a `decoderawtransaction` cannot -- it is handed bytes, not a position in a
    chain. The wallet-route case therefore seeds NO count here, so that if
    _vouts_through_the_wallet() ever stopped splicing in the wallet record's
    own count the event would arrive at 0 confirmations and this file would say
    so rather than quietly agreeing with a fixture that supplied one.
    """
    transaction = {
        "vout": [
            {"n": 0, "value": CHANGE_AMOUNT,
             "scriptPubKey": litecoin_core_scriptpubkey(SOMEBODY_ELSE, "0014" + "11" * 20)},
            {"n": DEPOSIT_VOUT, "value": DEPOSIT_AMOUNT,
             "scriptPubKey": litecoin_core_scriptpubkey(DEPOSIT_ADDRESS, "0014" + "22" * 20)},
        ],
    }
    if with_confirmations:
        transaction["confirmations"] = CONFIRMATIONS
    return transaction


class Litecoind(LitecoinAdapter):
    """The REAL LitecoinAdapter with its socket replaced by a seeded script.

    Subclassing the production adapter rather than standing in for it, so
    find_deposits_to_address(), _extract_matching_vouts(), _raw_tx_for_vouts()
    and _vouts_through_the_wallet() are all the real ones and `asset` is really
    "LTC". The same shape tests/test_deposit_vout_matching.py uses, named the
    same way on purpose (rule 8).

    `txindex` selects which of the two daemon states this stands in for, and
    BOTH are seeded because a stub that only ever raised could not tell the fix
    from the defect: the old code and the new one both fabricate when nothing
    can read the outputs.
    """

    def __init__(self, *, txindex: bool):
        # Not credentials. RPCAdapter.__init__ requires a non-empty user and
        # password -- an empty pair is a guaranteed 401, so an adapter built
        # from one is worse than no adapter -- and nothing here authenticates,
        # because `call` is overridden below. The port is the operator's real
        # litecoind testnet port, which network_target.CHAIN_PORTS classifies
        # as TEST; it is never connected to.
        super().__init__(
            user="fixture-rpc-user",
            password="fixture-rpc-auth-value",  # noqa: S106 -- checked: not a secret and never used; `call` is overridden and opens no socket.
            host="127.0.0.1",
            port=19332,
        )
        self.txindex = txindex
        self.calls: list[tuple] = []

    def call(self, method, *params) -> object:
        self.calls.append((method, params))
        if method == "listtransactions":
            return [{
                "category": "receive",
                "address": DEPOSIT_ADDRESS,
                "amount": DEPOSIT_AMOUNT,
                "txid": DEPOSIT_TXID,
                "confirmations": CONFIRMATIONS,
            }]
        if method == "getrawtransaction":
            if not self.txindex:
                raise RPCError(NO_TXINDEX_ERROR)
            return decoded_transaction(with_confirmations=True)
        if method == "gettransaction":
            # The wallet record. `hex` is what vout_read_route() picks first,
            # and the confirmation count on this record is the ONLY one the
            # wallet route can report.
            return {"confirmations": CONFIRMATIONS, "hex": "0100" + "00" * 32,
                    "blockhash": BLOCK_HASH}
        if method == "decoderawtransaction":
            return decoded_transaction(with_confirmations=False)
        raise AssertionError(f"the litecoind stub has no scripted answer for {method}")

    @property
    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]


@pytest.mark.parametrize("txindex", [True, False])
def test_an_LTC_deposit_is_credited_at_its_real_output_not_at_vout_zero(txindex):
    """THE MEASUREMENT. Litecoin's plural field shape, both daemon routes, real vout.

    MUTATION: in chains/base._extract_matching_vouts(), replace
    `pays_address(script_pub_key, address)` with
    `address in (script_pub_key.get("address") or "")` -- the Core-22+ spelling
    alone. Both parameters then fail, because Litecoin Core 0.21.4 sends no
    `address` key at all: the event comes back at vout 0 with the wallet
    summary's amount. Run and confirmed 2026-10-11.
    """
    adapter = Litecoind(txindex=txindex)

    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)

    assert len(events) == 1, f"one deposit was seeded and {len(events)} came back: {events}"
    event = events[0]
    assert event["vout"] == DEPOSIT_VOUT, (
        f"the deposit paid output {DEPOSIT_VOUT} and was recorded at output {event['vout']}. "
        f"{SUSPECT_VOUT} is what the FABRICATED branch stamps, so this is the branch being "
        f"reached on a daemon that can perfectly well be read -- which is the defect "
        f"swap_terminal/script_pub_key.py exists to close, measured on the other codebase"
    )
    assert event["amount"] == DEPOSIT_AMOUNT, (
        f"the credited amount is {event['amount']} rather than the {DEPOSIT_AMOUNT} the output "
        f"carries. A fabricated event takes its amount from the wallet's listtransactions "
        f"summary, which agrees here by construction -- so the vout above is the sharper test "
        f"and this one guards the day the two differ"
    )
    assert event["confirmations"] == CONFIRMATIONS, (
        f"confirmations came back {event['confirmations']} rather than {CONFIRMATIONS}. On the "
        f"wallet route the count is spliced in from the `gettransaction` record because "
        f"decoderawtransaction cannot report one; a 0 here means that splice is gone, and 0 "
        f"can never reach LTC_MIN_CONFIRMATIONS"
    )
    assert event["address"] == DEPOSIT_ADDRESS


def test_the_direct_route_costs_one_rpc_when_the_daemon_can_answer():
    """A node with -txindex, or a deposit still in the mempool: nothing extra is asked.

    Asserted because the ordering is what keeps the three-call recovery off the
    common path -- the watcher sees most deposits unconfirmed before it sees
    them confirmed -- and because a change that always took the wallet route
    would still produce the right event and would double the RPC cost of every
    poll with nothing saying so.
    """
    adapter = Litecoind(txindex=True)

    adapter.find_deposits_to_address(DEPOSIT_ADDRESS)

    assert adapter.methods == ["listtransactions", "getrawtransaction"], (
        f"the direct route made {adapter.methods}; a daemon that answers getrawtransaction "
        f"must never reach gettransaction"
    )


def test_a_confirmed_deposit_on_a_node_without_txindex_goes_through_the_WALLET():
    """The ladder, in order, on the state that produced the fabricated rows.

    `getrawtransaction` first and failing, then `gettransaction`, then
    `decoderawtransaction` on the wallet's own serialization -- which needs no
    block hash, and is why that route is tried before the block-hash one. Three
    RPCs for a confirmed deposit, which chains/base.py's own cost table states.
    """
    adapter = Litecoind(txindex=False)

    adapter.find_deposits_to_address(DEPOSIT_ADDRESS)

    assert adapter.methods == [
        "listtransactions", "getrawtransaction", "gettransaction", "decoderawtransaction",
    ], (
        f"the recovery ladder ran as {adapter.methods}. The wallet knew this transaction the "
        f"whole time -- the daemon's own -5 message says to ask it -- and a ladder that skips "
        f"`gettransaction` is back to fabricating every confirmed LTC deposit"
    )


def test_a_P2SH_LTC_deposit_address_matches_too():
    """The `Q...` form, because it is what litecoind ENCODES a P2SH address as.

    Litecoin declares SCRIPT_ADDRESS and SCRIPT_ADDRESS2 for every network and
    encodes new addresses with the SECOND (0x3A on testnet), so a P2SH deposit
    address from this daemon is a `Q...` rather than a `2...`. The match is a
    plain string comparison inside script_pub_key.pays_address(), so there is no
    arithmetic for the version byte to break -- which is exactly why it is worth
    one test: the claim "the form does not matter here" should be measured
    rather than assumed, and the same address form read as belonging to NO
    NETWORK in modules/address_network.P2SH_VERSIONS until 2026-09-27.
    """

    class P2SHLitecoind(Litecoind):
        def call(self, method, *params) -> object:
            answer = super().call(method, *params)
            if method == "listtransactions":
                return [{**answer[0], "address": LTC_P2SH_SCRIPT_ADDRESS2}]
            if method in ("getrawtransaction", "decoderawtransaction"):
                outputs = answer["vout"]
                outputs[DEPOSIT_VOUT]["scriptPubKey"]["addresses"] = [LTC_P2SH_SCRIPT_ADDRESS2]
                outputs[DEPOSIT_VOUT]["scriptPubKey"]["type"] = "scripthash"
            return answer

    events = P2SHLitecoind(txindex=False).find_deposits_to_address(LTC_P2SH_SCRIPT_ADDRESS2)

    assert [event["vout"] for event in events] == [DEPOSIT_VOUT], (
        f"a P2SH LTC deposit was not matched at its real output: {events}"
    )


def test_an_output_that_pays_somebody_else_is_not_credited():
    """The other direction, so the test above cannot be passed by matching everything.

    Without this, a mutation that returned True from pays_address() for every
    output would satisfy every assertion in this file -- and it would credit a
    swap for the change output, which on a wallet that funds its own deposits
    is a real transaction shape rather than a contrived one.
    """
    adapter = Litecoind(txindex=False)

    events = adapter.find_deposits_to_address(SOMEBODY_ELSE)

    assert events == [], (
        f"a scan for an address the seeded transaction pays at vout 0, with no matching "
        f"`listtransactions` receive row, returned {events}. Nothing should be credited: the "
        f"wallet reported no receive to that address at all"
    )
