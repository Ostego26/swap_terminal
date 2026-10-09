"""The deposit watcher must find the real output, on either daemon's field shape.

Role: test (read-only; no socket, no subprocess, no chain)
Reads: swap_terminal/chains/base.py, swap_terminal/script_pub_key.py
Writes: nothing
Can move funds: no -- every adapter here is a stub whose `call` is a seeded
      script. No socket is opened.
Mainnet-safe: yes

THE FOURTH COPY OF ONE DEFECT, and the one nothing pointed at.

    Bitcoin Core 28.1.0    scriptPubKey keys: ['address', 'asm', 'desc', 'hex', 'type']
    Litecoin Core 0.21.4   scriptPubKey keys: ['addresses', 'asm', 'hex', 'reqSigs', 'type']

`addresses` (plural) was deprecated in Core 0.20 and REMOVED in 22.0.
RPCAdapter._extract_matching_vouts() read it and nothing else, so on a Core
28.1 node the list was empty for every output of every deposit -- and the
failure is not an exception. The loop simply matched nothing and fell through
to the branch that FABRICATES an event: vout 0, the amount the wallet's own
`listtransactions` summary reported, and a confirmation count from a second
RPC. services/deposit_service.py cannot tell that apart from a real one.

The same defect was found and fixed twice in modules/ on 2026-09-25 and never
carried across, because the two families share no code and nothing named the
other. The survivor is swap_terminal/script_pub_key.py.

WHAT THESE TESTS ASSERT, and it is the vout rather than the exception: a
deposit at output 2 must be recorded as output 2. The fabricated fallback
stays -- turning it into a raise would stall swaps that credit today, which is
a fund decision and the operator's (rule 16) -- but it must stop being reached
by a transaction the adapter can perfectly well read.

NARROWED 2026-10-03, AND THE SENTENCE ABOVE IS NOW ONLY HALF TRUE. The
fabricated fallback no longer fires for a transaction with NO CONFIRMATIONS: a
zero-confirmation event cannot reach any swap's min_confirmations, so it can
never be credited, and the only thing it can do is occupy a
(asset, txid, vout=0) key that the real output's row will never reuse. The
operator authorized exactly that much after one BTC payment produced two
deposit_events rows on two separate swaps; the measurement, both swap ids and
the txid are at chains.base.fabricated_deposit_events(), which is the one
function both fallback sites now go through.

Above min_confirmations the fabrication is UNCHANGED and the tests below pin
that too, because "fixed it by refusing everything" would stall deposits that
credit today and would look identical to a fix in a test that only checked the
zero-confirmation case.

AND ON 2026-10-04 THE CAUSE TURNED OUT NOT TO BE A DECODE FAILURE AT ALL.
`_raw_tx_for_vouts()` asked `getrawtransaction(txid, True)` with no block hash,
and the operator measured their own bitcoind answering

    error code: -5
    No such mempool transaction. Use -txindex or provide a block hash to enable
    blockchain transaction queries. Use gettransaction for wallet transactions.

with `txindex=1` absent from its conf. So the call could see a transaction only
while it was in the MEMPOOL, and every deposit that confirmed between two 15s
polls fabricated. The wallet knew the transaction the whole time.

The tests from THE WALLET ROUTE onwards are built on a stub that reproduces both
daemon states -- the -5 for a confirmed transaction and a successful answer for
one in the mempool -- because a stub that only ever raises cannot tell the fix
from the defect: the old code and the new one both end up fabricating when
nothing can read the outputs. They assert on the EVENT RETURNED (a real vout
read from a real output) and on the RPCs actually made, not on log text.
"""

import pytest
import requests
from chains.base import (
    DecodedOutputs,
    RPCAdapter,
    RPCError,
    fabricated_deposit_events,
    vout_read_route,
)
from deposit_vout_artifact import SUSPECT_VOUT
from script_pub_key import NO_ADDRESS_REPORTED, address_of, addresses_of, pays_address
from valid_addresses import (
    BTC_REGTEST_DEPOSIT,
    BTC_REGTEST_SOMEBODY_ELSE,
    LTC_P2SH_TESTNET,
)

DEPOSIT_ADDRESS = BTC_REGTEST_DEPOSIT
OTHER_ADDRESS = BTC_REGTEST_SOMEBODY_ELSE
DEPOSIT_TXID = "ab" * 32


class StubAdapter(RPCAdapter):
    """An RPCAdapter whose `call` is a seeded script instead of a socket.

    Subclassing the REAL adapter, so find_deposits_to_address() and
    _extract_matching_vouts() are the real ones -- the same shape
    tests/test_address_validation.py uses, and named the same on purpose so a
    reader who finds one is not surprised by the other.
    """

    asset = "BTC"

    def __init__(self, responses):
        # Not a credential: this stub never opens a socket, so the values are
        # only here because RPCAdapter.__init__ requires them.
        super().__init__(user="u", password="p", host="127.0.0.1", port=18443)  # noqa: S106
        self._responses = responses
        self.calls = []

    def call(self, method, *params):
        self.calls.append((method, params))
        if method in self._responses:
            return self._responses[method]
        raise AssertionError(f"stub had no scripted answer for {method}")


def _listtransactions(amount=1.5):
    return [{"category": "receive", "address": DEPOSIT_ADDRESS, "amount": amount, "txid": DEPOSIT_TXID}]


def _raw_transaction(script_pub_keys, amount=1.5):
    """A verbose getrawtransaction whose deposit output is at index 2.

    Index 2 and not 0, because 0 is what the fabricated fallback invents -- a
    test that put the deposit at output 0 would pass whether or not the search
    worked.
    """
    return {
        "confirmations": 4,
        "vout": [
            {"n": 0, "value": 0.1, "scriptPubKey": script_pub_keys[0]},
            {"n": 1, "value": 0.2, "scriptPubKey": script_pub_keys[1]},
            {"n": 2, "value": amount, "scriptPubKey": script_pub_keys[2]},
        ],
    }


def test_a_core_28_deposit_is_found_at_its_real_vout():
    """THE DEFECT. Core 28.1 reports `address`, singular, and no `addresses`."""
    adapter = StubAdapter(
        {
            "listtransactions": _listtransactions(),
            "getrawtransaction": _raw_transaction(
                [
                    {"address": OTHER_ADDRESS, "hex": "00"},
                    {"address": OTHER_ADDRESS, "hex": "00"},
                    {"address": DEPOSIT_ADDRESS, "hex": "00"},
                ]
            ),
        }
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == 2, "the deposit was credited at the fabricated vout 0"
    assert events[0]["amount"] == 1.5
    assert events[0]["confirmations"] == 4
    # ONE RPC, and the absence of a second is how this test knows the real
    # branch ran rather than the fallback happening to agree. It used to be
    # phrased as "the fabricated branch calls get_confirmations(), a SECOND
    # gettransaction round trip"; that helper is gone (2026-10-04) and the
    # fallback now takes its count from the wallet record the route it tried
    # already read, so the extra call to look for is `gettransaction` itself.
    assert [method for method, _ in adapter.calls] == ["listtransactions", "getrawtransaction"]


def test_a_litecoin_0_21_deposit_is_still_found():
    """The other daemon's shape, which worked before and must keep working.

    Half of a merge is a regression: fixing Core 28.1 by simply swapping
    `addresses` for `address` would have broken Litecoin instead, which is the
    same defect pointed the other way.
    """
    adapter = StubAdapter(
        {
            "listtransactions": _listtransactions(),
            "getrawtransaction": _raw_transaction(
                [
                    {"addresses": [OTHER_ADDRESS], "hex": "00"},
                    {"addresses": [OTHER_ADDRESS], "hex": "00"},
                    {"addresses": [DEPOSIT_ADDRESS], "hex": "00"},
                ]
            ),
        }
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == 2


def test_an_output_that_names_no_address_is_not_credited():
    """A bare multisig or an OP_RETURN names nothing, and must not match.

    The fabricated fallback still fires here, which is the documented and
    unchanged behavior -- what must NOT happen is an output with no address
    matching a caller who was given one.
    """
    adapter = StubAdapter(
        {
            "listtransactions": _listtransactions(),
            "getrawtransaction": _raw_transaction(
                [{"asm": "OP_RETURN", "hex": "6a"}, {"asm": "OP_RETURN", "hex": "6a"}, {"asm": "OP_RETURN", "hex": "6a"}]
            ),
        }
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == 0, "this is the fabricated fallback, which is unchanged"


@pytest.mark.parametrize(
    ("script_pub_key", "expected"),
    [
        # Core 22.0 and later.
        ({"address": BTC_REGTEST_DEPOSIT}, [BTC_REGTEST_DEPOSIT]),
        # Core 0.19 and earlier, and Litecoin 0.21.4.
        ({"addresses": [LTC_P2SH_TESTNET]}, [LTC_P2SH_TESTNET]),
        # Both, which no daemon does today and which a daemon in between might.
        ({"address": "a", "addresses": ["b"]}, ["a", "b"]),
        # The same value in both fields is one address, not two.
        ({"address": "a", "addresses": ["a"]}, ["a"]),
        # Named nothing at all.
        ({"asm": "OP_RETURN", "hex": "6a"}, []),
        ({}, []),
        # Shapes that are not a mapping at all. Fed straight out of a daemon's
        # JSON, so a caller walking a hundred outputs must not die on one.
        (None, []),
        ("not a dict", []),
        # A daemon that answered with the field present but empty.
        ({"address": "", "addresses": []}, []),
    ],
)
def test_addresses_of_reads_both_field_shapes(script_pub_key, expected):
    """The decision, called with seeded inputs (rule 10)."""
    assert addresses_of(script_pub_key) == expected


def test_pays_address_never_matches_an_empty_address():
    """An output that names no address and a caller given no address must not agree."""
    assert pays_address({"address": "a"}, "a") is True
    assert pays_address({"addresses": ["a"]}, "a") is True
    assert pays_address({"address": "a"}, "b") is False
    assert pays_address({}, "") is False
    assert pays_address({"address": ""}, "") is False


def test_address_of_never_prints_nothing():
    """`(none)` is a result; a blank gap is ambiguous between zero and broken (rule 14)."""
    assert address_of({"address": BTC_REGTEST_DEPOSIT}) == BTC_REGTEST_DEPOSIT
    assert address_of({"addresses": ["a", "b"]}) == "a, b"
    assert address_of({"asm": "OP_RETURN"}) == NO_ADDRESS_REPORTED
    assert address_of(None) == NO_ADDRESS_REPORTED


# =============================================================================
# THE FABRICATED FALLBACK, NARROWED 2026-10-03
# =============================================================================
#
# MEASURED TWICE ON THE OPERATOR'S HOST. One BTC deposit of ONE transaction
# produced TWO rows in deposit_events, reproduced on swaps s_6cd1a920cbe5739e
# and s_02623852c1ea42cc. For txid
# fd898cfb8b5b8fa026f21ce30afc7f234126fe965a27c1330c8d6969a228eaf2 the screen
# showed, for the one payment:
#
#     0.001 BTC  0 confirmation(s)  NOT counted -- below min_confirmations=2  vout 1
#     0.001 BTC  2 confirmation(s)  COUNTED by the gate                      vout 0
#
# and `bitcoin-cli gettransaction <txid> true` proved there is ONE payment, of
# 0.001, whose real output is at vout 1. upsert_deposit_event() keys on
# (asset, txid, vout), so the two never collided and both persisted.
#
# These tests are built on that transaction's real shape -- one 0.001 output at
# vout 1, change at vout 0 -- rather than on the 1.5-at-index-2 fixture above,
# so that the vout the fabricated branch invents is a vout the transaction
# genuinely has for a DIFFERENT output. A fixture whose index 0 did not exist
# would pass against a weaker fix.

MEASURED_TXID = "fd898cfb8b5b8fa026f21ce30afc7f234126fe965a27c1330c8d6969a228eaf2"
MEASURED_AMOUNT = 0.001


def _measured_listtransactions():
    return [{
        "category": "receive",
        "address": DEPOSIT_ADDRESS,
        "amount": MEASURED_AMOUNT,
        "txid": MEASURED_TXID,
    }]


def _measured_raw_transaction(confirmations):
    """The real transaction: change at vout 0, the 0.001 deposit at vout 1."""
    return {
        "confirmations": confirmations,
        "vout": [
            {"n": 0, "value": 0.4, "scriptPubKey": {"address": OTHER_ADDRESS, "hex": "00"}},
            {"n": 1, "value": MEASURED_AMOUNT, "scriptPubKey": {"address": DEPOSIT_ADDRESS, "hex": "00"}},
        ],
    }


class UndecodableAdapter(StubAdapter):
    """A StubAdapter whose `getrawtransaction` raises, as a Core that cannot decode does.

    Subclassing the stub that subclasses the REAL adapter, so
    find_deposits_to_address() and _extract_matching_vouts() are still the real
    ones and the except branch is reached by an actual exception rather than by
    a flag a test set.

    RPCError and not AssertionError, because `except Exception` catching the
    stub's own "nothing scripted for this method" would make the test pass for
    the wrong reason -- it would prove the harness incomplete, not the adapter
    wrong.
    """

    def call(self, method, *params):
        self.calls.append((method, params))
        if method == "getrawtransaction":
            raise RPCError("error code: -5  error message: No such mempool transaction")
        if method in self._responses:
            return self._responses[method]
        raise AssertionError(f"stub had no scripted answer for {method}")


def test_an_undecodable_unconfirmed_deposit_produces_no_event():
    """A failure to read the outputs at zero confirmations emits NOTHING.

    Without it this returns one event at vout 0 -- a row that can never be
    credited (the gate needs min_confirmations, which is positive everywhere
    this terminal runs) and whose only effect is to occupy a key the real
    output's row at vout 1 will never reuse.
    """
    adapter = UndecodableAdapter({
        "listtransactions": _measured_listtransactions(),
        # The wallet's own view, and -- in this fixture -- nothing else: no
        # `hex` and no `blockhash`, so no route to the outputs exists.
        "gettransaction": {"confirmations": 0},
    })
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert events == [], f"a zero-confirmation fabricated event was emitted: {events}"


def test_a_decode_that_matches_nothing_while_unconfirmed_produces_no_event():
    """The OTHER fabrication site, refused on the same condition.

    The decode succeeds and matches no output -- here because every output pays
    somebody else, which is also what the removed `scriptPubKey.addresses`
    field produced on a Core 22+ node for EVERY output of every deposit. It
    reaches the same fabricated event without an exception and without a
    `noqa` marking it, so a fix applied only to the except branch would leave
    this one writing exactly the row the first test refuses.
    """
    adapter = StubAdapter({
        "listtransactions": _measured_listtransactions(),
        "getrawtransaction": {
            "confirmations": 0,
            "vout": [
                {"n": 0, "value": 0.4, "scriptPubKey": {"address": OTHER_ADDRESS, "hex": "00"}},
                {"n": 1, "value": MEASURED_AMOUNT, "scriptPubKey": {"address": OTHER_ADDRESS, "hex": "00"}},
            ],
        },
    })
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert events == [], f"a zero-confirmation fabricated event was emitted: {events}"


def test_one_payment_never_yields_two_vouts_across_polls():
    """THE INVARIANT, and it is the defect itself rather than either branch.

    Two polls of the same wallet, in the order that produces the REMAINING
    failure: the first while the transaction is unconfirmed and no route to its
    outputs exists, the second once it confirms and the outputs can be read.
    (The live 2026-10-03 ordering is the other way round -- a successful read
    while in the mempool, then a -5 once mined -- and it has its own test, at
    test_the_measured_two_poll_sequence_yields_one_key.) Every (txid, vout) pair either poll emits is a
    deposit_events key -- upsert_deposit_event() keys on (asset, txid, vout) --
    so the set of keys ACROSS the polls is what decides whether one payment
    becomes one row or two. It must be exactly the real output.

    This is asserted over the keys rather than over one call's return value
    because that is where the damage was: each poll's own answer looked
    perfectly reasonable in isolation, and nothing compared them.
    """
    keys = set()

    first = UndecodableAdapter({
        "listtransactions": _measured_listtransactions(),
        "gettransaction": {"confirmations": 0},
    })
    keys.update((event["txid"], event["vout"]) for event in first.find_deposits_to_address(DEPOSIT_ADDRESS))

    second = StubAdapter({
        "listtransactions": _measured_listtransactions(),
        "getrawtransaction": _measured_raw_transaction(confirmations=2),
    })
    keys.update((event["txid"], event["vout"]) for event in second.find_deposits_to_address(DEPOSIT_ADDRESS))

    assert keys == {(MEASURED_TXID, 1)}, (
        f"one payment produced these deposit_events keys: {sorted(keys)}  <- expected only "
        f"the real output at vout 1; a second key is the two-rows-for-one-payment defect "
        f"measured 2026-10-03 on swaps s_6cd1a920cbe5739e and s_02623852c1ea42cc"
    )


def test_a_confirmed_undecodable_deposit_is_still_fabricated():
    """UNCHANGED BEHAVIOR, pinned so that "refuse everything" cannot pass as the fix.

    Above min_confirmations the fabricated event still goes out, built from the
    wallet summary. Refusing here would stall deposits that credit today, which
    _extract_matching_vouts()'s own comment has said since 2026-09-25 is a fund
    decision and the operator's (rule 16). This test is the half of the
    behavior that must NOT move.

    WHAT THIS FIXTURE NOW MEANS, 2026-10-04, and it is narrower than it was.
    `gettransaction` here answers with a confirmation count and NOTHING ELSE --
    no `hex`, no `blockhash` -- so there is no way left to ask for the outputs
    and the fabrication is what remains. That is deliberate: it is one of the
    two failures that still reach the fallback now that the -5 route is
    handled, and no daemon in this tree has been measured answering this way.
    """
    adapter = UndecodableAdapter({
        "listtransactions": _measured_listtransactions(),
        "gettransaction": {"confirmations": 2},
    })
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == SUSPECT_VOUT
    assert events[0]["amount"] == MEASURED_AMOUNT
    assert events[0]["confirmations"] == 2


def test_a_confirmed_deposit_that_matches_nothing_is_still_fabricated():
    """The same unchanged half, at the no-match site."""
    adapter = StubAdapter({
        "listtransactions": _measured_listtransactions(),
        "getrawtransaction": {
            "confirmations": 3,
            "vout": [{"n": 0, "value": 0.4, "scriptPubKey": {"address": OTHER_ADDRESS, "hex": "00"}}],
        },
    })
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == SUSPECT_VOUT
    assert events[0]["confirmations"] == 3


def test_a_decode_failure_does_not_hide_a_confirmed_real_output():
    """The decode working is still what decides the vout, at any confirmation count.

    Zero confirmations and a successful decode must still record the real
    output: the refusal is about FABRICATION, not about unconfirmed deposits.
    Crediting is already gated on min_confirmations downstream, and suppressing
    a real unconfirmed row here would move that gate into the adapter.
    """
    adapter = StubAdapter({
        "listtransactions": _measured_listtransactions(),
        "getrawtransaction": _measured_raw_transaction(confirmations=0),
    })
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == 1
    assert events[0]["confirmations"] == 0


@pytest.mark.parametrize(
    ("confirmations", "expected_events"),
    [
        # Conflicted by a block: Core reports -1, which is further from
        # creditable than zero is.
        (-1, 0),
        (0, 0),
        # LITERAL 1, NOT MIN_FABRICATED_CONFIRMATIONS, and this was a real
        # defect in this test rather than a style point. Written as the
        # constant, the case moved WITH it: raising the threshold to 2 was
        # mutation-checked on 2026-10-03 and NOTHING in this file failed,
        # because every test that asserted a fabricated event was emitted used
        # either the constant itself or a count above it. A test that reads its
        # expectation out of the code under test pins a spelling, not an
        # invariant. The invariant is that ONE confirmation still fabricates --
        # the refusal is about a count that can never be credited, and 1 can be
        # credited by any swap whose min_confirmations is 1.
        (1, 1),
        (2, 1),
        (144, 1),
    ],
)
def test_fabricated_deposit_events_refuses_below_one_confirmation(confirmations, expected_events):
    """The decision, called with seeded inputs (rule 10).

    The threshold must not differ between the failures that reach this
    function -- a wallet record with no serialization and no block hash, a
    recovery call the daemon refused, and a decode that matched no output --
    which is why it is one function and this is a direct test of it rather than
    several adapter-level tests that happen to agree.

    `why` IS PASSED AND IS NOT OPTIONAL. It has no default precisely so that a
    caller cannot reach this function without saying which failure it saw; a
    test calling it with five arguments would be pinning a signature that
    allows the defect rule 14 names.
    """
    events = fabricated_deposit_events(
        asset="BTC",
        txid=MEASURED_TXID,
        address=DEPOSIT_ADDRESS,
        amount=MEASURED_AMOUNT,
        confirmations=confirmations,
        why="the outputs could not be read: seeded directly by this test",
    )
    assert len(events) == expected_events
    for event in events:
        assert event == {
            "txid": MEASURED_TXID,
            "vout": SUSPECT_VOUT,
            "address": DEPOSIT_ADDRESS,
            "amount": MEASURED_AMOUNT,
            "confirmations": confirmations,
        }


# =============================================================================
# THE WALLET ROUTE -- ROOT-CAUSED ON THE OPERATOR'S HOST 2026-10-04
# =============================================================================
#
# `_raw_tx_for_vouts()` was `getrawtransaction(txid, True)` and nothing else.
# Measured against the real deposit
# b2892636451355197be246e9f5dc56fcac309261fc17bd8d36b2a2b1108dd4f8 on the
# operator's own bitcoind:
#
#     error code: -5
#     No such mempool transaction. Use -txindex or provide a block hash to
#     enable blockchain transaction queries. Use gettransaction for wallet
#     transactions.
#
# and `grep -c '^txindex=1' ~/regtest/btc/bitcoin.conf` -> 0. Both readings are
# the operator's. So the call answered only for a transaction still in the
# MEMPOOL, and every deposit that confirmed before the next 15s poll fabricated.
#
# THE STUB BELOW REPRODUCES BOTH DAEMON STATES, which is the only way the defect
# is visible: a stub whose getrawtransaction always raises cannot tell the fix
# from the defect, because both end at the fabricated event when no route to the
# outputs exists.

MEASURED_BLOCK_HASH = "0000000000000000000a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcd"
# The serialization `gettransaction` hands back. Opaque here on purpose: this
# stub's `decoderawtransaction` is scripted, exactly as a daemon's would answer,
# so the test depends on the ROUTE rather than on any parsing in process --
# which is the establishment modules/htlc_rpc.lookup_contract_output() records
# for route 3 (an in-process parser could not read a segwit serialization at
# all).
MEASURED_TX_HEX = "02000000000101deadbeef00"

TXINDEX_MINUS_5 = (
    "No such mempool transaction. Use -txindex or provide a block hash to enable blockchain "
    "transaction queries. Use gettransaction for wallet transactions. (rpc code -5)"
)


class NoTxindexAdapter(StubAdapter):
    """A daemon with no -txindex: `getrawtransaction` answers ONLY for the mempool.

    Subclasses the stub that subclasses the REAL adapter, so
    find_deposits_to_address(), _extract_matching_vouts(),
    _raw_tx_for_vouts() and _vouts_through_the_wallet() are all the real ones.

    `in_mempool` is the daemon state, not a flag the code under test can see:
    True and the two-argument getrawtransaction answers, False and it raises
    the measured -5. A third argument (a block hash) is answered in either
    state, which is what a real node does.
    """

    def __init__(self, responses, *, in_mempool: bool):
        super().__init__(responses)
        self.in_mempool = in_mempool

    def call(self, method, *params):
        self.calls.append((method, params))
        if method == "getrawtransaction":
            has_block_hash = len(params) >= 3 and params[2]
            if not has_block_hash and not self.in_mempool:
                raise RPCError(TXINDEX_MINUS_5)
            if has_block_hash:
                if method + "+blockhash" not in self._responses:
                    raise AssertionError("stub had no scripted answer for getrawtransaction+blockhash")
                return self._responses[method + "+blockhash"]
        if method in self._responses:
            return self._responses[method]
        raise AssertionError(f"stub had no scripted answer for {method}")


def _wallet_record(confirmations, *, hex_: str | None = MEASURED_TX_HEX,
                   block_hash: str | None = MEASURED_BLOCK_HASH):
    """A `gettransaction` result, in the shape Core 28.1 and Litecoin 0.21.4 both return.

    `hex_=None` and `block_hash=None` are the ABSENT-KEY cases, which is why both
    are annotated `str | None` rather than left to be inferred as `str` from
    their defaults. The body below drops the key entirely when it is None, and
    three tests pass None on purpose -- a daemon that answers gettransaction
    without a `hex` (the no-txindex shape) or without a `blockhash` (a mempool
    transaction). Inferring the type from the default made those three calls
    type errors while the ONLY reason the parameters exist is to be None
    (pyright reportArgumentType x3, 2026-10-09).
    """
    record = {"confirmations": confirmations, "txid": MEASURED_TXID, "amount": MEASURED_AMOUNT}
    if hex_ is not None:
        record["hex"] = hex_
    if block_hash is not None:
        record["blockhash"] = block_hash
    return record


def test_a_confirmed_deposit_with_no_txindex_is_read_at_its_real_vout():
    """THE FIX, asserted on the event rather than on the warning.

    This is the operator's live shape: the payment at vout 1, change at vout 0,
    the transaction mined, and the node with no -txindex. Before the fix this
    returned one event at vout=SUSPECT_VOUT with the amount `listtransactions`
    summarized; now it returns the real output.
    """
    adapter = NoTxindexAdapter(
        {
            "listtransactions": _measured_listtransactions(),
            "gettransaction": _wallet_record(2),
            "decoderawtransaction": _measured_raw_transaction(confirmations=None),
        },
        in_mempool=False,
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1, f"expected exactly the real output, got {events}"
    assert events[0]["vout"] == 1, (
        "the deposit was credited at the fabricated vout 0 -- the real payment is at vout 1 "
        "(measured 2026-10-03)"
    )
    # FROM THE OUTPUT, not from listtransactions. The two agree here by
    # construction, so the amount alone cannot prove which was used -- the vout
    # above is what proves it, and this pins that the credited figure did not
    # move for a correctly read deposit.
    assert events[0]["amount"] == MEASURED_AMOUNT
    # `decoderawtransaction` reports no confirmation count -- it is handed
    # bytes, not a position in a chain -- so this is the wallet record's own
    # figure, read once.
    assert events[0]["confirmations"] == 2
    assert [method for method, _ in adapter.calls] == [
        "listtransactions",
        "getrawtransaction",
        "gettransaction",
        "decoderawtransaction",
    ], f"three RPCs per deposit read on this path, in this order; got {adapter.calls}"


def test_a_deposit_in_the_mempool_still_costs_one_rpc():
    """THE PATH THAT MUST NOT REGRESS, and it is most polls.

    A transaction in the mempool is exactly what the bare two-argument call
    CAN read, and the watcher sees a deposit unconfirmed before it sees it
    confirmed. The wallet route must not be taken here: `gettransaction`
    appearing in this list would be a round trip added to every poll of every
    chain.

    It is also why Gridcoin cannot regress. GRC answers the direct call for a
    CONFIRMED transaction too -- measured 2026-09-27, route 4 of
    modules/htlc_rpc.lookup_contract_output() answering on a daemon with no
    txindex setting -- so a GRC deposit never reaches a line of the new code.
    """
    adapter = NoTxindexAdapter(
        {
            "listtransactions": _measured_listtransactions(),
            "getrawtransaction": _measured_raw_transaction(confirmations=0),
        },
        in_mempool=True,
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == 1
    assert events[0]["confirmations"] == 0
    assert [method for method, _ in adapter.calls] == ["listtransactions", "getrawtransaction"], (
        f"the mempool path must stay at one RPC per deposit; got {adapter.calls}"
    )


def test_an_old_daemon_shape_is_read_through_the_wallet_too():
    """Litecoin 0.21.4's `addresses` (plural) decoded through the wallet route.

    Fixing Core 28.1 by reading `address` alone is the same defect pointed the
    other way (script_pub_key.py), and the wallet route reaches
    `decoderawtransaction`, whose output carries whichever shape the daemon
    speaks. So the field-shape merge has to hold on this route as well, and a
    test that only exercised it on the direct call would not say so.
    """
    decoded = {
        "vout": [
            {"n": 0, "value": 0.4, "scriptPubKey": {"addresses": [OTHER_ADDRESS], "hex": "00"}},
            {"n": 1, "value": MEASURED_AMOUNT, "scriptPubKey": {"addresses": [DEPOSIT_ADDRESS], "hex": "00"}},
        ],
    }
    adapter = NoTxindexAdapter(
        {
            "listtransactions": _measured_listtransactions(),
            "gettransaction": _wallet_record(6),
            "decoderawtransaction": decoded,
        },
        in_mempool=False,
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == 1
    assert events[0]["confirmations"] == 6


def test_the_block_hash_route_is_used_when_the_wallet_reports_no_hex():
    """The second route, and the block hash really is passed.

    A wallet record with a `blockhash` and no `hex` is the one case the
    serialization route cannot serve. The assertion is on the PARAMS the
    adapter sent, because "it asked getrawtransaction again" and "it asked
    getrawtransaction again WITH THE BLOCK HASH" are the defect and the fix,
    and only the third argument separates them.
    """
    adapter = NoTxindexAdapter(
        {
            "listtransactions": _measured_listtransactions(),
            "gettransaction": _wallet_record(3, hex_=None),
            "getrawtransaction+blockhash": _measured_raw_transaction(confirmations=3),
        },
        in_mempool=False,
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == 1
    assert events[0]["confirmations"] == 3
    assert ("getrawtransaction", (MEASURED_TXID, True, MEASURED_BLOCK_HASH)) in adapter.calls, (
        f"the block hash was not passed; the calls were {adapter.calls}"
    )


def test_the_measured_two_poll_sequence_yields_one_key():
    """THE 2026-10-03 DOUBLE ROW, in the order the operator's rows record it.

    Row one was vout 1 at 0 confirmations; row two was vout 0 at 2
    confirmations. That is a successful read while the transaction sat in the
    mempool, followed by a fabrication once it was mined and the bare
    `getrawtransaction` could no longer see it. Every (txid, vout) pair either
    poll emits is a deposit_events key -- upsert_deposit_event() keys on
    (asset, txid, vout) -- so the set across the polls is what decides whether
    one payment becomes one row or two.
    """
    keys = set()
    for in_mempool, confirmations in ((True, 0), (False, 2)):
        adapter = NoTxindexAdapter(
            {
                "listtransactions": _measured_listtransactions(),
                "getrawtransaction": _measured_raw_transaction(confirmations=0),
                "gettransaction": _wallet_record(confirmations),
                "decoderawtransaction": _measured_raw_transaction(confirmations=None),
            },
            in_mempool=in_mempool,
        )
        keys.update(
            (event["txid"], event["vout"]) for event in adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
        )
    assert keys == {(MEASURED_TXID, 1)}, (
        f"one payment produced these deposit_events keys: {sorted(keys)}  <- expected only the "
        f"real output at vout 1; (txid, 0) is the fabricated row measured 2026-10-03 on swaps "
        f"s_6cd1a920cbe5739e and s_02623852c1ea42cc"
    )


def test_a_transport_failure_also_takes_the_wallet_route():
    """`requests.RequestException` is the socket saying no, and it is caught by name.

    Narrowing the catch to RPCError alone would leave a connection reset
    raising out of a deposit poll -- a change this session cannot test against
    a real daemon and did not make. requests.exceptions.JSONDecodeError is a
    RequestException too (checked against requests 2.33.1's MRO), so a 200
    carrying junk arrives here as well.
    """

    class ResettingAdapter(NoTxindexAdapter):
        def call(self, method, *params):
            if method == "getrawtransaction" and len(params) < 3:
                self.calls.append((method, params))
                raise requests.ConnectionError("connection reset by peer")
            return super().call(method, *params)

    adapter = ResettingAdapter(
        {
            "listtransactions": _measured_listtransactions(),
            "gettransaction": _wallet_record(2),
            "decoderawtransaction": _measured_raw_transaction(confirmations=None),
        },
        in_mempool=False,
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == 1


def test_an_unexpected_exception_is_not_laundered_into_a_deposit_event():
    """THE NARROWING, and it is the rule 12 half of this change.

    `except Exception` caught a TypeError in this file and returned a deposit
    event built from the wallet's summary -- a defect in the adapter, credited
    as money received. Now it reaches the worker, which logs a FAILED cycle and
    credits nothing on any chain. That halts rather than releasing, which is
    the safe direction.
    """

    class BrokenAdapter(NoTxindexAdapter):
        def call(self, method, *params):
            self.calls.append((method, params))
            if method == "getrawtransaction":
                raise TypeError("a defect in this file, not an answer from a daemon")
            return self._responses[method]

    adapter = BrokenAdapter(
        {"listtransactions": _measured_listtransactions()},
        in_mempool=False,
    )
    with pytest.raises(TypeError):
        adapter.find_deposits_to_address(DEPOSIT_ADDRESS)


def test_a_refused_recovery_call_still_fabricates_rather_than_killing_the_cycle():
    """The second call IS wrapped, and the reason is that it must not be worse than before.

    A daemon that refuses `decoderawtransaction` -- an RPC surface older than
    anything measured here -- fabricated before this change existed. Turning
    that into an exception out of the deposit poll would stop every chain's
    credits on a path no test here can reach against a real daemon, so the
    behavior is held where it was and the reason is carried into the log.
    """

    class RefusingAdapter(NoTxindexAdapter):
        def call(self, method, *params):
            if method == "decoderawtransaction":
                self.calls.append((method, params))
                raise RPCError("Method not found (rpc code -32601)")
            return super().call(method, *params)

    adapter = RefusingAdapter(
        {
            "listtransactions": _measured_listtransactions(),
            "gettransaction": _wallet_record(2, block_hash=None),
        },
        in_mempool=False,
    )
    events = adapter.find_deposits_to_address(DEPOSIT_ADDRESS)
    assert len(events) == 1
    assert events[0]["vout"] == SUSPECT_VOUT
    assert events[0]["confirmations"] == 2


# =============================================================================
# THE ROUTE DECISION, CALLED WITH SEEDED INPUTS (rule 10)
# =============================================================================


def test_the_serialization_route_is_preferred_over_the_block_hash_route():
    """hex FIRST, and the order is a daemon fact rather than a taste.

    Gridcoin's `getrawtransaction` takes TWO parameters -- `getrawtransaction
    <txid> [verbose=bool]`, measured on the operator's GRC daemon 2026-09-27
    and recorded in docs/atomic_swap_runs_2026_09_27.md -- so a block hash is
    an argument one of the three chains cannot accept. `decoderawtransaction`
    predates all three, and needs no chain position, so it answers for a
    mempool transaction as well.
    """
    route = vout_read_route(MEASURED_TXID, _wallet_record(2))
    assert route.method == "decoderawtransaction"
    assert route.params == (MEASURED_TX_HEX,)


def test_the_block_hash_route_is_the_fallback_and_carries_the_hash():
    route = vout_read_route(MEASURED_TXID, _wallet_record(2, hex_=None))
    assert route.method == "getrawtransaction"
    assert route.params == (MEASURED_TXID, True, MEASURED_BLOCK_HASH)


@pytest.mark.parametrize(
    "wallet_tx",
    [
        # Mined, but the wallet reported neither field.
        {"confirmations": 2},
        # Both present and both empty, which is not an answer either.
        {"confirmations": 2, "hex": "", "blockhash": ""},
        # Not an object at all.
        None,
        "not a dict",
    ],
)
def test_no_route_is_reported_as_no_route_with_a_reason(wallet_tx):
    """`(none)` is a result; a blank gap is ambiguous between the two (rule 14)."""
    route = vout_read_route(MEASURED_TXID, wallet_tx)
    assert route.method == ""
    assert route.params == ()
    assert route.why.strip(), "a route refusal with no reason is the silence rule 14 forbids"


# =============================================================================
# THE WARNING NOW SAYS WHICH FAILURE IT SAW (rule 14)
# =============================================================================


def test_the_warning_distinguishes_a_failed_read_from_a_failed_match(caplog):
    """One sentence covered three failures, and it misled a reader of the 2026-10-04 log.

    Asserted ALONGSIDE the returned event rather than instead of it: both
    cases below return the same fabricated event, so the log line is the only
    thing that differs, and an operator deciding what to do about an uncredited
    deposit reads the log.
    """
    read_but_no_match = StubAdapter({
        "listtransactions": _measured_listtransactions(),
        "getrawtransaction": {
            "confirmations": 3,
            "vout": [{"n": 0, "value": 0.4, "scriptPubKey": {"address": OTHER_ADDRESS, "hex": "00"}}],
        },
    })
    with caplog.at_level("WARNING", logger="chains.base"):
        caplog.clear()
        events = read_but_no_match.find_deposits_to_address(DEPOSIT_ADDRESS)
        matched_text = caplog.text
    assert events[0]["vout"] == SUSPECT_VOUT
    assert "the outputs were read" in matched_text
    assert "none of them pays this address" in matched_text

    could_not_read = UndecodableAdapter({
        "listtransactions": _measured_listtransactions(),
        "gettransaction": {"confirmations": 3},
    })
    with caplog.at_level("WARNING", logger="chains.base"):
        caplog.clear()
        events = could_not_read.find_deposits_to_address(DEPOSIT_ADDRESS)
        unread_text = caplog.text
    assert events[0]["vout"] == SUSPECT_VOUT
    assert "the outputs could not be read" in unread_text
    # BOTH halves of the ladder are named, so the line says what the chain
    # query answered AND what the wallet then offered.
    assert "No such mempool transaction" in unread_text
    assert "neither `hex` nor `blockhash`" in unread_text
    assert matched_text != unread_text, (
        "the two failures printed the same sentence, which is the defect this test exists for"
    )


def test_decoded_outputs_keeps_read_and_empty_as_different_answers():
    """`read` is not `bool(outputs)`, and the difference is what the log depends on."""
    nothing_read = DecodedOutputs([], 0, False, "every route failed")
    read_and_empty = DecodedOutputs([], 0, True, "a daemon decoded it")
    assert nothing_read.read is False
    assert read_and_empty.read is True
