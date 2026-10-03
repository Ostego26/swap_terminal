#!/usr/bin/env python3
"""The `payouts` row records what the CHAIN sent, not what the quote computed.

Role: tests (pure functions, a real temp database, stub adapters; no socket)
Reads: chains/payout_quantization.py, chains/coin_amounts.py,
      chains/xrp_units.py, chains/solana_units.py, chains/base.py,
      services/payout_service.py, config.Config.ALLOWED_PAIRS
Writes: a temp SQLite database per test. Nothing to any chain.
Can move funds: no. Every adapter here either records its RPC calls instead of
      opening a socket, or runs against tests/xrp_seeded_transport.py with
      submit_and_wait stubbed out. No key is read and no amount is broadcast.
Mainnet-safe: yes

=============================================================================
THE DEFECT, MEASURED 2026-10-03 ON A REAL COMPLETED SWAP
=============================================================================

The `payouts` row recorded the amount the QUOTE computed, not the amount the
CHAIN actually sent. Measured on swap s_539d922e9ef0a5d8 (GRC -> XRP) by
querying the XRP testnet ledger directly for the transaction the row names,
799F8DED7CFB657411C5B1B9BE500C79CD62F07CF5634D2817804E89BDED7935:

    payouts.amount    3.3155893288590605 XRP
    the ledger        Amount 3315589 drops = 3.315589 XRP, Fee 10,
                      tesSUCCESS, validated true

The record overstated the payment by 0.00000032885906 XRP -- less than one
drop, so the ledger cannot express the number the database claims was paid.

AND IT WAS NEVER XRP-SPECIFIC. The same day, on the same host:

    LTC payout    booked 1.1996736819422498, broadcast 1.19967368
    BTC payout    broadcast 0.00401780
    GRC payout    booked 2701.3495803173805, broadcast 2701.34958031

ONE FIGURE IN THE BRIEF DOES NOT RECONCILE AND IS RECORDED AS SUCH (rule 17).
The LTC -> BTC case was described as "quoted 1.1996736819422498 and 0.00401780
went on chain", and those two numbers are not related by any quantization:
1.1996736819422498 truncates to 1.19967368 at eight decimals, which is the
figure the BTC -> LTC payout broadcast. 0.00401780 is a different payout's
amount, already exact at eight decimals, so nothing can be concluded about what
IT was booked as. Both numbers are kept below as the cases they are --
1.1996736819422498 -> 1.19967368 for the booked/broadcast pair, and 0.00401780
as an already-quantized amount that must come back untouched -- rather than
asserted as one swap.

THE CAUSE WAS WHERE THE QUANTIZATION HAPPENED, not whether it happened. Every
adapter already reduced the figure to its chain's precision and every one of
them returns only a txid, so services/payout_service.py could not learn what
had been sent even in principle. The fix quantizes ONCE, before the record, in
chains/payout_quantization.quantize_for_chain(), which dispatches to the same
conversion each adapter performs.

=============================================================================
WHAT THESE TESTS ESTABLISH, AND THE ONE THING THEY DO NOT
=============================================================================

ESTABLISHED, against seeded rows and the real code:

  - the recorded figure equals the figure handed to the chain, on every chain
  - quantizing an already-quantized amount returns it UNCHANGED, which is the
    property that makes the broadcast amount provably unaffected: the adapter
    quantizes AGAIN after the service has
  - the amount on the wire is the same number it was before this change
  - one figure is reserved and released, so no sub-unit residue is stranded in
    wallet_inventory.hot_reserved -- and the hazard is demonstrated separately,
    by reserving one figure and releasing another on purpose
  - a payout that quantizes to nothing still reaches the adapter's own refusal

NOT ESTABLISHED: nothing here was broadcast. The XRP case runs the real adapter
and the real xrpl-py serializer against a seeded transport with
submit_and_wait stubbed, so what is measured is the Payment that WOULD have
been submitted. The 3315589-drop figure it reproduces was read off the live
testnet ledger; the reproduction is in this process.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from decimal import Decimal

import pytest
from chains.base import RPCError
from chains.coin_amounts import CHAIN_DECIMALS, amount_to_base_units, fit_to_chain_precision
from chains.payout_quantization import QUANTIZERS, quantize_for_chain
from chains.solana import SolanaAdapter
from chains.solana_units import SOL_DECIMALS, base_units_to_amount
from chains.xrp import XRPAdapter
from chains.xrp_payout_seed import SIGNING_SEED_ENV_VAR
from chains.xrp_units import DROPS_PER_XRP, from_drops, to_drops
from config import Config
from db import SCHEMA, apply_migrations, connect_db
from recording_rpc_adapter import RecordingRPCAdapter
from services.payout_service import (
    amount_decided_and_logged,
    process_pending_payouts,
    release_inventory_after_send,
    reserve_inventory,
)

# SeededLedger IS IMPORTED, NOT REBUILT (rule 8). It answers server_info and
# account_info from tests/xrp_seeded_transport.py's measured payloads AND replaces
# xrpl.transaction.submit_and_wait -- one object on purpose, because every assertion
# about the XRP payout path is a pair ("it did this" AND "nothing was submitted") and
# two fixtures let a test take the first and forget the second.
#
# It lives in tests/test_xrp_payout_wiring.py rather than in xrp_seeded_transport.py
# because it closes over that file's `pytest.importorskip("xrpl.transaction")`: moving
# it into the shared support module would make that module import xrpl-py at import
# time, and xrpl-py is an OPTIONAL dependency here. A pointer back to this importer is
# recorded at its definition, so a reader who finds one is told the other exists.
from test_xrp_payout_wiring import SeededLedger
from valid_addresses import (
    BTC_PARTICIPANT,
    GRC_PAYOUT,
    LTC_PARTICIPANT,
    SOL_PAYOUT,
    XRP_CUSTOMER_PAYOUT,
    XRP_HOT_ACCOUNT,
)
from xrp_seeded_transport import TESTNET_URL, account_info, server_info

# xrpl-py is OPTIONAL and importorskip is the mechanism, for the reason
# tests/test_xrp_adapter.py records: a plain `import xrpl.wallet` at the top of the
# file runs BEFORE any skip could take effect and would break COLLECTION on a host
# without the library. No suppression is needed (rule 19).
wallet_module = pytest.importorskip(
    "xrpl.wallet", reason="xrpl-py absent, so the signed Payment's Amount field is UNCHECKED"
)

# THE FIGURES FROM THE LEDGER AND FROM THE OPERATOR'S HOST, in one place so no
# test retypes one. Each is (asset, what was booked, what the chain sent).
#
# THE SOL ROW IS DERIVED, NOT MEASURED, and the difference is named rather than
# blurred (rule 17). SOL IS a payout destination -- ALLOWED_PAIRS has carried
# ("GRC","SOL"), ("BTC","SOL") and ("LTC","SOL") since 79c4808 earlier the same
# day -- so this is not a hypothetical chain. What is unmeasured is the figure:
# no SOL payout appears in the `payouts` table of the database in this checkout
# (0 rows, read 2026-10-03), and nothing in this container can see the operator's
# host, so 1.199673681 is what chains/solana.SolanaAdapter.build_transfer_plan()
# computes for the same booked amount at native SOL's nine decimals rather than
# something a cluster answered.
#
# I FIRST WROTE "no pair in Config.ALLOWED_PAIRS pays out in SOL", copied from
# services/payout_service.py's module header, and it was false when I wrote it:
# the header was measured before 79c4808 landed and nobody updated it. Both are
# corrected, and the header now says so (rule 16). One grep of ALLOWED_PAIRS
# settled it, which is the whole of rule 17.
MEASURED_PAYOUTS = [
    ("XRP", 3.3155893288590605, 3.315589),
    ("LTC", 1.1996736819422498, 1.19967368),
    ("BTC", 1.1996736819422498, 1.19967368),
    ("GRC", 2701.3495803173805, 2701.34958031),
    ("SOL", 1.1996736819422498, 1.199673681),
]

#: The BTC payout from the brief: 0.00401780 on the chain, 0.0040178 as a Python
#: float, the same number. SEVEN decimal places.
ALREADY_QUANTIZED = 0.00401780

#: An amount per chain that is ALREADY at that chain's precision, so quantizing
#: must return it as itself rather than re-derive it through Decimal into
#: something that merely compares equal.
#:
#: I WROTE 0.00401780 FOR ALL FIVE AND THE TEST REFUTED IT, which is recorded
#: rather than quietly corrected (rule 17). It has seven decimals, so it is exact
#: for the three 8-decimal chains and for SOL's nine -- and it is NOT exact for
#: XRP, whose drop is 1e-6: 0.0040178 XRP is 4017.8 drops and quantizes to
#: 0.004018. The sentence "exact at eight decimals, at six, and at nine" was
#: plausible and wrong, and it took one run to find out.
#:
#: XRP's row is therefore 3.315589 -- the figure the XRP testnet actually reported
#: for swap s_539d922e9ef0a5d8, which is already a whole number of drops by
#: construction because the ledger produced it.
ALREADY_QUANTIZED_PER_CHAIN = [
    ("BTC", ALREADY_QUANTIZED),
    ("LTC", ALREADY_QUANTIZED),
    ("GRC", ALREADY_QUANTIZED),
    ("SOL", ALREADY_QUANTIZED),
    ("XRP", 3.315589),
]

#: Every asset this module claims to cover, pinned so that losing one is a
#: failure rather than a silent pass-through.
EVERY_CHAIN = ("BTC", "LTC", "GRC", "XRP", "SOL")


def _sample_amounts() -> list[float]:
    """60,000 random amounts from 1e-9 to 1e7, plus the figures that matter.

    SEEDED, so the measurement in the docstrings is the measurement this runs.
    `random.seed(20261003)` then 60,000 draws of uniform(0,1) * 10**randint(-9,7)
    is exactly the sample that produced the counts recorded in
    chains/payout_quantization.py's header and in
    chains/xrp_units.to_drops()'s docstring.

    A RANDOM SAMPLE RATHER THAN HAND-PICKED CASES, because the property under
    test is universal and hand-picked cases test the imagination of whoever
    picked them. The named figures are appended rather than substituted: a
    sample that happened to miss the shape of a real payout would be the weaker
    measurement.
    """
    named = [
        0.0, 1e-12, 1e-09, 1e-07, ALREADY_QUANTIZED, 0.0975, 1.0,
        1.1996736819422498, 3.3155893288590605, 55.52645238, 2701.3495803173805,
        9999999.123456789, 1e9 + 0.123456789, 1e15 + 0.5, 1e20, 0.1 + 0.2,
    ]
    generator = random.Random(20261003)  # noqa: S311 -- not cryptographic: this picks test amounts, and it is seeded precisely so the sample is reproducible.
    return named + [generator.uniform(0, 1) * 10 ** generator.randint(-9, 7) for _ in range(60000)]


# --- the function ------------------------------------------------------------


@pytest.mark.parametrize(("asset", "booked", "sent"), MEASURED_PAYOUTS)
def test_the_quantized_figure_is_THE_ONE_THE_CHAIN_SENT(asset, booked, sent):
    """The measured defect, one case per chain. See this module's docstring.

    THE EXPECTED VALUES ARE LEDGER AND DAEMON READINGS, not this function's own
    output written down after the fact -- which is the difference between a test
    and a snapshot. 3.315589 XRP is the 3315589 drops the XRP testnet reported
    for swap s_539d922e9ef0a5d8; 1.19967368 and 2701.34958031 are what the
    Litecoin and Gridcoin payouts actually broadcast.

    MUTATION: replace `from_drops(drops)` in _quantize_xrp() with `amount`, or
    drop "SOL" from QUANTIZERS. Each fails exactly one of these cases.
    """
    quantized, why = quantize_for_chain(booked, asset)

    assert quantized == sent
    assert why, "a figure that MOVED has to say so where an operator reconciling a chain can see it"


@pytest.mark.parametrize(("asset", "amount"), ALREADY_QUANTIZED_PER_CHAIN)
def test_an_already_quantized_amount_comes_back_UNTOUCHED_and_silent(asset, amount):
    """An amount already at the chain's precision must not move, and must say nothing.

    Both halves matter. The figure must not move, and `why` must be empty -- a
    note on every payout saying "nothing changed" is noise that trains an
    operator to skim past the one that says something did (rule 14).

    THE AMOUNTS ARE PER CHAIN BECAUSE THE FIRST VERSION OF THIS TEST WAS WRONG
    AND SAID SO LOUDLY: it used 0.00401780 for all five, and XRP failed with
    0.004018 != 0.0040178. See ALREADY_QUANTIZED_PER_CHAIN above -- seven
    decimals is exact for eight and nine and not for six.

    MUTATION: make _quantize_xrp() always return `from_drops(to_drops(amount)),
    "<a note>"`. The `why == ""` half fails on every chain, which is the half a
    reader is least likely to think of.
    """
    quantized, why = quantize_for_chain(amount, asset)

    assert quantized == amount
    assert why == ""


@pytest.mark.parametrize("asset", EVERY_CHAIN)
def test_quantizing_an_ALREADY_QUANTIZED_amount_returns_IT_UNCHANGED(asset):
    """IDEMPOTENCE, AND IT IS THE WHOLE LICENSE FOR THIS CHANGE.

    The service now quantizes before recording, and the adapter quantizes again
    before sending. So the only way the figure on the wire could move is if
    quantizing twice differed from quantizing once. Measured here over 60,016
    amounts per chain rather than argued: 0 non-idempotent on every one of the
    five.

    If this ever fails for a chain, the fix CHANGES A BROADCAST AMOUNT on that
    chain, which is live posture and the operator's call (rule 16) -- not
    something to paper over by quantizing in only one place.

    MUTATION: in chains/coin_amounts.amount_to_base_units(), replace
    `Decimal(str(amount))` with `Decimal(amount)`. That is the hazard its own
    docstring names, and it breaks this for the Bitcoin family and for SOL.
    """
    non_idempotent = []
    for amount in _sample_amounts():
        once, _why = quantize_for_chain(amount, asset)
        twice, _again = quantize_for_chain(once, asset)
        if twice != once:
            non_idempotent.append((amount, once, twice))

    assert non_idempotent == [], (
        f"{asset}: {len(non_idempotent)} amounts changed on a SECOND quantization, so the adapter "
        f"would send something other than what was recorded"
    )


@pytest.mark.parametrize("asset", EVERY_CHAIN)
def test_the_quantizer_CALLS_the_conversion_the_adapter_calls(asset):
    """Not a reimplementation of it (rule 8: deriving beats maintaining two copies).

    The three right-hand sides here are the exact expressions the three send
    paths evaluate:

        chains/base.RPCAdapter.send_to_address()        fit_to_chain_precision()
        chains/xrp.XRPAdapter.preview_payout()          to_drops()
        chains/solana.SolanaAdapter.build_transfer_plan()  amount_to_base_units()

    So a drift in any of them is a drift in both the record and the send, in the
    same direction, which is the only arrangement in which the two cannot
    disagree.

    ASSERTED OVER THE WHOLE SEEDED SAMPLE rather than on hand-picked values,
    because a reimplementation that is wrong only on ties, or only in the last
    place, is exactly the kind that survives three chosen cases. 60,016 amounts
    per chain.

    MUTATIONS RUN, both caught here: `_quantize_xrp()` returning the requested
    amount instead of from_drops(to_drops(...)) fails the XRP case, and
    `_quantize_bitcoin_family()` rewritten as `round(amount, 8)` -- a
    reimplementation that looks equivalent and rounds to nearest -- fails BTC,
    LTC and GRC.
    """
    for amount in _sample_amounts():
        quantized, _why = quantize_for_chain(amount, asset)
        if not amount > 0:
            assert quantized == amount, "a non-positive amount is not quantized at all"
            continue
        if asset in CHAIN_DECIMALS:
            expected, _ = fit_to_chain_precision(amount, asset)
        elif asset == "XRP":
            expected = from_drops(to_drops(amount))
        else:
            expected = base_units_to_amount(amount_to_base_units(amount, SOL_DECIMALS), SOL_DECIMALS)
        assert quantized == expected, f"{asset}: {amount!r} did not go through the adapter's own conversion"


def test_ONLY_XRP_can_quantize_a_payout_UPWARD():
    """The asymmetry between the five conversions, measured rather than described.

    chains/coin_amounts.fit_to_chain_precision() TRUNCATES, and its docstring
    argues the direction at length: the desk never sends more than it quoted.
    chains/xrp_units.to_drops() uses ROUND_HALF_UP, so an XRP payout can be
    rounded UP to the next whole drop.

    Measured 2026-10-03 over the 60,000 random amounts in _sample_amounts():
    24,850 of them -- 41.4% -- quantize upward for XRP, by at most half a drop
    (0.0000005 XRP). Zero do for BTC, LTC, GRC or SOL.

    THE EXACT COUNT IS IN THIS DOCSTRING AND NOT IN AN ASSERTION, deliberately:
    pinning 24850 would make this test fail the day a Python release changes
    `random`'s internals, for a reason that has nothing to do with payouts. What
    is asserted is the part that is a property rather than a sample -- the four
    chains that can never round up, and the half-drop bound on the one that can.

    NOT FIXED HERE, AND THAT IS RULE 16 RATHER THAN INDIFFERENCE. to_drops() is
    what chains/xrp.py has always called, so ROUND_HALF_UP is what the ledger
    has been receiving all along; switching it to truncation would change what
    gets sent by up to one drop. This change makes the RECORD agree with it and
    leaves the direction to the operator.

    MUTATION: change to_drops()'s rounding to ROUND_DOWN. The XRP half of this
    fails, which is what makes it a measurement of the live behavior rather than
    a restatement of the constant.
    """
    sample = [amount for amount in _sample_amounts() if amount > 0]

    for asset in ("BTC", "LTC", "GRC", "SOL"):
        over = [a for a in sample if quantize_for_chain(a, asset)[0] > a]
        assert over == [], f"{asset} quantized {len(over)} amounts UPWARD; it must only ever truncate"

    xrp_over = [a for a in sample if quantize_for_chain(a, "XRP")[0] > a]
    assert len(xrp_over) > len(sample) // 3, (
        "XRP's ROUND_HALF_UP should round a large minority of amounts up; measured 41.4% on "
        "2026-10-03. Finding none means to_drops() stopped rounding half up, which CHANGES WHAT "
        "IS SENT and is the operator's call"
    )
    half_a_drop = Decimal(1) / 2 / DROPS_PER_XRP
    worst = max(Decimal(str(quantize_for_chain(a, "XRP")[0])) - Decimal(str(a)) for a in xrp_over)
    assert worst == half_a_drop, (
        f"the most XRP can be rounded up by is half a drop ({half_a_drop}); measured {worst}"
    )
    assert "rounded UP" in quantize_for_chain(ALREADY_QUANTIZED * 10 + 8e-07, "XRP")[1], (
        "a payout rounded UPWARD has to say so in words, not just in digits"
    )


def test_EVERY_destination_asset_this_terminal_can_PAY_has_a_quantizer():
    """The hole closed from the other end, exactly as tests/test_address_authority.py does it.

    quantize_for_chain() returns an unlisted asset UNTOUCHED, because guessing a
    precision for a chain nobody listed is how a payout becomes silently wrong.
    That is the right default and it is not sufficient: a pair enabled for a
    chain QUANTIZERS does not cover would go straight back to recording a figure
    the chain never sent, silently, on the first payout.

    So the gap fails the suite instead. Config.ALLOWED_PAIRS is the operator's
    (rule 16), and this asserts that every destination in it is covered here --
    which is the same shape as "every asset this terminal can reach HAS an
    address validator".

    MUTATION: delete "XRP" from QUANTIZERS. This fails, and so does every
    XRP case above; without this test, enabling a sixth chain would not.
    """
    destinations = {to_asset for _from_asset, to_asset in Config.ALLOWED_PAIRS}

    uncovered = sorted(destinations - set(QUANTIZERS))

    assert uncovered == [], (
        f"these assets can be PAID OUT and have no entry in "
        f"chains/payout_quantization.QUANTIZERS: {uncovered}. Their payouts would record a figure "
        f"the chain never sent, which is the defect this module exists to remove"
    )


def test_an_unlisted_asset_passes_through_and_names_the_gap_as_OURS():
    """Because the sentence decides where the next reader looks.

    "DOGE sends 8 decimals" would be a guess. "DOGE has no entry in QUANTIZERS"
    sends the reader to the table, which is where the fix is.
    """
    quantized, why = quantize_for_chain(1.23456789012345, "DOGE")

    assert quantized == 1.23456789012345, "an unlisted asset must pass through, not get a guessed precision"
    assert "no entry in chains/payout_quantization.QUANTIZERS" in why
    assert "gap in QUANTIZERS and not a" in why, "the sentence has to say the fault is ours"


@pytest.mark.parametrize("asset", EVERY_CHAIN)
@pytest.mark.parametrize("amount", [0.0, -1.0, -0.000001])
def test_a_NON_POSITIVE_amount_cannot_RAISE_out_of_the_payout_loop(asset, amount):
    """Because amount_decided_and_logged() is called OUTSIDE the per-swap try block.

    chains/xrp_units.to_drops() raises XRPUnitError on a negative, and that is
    correct where it lives -- but reached from here it would abort
    process_pending_payouts() entirely and leave every OTHER pending swap
    unpaid, where today a bad amount fails one swap and the loop continues. The
    adapters already refuse a non-positive send, each naming its own cause, and
    they stay the place that does.

    MUTATION RUN: delete the `if not requested > 0` branch in
    quantize_for_chain(). All fifteen cases fail, and the two halves fail
    differently, which is worth knowing -- measured rather than assumed:

        XRP, -1.0 and -1e-06    XRPUnitError, raised out of the function
        everything else         no exception, but `why` is empty, so the
                                sentence that names who refuses is gone

    Only the first half is the dangerous one. to_drops() accepts 0.0 and returns
    0 drops, so a zero amount never raised; it is the NEGATIVE that would abort
    process_pending_payouts() and strand every other pending swap.
    """
    quantized, why = quantize_for_chain(amount, asset)

    assert quantized == amount, "a non-positive amount is passed through, not reinterpreted"
    assert "not a positive amount" in why
    assert "adapter refuses" in why, "the sentence has to say who DOES refuse it"


# --- the payout path, end to end --------------------------------------------


@pytest.fixture
def ledger(monkeypatch):
    """SeededLedger, which answers the two reads and captures what would be submitted.

    Two lines rather than a shared fixture, because a pytest fixture is not a rule --
    it is the wiring that hands this module the one SeededLedger implementation. The
    class is imported; only the `monkeypatch` plumbing is local, and sharing THAT
    across files would mean a conftest entry that every unrelated test pays for at
    collection.
    """
    return SeededLedger(monkeypatch)


# THE ADAPTER IS tests/recording_rpc_adapter.RecordingRPCAdapter, imported above:
# the REAL chains/base.RPCAdapter with `call` overridden, so send_to_address() runs
# its own fit_to_chain_precision() on whatever it is handed. That second reduction is
# exactly what the idempotence tests are about, and a stub that merely recorded its
# argument would measure the service while skipping the half that has to agree with
# it. It moved out of tests/test_coin_amounts.py when this file became its second
# caller (rule 8).


PAYOUT_ADDRESS = {"LTC": LTC_PARTICIPANT, "BTC": BTC_PARTICIPANT, "GRC": GRC_PAYOUT}


def _seed_one_pending_swap(db_path: str, to_asset: str, estimate: float, swap_id: str = "s_quantize") -> None:
    """One GRC -> `to_asset` swap in `payout_pending`, the only state the worker acts on.

    expected and actual input are BOTH 100.0 so that payout_amount()'s ratio is
    exactly 1 and the booked figure is the stored estimate unchanged. That keeps
    this file measuring quantization rather than re-measuring the deposit
    scaling tests/test_payout_amount_tracks_the_real_deposit.py already covers.

    apply_migrations() runs because payout_worker.py runs it once at startup:
    idx_payouts_one_live_per_swap is part of the database the worker sees, and a
    test against a database without it is a test against something else.
    """
    conn = connect_db(db_path)
    conn.executescript(SCHEMA)
    now = "2026-10-03T00:00:00+00:00"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES ('q_quantize', 'GRC', ?, 100.0, 0.02, 150, 0.0, ?, ?, ?)",
        (to_asset, estimate, now, now),
    )
    conn.execute(
        """
        INSERT INTO swaps (
            id, quote_id, from_asset, to_asset, deposit_address, payout_address,
            expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, status, min_confirmations,
            deposit_txid, payout_txid, created_at, updated_at, credited_at,
            completed_at, expires_at, failed_reason
        ) VALUES (?, 'q_quantize', 'GRC', ?, ?, ?,
                  100.0, 100.0, 0.02, 150, 0.0, ?, 'payout_pending', 6,
                  'grc_txid', NULL, ?, ?, ?, NULL, ?, NULL)
        """,
        (swap_id, to_asset, GRC_PAYOUT, PAYOUT_ADDRESS[to_asset], estimate, now, now, now, now),
    )
    conn.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) "
        "VALUES (?, 10000.0, 0.0, 10000.0, ?)",
        (to_asset, now),
    )
    conn.commit()
    apply_migrations(conn)
    conn.close()


def _one_payout_row(db_path: str) -> sqlite3.Row:
    conn = connect_db(db_path)
    try:
        rows = conn.execute("SELECT * FROM payouts").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"expected exactly one payouts row, found {len(rows) or '(none)'}"
    return rows[0]


@pytest.mark.parametrize(
    ("asset", "booked", "sent"),
    [
        ("LTC", 1.1996736819422498, 1.19967368),
        ("BTC", 0.00041198765432109, 0.00041198),
        ("GRC", 2701.3495803173805, 2701.34958031),
    ],
)
def test_the_payouts_ROW_and_the_WIRE_carry_the_same_number(tmp_path, monkeypatch, asset, booked, sent):
    """THE DEFECT, through the real worker against a real database.

    Before 2026-10-03 the row read `booked` and the daemon was handed `sent`,
    and the two columns a human reconciles -- the `payouts` row and the block
    explorer -- disagreed in their last digits with nothing saying so.

    BOTH NUMBERS COME OUT OF THE RUN, not out of this test's arithmetic: `sent`
    is read off the recorded `sendtoaddress` parameter, and the row is read back
    out of the database the worker wrote. The expected values are the daemon
    readings in this module's docstring.

    MUTATION: in amount_decided_and_logged(), `return quantized` ->
    `return amount`. Every case here fails on the row, and the wire assertion
    keeps passing -- which is precisely the defect's signature and why both
    assertions are here rather than either one alone.
    """
    monkeypatch.setenv("GRIDCOIN_WALLET_PASSPHRASE", "not-a-real-passphrase")
    db_path = str(tmp_path / "quantize.db")
    _seed_one_pending_swap(db_path, asset, booked)
    adapter = RecordingRPCAdapter(asset)

    conn = connect_db(db_path)
    try:
        completed = process_pending_payouts(conn, {}, {asset: adapter})
    finally:
        conn.close()

    assert len(completed) == 1, "the payout must actually have happened, or this measures a quiet path"
    assert adapter.sent == [sent], "the figure on the wire is the chain's own precision"
    assert _one_payout_row(db_path)["amount"] == sent, (
        "the RECORD must be the figure the chain was handed, not the figure the quote computed"
    )


@pytest.mark.parametrize(
    ("asset", "booked", "sent"),
    [
        ("LTC", 1.1996736819422498, 1.19967368),
        ("BTC", 0.00041198765432109, 0.00041198),
        ("GRC", 2701.3495803173805, 2701.34958031),
    ],
)
def test_the_BROADCAST_amount_is_NOT_CHANGED_by_recording_the_right_figure(asset, booked, sent):
    """THE HARD CONSTRAINT. The customer receives exactly what they received before.

    Asserted as the equality that constitutes it: what the adapter puts on the
    wire for the BOOKED figure -- the number it was handed before this change --
    and what it puts on the wire for the QUANTIZED figure it is handed now, are
    the same number, through the real send path on a real RPCAdapter.

    This is idempotence measured at the call site rather than on the function,
    which is the form the constraint is actually stated in: "your change must
    not alter by one base unit what any customer receives".

    MUTATION: replace _quantize_bitcoin_family()'s body with
    `return round(amount, 8), ""`. The BTC case fails -- 0.00041198765432109
    rounds to 0.00041199 and truncates to 0.00041198 -- so a quantizer that
    rounded to nearest instead of inheriting the adapter's truncation would be
    caught here, on the wire, rather than in a note.
    """
    unquantized = RecordingRPCAdapter(asset)
    unquantized.send_to_address(PAYOUT_ADDRESS[asset], booked)

    quantized, _why = quantize_for_chain(booked, asset)
    prequantized = RecordingRPCAdapter(asset)
    prequantized.send_to_address(PAYOUT_ADDRESS[asset], quantized)

    assert unquantized.sent == [sent], "the pre-change path put this on the wire"
    assert prequantized.sent == unquantized.sent, (
        "pre-quantizing CHANGED what the chain is asked to send, which is live posture and the "
        "operator's call -- not something this fix may do"
    )


def test_the_SOL_BROADCAST_amount_is_NOT_CHANGED_either(booked=1.1996736819422498):
    """The same constraint on SOL, through SolanaAdapter's own plan rather than a stub.

    SOL IS A PAYOUT DESTINATION -- ALLOWED_PAIRS has carried ("GRC","SOL"),
    ("BTC","SOL") and ("LTC","SOL") since 79c4808 on 2026-10-03 -- so this is a
    live chain and not a precaution.

    build_transfer_plan() IS THE CONVERSION, and for native SOL it opens no
    socket: it validates the address locally (base58, length, on-curve) and reads
    SOL_DECIMALS rather than asking a mint, because preview_payout() refuses an
    SPL mint outright before any network call. So the real adapter's own
    `base_units` can be compared for the booked figure and the quantized one with
    nothing reachable, which is the strongest form available here -- no keypair
    is created, read or named, and nothing is signed.

    MUTATION RUN: `_quantize_solana()` using SOL_DECIMALS - 1. Both this and the
    SOL rows above fail; the lamport count drops by a factor of ten, which is the
    magnitude error CLAUDE.md rule 11 is about.
    """
    adapter = SolanaAdapter(
        url="http://127.0.0.1:1/", commitment="finalized", timeout=1,
        mint="", hot_wallet="", min_commitment_rank=3,
    )

    unquantized = adapter.build_transfer_plan(SOL_PAYOUT, booked)
    quantized, _why = quantize_for_chain(booked, "SOL")
    prequantized = adapter.build_transfer_plan(SOL_PAYOUT, quantized)

    assert unquantized["decimals"] == 9, "the payout path is native SOL; an SPL mint is refused upstream"
    assert unquantized["base_units"] == 1199673681
    assert prequantized["base_units"] == unquantized["base_units"], (
        "pre-quantizing CHANGED the lamport count the cluster would be asked to move"
    )


def test_the_XRP_BROADCAST_drop_count_is_NOT_CHANGED_either(ledger, booked=3.3155893288590605):
    """And on XRP, through the real preview_payout() against the seeded server.

    preview_payout() is where to_drops() runs on the payout path, and it returns
    `send_drops` -- the integer that becomes the Payment's Amount field. Asked
    twice, with the booked figure and with the quantized one, against the same
    seeded server_info and account_info.

    NO KEY IS INVOLVED. preview_payout() is the read-only half by construction:
    it imports no signing library, and the arming check happens in
    send_to_address() after it returns. 3315589 is the drop count the XRP testnet
    reported for swap s_539d922e9ef0a5d8.

    MUTATION RUN: `_quantize_xrp()` returning the requested amount. This keeps
    passing -- which is correct and is the point: the constraint is that the
    broadcast figure does not move, and a quantizer that does nothing cannot move
    it. What catches that mutation is the ROW assertions, and both kinds of test
    are here because neither is sufficient.
    """
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1000000000"))
    adapter = XRPAdapter(url=TESTNET_URL, min_confirmations=1)

    unquantized = adapter.preview_payout(XRP_CUSTOMER_PAYOUT, booked, XRP_HOT_ACCOUNT)
    quantized, _why = quantize_for_chain(booked, "XRP")
    prequantized = adapter.preview_payout(XRP_CUSTOMER_PAYOUT, quantized, XRP_HOT_ACCOUNT)

    assert unquantized["send_drops"] == 3315589, "the drop count the testnet ledger reported"
    assert prequantized["send_drops"] == unquantized["send_drops"], (
        "pre-quantizing CHANGED the drop count the ledger would be asked to deliver"
    )
    assert ledger.submitted == [], "a preview signs nothing and submits nothing"


def test_the_reservation_returns_to_zero_and_strands_NO_sub_unit_residue(tmp_path):
    """wallet_inventory.hot_reserved, measured before and after a real payout.

    THE HYPOTHESIS THIS TESTS WAS THAT RESERVE AND RELEASE DISAGREED -- reserve
    against the booked figure, release against the figure handed to
    _record_broadcast() -- which would strand a sub-unit reservation on every
    payout, permanently, growing forever.

    MEASURED: it does not happen, and the reason is structural rather than
    lucky. process_pending_payouts() uses ONE local for reserve_inventory(), the
    payouts INSERT, broadcast_payout() and _record_broadcast(), so the two
    figures are the same object and cannot differ -- before this change or
    after. The test exists because "I read the code and the variable is the
    same" is not a measurement (rule 17), and because the invariant is worth
    pinning: a future caller that passes a different figure to either side
    reintroduces the hazard, which the test below demonstrates in isolation.

    MUTATION: in process_pending_payouts(), change the _record_broadcast() call
    to pass `float(swap["output_amount_estimate"])` instead of `amount`. This
    fails with a residue of 0.0000000019422498 LTC, which is what the hypothesis
    described.
    """
    booked = 1.1996736819422498
    db_path = str(tmp_path / "residue.db")
    _seed_one_pending_swap(db_path, "LTC", booked)

    conn = connect_db(db_path)
    try:
        before = conn.execute("SELECT hot_reserved FROM wallet_inventory WHERE asset = 'LTC'").fetchone()
        assert before["hot_reserved"] == 0.0, "the fixture must start with nothing reserved"
        process_pending_payouts(conn, {}, {"LTC": RecordingRPCAdapter("LTC")})
        after = conn.execute("SELECT * FROM wallet_inventory WHERE asset = 'LTC'").fetchone()
    finally:
        conn.close()

    assert after["hot_reserved"] == 0.0, (
        f"hot_reserved is {after['hot_reserved']!r} after one payout, so a sub-unit reservation was "
        f"stranded. It is never released, so every payout would add another"
    )
    assert after["hot_confirmed"] == 10000.0 - 1.19967368, (
        "hot_confirmed must fall by the figure that LEFT THE WALLET, not by the booked figure"
    )


def test_reserving_one_figure_and_releasing_another_DOES_strand_a_residue():
    """The hazard the test above shows is not reachable, demonstrated in isolation.

    Both functions take an `amount` argument and neither consults the other, so
    nothing in the code itself prevents a caller from reserving the booked
    figure and releasing the quantized one. This is what that costs, in the one
    place it can be shown without pretending the worker does it: a permanent
    0.0000000019422498 LTC reservation from a single payout, which
    release_inventory_after_send() will never remove because it clamps at zero
    from the other side.

    Kept as its own test rather than folded into the one above, because the two
    establish different things: that one is about the worker, this one is about
    the mechanism. If a future refactor splits the worker's single local into
    two, that one fails and this one explains why.
    """
    booked = 1.1996736819422498
    quantized, _why = quantize_for_chain(booked, "LTC")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, updated_at) "
        "VALUES ('LTC', 10.0, 0.0, 10.0, '2026-10-03T00:00:00+00:00')"
    )

    reserve_inventory(conn, "LTC", booked)
    release_inventory_after_send(conn, "LTC", quantized)
    residue = conn.execute("SELECT hot_reserved FROM wallet_inventory WHERE asset = 'LTC'").fetchone()
    conn.close()

    assert residue["hot_reserved"] == pytest.approx(booked - quantized, rel=1e-09)
    assert residue["hot_reserved"] > 0, (
        "reserving one figure and releasing another leaves the difference standing forever"
    )


def test_a_payout_that_quantizes_to_NOTHING_still_reaches_the_adapters_own_refusal(tmp_path, caplog):
    """1e-09 BTC is a tenth of a satoshi, and the message must still name the cause.

    THE BRANCH THIS COVERS IS THE ONE PLACE amount_decided_and_logged() DOES NOT
    TAKE THE QUANTIZED FIGURE. Handing the adapter 0.0 would reach
    `sendtoaddress(address, 0.0)` -- a daemon error about a positive amount, for
    a figure WE reduced to zero, which is the hardest kind of message to trace
    back -- because chains/base.py's refusal fires on `fitted <= 0 < amount` and
    a pre-quantized zero does not satisfy it. tests/test_coin_amounts.py pins
    that a genuinely zero amount is NOT turned into a precision complaint, so
    widening the adapter's guard would have broken a decision somebody already
    made on purpose.

    So the requested figure goes through and the existing refusal fires exactly
    as it did before: nothing sent, the payouts row 'failed', the swap 'failed'
    with the reason on it.

    MUTATION: delete the `if quantized <= 0 < amount: return amount` branch.
    This fails on `adapter.sent == []` -- the daemon is asked to send zero --
    which is the assertion that distinguishes a refusal from a refusal that came
    after the call.
    """
    db_path = str(tmp_path / "dust.db")
    _seed_one_pending_swap(db_path, "BTC", 1e-09)
    adapter = RecordingRPCAdapter("BTC")

    conn = connect_db(db_path)
    try:
        with caplog.at_level(logging.ERROR):
            completed = process_pending_payouts(conn, {}, {"BTC": adapter})
        swap = conn.execute("SELECT * FROM swaps WHERE id = 's_quantize'").fetchone()
    finally:
        conn.close()

    assert completed == [], "a payout that cannot be expressed on the chain must not be reported completed"
    assert adapter.sent == [], "NOTHING may reach the daemon once the amount is known to be unsendable"
    assert swap["status"] == "failed"
    assert "which is nothing" in (swap["failed_reason"] or ""), (
        "the recorded reason has to name the chain's precision, not a daemon complaint about zero"
    )
    assert _one_payout_row(db_path)["status"] == "failed"


# --- XRP, through the real serializer ---------------------------------------


def test_the_XRP_payouts_row_matches_the_Amount_FIELD_the_ledger_would_receive(
    tmp_path, ledger, monkeypatch,
):
    """THE SWAP FROM THE DOCSTRING, reproduced through the real adapter and serializer.

    The ledger's answer for s_539d922e9ef0a5d8 was Amount 3315589 drops against
    a `payouts` row of 3.3155893288590605 XRP. Here the booked figure is the
    same, the Payment that reaches submit_and_wait is checked for the same drop
    count, and the row is checked to carry the XRP equivalent of THAT -- not of
    the quote.

    THROUGH THE WORKER, not through broadcast_payout() directly, because what
    was wrong was the relationship between the send and the WRITE and only the
    worker performs both.

    The drop count is read off the SERIALIZED transaction (`to_xrpl()`), which is
    the form the ledger parses, for the reason
    tests/test_xrp_payout_wiring.py gives: "the code does not pass flags" is a
    claim about a call site, and this is a claim about the transaction.

    MUTATION: in amount_decided_and_logged(), `return quantized` ->
    `return amount`. The row assertion fails and the Amount assertion does not,
    which is the defect exactly as it was measured.
    """
    paying = wallet_module.Wallet.create()
    monkeypatch.setenv(SIGNING_SEED_ENV_VAR, paying.seed)
    ledger.answer(server_info=server_info(), account_info=account_info(drops="1000000000"))

    db_path = str(tmp_path / "xrp_quantize.db")
    _seed_one_pending_xrp_swap(db_path, 3.3155893288590605)
    adapters = {"XRP": XRPAdapter(url=TESTNET_URL, min_confirmations=1)}
    config = {"XRP_DEPOSIT_ACCOUNT": paying.classic_address, "XRP_MIN_CONFIRMATIONS": 1}

    conn = connect_db(db_path)
    try:
        completed = process_pending_payouts(conn, config, adapters)
    finally:
        conn.close()

    assert len(completed) == 1, "the payout must actually have happened, or this measures a quiet path"
    assert len(ledger.submitted) == 1
    serialized = ledger.submitted[0].to_xrpl()
    assert serialized["Amount"] == "3315589", (
        "drops as a string, exactly the figure the XRP testnet reported for the swap this measures"
    )
    assert _one_payout_row(db_path)["amount"] == from_drops(int(serialized["Amount"]))
    assert _one_payout_row(db_path)["amount"] == 3.315589


def _seed_one_pending_xrp_swap(db_path: str, estimate: float, swap_id: str = "s_quantize_xrp") -> None:
    """One GRC -> XRP swap in `payout_pending`. See _seed_one_pending_swap() for the shape.

    Separate from that helper because XRP takes no inventory row -- the XRP
    adapter refuses get_balance() by design, holding no hot-wallet account --
    and because its payout address comes from a different fixture.
    """
    conn = connect_db(db_path)
    conn.executescript(SCHEMA)
    now = "2026-10-03T00:00:00+00:00"
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES ('q_quantize_xrp', 'GRC', 'XRP', 100.0, 0.02, 150, 0.0, ?, ?, ?)",
        (estimate, now, now),
    )
    conn.execute(
        """
        INSERT INTO swaps (
            id, quote_id, from_asset, to_asset, deposit_address, payout_address,
            expected_input_amount, actual_input_amount, quoted_rate, fee_bps,
            network_fee_reserve, output_amount_estimate, status, min_confirmations,
            deposit_txid, payout_txid, created_at, updated_at, credited_at,
            completed_at, expires_at, failed_reason
        ) VALUES (?, 'q_quantize_xrp', 'GRC', 'XRP', ?, ?,
                  100.0, 100.0, 0.02, 150, 0.0, ?, 'payout_pending', 6,
                  'grc_txid', NULL, ?, ?, ?, NULL, ?, NULL)
        """,
        (swap_id, GRC_PAYOUT, XRP_CUSTOMER_PAYOUT, estimate, now, now, now, now),
    )
    conn.commit()
    apply_migrations(conn)
    conn.close()


def test_the_decision_function_logs_the_figure_it_recorded_and_why(caplog):
    """Rule 14: two correct numbers that disagree in their last digits.

    An operator reconciling a `payouts` row against a block explorer has to be
    told which figure is on the chain and that the other one is the quote. A
    silent substitution would be this fix hiding the thing it exists to reveal.

    Called directly rather than through the worker, because the subject is the
    log line and the worker would add six others.

    MUTATION: drop the `if quantization:` log in amount_decided_and_logged().
    This fails, and nothing else in the suite does -- a silent correct number
    and a silent wrong number look identical in a terminal.
    """
    swap = {
        "id": "s_log", "to_asset": "XRP", "output_amount_estimate": 3.3155893288590605,
        "expected_input_amount": 100.0, "actual_input_amount": 100.0,
    }

    with caplog.at_level(logging.INFO):
        amount = amount_decided_and_logged(swap)

    assert amount == 3.315589
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "3315589 drops" in text, "the operator reads drops off the ledger, so the log names drops"
    assert "3.315589" in text
    assert "RECORDED" in text


def test_the_refusal_for_an_unsendable_amount_still_comes_from_the_adapter():
    """The adapter's guard is untouched by this change, asserted directly.

    tests/test_coin_amounts.py owns this behavior; it is re-asserted here
    because amount_decided_and_logged()'s dust branch exists ONLY to keep
    reaching it, and a test of that branch that did not also pin its target
    would pass if the target were deleted.
    """
    adapter = RecordingRPCAdapter("BTC")

    with pytest.raises(RPCError, match="which is nothing"):
        adapter.send_to_address(BTC_PARTICIPANT, 1e-09)

    assert adapter.calls == []
