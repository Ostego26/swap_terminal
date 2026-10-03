"""Can the destination wallet fund the payout. The gate a real deposit paid for.

Role: test (pure functions and stub adapters; opens no socket)
Reads: swap_terminal/services/payout_capacity.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

THE INCIDENT, 2026-10-03, which every fixture here is shaped by. The first
BTC -> GRC swap this terminal ever ran took a flawless deposit -- 0.001 BTC, 2 of
2 confirmations, COUNTED by the gate, credited 12:46:16 and irreversible -- and
the payout died 11 seconds later:

    need   9049.68583412 GRC
    have   3780.08854497 GRC spendable
    short  5269.59728915 GRC   -- the wallet held 41.8% of the payout

"Insufficient funds (rpc code -4)" from the Gridcoin daemon, which was right.
Nothing had compared the two numbers: quote_service.py and swap_service.py held
zero get_balance() calls between them, so the payout's funding was first tested
BY THE DAEMON, at the one moment when refusing costs a customer their deposit
rather than a retry.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from services.payout_capacity import (
    FUNDABLE,
    as_amount,
    largest_fundable_payout,
    why_the_payout_cannot_be_funded,
)

# The real figures from the incident, so a reader comparing this file to the
# commit message is comparing the same numbers.
NEEDED = 9049.68583412
HELD = 3780.08854497
RESERVE = 0.001


class Wallet:
    """An adapter that reports a balance, and records that it was asked."""

    def __init__(self, balance=0.0, raises=None):
        self._balance = balance
        self._raises = raises
        self.asked = 0

    def get_balance(self):
        self.asked += 1
        if self._raises is not None:
            raise self._raises
        return self._balance


class NoBalanceAdapter:
    """An adapter shape with no get_balance at all. No real one is like this.

    Counted 2026-10-03: three adapter classes serve all five chains and every one
    implements get_balance -- chains/base.py (BTC, LTC, GRC), chains/solana.py,
    chains/xrp.py. So this stub stands in for a FUTURE adapter, and the point of
    the test using it is that such a chain is reported, not refused.
    """


def test_a_wallet_that_cannot_cover_the_payout_refuses_with_both_numbers():
    """The incident, as a unit test. Both figures and the shortfall, by name.

    The numbers are asserted individually rather than as a formatted sentence: a
    test pinning the phrasing breaks on a reworded line and passes on a wrong
    subtraction, which is the wrong way round. (Two assertions in this suite were
    rewritten this session for exactly that.)
    """
    verdict = why_the_payout_cannot_be_funded({"GRC": Wallet(HELD)}, "GRC", NEEDED, RESERVE)

    assert verdict.refuses is True
    assert verdict.unchecked is False
    assert str(HELD) in verdict.why, "the operator reads the screen: what the wallet holds has to be on it"
    assert str(NEEDED + RESERVE) in verdict.why, "and what the payout needs, including the chain fee"
    assert str(NEEDED + RESERVE - HELD) in verdict.why, "and the shortfall, so nobody has to subtract"


def test_a_wallet_that_can_cover_it_is_not_refused():
    """The PASS half, or a gate that refused everything would pass the test above."""
    assert why_the_payout_cannot_be_funded({"GRC": Wallet(HELD)}, "GRC", 10.0, RESERVE) == FUNDABLE


def test_the_reserve_is_ADDED_to_what_the_wallet_must_hold():
    """Not subtracted from the payout, and the boundary is where that is visible.

    services/quote_service.create_quote() stopped deducting the reserve from the
    payout, so the wallet must hold the payout AND the chain fee that sends it.
    That sum is exactly what the daemon was short of on 2026-10-03.

    Both sides of the boundary, because a `>` where a `>=` belongs refuses a
    wallet that can pay to the satoshi, and only the exact case shows it.
    """
    exact = 100.0 + RESERVE

    assert why_the_payout_cannot_be_funded({"GRC": Wallet(exact)}, "GRC", 100.0, RESERVE) == FUNDABLE
    short = why_the_payout_cannot_be_funded({"GRC": Wallet(100.0)}, "GRC", 100.0, RESERVE)
    assert short.refuses is True, (
        "a wallet holding exactly the payout and nothing for the fee is the case the daemon refuses, "
        "so a gate that ignores the reserve would pass this swap through to the same failure"
    )


def test_a_balance_that_cannot_be_read_REFUSES_rather_than_passing():
    """Fail-closed, and the two costs are why.

    Fail-open means a swap is created, a deposit is taken and confirmed, and the
    payout discovers the problem -- the 2026-10-03 failure exactly. Fail-closed
    costs a retry. The sentence has to say WHICH happened, or the operator cannot
    tell a short wallet from a daemon that did not answer, and the remedies are
    different.
    """
    verdict = why_the_payout_cannot_be_funded(
        {"GRC": Wallet(raises=RuntimeError("connection refused"))}, "GRC", 10.0, RESERVE
    )

    assert verdict.refuses is True
    assert "could not be read" in verdict.why, "distinguishable from a wallet that is merely short"
    assert "connection refused" in verdict.why, "the daemon's own words, or there is nothing to act on"


def test_a_named_source_account_is_NOT_CHECKED_rather_than_refused():
    """XRP broke this gate within minutes of it being written, and correctly.

    XRPAdapter.get_balance() refuses BY DESIGN -- it has no account of its own,
    and XRP payouts are debited from XRP_DEPOSIT_ACCOUNT via
    account_balance(address). A gate built on get_balance() would have refused
    EVERY XRP-destination swap on a deliberate refusal from a method that was
    never the right question: a posture change far larger than the one that was
    authorized, arriving as a side effect of a method name.

    So `refuses` is asserted False specifically. A verdict-only assertion would
    be satisfied by a version that refused XRP with a nicer sentence.
    """
    verdict = why_the_payout_cannot_be_funded(
        {"XRP": Wallet(0.0)}, "XRP", 1.97, 0.00001, source_account="rPAYOUTACCOUNT",
    )

    assert verdict.refuses is False, "an XRP destination must not be refused on a balance nobody can read"
    assert verdict.unchecked is True, "and must not be reported as verified either"
    assert "rPAYOUTACCOUNT" in verdict.why
    assert "account_balance" in verdict.why, "the line names the read that WOULD answer it"


def test_the_named_account_path_does_not_even_ask_the_adapter():
    """Because the answer would be wrong, not merely unavailable.

    get_balance() on an XRP adapter returns or raises something about a DIFFERENT
    account than the payout is debited from. Asking and discarding would leave a
    reader thinking the figure had been considered.
    """
    wallet = Wallet(0.0)
    why_the_payout_cannot_be_funded({"XRP": wallet}, "XRP", 1.0, 0.0, source_account="rACCOUNT")

    assert wallet.asked == 0


def test_an_adapter_with_no_get_balance_is_reported_not_refused():
    """A chain that cannot be asked is `unchecked`, with the reason.

    No adapter in this tree is like this; the branch exists for a future shape.
    What it must not do is answer "fine" to a question it could not ask, which is
    the defect this whole module removes one level up.
    """
    verdict = why_the_payout_cannot_be_funded({"NEW": NoBalanceAdapter()}, "NEW", 5.0, 0.0)

    assert verdict.refuses is False
    assert verdict.unchecked is True
    assert "NoBalanceAdapter" in verdict.why, "name the adapter, or the reader cannot find it"


def test_a_chain_with_no_adapter_is_not_this_functions_question():
    """chains/registry.unconfigured_chains() owns it and create_swap() asks first.

    Answering it here too would be two sentences for one cause (rule 8), and the
    other one names the variable to export.
    """
    assert why_the_payout_cannot_be_funded({}, "GRC", 10.0, RESERVE) == FUNDABLE


# --- the amount-free question every preview needs ----------------------------


def test_the_ceiling_subtracts_the_reserve_and_says_where_it_came_from():
    """A preview has no payout figure, so it needs the other half of the question."""
    ceiling, how = largest_fundable_payout({"GRC": Wallet(HELD)}, "GRC", RESERVE)

    assert ceiling == HELD - RESERVE
    assert str(HELD) in how and str(RESERVE) in how, "state what the number means, next to the number"


@pytest.mark.parametrize(
    ("adapters", "fragment"),
    [
        ({}, "no GRC adapter"),
        ({"GRC": Wallet(raises=RuntimeError("timed out"))}, "could not be read"),
    ],
)
def test_an_unestablished_ceiling_is_a_sentinel_and_not_a_zero(adapters, fragment):
    """-1.0, because 0.0 is a legitimate answer for an empty wallet.

    Rule 13: "did nothing" must not look like "did work". A caller printing 0.0
    for "the balance did not read" tells the operator their wallet is empty, which
    sends them to fund a wallet that may be full.
    """
    ceiling, how = largest_fundable_payout(adapters, "GRC", RESERVE)

    assert ceiling == -1.0
    assert fragment in how


def test_an_empty_wallet_reports_zero_and_not_the_sentinel():
    """The other side of the line above, or the sentinel and the real zero merge."""
    ceiling, _how = largest_fundable_payout({"GRC": Wallet(0.0)}, "GRC", RESERVE)

    assert ceiling == 0.0, "a wallet that really is empty has a ceiling of zero, which is a measurement"


# --- the displayed figure ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        (3780.08854497 - 0.001, "3780.08754497"),   # the operator's own wallet, the case that shipped wrong
        (1.25 - 0.00002, "1.24998"),                # the case the FIRST attempt got wrong, under by 0.0099
        (100 - 0.001, "99.999"),                    # and the second, under by 0.099
        (0.0, "0"),                                 # not "0.00000000"
        (9049.685834122582, "9049.68583412"),       # the incident's payout
    ],
)
def test_a_displayed_amount_has_no_float_tail_and_is_never_rounded_up(value, shown):
    """Eight decimals, truncated DOWN, trailing zeros gone.

    THE PARAMETRIZATION IS THE ARGUMENT. The first implementation derived the
    precision from how the daemon reported the balance -- "the daemon already told
    us its precision", which reads like deriving from data rather than inventing a
    per-chain table. The first two rows above refuted it: a round balance reports
    few decimals without having declared a precision, so the ceiling was truncated
    coarsely for exactly the wallets whose balance happens to be tidy.

    DOWN matters because this renders a CEILING. Showing one satoshi more than the
    wallet holds presents an unfundable payout as fundable, which is the failure
    this module exists to prevent, reintroduced through a display convention.
    """
    assert as_amount(value) == shown
    assert float(as_amount(value)) <= value + 1e-12, "a ceiling must never be displayed larger than it is"


def test_a_sub_satoshi_ceiling_displays_as_zero_rather_than_as_a_number():
    """5e-09 GRC cannot fund anything, and printing it as 0 says so.

    This is the safe direction and the honest one: the figure is a ceiling on what
    a swap may pay out, and no payout of five billionths of a coin exists.
    """
    assert as_amount(0.000000005) == "0"
