#!/usr/bin/env python3
"""Four wallets in one unit, and the unit itself checked.

Role: tests (read-only)
Reads: nothing. Every input is seeded, including the operator's own balances.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

Operator instruction 2026-09-29: "i'd like to be able to level the value of each
chains wallet relative to the usdc/usdt ratio", then "just make them all about
$1000".

TWO THINGS ARE WORTH TESTING HERE and the rest is formatting.

  THE PEG      no feed publishes a dollar. It publishes what the market pays in
               USDT or USDC and calls it USD. Both have broken their peg -- USDC
               near $0.88 in 2023, USDT near $0.95 in 2022 -- and when the
               yardstick moves every value moves with it while an ordinary
               balance report says nothing. An UNPRICED stablecoin has to read as
               "unchecked" rather than as "fine", which is the case a naive
               implementation gets wrong by omission.
  THE MOVES    a dollar figure AND a coin figure, because the operator compares
               in dollars and acts in coins. Deriving one from the other at the
               print site is how two callers come to round differently.

The seeded prices are the ones CoinPaprika actually returned from the operator's
host that day, so the worked figures below are arithmetic on real numbers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

# EVERY TOLERANCE BELOW IS A Decimal, NOT A FLOAT. pytest.approx multiplies the
# relative tolerance by the expected value, so `rel=1e-6` against a Decimal raises
# "unsupported operand type(s) for *: 'float' and 'decimal.Decimal'" -- which is
# the same float/Decimal boundary the money code itself has to respect, surfacing
# in the test harness instead of in a balance.
from services.wallet_leveling import (
    DEFAULT_TARGET_USD,
    PEG_TOLERANCE,
    WalletValue,
    even_target,
    moves_to,
    peg_findings,
)

# Measured 2026-09-29 from the operator's host.
PRICES = {"BTC": Decimal("83395.85957216246"), "LTC": Decimal("67.30437294473235"),
          "GRC": Decimal("0.016687182941158063"), "XRP": Decimal("1.493710814860317")}
# Their actual spendable balances the same evening.
HELD = {"BTC": Decimal("13879.50050000"), "LTC": Decimal("14945.77514175"),
        "GRC": Decimal("3862.75844485")}

PEGGED = {"USDC": Decimal("1.0001"), "USDT": Decimal("0.9998")}


def _values(chains=("BTC", "LTC", "GRC")):
    return [WalletValue(chain=chain, units=HELD[chain], price_usd=PRICES[chain]) for chain in chains]


def test_the_target_is_1000_dollars_because_the_operator_said_so():
    """A fixed figure, not an even split, and the difference is what it tells you.

    An even split is reachable by moving value between wallets and says nothing
    about how much to acquire. A fixed target says exactly what to fund each
    chain with, which is what "make them all about $1000" asks for.
    """
    assert Decimal(1000) == DEFAULT_TARGET_USD


def test_a_wallet_holding_MORE_than_the_target_reads_as_SURPLUS_in_both_units():
    """The operator's BTC regtest wallet is the extreme case and the useful one.

    13,879.5005 BTC at $83,395.86 is $1.157 BILLION notional against a $1,000
    target, so the move is a surplus of essentially the whole wallet. That the
    figure is absurd is the point: it is testnet coins at mainnet prices, and the
    arithmetic has to survive the absurdity rather than overflow or round to zero.
    """
    moves = {move.chain: move for move in moves_to(_values(), DEFAULT_TARGET_USD)}
    btc = moves["BTC"]
    assert btc.direction == "SURPLUS"
    # 13879.5005 * 83395.85957216246, COMPUTED rather than estimated. The first
    # version of this line said 1157527955, which I arrived at by rounding in my
    # head and asserted as a measurement -- rule 17's register error, in a test.
    assert btc.have_usd == pytest.approx(Decimal("1157492874.63"), rel=Decimal("1e-9"))
    # And the coin figure is the dollar figure at this wallet's own price.
    assert btc.delta_units == pytest.approx(btc.delta_usd / PRICES["BTC"], rel=Decimal("1e-12"))
    # $1000 of BTC is a small fraction of one coin, and that has to survive too.
    assert (DEFAULT_TARGET_USD / PRICES["BTC"]) == pytest.approx(Decimal("0.011991"), rel=Decimal("1e-3"))


def test_a_wallet_holding_LESS_than_the_target_says_how_many_COINS_to_add():
    """GRC at $0.0167 is the other extreme: $1,000 is sixty thousand coins.

    MUTATION: return delta_usd alone and drop delta_units, and this fails -- the
    operator funds a wallet in coins, and converting a dollar figure by hand at
    a price of $0.0167 is where a zero gets lost.
    """
    grc = WalletValue(chain="GRC", units=Decimal(100), price_usd=PRICES["GRC"])
    move = moves_to([grc], DEFAULT_TARGET_USD)[0]
    assert move.direction == "ADD"
    assert move.have_usd == pytest.approx(Decimal("1.6687"), rel=Decimal("1e-4"))
    assert move.delta_units == pytest.approx(Decimal("59826.2322"), rel=Decimal("1e-8")), (
        f"got {move.delta_units}; (1000 - 100*price)/price at ${PRICES['GRC']}"
    )


def test_the_moves_are_ordered_by_what_is_THERE_not_by_chain_name():
    """The question is "what do I move and from where", and the answer starts full.

    A name-ordered list makes the reader sort it themselves, which is the kind of
    work a report exists to have already done (rule 14).
    """
    order = [move.chain for move in moves_to(_values(), DEFAULT_TARGET_USD)]
    assert order == ["BTC", "LTC", "GRC"], (
        f"got {order}. By value: BTC $1.16B, LTC $1.01M, GRC $64.5K -- fullest first"
    )


def test_a_zero_price_gives_zero_coins_rather_than_dividing():
    """"Infinite coins needed" is not a better answer than "this cannot be sized".

    A zero price means the feed said nothing usable. The caller reports the price
    it got; this must not raise ZeroDivisionError three frames up.
    """
    move = moves_to([WalletValue("XXX", Decimal(1), Decimal(0))], DEFAULT_TARGET_USD)[0]
    assert move.delta_units == 0
    assert move.delta_usd == DEFAULT_TARGET_USD


def test_an_even_target_is_the_average_and_needs_no_new_money():
    """The other question, kept because it is a different one.

    Leveling to the average moves value between wallets; leveling to $1000
    acquires or disposes. Both are useful and they are not interchangeable.
    """
    values = _values()
    aim = even_target(values)
    total = sum((value.value_usd for value in values), Decimal(0))
    assert aim == total / 3
    # By construction the moves cancel: nothing enters or leaves the set.
    assert sum((move.delta_usd for move in moves_to(values, aim)), Decimal(0)) == pytest.approx(0, abs=Decimal("1e-6"))


def test_no_wallets_gives_a_target_of_zero_rather_than_dividing_by_zero():
    assert even_target([]) == 0
    assert moves_to([], DEFAULT_TARGET_USD) == []


# ---------------------------------------------------------------------------
# THE PEG. Nothing here refuses on a broken one -- that is posture and the
# operator's -- but everything here makes it impossible to miss.
# ---------------------------------------------------------------------------


def test_two_stablecoins_ON_peg_report_a_usable_yardstick():
    suspect, findings = peg_findings(PEGGED)
    assert suspect is False, findings
    assert any("USDC/USDT" in line and "parity" in line for line in findings), findings


def test_a_stablecoin_OFF_peg_says_every_value_below_is_quoted_in_a_moved_unit():
    """USDC traded near $0.88 in March 2023. That is not a hypothetical shape.

    MUTATION: compare the two against each other only, and this fails -- a pair
    that has BOTH moved together agrees with itself perfectly and is still wrong
    against the dollar.
    """
    suspect, findings = peg_findings({"USDC": Decimal("0.88"), "USDT": Decimal("0.88")})
    assert suspect is True
    assert any("OFF PEG" in line for line in findings), findings
    # They agree with EACH OTHER exactly, which is why the dollar check is separate.
    assert any("USDC/USDT" in line and "parity" in line for line in findings), findings


def test_the_two_DISAGREEING_is_reported_even_when_both_are_near_the_dollar():
    """A spread between them means at least one feed's "USD" is not the other's."""
    suspect, findings = peg_findings({"USDC": Decimal("1.00"), "USDT": Decimal("0.98")})
    assert suspect is True
    assert any("disagree" in line for line in findings), findings


def test_an_UNPRICED_stablecoin_reads_as_UNCHECKED_and_not_as_fine():
    """The case an implementation gets wrong by omission, which is why it is here.

    A feed that does not carry USDC is a reason to say the peg is unchecked, not
    a reason to stop reporting balances -- and silence would read as "fine",
    which is the one thing it cannot mean (rule 14).

    MUTATION: skip absent assets instead of reporting them and this fails: the
    findings come back clean and `suspect` comes back False on a dollar nothing
    was compared against.
    """
    suspect, findings = peg_findings({"USDC": Decimal("1.0")})
    assert suspect is True, findings
    assert any("USDT" in line and "NOT PRICED" in line for line in findings), findings
    assert any("not computable" in line for line in findings), findings

    suspect, findings = peg_findings({})
    assert suspect is True
    assert len([line for line in findings if "NOT PRICED" in line]) == 2, findings


def test_the_tolerance_is_a_REPORTING_threshold_and_is_stated_in_the_finding():
    """An operator reads the screen, not the source (rule 14).

    A bare "OFF PEG" leaves them to guess what counts; the line carries the
    number it was compared against.
    """
    _, findings = peg_findings(PEGGED)
    assert any(str(PEG_TOLERANCE) in line for line in findings), findings
