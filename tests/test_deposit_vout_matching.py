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
"""

import pytest
from chains.base import RPCAdapter, RPCError, fabricated_deposit_events
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
    # The fabricated branch calls get_confirmations(), which is a SECOND
    # gettransaction round trip. Its absence is how this test knows the real
    # branch ran rather than the fallback happening to agree.
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
    """THE FIX. A decode failure at zero confirmations emits NOTHING.

    Without it this returns one event at vout 0 -- a row that can never be
    credited (the gate needs min_confirmations, which is positive everywhere
    this terminal runs) and whose only effect is to occupy a key the real
    output's row at vout 1 will never reuse.
    """
    adapter = UndecodableAdapter({
        "listtransactions": _measured_listtransactions(),
        # get_confirmations() goes through gettransaction, the wallet's own view.
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

    Two polls of the same wallet, as the worker makes them: the first while the
    transaction is unconfirmed and cannot be decoded, the second once it
    confirms and can. Every (txid, vout) pair either poll emits is a
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

    The two call sites differ in where their confirmation count comes from -- a
    second `gettransaction` round trip when the decode raised, the decoded
    transaction's own field when it did not -- and the threshold must not
    differ with them, which is why it is one function and this is a direct test
    of it rather than two adapter-level tests that happen to agree.
    """
    events = fabricated_deposit_events(
        asset="BTC",
        txid=MEASURED_TXID,
        address=DEPOSIT_ADDRESS,
        amount=MEASURED_AMOUNT,
        confirmations=confirmations,
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
