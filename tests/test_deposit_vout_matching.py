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
"""

import pytest
from chains.base import RPCAdapter
from script_pub_key import NO_ADDRESS_REPORTED, address_of, addresses_of, pays_address

DEPOSIT_ADDRESS = "bcrt1qdepositaddressexample00000000000000000"
OTHER_ADDRESS = "bcrt1qsomebodyelse0000000000000000000000000"
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
        ({"address": "bcrt1qexample"}, ["bcrt1qexample"]),
        # Core 0.19 and earlier, and Litecoin 0.21.4.
        ({"addresses": ["2NexampleLTC"]}, ["2NexampleLTC"]),
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
    assert address_of({"address": "bcrt1qexample"}) == "bcrt1qexample"
    assert address_of({"addresses": ["a", "b"]}) == "a, b"
    assert address_of({"asm": "OP_RETURN"}) == NO_ADDRESS_REPORTED
    assert address_of(None) == NO_ADDRESS_REPORTED
