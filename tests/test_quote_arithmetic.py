"""What a customer is charged, asserted on the real create_quote() arithmetic.

Role: test (pure arithmetic through the real function; one seeded price cache)
Reads: services/quote_service.py, services/swap_service.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS. The operator's instruction, 2026-10-02, was "fix the
regressive reserve". What made it regressive is that `output_amount_estimate`
subtracted a FLAT amount from a PERCENTAGE fee, so the reserve's share of a
payout fell as the payout grew. Measured across their six delivered payouts:

    56 GRC gross    1.8bps        89 GRC gross    1.1bps
    84 GRC gross    1.2bps      3134 GRC gross    0.0bps

a sixty-fold spread in what a customer paid, decided by nothing but the size of
their swap, on top of a schedule that says 150bps flat.

AND IT WAS NOT PAYING FOR ANYTHING, which is what made this a defect rather than
a pricing preference. `sendtoaddress(address, amount)` delivers `amount` exactly
and takes its fee from the wallet's own inputs. Measured to the last digit on the
operator's host 2026-10-01: `getreceivedbyaddress mmr6ATb3...` read 143.39622296
GRC against two output_amount_estimate values summing to 143.39622296 --
difference zero. Had the reserve funded the fee the recipient would have been
0.02 GRC short across those two payouts. The real fee was 0.001 GRC, a tenth of
the reserve, and the wallet paid it separately.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from time import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA, db_session, dict_factory  # noqa: E402
from services import pricing  # noqa: E402
from services.pricing import IDS  # noqa: E402
from services.quote_service import create_quote, get_network_fee_reserve  # noqa: E402
from workers.common import get_config_dict  # noqa: E402

FEE_BPS = 150
RESERVE = 0.01


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "quotes.db"
    connection = sqlite3.connect(path)
    connection.row_factory = dict_factory
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()

    now = time()
    pricing._cache.update({
        "raw": {cg: {"usd": 100.0, "usd_market_cap": 5_000_000_000.0,
                     "usd_24h_vol": 200_000_000.0, "usd_24h_change": 1.0,
                     "last_updated_at": 1790717713} for cg in IDS.values()},
        "prices": None, "context": None, "source": "seeded",
        "fetched_at": now, "expires_at": now + 999,
    })
    settings = dict(get_config_dict())
    settings["DB_PATH"] = str(path)
    settings["DEFAULT_FEE_BPS"] = FEE_BPS
    settings["GRC_NETWORK_FEE_RESERVE"] = RESERVE
    return settings


def quote_for(config, amount: float) -> dict:
    with db_session(config["DB_PATH"]) as db:
        return create_quote(db, config, "SOL", "GRC", amount)


# ------------------------------------------- the customer pays the schedule, flat


@pytest.mark.parametrize("amount", [0.01, 0.25, 1.0, 10.0])
def test_the_payout_is_the_gross_less_the_fee_and_nothing_else(config, amount):
    """No reserve term. This is the arithmetic the whole change is.

    Parametrized across three orders of magnitude, because the defect was that the
    answer DEPENDED on the magnitude.
    """
    quote = quote_for(config, amount)
    gross = quote["input_amount"] * quote["quoted_rate"]

    assert quote["output_amount_estimate"] == pytest.approx(gross * (1 - FEE_BPS / 10000.0), rel=1e-12)
    assert quote["output_amount_estimate"] > gross * (1 - FEE_BPS / 10000.0) - RESERVE


@pytest.mark.parametrize("amount", [0.01, 0.25, 1.0, 10.0])
def test_the_realized_fee_is_the_scheduled_fee_at_every_size(config, amount):
    """THE PROPERTY THE OPERATOR ASKED FOR, stated as the one sentence it is.

    Before: 151.77bps on a 56 GRC gross and 150.03bps on a 3134 GRC one, from one
    150bps schedule. After: 150.00 at both, and at every size in between.
    """
    quote = quote_for(config, amount)
    gross = quote["input_amount"] * quote["quoted_rate"]
    retained_bps = (gross - quote["output_amount_estimate"]) / gross * 10000

    assert retained_bps == pytest.approx(float(FEE_BPS), abs=1e-9)


def test_the_reserve_is_still_recorded_on_the_quote(config):
    """It is a COST now, not a charge -- and dropping the column would lose the one
    figure that says whether a swap's margin covers its own payout."""
    quote = quote_for(config, 0.25)
    assert quote["network_fee_reserve"] == RESERVE


def test_a_missing_reserve_still_refuses_and_says_why_it_matters_now(config):
    """The gate survives, with its justification corrected.

    It is no longer "the chain will not deliver a payout quoted without it" -- the
    chain delivers fine, which is the measurement above. It is that a pair whose
    chain cost nobody wrote down is a pair whose MARGIN is unknown.
    """
    del config["GRC_NETWORK_FEE_RESERVE"]
    with pytest.raises(ValueError, match="costs this desk") as raised:
        get_network_fee_reserve(config, "GRC")

    message = str(raised.value)
    assert "will not deliver" not in message, (
        "the retracted justification must not survive in the message"
    )
    assert "NOT out of the customer's payout" in message
