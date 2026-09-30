#!/usr/bin/env python3
"""Every enabled pair can actually be quoted, or this fails naming what is missing.

Role: tests (read-only)
Reads: config.Config and services/pricing.IDS. No network, no database, no chain.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

CONFIG.ALLOWED_PAIRS IS NOT ENOUGH TO ENABLE A PAIR, and this tree has learned
that twice the expensive way. A pair needs three things beyond membership:

  an adapter      chains/registry.build_adapters() must construct both chains
  a USD price     services/pricing.IDS must carry both assets, or create_quote()
                  accepts the swap and then fails on a missing price
  a fee reserve    config.<TO_ASSET>_NETWORK_FEE_RESERVE must exist, or
                  quote_service.network_fee_reserve() refuses -- it is never
                  defaulted to zero, because zero quotes a payout the
                  destination chain will not deliver

THE SECOND ONE COST A CUSTOMER-FACING ERROR. ("XRP", "GRC") and ("GRC", "XRP")
were added on 2026-09-26 and the operator got this from their browser:

    No quote: 'XRP_NETWORK_FEE_RESERVE'

str(KeyError(...)), rendered to a person. The message is a sentence now, but the
PAIR IS STILL BROKEN: no XRP reserve exists, so every GRC->XRP quote refuses,
four days later. Setting that number is a pricing decision and the operator's
(rule 16) -- so this test does not invent one. It fails, by name, until the
number exists or the pair is removed.

That is deliberate: a failing test is how "we enabled a pair nobody can trade"
stops being invisible. It is NOT a ratchet and has no baseline file (rule 19) --
there is one known instance, it is named in the assertion, and the test goes
green the moment either half is resolved.
"""

from __future__ import annotations

import pytest
from config import Config
from services.pricing import IDS

PAIRS = sorted(Config.ALLOWED_PAIRS)

#: The pair enabled without a reserve, and the only one this test tolerates as a
#: KNOWN break -- listed so the failure names it as known rather than as new. Any
#: OTHER pair missing a reserve is an unknown break and fails the strict test.
KNOWN_UNQUOTABLE = {("GRC", "XRP")}

#: PAIRS THAT ARE DELIBERATELY ONE-WAY, each with the reason. This test caught my
#: own asymmetry on the commit that added these two, which is what it is for.
#:
#: XRP->BTC and XRP->LTC take XRP as the INPUT and pay out to an asset whose fee
#: reserve exists. The reverse of each -- BTC->XRP, LTC->XRP -- would pay out in
#: XRP, and XRP_NETWORK_FEE_RESERVE does not exist, so enabling them would enable
#: a pair that refuses every quote. That is the same break ("GRC", "XRP") already
#: has, and adding two more of it to look symmetrical would be choosing the shape
#: of the table over whether a customer can trade.
#:
#: When XRP_NETWORK_FEE_RESERVE is set, all four XRP-payout directions become
#: enable-able at once and this table should empty.
DELIBERATELY_ONE_WAY = {
    ("XRP", "BTC"): "the reverse pays out XRP and XRP_NETWORK_FEE_RESERVE does not exist",
    ("XRP", "LTC"): "the reverse pays out XRP and XRP_NETWORK_FEE_RESERVE does not exist",
}


def test_every_pair_has_a_usd_price_for_both_assets():
    """Without both prices, create_quote() accepts the swap and then cannot price it.

    An accepted swap that cannot be priced is worse than a refused one: the
    customer has been told yes.
    """
    for from_asset, to_asset in PAIRS:
        for asset in (from_asset, to_asset):
            assert asset in IDS, (
                f"({from_asset}, {to_asset}) is enabled but {asset} has no entry in "
                f"services/pricing.IDS, so create_quote() would accept the swap and fail on a "
                f"missing price"
            )


@pytest.mark.parametrize("pair", [p for p in PAIRS if p not in KNOWN_UNQUOTABLE])
def test_every_pair_NOT_known_broken_has_a_network_fee_reserve(pair):
    """The payout asset needs a reserve or the quote refuses.

    MUTATION: add ("BTC", "XRP") to ALLOWED_PAIRS and this fails -- that pair pays
    out XRP and no XRP reserve exists. Verified 2026-09-30; it is the reason the
    two XRP-payout pairs were NOT added alongside the four that were.
    """
    _from_asset, to_asset = pair
    key = f"{to_asset}_NETWORK_FEE_RESERVE"
    assert hasattr(Config, key), (
        f"{pair} is enabled and {key} does not exist, so quote_service.network_fee_reserve() "
        f"refuses every quote for it. A reserve is a pricing decision and is never defaulted to "
        f"zero -- set it, or remove the pair"
    )


def test_the_KNOWN_unquotable_pair_is_STILL_broken_and_says_so():
    """This fails when the break is FIXED, which is the point.

    ("GRC", "XRP") has been enabled and unquotable since 2026-09-26. When
    XRP_NETWORK_FEE_RESERVE is set, this test fails and KNOWN_UNQUOTABLE should
    be emptied -- a tolerated break that outlives its fix is a lie in the test
    suite, and rule 19 says a ratchet that reaches zero gets deleted along with
    its baseline.
    """
    for _from_asset, to_asset in sorted(KNOWN_UNQUOTABLE):
        key = f"{to_asset}_NETWORK_FEE_RESERVE"
        assert not hasattr(Config, key), (
            f"{key} now exists, so ('GRC', '{to_asset}') is quotable and is no longer a known "
            f"break. Remove it from KNOWN_UNQUOTABLE -- and if that empties the set, delete it "
            f"and this test with it"
        )


def test_the_pair_set_is_SYMMETRIC_or_says_which_direction_is_missing():
    """A one-way pair is legitimate and should be deliberate, not accidental.

    SOL can only ever be an INPUT -- chains/solana.py's send_to_address raises
    NotImplementedError -- so a SOL pair would correctly be one-way. Nothing
    enabled today is, and this reports any asymmetry rather than asserting
    against it, because the honest answer depends on which chain can pay out.
    """
    asymmetric = [(a, b) for (a, b) in PAIRS
                  if (b, a) not in Config.ALLOWED_PAIRS and (a, b) not in DELIBERATELY_ONE_WAY]
    assert not asymmetric, (
        f"these pairs are enabled in one direction only: {asymmetric}. That is legitimate when the "
        f"reverse chain cannot pay out (SOL, whose send_to_address raises) or the reverse "
        f"payout asset has no fee reserve, and a mistake otherwise. If it is deliberate, add it "
        f"to DELIBERATELY_ONE_WAY with the reason"
    )


def test_every_DELIBERATELY_one_way_pair_is_actually_enabled_and_actually_one_way():
    """The exemption table cannot outlive what it exempts.

    Two ways it goes stale and both are silent: a row for a pair nobody enabled
    any more, and a row for a pair whose reverse was since enabled -- the second
    of which would hide a real asymmetry behind a stale excuse.
    """
    for pair, reason in sorted(DELIBERATELY_ONE_WAY.items()):
        assert pair in Config.ALLOWED_PAIRS, (
            f"{pair} is exempted from the symmetry check and is not enabled at all. Remove the row"
        )
        reverse = (pair[1], pair[0])
        assert reverse not in Config.ALLOWED_PAIRS, (
            f"{reverse} is now enabled, so {pair} is no longer one-way and its exemption "
            f"({reason}) is stale. Remove the row"
        )
