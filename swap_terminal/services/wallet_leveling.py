#!/usr/bin/env python3
"""What each wallet is worth in dollars, and what it would take to level them.

Role: submodule -> functions (the decisions; nothing here fetches or prints)
Reads: nothing. Every input is passed in.
Writes: nothing
Can move funds: no. It computes what a move WOULD be. Nothing here sends.
Mainnet-safe: yes

WHY A DOLLAR IS NOT A GIVEN, which is the operator's instruction of 2026-09-29:
"level the value of each chains wallet relative to the usdc/usdt ratio."

Four wallets on four chains hold four different coins, and "which is heaviest"
has no answer until they are all in one unit. The unit is a dollar, and the
awkward part is that nobody publishes a dollar -- a price feed publishes what
the market pays in USDT or USDC and calls it USD. Those two are pegged, not
fixed, and they have both broken their peg: USDC traded near $0.88 in March
2023 and USDT near $0.95 in 2022. When the yardstick moves, every "value" below
moves with it and nothing in an ordinary balance report says so.

So the peg is MEASURED and REPORTED rather than assumed:

  the two against the dollar   each should be 1.0. A deviation is the yardstick
                               itself drifting, and it scales every figure here.
  the two against each other   USDC/USDT should be 1.0. If they disagree, at
                               least one feed's "USD" is not the other's, and a
                               value derived from either carries that spread.

Nothing here REFUSES on a broken peg. A refusal would be a posture decision and
the operator's (rule 16); what this owes them is the number, beside the figures
it affects.

AND THE VALUES ARE NOTIONAL ON A TEST NETWORK, which has to be said louder than
it is comfortable to say. A regtest wallet holding 13,879 BTC is worth
$1.16 billion at a mainnet price and nothing at all in fact -- the coins exist
on a private chain nobody else has. Every function here is arithmetic on a
hypothetical, and it is useful for exactly one thing: rehearsing the sizing of a
real book before there is one. A caller that prints these numbers without saying
which they are is lying to its reader, which is why format_leveling_block()
carries the label rather than leaving it to each caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: What a stablecoin should be worth, and how far off is worth saying out loud.
#: 0.005 is half a cent on the dollar -- below that is ordinary feed noise between
#: venues, and above it is the kind of move that was news both times it happened.
#: This is a REPORTING threshold and not a refusal: the number is printed either
#: way, and the flag only decides whether it is called out.
DOLLAR = Decimal(1)
PEG_TOLERANCE = Decimal("0.005")

#: WHAT EACH WALLET SHOULD BE WORTH. Operator instruction 2026-09-29: "just make
#: them all about $1000." A fixed figure rather than an even split of what is
#: already there, and the difference matters: an even split is reachable by moving
#: value between wallets and tells you nothing about how much to acquire, while a
#: fixed target says exactly what to fund each chain with. even_target() below is
#: kept for the other question and is not the default.
DEFAULT_TARGET_USD = Decimal(1000)

#: The stablecoins the dollar is checked against. Two rather than one, because a
#: single one cannot tell "this peg slipped" from "this feed is wrong" -- two that
#: agree with each other and with the dollar are evidence; one is an assertion.
PEG_ASSETS = ("USDC", "USDT")


@dataclass(frozen=True)
class WalletValue:
    """One chain's spendable holding, and what a feed says it is worth.

    Frozen because it is a MEASUREMENT of two things at one instant -- a balance
    and a price -- and a value whose inputs can be reassigned afterwards is a
    figure that can stop agreeing with either.
    """

    chain: str
    units: Decimal
    price_usd: Decimal

    @property
    def value_usd(self) -> Decimal:
        return self.units * self.price_usd


@dataclass(frozen=True)
class Move:
    """What one wallet would need to reach a target. Positive means ADD.

    BOTH A DOLLAR FIGURE AND A COIN FIGURE, because the operator acts in coins
    and compares in dollars. Deriving one from the other at the print site is how
    two callers come to round differently (rule 8).
    """

    chain: str
    have_usd: Decimal
    target_usd: Decimal
    price_usd: Decimal

    @property
    def delta_usd(self) -> Decimal:
        return self.target_usd - self.have_usd

    @property
    def delta_units(self) -> Decimal:
        """The coins to add (positive) or take out (negative).

        Returns 0 rather than dividing when the price is 0. A zero price means the
        feed said nothing usable, and "infinite coins needed" is not a better
        answer than "this cannot be sized" -- the caller reports the price it got.
        """
        if self.price_usd == 0:
            return Decimal(0)
        return self.delta_usd / self.price_usd

    @property
    def direction(self) -> str:
        if self.delta_usd > 0:
            return "ADD"
        if self.delta_usd < 0:
            return "SURPLUS"
        return "LEVEL"


def peg_findings(prices: dict[str, Decimal]) -> tuple[bool, list[str]]:
    """(is the yardstick suspect, one line per finding).

    NEVER RAISES ON A MISSING STABLECOIN. A feed that does not carry USDC is a
    reason to say the peg is unchecked, not a reason to stop reporting balances --
    and "unchecked" must not read as "fine", which is why an absent price produces
    a finding of its own rather than silence (rule 14).
    """
    findings, suspect = [], False
    for asset in PEG_ASSETS:
        price = prices.get(asset)
        if price is None:
            findings.append(f"{asset}: NOT PRICED -- the dollar this report uses is unchecked against it")
            suspect = True
            continue
        drift = abs(price - DOLLAR)
        if drift > PEG_TOLERANCE:
            findings.append(f"{asset}: ${price} is {drift} off the dollar  <- OFF PEG by more than "
                            f"{PEG_TOLERANCE}; every value below is quoted in a unit that has moved")
            suspect = True
        else:
            findings.append(f"{asset}: ${price}, within {PEG_TOLERANCE} of the dollar")

    usdc, usdt = prices.get("USDC"), prices.get("USDT")
    if usdc and usdt and usdt != 0:
        ratio = usdc / usdt
        off = abs(ratio - DOLLAR)
        findings.append(
            f"USDC/USDT = {ratio}"
            + (f"  <- the two disagree by {off}, so at least one feed's 'USD' is not the other's"
               if off > PEG_TOLERANCE else f", within {PEG_TOLERANCE} of parity")
        )
        suspect = suspect or off > PEG_TOLERANCE
    else:
        findings.append("USDC/USDT: not computable -- one or both is unpriced")
        suspect = True
    return suspect, findings


def even_target(values) -> Decimal:
    """The per-wallet figure that levels them without adding or removing anything.

    The total divided by the count, which is the only target reachable by moving
    value BETWEEN wallets. Returns 0 for no wallets rather than raising: a caller
    with nothing to level has a report to print, not an error to handle.
    """
    values = list(values)
    if not values:
        return Decimal(0)
    return sum((value.value_usd for value in values), Decimal(0)) / len(values)


def moves_to(values, target_usd: Decimal) -> list[Move]:
    """What each wallet needs to reach `target_usd`, largest surplus first.

    SORTED BY WHAT IS THERE, not by chain name, because the question this answers
    is "what do I move and from where" and the answer starts at the fullest
    wallet. A name-ordered list makes the reader do that sort themselves.
    """
    return sorted(
        (Move(chain=value.chain, have_usd=value.value_usd, target_usd=target_usd,
              price_usd=value.price_usd) for value in values),
        key=lambda move: move.have_usd,
        reverse=True,
    )
