"""The decimal places a chain's RPC actually accepts, and fitting a payout to them.

Role: test (pure functions; opens no socket)
Reads: swap_terminal/chains/coin_amounts.py, chains/base.py's send path
Writes: nothing
Can move funds: no
Mainnet-safe: yes

MEASURED ON THE OPERATOR'S REGTEST NODE 2026-10-03, with createrawtransaction --
which runs Core's own amount parser and broadcasts nothing:

    0.00041198765432109  ->  error code -3, "Invalid amount"
    4.1e-07              ->  ACCEPTED, an output of 0x29 = 41 satoshis

So BTC and LTC as DESTINATIONS could not have paid out at all: a quoted payout is
a float carrying 13-17 decimals, and Core's ParseFixedPoint(value, 8) rejects
more than eight rather than rounding. Gridcoin masked it by accepting
2701.3495803173805 and rounding on chain to 2701.34958032, which is why five
swaps settled without finding it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains.base import RPCAdapter, RPCError
from chains.coin_amounts import CHAIN_DECIMALS, amount_to_base_units, fit_to_chain_precision
from chains.solana_units import amount_to_base_units as reexported
from valid_addresses import BTC_REGTEST_DEPOSIT


@pytest.mark.parametrize(
    ("amount", "fitted"),
    [
        # The two amounts actually tested against the daemon.
        (0.00041198765432109, 0.00041198),
        (4.1e-07, 4.1e-07),
        # The GRC payout that settled, which Gridcoin rounded for us.
        (2701.3495803173805, 2701.34958031),
        # Already exact: must come back untouched, not re-derived through Decimal.
        (0.0003, 0.0003),
        (1.0, 1.0),
    ],
)
def test_an_amount_is_fitted_to_eight_decimals_and_never_rounded_up(amount, fitted):
    """DOWN, inherited from amount_to_base_units() and for its reason.

    Rounding up would send a fraction of a unit more than was quoted out of the
    hot wallet, every time. The customer is short by at most one satoshi and the
    desk is never over -- which is the only direction a payout may err.
    """
    result, _why = fit_to_chain_precision(amount, "BTC")

    assert result == fitted
    assert result <= amount, "a payout may never be fitted UPWARD"


def test_an_unchanged_amount_reports_no_change():
    """So the log line only appears when a number actually moved.

    A `changed` string on every send would make the one that matters invisible,
    which is the opposite of what rule 14 asks of it.
    """
    _result, why = fit_to_chain_precision(0.0003, "BTC")

    assert why == ""


def test_a_fitted_amount_says_what_it_was_and_what_it_became():
    """The operator reconciles a chain explorer against a quote; they differ in the last digit."""
    _result, why = fit_to_chain_precision(0.00041198765432109, "BTC")

    assert "0.00041198765432109" in why and "0.00041198" in why
    assert "down" in why, "the direction is the part that needs no second guess"
    assert "BTC's 8 decimal places" in why


@pytest.mark.parametrize("asset", ["XRP", "SOL"])
def test_an_integer_base_unit_chain_is_left_alone_and_says_why(asset):
    """XRP and SOL are ABSENT from the table deliberately, not forgotten.

    Both convert before sending -- chains/xrp.py through to_drops() and
    chains/solana.py through amount_to_base_units() -- so no decimal string ever
    reaches those RPCs. Solana's figure is not a per-chain constant either: an SPL
    mint carries its own `decimals`, so a table entry would be a second answer to
    a question the chain already answers.
    """
    result, why = fit_to_chain_precision(1.23456789012, asset)

    assert result == 1.23456789012, "an unlisted asset must pass through untouched, not get 8 decimals"
    assert "not in CHAIN_DECIMALS" in why
    assert "integer base units" in why


def test_the_table_covers_exactly_the_chains_whose_rpc_takes_a_decimal():
    """Three chains, eight places each, and nothing else.

    A new entry here changes what a payout sends, so the table is pinned rather
    than left to drift: BTC, LTC and GRC are the chains chains/base.RPCAdapter
    serves, and 8 is the COIN constant all three are built on.
    """
    assert CHAIN_DECIMALS == {"BTC": 8, "LTC": 8, "GRC": 8}


def test_the_truncation_is_one_implementation_not_two():
    """chains/solana_units still exports it, so its callers were not churned.

    The function moved under rule 8 when chains/base.py became its second caller,
    and the old import path is kept deliberately -- `ruff --fix` deleted the
    re-export once as an unused import, which failed tests/test_solana_units.py at
    collection, so the explicit-alias form carries a noqa with that reason.
    """
    assert reexported is amount_to_base_units


# --- the send path -----------------------------------------------------------


# THE ADDRESS IS DERIVED, NOT WRITTEN. The first version of this file invented a
# plausible-looking bech32 regtest string, and tests/test_address_literals_are_valid.py
# failed twice over it: it is address-SHAPED and does not decode, and it pushed the
# tree back over its literal ceiling. That gate's message says what to do -- "Use
# tests/valid_addresses.py rather than writing one -- a derived address cannot be
# mistyped and says what it is for" -- and the ceiling was not raised (rule 19:
# never raise a baseline to let your own change land).
#
# THEN IT FAILED A THIRD TIME, because the comment explaining all this QUOTED the
# invented string, and the gate scans comments. Naming a bad literal verbatim
# recreates it; describing it does not. That is the gate being right, not pedantic
# -- the 25 undecodable literals it was built to remove arrived one at a time, each
# written by somebody who needed a plausible string for one test.


class RecordingAdapter(RPCAdapter):
    """An RPCAdapter whose `call` records instead of opening a socket."""

    asset = "BTC"

    def __init__(self):
        super().__init__(user="u", password="p", host="127.0.0.1", port=18443)  # noqa: S106 -- not a credential: this stub never opens a socket, and RPCAdapter.__init__ requires the pair.
        self.calls = []

    def call(self, method, *params):
        self.calls.append((method, params))
        return "deadbeef"


def test_the_send_puts_a_FITTED_amount_on_the_wire():
    """The call site, because the fitter being right is not what was broken.

    `self.call("sendtoaddress", address, float(amount))` sent the raw float, and
    the assertion is on what reaches `call` -- not on the return value, which was
    always a plausible txid.
    """
    adapter = RecordingAdapter()

    adapter.send_to_address(BTC_REGTEST_DEPOSIT, 0.00041198765432109)

    assert adapter.calls == [("sendtoaddress", (BTC_REGTEST_DEPOSIT, 0.00041198))]


def test_a_payout_that_fits_to_NOTHING_is_refused_before_the_daemon_is_asked():
    """1e-09 BTC is a tenth of a satoshi, so it quantizes to zero.

    Sending zero is never right, and "amount must be positive" arriving from a
    daemon for a payout WE reduced to zero is the hardest kind of message to trace
    back. The refusal names the cause, and asserts NOTHING was sent -- a version
    that raised after calling would satisfy a message-only assertion while having
    already asked the daemon.
    """
    adapter = RecordingAdapter()

    with pytest.raises(RPCError, match="which is nothing"):
        adapter.send_to_address(BTC_REGTEST_DEPOSIT, 1e-09)

    assert adapter.calls == [], "nothing may reach the daemon once the amount is known to be zero"


def test_a_genuinely_zero_amount_is_not_turned_into_a_refusal_about_precision():
    """Because it is not a precision problem, and the message would misdirect.

    create_swap() already refuses a quote whose output is <= 0, so a zero here
    means something else went wrong upstream; claiming it was "smaller than the
    chain's smallest unit" would send the reader to the wrong place.
    """
    adapter = RecordingAdapter()

    adapter.send_to_address(BTC_REGTEST_DEPOSIT, 0.0)

    assert adapter.calls == [("sendtoaddress", (BTC_REGTEST_DEPOSIT, 0.0))]
