#!/usr/bin/env python3
"""Every pair the customer page badges AVAILABLE prices a real quote. Behaviorally.

Role: tests (read-only)
Reads: services/pair_view, services/quote_service, config.Config, a seeded price
      cache and a temporary SQLite database. No network, no chain, no live database.
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes

THE PAGE MAKES A PROMISE IN ITS OWN LEDE and on 2026-10-02 it broke it. The
operator had XRP_PAYOUT_SECRET_SEED exported, opened the customer page, and read:

    GRC -> XRP   AVAILABLE   Ready to quote now.
    2 of 13 directions can be quoted right now

They picked it, and the quote refused:

    No quote: No quote: nobody has recorded what one XRP payout costs this desk ...

Two defects in one screen. The doubled prefix is static/script.js's and is fixed
there. The one this file exists for is the badge: the page says "the form below
offers exactly the ones marked available, so what you see here and what you can
pick cannot differ", and it differed.

WHY THE OLD TESTS DID NOT CATCH IT, which is the part worth writing down.
tests/test_allowed_pairs_are_serviceable.py knew the whole fact -- its
KNOWN_UNQUOTABLE set names ("GRC", "XRP") and has since 2026-09-26 -- and
tests/test_customer_page_layout.py asserted that every direction gets exactly one
indicator. Both passed. Neither connected the two halves, because one tested the
CONFIG and the other tested the MARKUP, and the defect was that the badge and the
quote path asked different questions. So this test asks them about the same pair in
the same process and asserts the answers agree, which is the only shape that would
have failed.

PER swap_terminal/CLAUDE.md's behavioral-verification principle: seeded rows through
the real services. Nothing here greps for a condition in source text -- it renders
the verdict, then runs create_quote() and looks at what came back.
"""

from __future__ import annotations

import sqlite3
from time import time

import pytest
from config import Config
from db import SCHEMA, db_session, dict_factory
from services import pricing
from services.pair_view import allowed_pair_rows, customer_availability
from services.pricing import IDS
from services.quote_service import create_quote, why_cannot_quote

# THE SHARED ACCOUNT TABLE, NOT A SECOND COPY OF IT. These are real-format
# accounts that go through a real decode, so inventing strings here would make the
# tag-attributed pairs refuse for their FORMAT and this file would pass for the
# wrong reason. tests/test_customer_page_layout.py imports from the same module.
from test_web_surfaces import DEPOSIT_ACCOUNTS
from workers.common import get_config_dict

PAIRS = sorted(Config.ALLOWED_PAIRS)


class _Reachable:
    """A chain that is reachable, can pay out and will validate any account.

    DELIBERATELY THE MOST PERMISSIVE ADAPTER THIS SUITE CAN BUILD, because the
    question here is not "which chains are configured on this host" -- that moves
    with the shell -- it is whether the badge and the quote agree ONCE the chain
    questions are all answered yes. A stub that refused anything would make the
    pairs unserviceable for a reason that has nothing to do with what is measured.
    """

    can_spend = True
    payout_refusal = ""

    def validate_address(self, _address):
        return True

    def why_cannot_pay_out(self):
        return ""


def _every_chain_reachable():
    return {asset: _Reachable() for asset in IDS}


def _seeded_config(db_path):
    """A config with every deposit account set, so only the reserve can refuse.

    The accounts come from the same table tests/test_web_surfaces.py uses, through
    its own helper, rather than being invented here: deposit_account() runs a REAL
    format decode, so a made-up string would refuse for its shape and this test
    would pass for the wrong reason.
    """
    config = dict(get_config_dict())
    config.update(DEPOSIT_ACCOUNTS)
    config["DB_PATH"] = str(db_path)
    return config


@pytest.fixture
def priced_db(tmp_path):
    """A database with the schema, and a price cache warm for every asset in IDS."""
    db_path = tmp_path / "available.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()

    now = time()
    pricing._cache.update({
        "raw": {cg: {"usd": 100.0, "usd_market_cap": 5_000_000_000.0,
                     "usd_24h_vol": 200_000_000.0, "usd_24h_change": 1.0,
                     "last_updated_at": 1790717713} for cg in IDS.values()},
        "prices": None, "context": None, "source": "seeded",
        "fetched_at": now, "expires_at": now + 999,
    })
    return db_path


def test_a_pair_badged_AVAILABLE_prices_a_quote_and_one_badged_otherwise_is_named(priced_db):
    """The promise, asserted both ways round.

    AVAILABLE must price. That is the half that broke.

    NOT-AVAILABLE must have a reason, and the reason must be one of the four
    conditions rather than a blank -- because a pair refused with nothing said is
    rule 14's empty gap, and because a pair that is refused for no recorded reason
    is how the FOURTH condition came to be missing in the first place.

    MUTATION: drop `cannot_quote` from pair_serviceability()'s `serviceable`
    conjunction and this test fails on GRC -> XRP, naming it. Verified 2026-10-03.
    """
    config = _seeded_config(priced_db)
    adapters = _every_chain_reachable()
    rows = allowed_pair_rows(config, adapters)
    assert rows, "no allowed pairs at all, so this test measured nothing"

    quotable, refused = [], []
    for row in rows:
        pair = (row["from_asset"], row["to_asset"])
        state = customer_availability(row)
        with db_session(str(priced_db)) as db:
            try:
                quote = create_quote(db, config, row["from_asset"], row["to_asset"], 10)
            except ValueError as exc:
                refused.append((pair, str(exc)))
                priced = False
            else:
                quotable.append(pair)
                priced = quote["output_amount_estimate"] > 0

        if state["available"]:
            assert priced, (
                f"the customer page badges {row['label']} {state['word']} -- {state['note']} -- and a "
                f"real quote for it did not price. The page's own lede promises the form offers exactly "
                f"the available ones, so this is the promise broken, not a stale note. What the quote "
                f"said: {dict(refused).get(pair, '(it priced to zero)')}"
            )
        else:
            assert row["reason"].strip(), (
                f"{row['label']} is not offered and the row carries no reason, so neither surface can "
                f"say what refused it"
            )

    # RULE 14: the counts, with what they were counted out of, and what they mean.
    # A bare pass tells a reader nothing about whether the suite exercised one pair
    # or thirteen -- and this file's whole subject is a page that printed
    # "2 of 13 directions can be quoted right now" while one of the two could not.
    print(f"\nof {len(rows)} allowed pairs: {len(quotable)} priced a real quote, "
          f"{len(refused)} refused. Refusals (each needs a reason on the row):")
    for pair, message in refused:
        print(f"  {pair[0]} -> {pair[1]}: {message.split('.')[0]}")


def test_the_quote_authority_and_the_pair_badge_read_THE_SAME_function(priced_db):
    """The fourth condition is asked of quote_service, not re-derived in pair_view.

    RULE 8, AND THIS EXACT VERDICT HAS ALREADY COST FOUR IMPLEMENTATIONS. The
    pay-out test was written in four places and three of them were wrong (see
    services/pair_view.py's header). The fix for the quotability hole is a function
    in services/quote_service.py that pair_view CALLS -- so this asserts the two
    answers are the same object's, by checking that a pair pair_view refuses for
    `cannot_quote` is refused by why_cannot_quote() with the identical sentence.

    MUTATION: have pair_view spell its own `hasattr(Config, ...)` test instead of
    calling why_cannot_quote(). The booleans would still agree; the STRINGS would
    not, because nobody writes the same sentence twice. Verified 2026-10-03.
    """
    config = _seeded_config(priced_db)
    rows = allowed_pair_rows(config, _every_chain_reachable())
    unquotable = [row for row in rows if row["cannot_quote"]]
    if not unquotable:
        pytest.skip("every allowed pair has a fee reserve, so there is nothing to compare")

    for row in unquotable:
        assert row["cannot_quote"] == why_cannot_quote(config, row["to_asset"]), (
            f"{row['label']}'s refusal on the page is not the sentence "
            f"services/quote_service.why_cannot_quote() returns, so there are two copies of this "
            f"condition and they have already drifted"
        )
        # AND IT IS THE SAME SENTENCE THE CUSTOMER'S OWN ATTEMPT WOULD GET, which is
        # the property that was actually missing: a page may word a refusal for a
        # different reader, but it must not refuse for a different REASON.
        with db_session(str(priced_db)) as db, pytest.raises(ValueError) as refusal:
            create_quote(db, config, row["from_asset"], row["to_asset"], 10)
        assert row["cannot_quote"] in str(refusal.value), (
            f"the page says {row['cannot_quote'][:60]!r} and the quote path says "
            f"{str(refusal.value)[:60]!r}"
        )
