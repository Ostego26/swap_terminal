"""Correcting `payouts` rows that record an amount their chain cannot express.

Role: test (seeded temp database and seeded chain responses; opens no socket)
Reads: correct_payout_amounts.py, chains/payout_on_chain.py
Writes: a temp database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes

THE DEFECT, MEASURED ON THE OPERATOR'S HOST 2026-10-03.
chains/payout_quantization.quantize_for_chain() (landed in 54892d5) says all 23
of 23 rows in their `payouts` table record an amount the chain cannot express.
15 carry a txid -- the money left -- and 8 are status=failed with txid=(none).
The 15, with what the chain could actually send:

    id=3  s_5ca7e29438479d29 GRC 55.52645238097888     -> 55.52645238
    id=6  s_aaa81fa6e8538163 GRC 82.65089987734812     -> 82.65089987
    id=7  s_c7dbce4be8b0efc2 GRC 87.97839509528555     -> 87.97839509
    id=8  s_e397e7d6b66830b7 GRC 87.86977058217977     -> 87.86977058
    id=9  s_95a807c181644190 GRC 88.59758626203558     -> 88.59758626
    id=10 s_e820c23626002c37 GRC 3086.581539002641     -> 3086.581539
    id=11 s_c992993c19d28bef GRC 3110.9183687421837    -> 3110.91836874
    id=14 s_1a44ddb70c118d13 SOL 0.0007616540576794097 -> 0.000761654
    id=15 s_45175de92a535066 SOL 0.0007616540576794097 -> 0.000761654
    id=17 s_6cd1a920cbe5739e GRC 2701.3495803173805    -> 2701.34958031
    id=19 s_2daaceb70dadc1d4 LTC 1.1996736819422498    -> 1.19967368
    id=20 s_02623852c1ea42cc LTC 1.2077457737321198    -> 1.20774577
    id=21 s_68f2cdef60e9252e BTC 0.004017802860711172  -> 0.0040178
    id=22 s_612fac62489f2122 GRC 55.44825689015776     -> 55.44825689
    id=23 s_539d922e9ef0a5d8 XRP 3.3155893288590605    -> 3.315589

One of them is independently confirmed against a chain and FOURTEEN ARE NOT:
id=23's transaction 799F8DED...D7935 reads Amount "3315589" drops, Fee "10",
tesSUCCESS, validated true on the XRP testnet. Every expected figure in this
file is TYPED from that measurement rather than computed by calling the code
under test -- a parametrized case that derives its expectation the way the
implementation does passes when both are wrong, which happened in this tree on
2026-10-03 and is recorded in tests/test_resolve_halted_swap.py.

WHAT IS MEASURED AND WHAT IS SHAPED. The 15 rows above are the operator's, to
the digit. Of the 8 failed rows, three are measured as duplicates of a swap that
also has a corrected row -- ids 12 and 13 belong to the same swap as 14, and 18
to the same swap as 19 -- which is why this tool keys on `payouts.id` and never
on a swap. The other five failed rows are known only as a COUNT: eight rows,
failed, no txid. Their ids, swaps and amounts here are shaped like the measured
ones and are not themselves measurements.

ADDRESSES COME FROM tests/valid_addresses.py AND ARE NEVER WRITTEN AS LITERALS.
tests/test_address_literals_are_valid.py is a clean gate at a ceiling of 60 with
no headroom, and rule 19 forbids raising it. Every destination below is a
derived fixture; the txids are not addresses and are not in that gate's scope
(its base58 matcher requires a 25-40 character string starting r/R/S/m/n/1/2/3).
"""

from __future__ import annotations

import sqlite3

import pytest
from chains.base import RPCError
from chains.payout_on_chain import (
    READERS,
    ChainAmount,
    delivered_to_destination,
)
from chains.payout_quantization import QUANTIZERS
from db import SCHEMA, db_session, dict_factory
from valid_addresses import (
    BTC_PARTICIPANT,
    GRC_PAYOUT,
    LTC_PARTICIPANT,
    SOL_PAYOUT,
    XRP_CUSTOMER_PAYOUT,
)

import correct_payout_amounts
from correct_payout_amounts import (
    CORRECT,
    FROM_ARITHMETIC,
    FROM_CHAIN,
    NOT_ASKED,
    REFUSE,
    SKIP,
    _footer,
    _summary_line,
    apply_correction,
    main,
    plan_for_payout,
)

WHEN = "2026-10-03T04:00:00+00:00"

#: The one txid in the fifteen that has been read off a chain. Hex, 64 characters,
#: so it is not in the address gate's shape (see this module's docstring).
XRP_CONFIRMED_TXID = "799F8DED7CFB657411C5B1B9BE500C79CD62F07CF5634D2817804E89BDED7935"

#: The one payout row whose transaction has been read off a chain. The other
#: fourteen have not, which is the whole reason the tool asks rather than assumes.
CONFIRMED_PAYOUT_ID = 23

#: id, swap, asset, recorded, what the chain can send, destination.
#: The operator's fifteen, in their order, with the expectations TYPED.
CORRECTABLE = [
    (3, "s_5ca7e29438479d29", "GRC", 55.52645238097888, 55.52645238, GRC_PAYOUT),
    (6, "s_aaa81fa6e8538163", "GRC", 82.65089987734812, 82.65089987, GRC_PAYOUT),
    (7, "s_c7dbce4be8b0efc2", "GRC", 87.97839509528555, 87.97839509, GRC_PAYOUT),
    (8, "s_e397e7d6b66830b7", "GRC", 87.86977058217977, 87.86977058, GRC_PAYOUT),
    (9, "s_95a807c181644190", "GRC", 88.59758626203558, 88.59758626, GRC_PAYOUT),
    (10, "s_e820c23626002c37", "GRC", 3086.581539002641, 3086.581539, GRC_PAYOUT),
    (11, "s_c992993c19d28bef", "GRC", 3110.9183687421837, 3110.91836874, GRC_PAYOUT),
    (14, "s_1a44ddb70c118d13", "SOL", 0.0007616540576794097, 0.000761654, SOL_PAYOUT),
    (15, "s_45175de92a535066", "SOL", 0.0007616540576794097, 0.000761654, SOL_PAYOUT),
    (17, "s_6cd1a920cbe5739e", "GRC", 2701.3495803173805, 2701.34958031, GRC_PAYOUT),
    (19, "s_2daaceb70dadc1d4", "LTC", 1.1996736819422498, 1.19967368, LTC_PARTICIPANT),
    (20, "s_02623852c1ea42cc", "LTC", 1.2077457737321198, 1.20774577, LTC_PARTICIPANT),
    (21, "s_68f2cdef60e9252e", "BTC", 0.004017802860711172, 0.0040178, BTC_PARTICIPANT),
    (22, "s_612fac62489f2122", "GRC", 55.44825689015776, 55.44825689, GRC_PAYOUT),
    (23, "s_539d922e9ef0a5d8", "XRP", 3.3155893288590605, 3.315589, XRP_CUSTOMER_PAYOUT),
]

#: id, swap, asset, recorded, destination. The eight failed rows with no txid.
#: 12, 13 and 18 are the measured duplicates: a swap can own several payout rows,
#: and 12/13 share swap s_1a44ddb70c118d13 with the corrected row 14 while 18
#: shares s_2daaceb70dadc1d4 with 19. A tool keyed on swaps would have corrected
#: one of each pair and silently left the other.
FAILED_NO_TXID = [
    (1, "s_0f1aa0c9b1d14e75", "GRC", 143.39622296118311, GRC_PAYOUT),
    (2, "s_0f1aa0c9b1d14e75", "GRC", 143.39622296118311, GRC_PAYOUT),
    (4, "s_4bb0e1a7cb2f4d80", "GRC", 9049.690138217977, GRC_PAYOUT),
    (5, "s_4bb0e1a7cb2f4d80", "GRC", 9049.690138217977, GRC_PAYOUT),
    (12, "s_1a44ddb70c118d13", "SOL", 0.0007616540576794097, SOL_PAYOUT),
    (13, "s_1a44ddb70c118d13", "SOL", 0.0007616540576794097, SOL_PAYOUT),
    (16, "s_73c0d2e8f41ab659", "GRC", 277.2412844507888, GRC_PAYOUT),
    (18, "s_2daaceb70dadc1d4", "LTC", 1.1996736819422498, LTC_PARTICIPANT),
]

ALL_ROWS = len(CORRECTABLE) + len(FAILED_NO_TXID)


def txid_for(payout_id: int) -> str:
    """The txid a seeded row carries. Only id=23's was measured; see the docstring."""
    # 23 is the operator's own payout row id, named in this module's docstring. It is
    # an identifier rather than a threshold, which is why it is written out here.
    return XRP_CONFIRMED_TXID if payout_id == CONFIRMED_PAYOUT_ID else f"seeded-payout-txid-{payout_id:02d}"


def seeded(tmp_path):
    """All 23 rows as they stand on the operator's host. One database per test."""
    db_path = tmp_path / "payouts.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    # One swap row per distinct swap_id, because several payout rows share one swap
    # (ids 12/13 with 14, and 18 with 19 -- measured, see this module's docstring).
    swaps = {}
    for swap_id, asset, recorded, destination in (
        [(row[1], row[2], row[3], row[5]) for row in CORRECTABLE]
        + [(row[1], row[2], row[3], row[4]) for row in FAILED_NO_TXID]
    ):
        swaps.setdefault(swap_id, (asset, destination, recorded))
    for swap_id, (asset, destination, recorded) in swaps.items():
        conn.execute(
            "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
            "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
            "VALUES (?, 'XRP', ?, 5.0, 56.29, 150, 0.001, ?, ?, ?)",
            (f"q{swap_id}", asset, recorded, WHEN, WHEN),
        )
        conn.execute(
            "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
            "expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve, "
            "output_amount_estimate, status, min_confirmations, created_at, updated_at, expires_at) "
            "VALUES (?, ?, 'XRP', ?, 'rDEPOSITACCOUNT', ?, 5.0, 5.0, 56.29, 150, 0.001, ?, "
            "'completed', 1, ?, ?, ?)",
            (swap_id, f"q{swap_id}", asset, destination, recorded, WHEN, WHEN, WHEN),
        )
    for payout_id, swap_id, asset, recorded, _chain, destination in CORRECTABLE:
        conn.execute(
            "INSERT INTO payouts (id, swap_id, asset, destination_address, amount, txid, status, "
            "created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, 'broadcast', ?, ?)",
            (payout_id, swap_id, asset, destination, recorded, txid_for(payout_id), WHEN, WHEN),
        )
    for payout_id, swap_id, asset, recorded, destination in FAILED_NO_TXID:
        conn.execute(
            "INSERT INTO payouts (id, swap_id, asset, destination_address, amount, txid, status, "
            "created_at, sent_at) VALUES (?, ?, ?, ?, ?, NULL, 'failed', ?, NULL)",
            (payout_id, swap_id, asset, destination, recorded, WHEN),
        )
    conn.commit()
    conn.close()
    return db_path


def amounts(db_path) -> dict[int, float]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    try:
        return {row["id"]: row["amount"] for row in conn.execute("SELECT id, amount FROM payouts")}
    finally:
        conn.close()


def audit_rows(db_path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    try:
        return conn.execute("SELECT * FROM swap_audit_log ORDER BY id").fetchall()
    finally:
        conn.close()


def recorded_amounts() -> dict[int, float]:
    return ({row[0]: row[3] for row in CORRECTABLE}
            | {row[0]: row[3] for row in FAILED_NO_TXID})


# ---------------------------------------------------------------------------
# THE DECISION, called with seeded rows (rule 10). No database, no chain.
# ---------------------------------------------------------------------------


def row_for(payout_id: int, asset: str, recorded: float, txid: str | None, status: str) -> dict:
    return {"id": payout_id, "swap_id": "s_seed", "asset": asset, "amount": recorded,
            "txid": txid, "status": status, "swap_status": "completed",
            "destination_address": GRC_PAYOUT, "swap_to_asset": asset}


@pytest.mark.parametrize("case", CORRECTABLE, ids=[str(row[0]) for row in CORRECTABLE])
def test_every_one_of_the_operators_fifteen_rows_is_planned_for_CORRECTION(case):
    """The figure written is the TYPED one from their host, not one this test derived."""
    payout_id, _swap_id, asset, recorded, chain, _destination = case
    plan = plan_for_payout(row_for(payout_id, asset, recorded, txid_for(payout_id), "broadcast"), NOT_ASKED)

    assert plan.verdict == CORRECT
    assert plan.corrected == chain
    assert plan.authority == FROM_ARITHMETIC, "with no chain answer the claim is arithmetic, and must say so"


@pytest.mark.parametrize("case", FAILED_NO_TXID, ids=[str(row[0]) for row in FAILED_NO_TXID])
def test_a_FAILED_row_with_no_txid_is_SKIPPED_and_says_it_recorded_a_proposal(case):
    """The eight. Quantizing one would make an intention look like a send."""
    payout_id, _swap_id, asset, recorded, _destination = case
    plan = plan_for_payout(row_for(payout_id, asset, recorded, None, "failed"), NOT_ASKED)

    assert plan.verdict == SKIP
    assert plan.corrected is None
    assert "PROPOSAL" in plan.why


def test_a_row_that_already_holds_the_chains_figure_is_SKIPPED():
    """What makes a second run a no-op, asserted on the decision rather than on a run."""
    plan = plan_for_payout(row_for(3, "GRC", 55.52645238, txid_for(3), "broadcast"), NOT_ASKED)

    assert plan.verdict == SKIP
    assert "already what GRC's chain can express" in plan.why


def test_a_chain_figure_that_AGREES_corrects_from_the_chain_and_says_so():
    plan = plan_for_payout(
        row_for(23, "XRP", 3.3155893288590605, XRP_CONFIRMED_TXID, "broadcast"),
        ChainAmount(3.315589, "meta.delivered_amount = 3315589 drops"),
    )

    assert plan.verdict == CORRECT
    assert plan.authority == FROM_CHAIN
    assert plan.corrected == 3.315589
    assert "3315589 drops" in plan.why, "the provenance has to reach the audit row"


def test_a_chain_figure_that_DISAGREES_refuses_the_row_and_names_both_numbers():
    """The bigger finding: the quantizer would be wrong for every FUTURE payout too."""
    plan = plan_for_payout(
        row_for(23, "XRP", 3.3155893288590605, XRP_CONFIRMED_TXID, "broadcast"),
        ChainAmount(3.315588, "meta.delivered_amount = 3315588 drops"),
    )

    assert plan.verdict == REFUSE
    assert plan.corrected is None
    assert "3.315588" in plan.why and "3.315589" in plan.why
    assert "quantizer is wrong" in plan.why


# ---------------------------------------------------------------------------
# THE REAL TOOL AGAINST A REAL DATABASE. Asserted on the ROWS, never on the text
# alone -- CLAUDE.md "Verify by behavior, never by reading the code".
# ---------------------------------------------------------------------------


def test_a_DRY_RUN_writes_nothing_at_all(tmp_path):
    db_path = seeded(tmp_path)

    code = main(["--db", str(db_path), "--no-chain"])

    assert code == 0
    assert amounts(db_path) == recorded_amounts(), "a dry run changed a payout amount"
    assert audit_rows(db_path) == [], "a dry run wrote an audit row"


def test_APPLY_corrects_exactly_the_fifteen_and_leaves_the_eight(tmp_path):
    """The operator's instruction, asserted on the rows that are actually there.

    WHAT THIS TEST ALONE DOES NOT ESTABLISH, measured by mutation on 2026-10-03
    and written down rather than left for the next reader to rediscover: deleting
    the no-txid refusal from plan_for_payout() does NOT change the rows this test
    reads. The eight stay untouched anyway, because apply_correction()'s
    compare-and-swap matches on `txid = ?` and SQL's NULL never equals anything,
    so the UPDATE finds no row. Two independent guards, which is the right number
    on a row that must never be rewritten -- and it means the mutation is caught
    by the eight per-row SKIP tests above and not by this one.
    """
    db_path = seeded(tmp_path)

    code = main(["--db", str(db_path), "--no-chain", "--apply"])

    assert code == 0
    after = amounts(db_path)
    assert len(after) == ALL_ROWS
    for payout_id, _swap, _asset, _recorded, chain, _destination in CORRECTABLE:
        assert after[payout_id] == chain, f"payouts.id={payout_id} is not the chain's figure"
    for payout_id, _swap, _asset, recorded, _destination in FAILED_NO_TXID:
        assert after[payout_id] == recorded, (
            f"payouts.id={payout_id} has no txid and was touched anyway -- that turns an amount this "
            f"desk never sent into a record of one it did"
        )


def test_every_correction_carries_an_audit_row_holding_the_ORIGINAL_figure(tmp_path):
    """The original must be recoverable from the database alone, not only from git."""
    db_path = seeded(tmp_path)

    main(["--db", str(db_path), "--no-chain", "--apply"])

    rows = audit_rows(db_path)
    assert len(rows) == len(CORRECTABLE), "one audit row per correction, and no more"
    by_payout = {}
    for row in rows:
        marker = "payouts.id="
        by_payout[int(row["message"].split(marker)[1].split()[0])] = row
    for payout_id, swap_id, asset, recorded, chain, _destination in CORRECTABLE:
        audit = by_payout[payout_id]
        assert audit["swap_id"] == swap_id
        assert repr(recorded) in audit["message"], "the ORIGINAL figure is gone from the database"
        assert repr(chain) in audit["message"]
        assert txid_for(payout_id) in audit["message"]
        assert FROM_ARITHMETIC in audit["message"], "which authority produced the figure has to be in the row"
        assert asset in audit["message"]
        assert audit["old_status"] == "completed" and audit["new_status"] == "completed", (
            "this is not a status transition and an audit row claiming one would falsify the trail"
        )


def test_a_SECOND_run_corrects_nothing_and_writes_no_second_audit_row(tmp_path):
    db_path = seeded(tmp_path)
    main(["--db", str(db_path), "--no-chain", "--apply"])
    first = amounts(db_path)

    code = main(["--db", str(db_path), "--no-chain", "--apply"])

    assert code == 0
    assert amounts(db_path) == first
    assert len(audit_rows(db_path)) == len(CORRECTABLE), "the second run wrote another audit row"


def test_the_second_run_prints_none_rather_than_an_empty_section(tmp_path, capsys):
    """Rule 14: `(none)` is a result; a blank gap is ambiguous between zero and broken."""
    db_path = seeded(tmp_path)
    main(["--db", str(db_path), "--no-chain", "--apply"])
    capsys.readouterr()

    main(["--db", str(db_path), "--no-chain"])

    printed = capsys.readouterr().out
    assert f"{CORRECT}  0 row(s)" in printed
    assert "(none)" in printed
    assert f"{SKIP}  {ALL_ROWS} row(s)" in printed


def test_a_row_that_CHANGED_between_the_read_and_the_write_is_REFUSED(tmp_path):
    """The compare-and-swap, and the audit row that must not be written without it.

    IT CALLS apply_correction() DIRECTLY, AND THE FIRST VERSION OF THIS TEST DID
    NOT -- it is recorded here because the first version was GREEN FOR THE WRONG
    REASON and a mutation found it rather than review. That version set
    payouts.id=3 to 1.0 before running the tool and asserted the row still read
    1.0 afterwards. It did, but not because of the compare-and-swap: 1.0 is
    already expressible in GRC, so plan_for_payout() SKIPPED the row and the
    UPDATE never ran. Deleting `AND amount = ? AND txid = ?` from the statement
    left the whole suite green.

    A race cannot be staged through main(), because main() reads and writes in
    one process and would legitimately correct whatever it read. So the stale row
    is constructed the way a concurrent writer would produce one: read the row,
    let something else change it, then hand the ORIGINAL row and its plan to the
    function that writes.
    """
    db_path = seeded(tmp_path)
    with db_session(str(db_path)) as db:
        row = dict(db.execute("SELECT p.*, s.status AS swap_status FROM payouts p "
                              "JOIN swaps s ON s.id = p.swap_id WHERE p.id = 3").fetchone())
        plan = plan_for_payout(row, NOT_ASKED)
        assert plan.verdict == CORRECT, "the fixture has to be a row that WOULD be corrected"
        # The concurrent writer: a second settlement tool correcting the same row,
        # or a worker rewriting it, between this tool's read and its write.
        db.execute("UPDATE payouts SET amount = ? WHERE id = 3", (99.123456789,))
        db.commit()

        wrote, sentence = apply_correction(db, row, plan)

    assert not wrote
    assert "no longer holds" in sentence and "changed between the read and the write" in sentence
    assert amounts(db_path)[3] == 99.123456789, "the CAS let a changed row be overwritten"
    assert audit_rows(db_path) == [], (
        "an audit row was written for a correction that did not happen -- the UPDATE and the INSERT "
        "are in one transaction precisely so that cannot occur"
    )


def test_a_missing_database_is_REFUSED_rather_than_created(tmp_path):
    """sqlite3.connect() creates the file, and then every count reads 0 for nothing."""
    missing = tmp_path / "not-here.db"

    code = main(["--db", str(missing), "--no-chain"])

    assert code == 2
    assert not missing.exists(), "a read-only run created a database"


# ---------------------------------------------------------------------------
# AGAINST A CHAIN. The adapters are seeded responses, not sockets: no test in
# this suite opens one (tests/conftest.py's header).
# ---------------------------------------------------------------------------


class SeededGridcoinDaemon:
    """A `gettransaction` answer for every txid it is asked about. Records the calls."""

    asset = "GRC"

    def __init__(self, amount_by_txid: dict[str, float], address: str):
        self.amount_by_txid, self.address, self.calls = amount_by_txid, address, []

    def get_transaction(self, txid: str) -> dict:
        self.calls.append(txid)
        return {"txid": txid, "confirmations": 12, "details": [
            {"address": self.address, "category": "send", "amount": -self.amount_by_txid[txid], "vout": 0},
        ]}


def grc_rows() -> list[tuple]:
    return [row for row in CORRECTABLE if row[2] == "GRC"]


def test_a_CHAIN_VERIFIED_correction_records_the_chains_own_provenance(tmp_path, monkeypatch):
    """A row corrected from the chain and one corrected from arithmetic are different claims."""
    db_path = seeded(tmp_path)
    daemon = SeededGridcoinDaemon(
        {txid_for(row[0]): row[4] for row in grc_rows()}, GRC_PAYOUT,
    )
    monkeypatch.setattr(correct_payout_amounts, "build_adapters", lambda rpc: {"GRC": daemon})

    code = main(["--db", str(db_path), "--apply"])

    assert code == 0
    after = amounts(db_path)
    messages = {}
    for row in audit_rows(db_path):
        messages[int(row["message"].split("payouts.id=")[1].split()[0])] = row["message"]
    for payout_id, _swap, _asset, _recorded, chain, _destination in grc_rows():
        assert after[payout_id] == chain
        assert FROM_CHAIN in messages[payout_id], "a chain-read figure recorded as arithmetic"
        assert "gettransaction.details" in messages[payout_id], (
            "the audit row has to name the method and field the figure came from"
        )
    assert sorted(daemon.calls) == sorted(txid_for(row[0]) for row in grc_rows())
    # Every non-GRC row still corrects, from arithmetic, and says which.
    for payout_id, _swap, asset, _recorded, chain, _destination in CORRECTABLE:
        if asset != "GRC":
            assert after[payout_id] == chain
            assert FROM_ARITHMETIC in messages[payout_id]


def test_a_chain_that_DISAGREES_writes_nothing_for_that_row_and_exits_nonzero(tmp_path, monkeypatch):
    """One satoshi off is enough: the quantizer would then be wrong for every future payout."""
    db_path = seeded(tmp_path)
    off_by_one = {txid_for(row[0]): row[4] - 1e-8 for row in grc_rows()}
    monkeypatch.setattr(correct_payout_amounts, "build_adapters",
                        lambda rpc: {"GRC": SeededGridcoinDaemon(off_by_one, GRC_PAYOUT)})

    code = main(["--db", str(db_path), "--apply"])

    assert code == 5, "a disagreement must not exit 0 beside the rows that did correct"
    after = amounts(db_path)
    messages = [row["message"] for row in audit_rows(db_path)]
    for payout_id, _swap, _asset, recorded, _chain, _destination in grc_rows():
        assert after[payout_id] == recorded, f"payouts.id={payout_id} was corrected despite a disagreement"
        assert not any(f"payouts.id={payout_id} " in message for message in messages)
    assert len(messages) == len(CORRECTABLE) - len(grc_rows()), (
        "the rows whose chain was not asked still correct; only the disagreeing ones are held"
    )


def test_an_unreachable_daemon_degrades_to_ARITHMETIC_and_never_to_a_green_default(tmp_path, monkeypatch):
    """The honest degradation: corrected, and the row says the chain could not answer."""
    class RefusingDaemon:
        asset = "GRC"

        def get_transaction(self, txid: str) -> dict:
            raise RPCError("connection refused")

    db_path = seeded(tmp_path)
    monkeypatch.setattr(correct_payout_amounts, "build_adapters", lambda rpc: {"GRC": RefusingDaemon()})

    code = main(["--db", str(db_path), "--apply"])

    assert code == 0
    messages = {}
    for row in audit_rows(db_path):
        messages[int(row["message"].split("payouts.id=")[1].split()[0])] = row["message"]
    for payout_id, _swap, _asset, _recorded, chain, _destination in grc_rows():
        assert amounts(db_path)[payout_id] == chain
        assert FROM_ARITHMETIC in messages[payout_id]
        assert "connection refused" in messages[payout_id], (
            "the reason the chain could not answer belongs in the row, not only on the screen"
        )


# ---------------------------------------------------------------------------
# THE PER-CHAIN READS, with responses shaped like each daemon's.
# ---------------------------------------------------------------------------


class SeededCall:
    """An adapter whose `call` returns one seeded response and records what it was asked."""

    def __init__(self, response):
        self.response, self.calls = response, []

    def call(self, method, *params):
        self.calls.append((method, params))
        return self.response


def test_the_bitcoin_family_read_prefers_the_wallets_details_entry():
    read = delivered_to_destination(
        SeededGridcoinDaemon({txid_for(3): 55.52645238}, GRC_PAYOUT), "GRC", txid_for(3), GRC_PAYOUT,
    )

    assert read.amount == 55.52645238
    assert "details" in read.how and "GRC" in read.how


def test_the_raw_transaction_shape_is_read_through_script_pub_key():
    """`addresses` was removed from Core 22.0 and Litecoin 0.21.4 still sends it.

    Both spellings reach the same answer because script_pub_key.pays_address() is
    the one place that knows -- the defect this tree has found four times.
    """
    class RawOnlyDaemon:
        def get_transaction(self, txid):
            return {"txid": txid, "vout": [
                {"n": 0, "value": 0.1, "scriptPubKey": {"address": "somebody-else"}},
                {"n": 1, "value": 1.19967368, "scriptPubKey": {"addresses": [LTC_PARTICIPANT]}},
            ]}

    read = delivered_to_destination(RawOnlyDaemon(), "LTC", txid_for(19), LTC_PARTICIPANT)

    assert read.amount == 1.19967368
    assert "getrawtransaction.vout" in read.how


def test_two_outputs_to_one_address_with_different_values_is_UNREAD_not_summed():
    class TwoOutputDaemon:
        def get_transaction(self, txid):
            return {"txid": txid, "vout": [
                {"n": 0, "value": 1.0, "scriptPubKey": {"address": GRC_PAYOUT}},
                {"n": 1, "value": 2.0, "scriptPubKey": {"address": GRC_PAYOUT}},
            ]}

    read = delivered_to_destination(TwoOutputDaemon(), "GRC", txid_for(3), GRC_PAYOUT)

    assert read.amount is None
    assert "Refusing to choose or to sum" in read.how


def test_the_XRP_read_takes_meta_delivered_amount_and_asks_the_tx_method():
    """The operator's own confirmed transaction, as the ledger returned it."""
    response = {
        "tx_json": {"TransactionType": "Payment", "Destination": XRP_CUSTOMER_PAYOUT,
                    "Amount": "3315589", "Fee": "10", "hash": XRP_CONFIRMED_TXID},
        "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": "3315589"},
        "validated": True,
    }
    adapter = SeededCall(response)

    read = delivered_to_destination(adapter, "XRP", XRP_CONFIRMED_TXID, XRP_CUSTOMER_PAYOUT)

    assert read.amount == 3.315589
    assert adapter.calls == [("tx", ({"transaction": XRP_CONFIRMED_TXID},))]
    assert "delivered_amount" in read.how


def test_a_PARTIAL_PAYMENT_is_refused_rather_than_recorded():
    """Amount is never credited -- it is the exploit -- and a gap is a finding."""
    response = {
        "tx_json": {"TransactionType": "Payment", "Destination": XRP_CUSTOMER_PAYOUT,
                    "Amount": "3315589", "hash": XRP_CONFIRMED_TXID},
        "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": "1"},
        "validated": True,
    }

    read = delivered_to_destination(SeededCall(response), "XRP", XRP_CONFIRMED_TXID, XRP_CUSTOMER_PAYOUT)

    assert read.amount is None
    assert "PARTIAL PAYMENT" in read.how


def test_an_unvalidated_or_failed_XRP_response_reads_as_nothing():
    for meta, validated in (
        ({"TransactionResult": "tecUNFUNDED_PAYMENT", "delivered_amount": "3315589"}, True),
        ({"TransactionResult": "tesSUCCESS", "delivered_amount": "3315589"}, False),
    ):
        response = {"tx_json": {"TransactionType": "Payment", "Destination": XRP_CUSTOMER_PAYOUT,
                                "Amount": "3315589", "hash": XRP_CONFIRMED_TXID},
                    "meta": meta, "validated": validated}

        read = delivered_to_destination(SeededCall(response), "XRP", XRP_CONFIRMED_TXID,
                                        XRP_CUSTOMER_PAYOUT)

        assert read.amount is None, f"{meta}/{validated} produced a figure"
        assert read.how


def test_the_SOLANA_read_subtracts_the_destinations_balances():
    lamports = 761654
    response = {
        "transaction": {"message": {"accountKeys": [{"pubkey": "payer"}, {"pubkey": SOL_PAYOUT}]}},
        "meta": {"err": None, "preBalances": [10_000_000, 0], "postBalances": [9_000_000, lamports]},
    }
    adapter = SeededCall(response)

    read = delivered_to_destination(adapter, "SOL", txid_for(14), SOL_PAYOUT)

    assert read.amount == 0.000761654
    assert adapter.calls[0][0] == "getTransaction"
    assert "preBalances" in read.how


def test_a_FAILED_solana_transaction_reads_as_nothing():
    response = {"transaction": {"message": {"accountKeys": [SOL_PAYOUT]}},
                "meta": {"err": {"InstructionError": [0, "Custom"]}, "preBalances": [0],
                         "postBalances": [0]}}

    read = delivered_to_destination(SeededCall(response), "SOL", txid_for(14), SOL_PAYOUT)

    assert read.amount is None
    assert "FAILED" in read.how


# ---------------------------------------------------------------------------
# THE CHAIN/QUANTIZER DISAGREEMENT, AND THE FLAG THAT RESOLVES IT.
#
# MEASURED ON THE OPERATOR'S HOST 2026-10-03, on a dry run after --apply had
# corrected 12 of the 15 rows. Three were REFUSED, all GRC, for a chain/quantizer
# disagreement -- and the cause was then established: Gridcoin 5.5.1.0's RPC
# rounds an over-precise amount HALF UP (`roundint64(dAmount * COIN)`,
# src/rpc/server.cpp:105-114) where chains/coin_amounts.fit_to_chain_precision()
# TRUNCATES.
#
# Over all 9 of their GRC payout rows:
#
#     id   remainder beyond 8dp   truncate==chain?   half-up==chain?
#     3            0.0979         True               True
#     6            0.7348         FALSE              True
#     7            0.5286         FALSE              True
#     8            0.2180         True               True
#     9            0.2036         True               True
#     10           0.2641         True               True
#     11           0.2184         True               True
#     17           0.7380         FALSE              True
#     22           0.0158         True               True
#
# 9 of 9 fit ROUND_HALF_UP; 6 of 9 fit truncation, and the 6 are exactly the rows
# whose remainder is below 0.5, where the two roundings cannot differ. The three
# that discriminate are the three that were refused.
#
# THE CHAIN FIGURES BELOW ARE THE OPERATOR'S MEASUREMENT, read off their own
# daemon. Nothing in this container can reach it and they were NOT re-run here
# (rule 17); they are typed from that reading, like every other expectation in
# this file.
#
# AND THE OPERATOR'S 2026-10-03 DECISION TO MAKE EVERY CHAIN TRUNCATE DID NOT
# RESOLVE THEM. That changed XRP's to_drops(); these are GRC sends made before
# the payout service began quantizing first. The quantizer still truncates, the
# chain still rounded, and the disagreement on these three rows is permanent.
# ---------------------------------------------------------------------------

#: payout id -> the figure the GRC chain reports, which is NOT what the quantizer
#: computes. The quantizer's figure for each is in CORRECTABLE above: 82.65089987,
#: 87.97839509 and 2701.34958031 -- one satoshi below each of these.
GRIDCOIN_ROUNDED_UP = {
    6: 82.65089988,
    7: 87.9783951,
    17: 2701.34958032,
}


def gridcoin_as_the_operators_daemon_answers() -> SeededGridcoinDaemon:
    """Every GRC row's chain figure as their daemon reports it: 6 agreeing, 3 not."""
    answers = {
        txid_for(row[0]): GRIDCOIN_ROUNDED_UP.get(row[0], row[4])
        for row in grc_rows()
    }
    return SeededGridcoinDaemon(answers, GRC_PAYOUT)


@pytest.mark.parametrize("payout_id", sorted(GRIDCOIN_ROUNDED_UP), ids=lambda i: f"id{i}")
def test_the_three_rows_GRIDCOIN_ROUNDED_UP_are_REFUSED_without_the_flag(payout_id, tmp_path, monkeypatch):
    """Today's behavior, unchanged by the flag existing. The ROWS are the assertion.

    The default must stay exactly what it was: nothing written for a disagreeing
    row, both figures printed, exit 5. A flag that changed the default would be a
    patch on a refusal rather than a way to resolve one (rule 19).
    """
    db_path = seeded(tmp_path)
    monkeypatch.setattr(correct_payout_amounts, "build_adapters",
                        lambda rpc: {"GRC": gridcoin_as_the_operators_daemon_answers()})

    code = main(["--db", str(db_path), "--apply"])

    assert code == 5, "a disagreement must not exit 0"
    assert amounts(db_path)[payout_id] == recorded_amounts()[payout_id], (
        f"payouts.id={payout_id} was rewritten although the chain and the quantizer disagreed"
    )
    assert not any(f"payouts.id={payout_id} " in row["message"] for row in audit_rows(db_path)), (
        "an audit row exists for a row nothing was written for"
    )
    assert len(audit_rows(db_path)) == len(CORRECTABLE) - len(GRIDCOIN_ROUNDED_UP), (
        "the agreeing rows must still correct; only the three disagreeing ones are held"
    )


def test_plan_for_payout_REFUSES_a_disagreement_WHEN_THE_KEYWORD_IS_NOT_PASSED():
    """The keyword default, pinned directly. A MUTATION SURVIVED FOR WANT OF THIS.

    WRITTEN BECAUSE A MUTATION SURVIVED, and the reason is worth more than the
    test. Flipping `trust_chain_over_quantizer`'s default from False to True left
    the whole suite green, including
    test_the_three_rows_GRIDCOIN_ROUNDED_UP_are_REFUSED_without_the_flag -- which
    is exactly the test that looks like it covers this. It does not: main() passes
    the value EXPLICITLY (`trust_chain_over_quantizer=args.trust_chain_over_
    quantizer`), so no run through main() ever reads the keyword default, and a
    mutation of it cannot change what main() does.

    So the default was a documented safety property with nothing pinning it, and
    the only way to pin it is to call the function the way a NEW caller would --
    without the keyword. That is the case that matters: the next caller of this
    decision gets whatever the default is.

    MUTATION RUN 2026-10-03: default False -> True. This fails. The parser's own
    default is a SECOND default and is pinned separately below, because
    `action="store_true"` is a different line that could change on its own.
    """
    chain = ChainAmount(GRIDCOIN_ROUNDED_UP[17], "gettransaction.details, the operator's own daemon")
    row = row_for(17, "GRC", recorded_amounts()[17], txid_for(17), "broadcast")

    plan = plan_for_payout(row, chain)

    assert plan.verdict == REFUSE, (
        "a chain/quantizer disagreement must be REFUSED unless a caller explicitly asks for the "
        "chain to be preferred. This is the default that decides what a new caller gets"
    )
    assert plan.corrected is None, "a refused row must carry no figure a caller could write"
    assert plan.authority == ""


def test_the_PARSER_defaults_the_flag_to_OFF():
    """The second default: argparse's. A separate line, so a separate assertion.

    MUTATION RUN 2026-10-03: `action="store_true"` -> `action="store_false"`.
    This fails, and so does
    test_the_three_rows_GRIDCOIN_ROUNDED_UP_are_REFUSED_without_the_flag -- which
    is the pair that covers main()'s wiring, where the keyword default covers a
    direct caller's.
    """
    default = correct_payout_amounts.build_parser().parse_args([])
    asked = correct_payout_amounts.build_parser().parse_args([correct_payout_amounts.TRUST_CHAIN_FLAG])

    assert default.trust_chain_over_quantizer is False, (
        "the flag is on without being asked for, which would make a disagreement correct itself"
    )
    assert asked.trust_chain_over_quantizer is True, "the flag does not reach the decision"
    assert default.apply is False and default.no_chain is False, (
        "the other two defaults, pinned here because this is the one place they are all visible"
    )


@pytest.mark.parametrize("payout_id", sorted(GRIDCOIN_ROUNDED_UP), ids=lambda i: f"id{i}")
def test_the_flag_corrects_a_disagreeing_row_to_THE_CHAINS_OWN_FIGURE(payout_id, tmp_path, monkeypatch):
    """The chain's figure, not the quantizer's -- asserted on the row that is there.

    THE DISTINCTION THAT MAKES THIS TEST WORTH HAVING: the two candidate figures
    are one satoshi apart, so a flag that wrote the QUANTIZER's number would look
    correct in every count, in the exit code, and in the audit row's shape. Only
    the last digit tells them apart, which is why the expectation is typed from
    the operator's chain reading rather than computed here.
    """
    db_path = seeded(tmp_path)
    monkeypatch.setattr(correct_payout_amounts, "build_adapters",
                        lambda rpc: {"GRC": gridcoin_as_the_operators_daemon_answers()})

    code = main(["--db", str(db_path), "--apply", correct_payout_amounts.TRUST_CHAIN_FLAG])

    assert code == 0, "every row was resolved, so nothing is left for a reader to look at"
    assert amounts(db_path)[payout_id] == GRIDCOIN_ROUNDED_UP[payout_id], (
        f"payouts.id={payout_id} is not the CHAIN's figure; the quantizer's is one satoshi lower"
    )
    quantizer_figure = next(row[4] for row in CORRECTABLE if row[0] == payout_id)
    assert amounts(db_path)[payout_id] != quantizer_figure, (
        "the quantizer's figure was written, which is the one thing this flag must not do"
    )
    # Every other row still corrects, so the flag resolved three and widened to nothing.
    assert len(audit_rows(db_path)) == len(CORRECTABLE)


@pytest.mark.parametrize("payout_id", sorted(GRIDCOIN_ROUNDED_UP), ids=lambda i: f"id{i}")
def test_the_audit_row_for_such_a_correction_holds_BOTH_figures_and_names_the_authority(
    payout_id, tmp_path, monkeypatch,
):
    """A reader a year later must see the DISAGREEMENT, not just the result.

    FIVE THINGS HAVE TO BE IN THAT ROW and each is a separate assertion, because
    a row carrying four of them reads like a routine correction: the original
    figure, the CHAIN's figure that was written, the QUANTIZER's figure that was
    not, the statement that the chain was taken as authoritative, and an
    authority string distinct from plain CHAIN-VERIFIED. Without the quantizer's
    figure the row records a correction and hides that anything was in dispute.

    THE FIRST VERSION OF THIS TEST WAS GREEN FOR THE WRONG REASON, found by
    mutation and not by reading it, and the reason is recorded because it will
    recur in any test comparing a truncated figure against the figure it was
    truncated FROM. It asserted `repr(quantizer_figure) in message`. Every
    quantizer figure here is the recorded figure truncated at eight decimals, so
    its repr is a PREFIX of the recorded figure's repr:

        recorded   2701.3495803173805
        quantizer  2701.34958031        <- a substring of the line above

    and the audit row carries the recorded figure by design. So the assertion
    passed on all three rows with the quantizer's figure removed from the message
    entirely -- the mutation that deletes it SURVIVED. The figure is now looked
    for in the message WITH THE RECORDED FIGURE REMOVED, which is the only way
    the two can be told apart, plus the structured phrase that names it.

    MUTATION RUN 2026-10-03, after the fix: both interpolations of `quantized`
    removed from the correction's sentence. All three cases fail.
    """
    db_path = seeded(tmp_path)
    monkeypatch.setattr(correct_payout_amounts, "build_adapters",
                        lambda rpc: {"GRC": gridcoin_as_the_operators_daemon_answers()})

    main(["--db", str(db_path), "--apply", correct_payout_amounts.TRUST_CHAIN_FLAG])

    message = next(row["message"] for row in audit_rows(db_path)
                   if f"payouts.id={payout_id} " in row["message"])
    quantizer_figure = next(row[4] for row in CORRECTABLE if row[0] == payout_id)

    recorded = recorded_amounts()[payout_id]
    assert repr(recorded) in message, "the ORIGINAL figure is gone from the database"
    assert repr(GRIDCOIN_ROUNDED_UP[payout_id]) in message, "the figure WRITTEN is not in its own audit row"
    # THE RECORDED FIGURE IS REMOVED FIRST. repr(quantizer) is a prefix of
    # repr(recorded) on every one of these rows, so searching the whole message
    # would pass with the quantizer's figure deleted -- see this test's docstring
    # for the mutation that proved it.
    assert repr(quantizer_figure) in message.replace(repr(recorded), ""), (
        "the QUANTIZER's figure is absent, so the row records a correction and not a disagreement. "
        "It must appear somewhere other than inside the recorded figure it is a prefix of"
    )
    assert f"says {quantizer_figure!r}" in message, (
        "the quantizer's figure is in the row but not as the quantizer's claim, so a reader cannot "
        "tell which of the two numbers it is"
    )
    assert "THE CHAIN AND THE QUANTIZER DISAGREE" in message
    assert "TAKEN AS AUTHORITATIVE" in message, "which of the two won has to be stated, not implied"
    assert correct_payout_amounts.FROM_CHAIN_OVER_QUANTIZER in message
    assert FROM_CHAIN not in message.replace(correct_payout_amounts.FROM_CHAIN_OVER_QUANTIZER, ""), (
        "a disagreed correction must not also read as plain CHAIN-VERIFIED"
    )


def test_a_DRY_RUN_with_the_flag_shows_what_it_would_write_and_writes_nothing(
    tmp_path, monkeypatch, capsys,
):
    """--apply is still required. The dry run has to show the exact figure.

    BOTH HALVES ARE THE TEST. The rows must be untouched, and the output must
    name the figure that WOULD be written -- a dry run that says "3 rows would be
    corrected" without the digits cannot be checked before it is run for real,
    and on this tool the digits are the entire subject.
    """
    db_path = seeded(tmp_path)
    monkeypatch.setattr(correct_payout_amounts, "build_adapters",
                        lambda rpc: {"GRC": gridcoin_as_the_operators_daemon_answers()})

    code = main(["--db", str(db_path), correct_payout_amounts.TRUST_CHAIN_FLAG])

    assert code == 0
    assert amounts(db_path) == recorded_amounts(), "a dry run with the flag changed a row"
    assert audit_rows(db_path) == [], "a dry run with the flag wrote an audit row"
    printed = capsys.readouterr().out
    for payout_id, chain_figure in GRIDCOIN_ROUNDED_UP.items():
        assert f"correct to  {chain_figure!r}" in printed, (
            f"the dry run did not show the figure it would write for payouts.id={payout_id}"
        )
    assert "CHAIN AND QUANTIZER DISAGREED -- CHAIN TAKEN AS AUTHORITATIVE" in printed, (
        "a correction over a disagreement must not print like an ordinary chain-verified one"
    )


#: Each of chains/payout_on_chain.py's read refusals, as the response that
#: produces it, with the asset it belongs to and the phrase it reports. These are
#: DIFFERENT refusals from the chain/quantizer disagreement and the flag must not
#: reach any of them -- see correct_payout_amounts.py's docstring for why that is
#: structural (they all return ChainAmount(None, why), which cannot reach the
#: branch the flag governs).
OTHER_REFUSALS = [
    ("XRP", "PARTIAL PAYMENT", {
        "tx_json": {"TransactionType": "Payment", "Destination": XRP_CUSTOMER_PAYOUT,
                    "Amount": "3315589", "hash": XRP_CONFIRMED_TXID},
        "meta": {"TransactionResult": "tesSUCCESS", "delivered_amount": "1"},
        "validated": True,
    }),
    ("SOL", "not a credit", {
        "transaction": {"message": {"accountKeys": [{"pubkey": "payer"}, {"pubkey": SOL_PAYOUT}]}},
        "meta": {"err": None, "preBalances": [10_000_000, 500], "postBalances": [9_000_000, 500]},
    }),
    ("SOL", "not a credit", {
        "transaction": {"message": {"accountKeys": [{"pubkey": "payer"}, {"pubkey": SOL_PAYOUT}]}},
        "meta": {"err": None, "preBalances": [10_000_000, 900], "postBalances": [9_000_000, 500]},
    }),
    ("GRC", "Refusing to choose or to sum", {
        "vout": [{"n": 0, "value": 1.0, "scriptPubKey": {"address": GRC_PAYOUT}},
                 {"n": 1, "value": 2.0, "scriptPubKey": {"address": GRC_PAYOUT}}],
    }),
    ("GRC", "no output of", {
        "vout": [{"n": 0, "value": 1.0, "scriptPubKey": {"address": "somebody-else"}}],
    }),
    ("GRC", "neither a `details` list nor a `vout` list", {"confirmations": 3}),
]


class SeededTransaction:
    """A Bitcoin-family adapter that returns one seeded `gettransaction` response.

    SEPARATE FROM SeededCall BECAUSE THE TWO READ PATHS DIFFER, which is a real
    difference rather than a duplicate stub (rule 8): chains/payout_on_chain.py
    asks the Bitcoin-derived chains through the adapter's get_transaction() and
    asks XRP and Solana through its call(). A single stub carrying both would let
    a test pass against the wrong path.
    """

    def __init__(self, response):
        self.response, self.calls = response, []

    def get_transaction(self, txid: str) -> dict:
        self.calls.append(txid)
        return self.response | {"txid": txid}


def adapter_for(asset: str, response):
    """The stub shaped like `asset`'s read path. See SeededTransaction."""
    return SeededCall(response) if asset in ("XRP", "SOL") else SeededTransaction(response)


@pytest.mark.parametrize(("asset", "phrase", "response"), OTHER_REFUSALS,
                         ids=[f"{row[0]}-{index}" for index, row in enumerate(OTHER_REFUSALS)])
def test_the_flag_does_NOT_reach_any_OTHER_kind_of_refusal(asset, phrase, response):
    """A partial payment, a non-positive Solana delta, two outputs -- none of them.

    WHAT THESE ACTUALLY ARE, said precisely, because "refusal" is doing two jobs
    in this tree. chains/payout_on_chain.py refuses to READ a transaction in nine
    situations and returns ChainAmount(None, why) for every one. The tool then
    corrects such a row from ARITHMETIC, carrying that sentence into the audit
    row -- so they are not REFUSE verdicts at all, and the flag cannot resolve
    them even in principle: the branch it governs sits below `if chain.amount is
    None`.

    ASSERTED BY COMPARING THE WHOLE VERDICT WITH AND WITHOUT THE FLAG, which is
    the strongest form available: not merely that the row still corrects, but
    that nothing about the decision moved. Run through the REAL
    delivered_to_destination() against each shaped response rather than by
    constructing ChainAmount(None, ...) by hand, because the claim is about what
    those nine situations produce and not about what None does.
    """
    chain = delivered_to_destination(adapter_for(asset, response), asset, txid_for(3), {
        "XRP": XRP_CUSTOMER_PAYOUT, "SOL": SOL_PAYOUT, "GRC": GRC_PAYOUT,
    }[asset])
    assert chain.amount is None and phrase in chain.how, "the fixture does not produce this refusal"
    recorded = recorded_amounts()[3] if asset == "GRC" else 3.3155893288590605
    row = row_for(3, asset, recorded, txid_for(3), "broadcast")

    with_flag = plan_for_payout(row, chain, trust_chain_over_quantizer=True)
    without = plan_for_payout(row, chain)

    assert with_flag == without, (
        f"the flag changed the verdict for a {asset} read that returned no figure at all. It must "
        f"reach the chain/quantizer disagreement and nothing else"
    )
    assert with_flag.verdict == CORRECT and with_flag.authority == FROM_ARITHMETIC, (
        "an unread chain is an ARITHMETIC ONLY correction, flag or no flag"
    )
    assert phrase in with_flag.why, "the chain's reason for not answering has to reach the audit row"


def test_the_flag_and_NO_CHAIN_together_are_REFUSED_rather_than_silently_inert(tmp_path):
    """Two flags that contradict each other in words. Rule 14: an inert flag must not look busy."""
    db_path = seeded(tmp_path)

    code = main(["--db", str(db_path), "--no-chain", correct_payout_amounts.TRUST_CHAIN_FLAG])

    assert code == 2
    assert amounts(db_path) == recorded_amounts(), "a refused invocation wrote a row"


def test_the_flag_still_needs_APPLY_and_the_CAS_still_guards_a_changed_row(tmp_path):
    """The flag resolves a disagreement; it does not loosen anything else.

    The compare-and-swap is the guard against a concurrent writer and is a THIRD
    kind of refusal, below both the verdict and the flag. A row that changed
    between the read and the write must still match zero rows and write no audit
    row, whichever authority produced the figure.
    """
    db_path = seeded(tmp_path)
    chain = ChainAmount(GRIDCOIN_ROUNDED_UP[17], "gettransaction.details, the operator's own daemon")
    with db_session(str(db_path)) as db:
        row = dict(db.execute("SELECT p.*, s.status AS swap_status FROM payouts p "
                              "JOIN swaps s ON s.id = p.swap_id WHERE p.id = 17").fetchone())
        plan = plan_for_payout(row, chain, trust_chain_over_quantizer=True)
        assert plan.verdict == CORRECT and plan.authority == correct_payout_amounts.FROM_CHAIN_OVER_QUANTIZER
        db.execute("UPDATE payouts SET amount = ? WHERE id = 17", (99.123456789,))
        db.commit()

        wrote, sentence = apply_correction(db, row, plan)

    assert not wrote and "no longer holds" in sentence
    assert amounts(db_path)[17] == 99.123456789, "the CAS let a changed row be overwritten"
    assert audit_rows(db_path) == [], "an audit row was written for a correction that did not happen"


# ---------------------------------------------------------------------------
# THE FOOTER AND THE SUMMARY. Four states that must not share a shape (rule 14).
# ---------------------------------------------------------------------------


#: Database paths the footer and the summary only ECHO. Nothing opens them, and
#: they are deliberately not under /tmp: a real temp path makes ruff's S108 fire
#: on a string that is never a file, and rule 19 forbids answering that with a
#: suppression. tmp_path is used wherever a database is actually written.
ECHOED_DB_PATH = "seeded/not-opened.db"
CHOSEN_DB_PATH = "seeded/chosen-with-db-flag.db"


def test_a_row_the_chain_ROUNDED_UP_is_reported_as_UNDERSTATED_not_negatively_overstated():
    """A minus sign in front of a word that says the opposite. INTRODUCED HERE, THEN READ.

    Every correction before --trust-the-chain-over-the-quantizer existed reduced
    the recorded figure, because all five quantizers truncate -- so describe()'s
    constant "overstated by" label was right for every case that could occur.
    The flag creates the other case for the first time: Gridcoin rounded UP, so
    the chain's figure is LARGER than the record and the record UNDERSTATES what
    moved. The line printed:

        overstated by -0.0000000026195 GRC

    which makes a reader notice the sign and then distrust the label. Found by
    reading the tool's own output, not by a failing test, which is why there is
    now one.

    BOTH DIRECTIONS ARE ASSERTED, because choosing the word from the sign could
    as easily be inverted, and an inverted label is worse than a constant one --
    it reads as confident and is wrong.
    """
    recorded = recorded_amounts()[17]
    over = correct_payout_amounts.misstatement(recorded, 2701.34958031)
    under = correct_payout_amounts.misstatement(recorded, GRIDCOIN_ROUNDED_UP[17])

    assert over.startswith("overstated by 0.0000000073805"), over
    assert under.startswith("UNDERSTATED by 0.0000000026195"), under
    assert "-" not in under, "a negative number under a label that says the opposite"
    assert "-" not in over

    # And through the real printer, on the row the operator's daemon rounded up.
    row = row_for(17, "GRC", recorded, txid_for(17), "broadcast")
    plan = plan_for_payout(row, ChainAmount(GRIDCOIN_ROUNDED_UP[17], "gettransaction.details"),
                           trust_chain_over_quantizer=True)
    lines = "\n".join(correct_payout_amounts.describe(row, plan))

    assert "UNDERSTATED by 0.0000000026195 GRC" in lines
    assert "overstated by" not in lines, (
        "the chain sent MORE than the record says; calling that an overstatement inverts it"
    )
    assert "which the chain cannot send" not in lines, (
        "the chain DID send this row -- what it cannot do is express the recorded figure"
    )


class FakeArgs:
    """The four fields the footer reads, so the states can be seeded directly."""

    def __init__(self, *, apply=False, no_chain=False, trust=False, db=""):
        self.apply, self.no_chain, self.trust_chain_over_quantizer, self.db = apply, no_chain, trust, db


def test_the_DRY_RUN_FOOTER_offers_no_command_when_there_is_nothing_to_correct():
    """The defect, measured on the operator's host 2026-10-03.

    A second dry run after --apply had corrected 12 rows printed, correctly,
    `CORRECT 0 row(s) / (none)` and `REFUSE 3 row(s)` -- and then ended:

        DRY RUN: nothing written. To correct the 0 row(s) above:
            python3 correct_payout_amounts.py --apply

    "the 0 row(s) above" is a sentence about an empty set, and the command would
    have written nothing. The footer's SHAPE was identical whether there were
    twelve rows to correct or none, so a reader skimming saw a call to action
    that was not one.
    """
    nothing_to_do = _footer(FakeArgs(), ECHOED_DB_PATH, correctable=0, refusals=0)
    some_to_do = _footer(FakeArgs(), ECHOED_DB_PATH, correctable=12, refusals=0)

    joined = "\n".join(nothing_to_do)
    assert "--apply" in joined, "the reader still has to be told --apply would do nothing"
    assert f"python3 {correct_payout_amounts.SELF} --apply" not in joined, (
        "a command was offered for an empty set of rows -- the measured defect"
    )
    assert "NOTHING TO DO" in joined
    assert "the 0 row(s)" not in joined, "a sentence about an empty set"
    assert f"python3 {correct_payout_amounts.SELF} --apply" in "\n".join(some_to_do), (
        "the twelve-row state must still name the command; that half was never wrong"
    )
    assert joined != "\n".join(some_to_do), (
        "did-nothing and did-work printed the same footer, which is the defect itself"
    )


def test_the_DRY_RUN_FOOTER_points_at_THE_FLAG_when_only_refusals_remain():
    """--apply cannot resolve a refusal, so offering it bare would be wrong."""
    lines = _footer(FakeArgs(), ECHOED_DB_PATH, correctable=0, refusals=3)

    joined = "\n".join(lines)
    assert correct_payout_amounts.TRUST_CHAIN_FLAG in joined, (
        "the one flag that writes anything for those rows is not named"
    )
    assert "3 row(s) were REFUSED" in joined
    assert "--apply alone would write NOTHING" in joined
    assert "NOTHING TO DO" not in joined, (
        "there IS something to do -- three rows are refused -- so the nothing-to-do sentence is false"
    )


def test_the_DRY_RUN_FOOTER_gives_BOTH_when_rows_are_correctable_AND_refused():
    """Corrections first, so the refusals are not buried under a command that skips them."""
    lines = _footer(FakeArgs(), ECHOED_DB_PATH, correctable=12, refusals=3)

    joined = "\n".join(lines)
    assert joined.index("To correct the 12 row(s)") < joined.index("were REFUSED"), (
        "the refusals must come after the command, not be hidden above it"
    )
    assert correct_payout_amounts.TRUST_CHAIN_FLAG in joined


def test_the_FOOTER_offers_nothing_once_the_flag_has_already_been_passed():
    """Re-suggesting a flag that is already on is noise, and reads as though it failed."""
    for apply_mode in (False, True):
        lines = _footer(FakeArgs(apply=apply_mode, trust=True), ECHOED_DB_PATH, correctable=0, refusals=0)
        assert correct_payout_amounts.TRUST_CHAIN_FLAG not in "\n".join(lines)


def test_the_FOOTER_echoes_NO_CHAIN_and_DB_so_a_pasted_command_repeats_the_SAME_run():
    """Rule 14: echo the parameters that decide the answer."""
    lines = _footer(FakeArgs(no_chain=True, db=CHOSEN_DB_PATH), CHOSEN_DB_PATH,
                    correctable=4, refusals=0)

    joined = "\n".join(lines)
    assert "--no-chain" in joined and f"--db {CHOSEN_DB_PATH}" in joined
    assert "--db" not in "\n".join(_footer(FakeArgs(), ECHOED_DB_PATH, correctable=4, refusals=0)), (
        "--db was echoed although it was never passed; the default is already on the database line"
    )


def test_the_FOOTERS_command_RE_RUNS_THE_SAME_RUN_when_the_flag_is_already_on():
    """The count and the command must describe ONE run. They disagreed by three rows.

    THE DEFECT THIS PINS WAS SHIPPED AND THEN READ, 2026-10-03, in the first
    version of _rerun_command(). With the flag on, 15 rows correctable and 3 of
    them resolved only because the flag was passed, the footer said:

        DRY RUN: nothing written. To correct the 15 row(s) above:
            python3 correct_payout_amounts.py --apply --db ...

    That command omits the flag, so it corrects 12 and refuses 3. The sentence
    was true about the run the reader had just done and false about the one it
    told them to do -- which is the same shape as the "0 row(s) above" footer
    this whole group of tests exists to remove, found the same way: by reading
    the output rather than the code.

    ASSERTED ON BOTH DIRECTIONS, because echoing it unconditionally would be the
    opposite error -- a reader who did not pass the flag must not be handed a
    command that quietly resolves disagreements for them.
    """
    with_flag = "\n".join(_footer(FakeArgs(trust=True), ECHOED_DB_PATH, correctable=15, refusals=0))
    without = "\n".join(_footer(FakeArgs(), ECHOED_DB_PATH, correctable=12, refusals=0))

    assert "To correct the 15 row(s)" in with_flag
    assert correct_payout_amounts.TRUST_CHAIN_FLAG in with_flag, (
        "the command would correct 12 rows, not the 15 the line above it counts"
    )
    assert "To correct the 12 row(s)" in without
    assert correct_payout_amounts.TRUST_CHAIN_FLAG not in without, (
        "a reader who did not ask for the chain to be preferred was handed a command that does it"
    )
    # And the flag is never printed twice in the one footer that adds it itself.
    both = "\n".join(_footer(FakeArgs(), ECHOED_DB_PATH, correctable=12, refusals=3))
    assert both.count(correct_payout_amounts.TRUST_CHAIN_FLAG + " ") <= 1, (
        f"the flag appears more than once in one command:\n{both}"
    )


def test_the_SUMMARY_says_none_rather_than_dividing_an_empty_set_in_two():
    """`0 of those 0 verified ... 0 from arithmetic alone` reads like a defect.

    Rule 14 asks that an empty result print `(none)` rather than nothing. The
    companion, which this is: a BREAKDOWN of an empty set is not a result either,
    and printing one makes a reader check whether the tool broke. The COUNT with
    its denominator still prints, because `0 correctable of 23` is the answer.
    """
    empty = _summary_line(ECHOED_DB_PATH, rows=23, correctable=0, verified=0, over_quantizer=0)

    assert "0 correctable of 23 payout row(s)" in empty, "the count and its denominator are the answer"
    assert "(none)" in empty
    assert "0 of those 0" not in empty
    assert "arithmetic alone" not in empty, "an empty set has no chain/arithmetic split"


def test_the_SUMMARY_breaks_out_the_rows_corrected_OVER_a_disagreement():
    """Three provenances, three counts. A disagreed correction is not chain-verified."""
    line = _summary_line(ECHOED_DB_PATH, rows=23, correctable=15, verified=6, over_quantizer=3)

    assert "15 correctable of 23 payout row(s)" in line
    assert "6 of those 15 verified against the chain itself" in line
    assert "3 corrected to the chain's figure OVER a quantizer disagreement" in line
    assert "6 from arithmetic alone" in line, "15 - 6 - 3; a run's provenances have to sum to its count"
    assert correct_payout_amounts.TRUST_CHAIN_FLAG in line


def test_a_run_that_corrects_OVER_a_disagreement_WARNS_that_the_disagreement_is_real(
    tmp_path, monkeypatch, capsys,
):
    """Resolving the row says nothing about the chain, and a reader would conclude otherwise."""
    db_path = seeded(tmp_path)
    monkeypatch.setattr(correct_payout_amounts, "build_adapters",
                        lambda rpc: {"GRC": gridcoin_as_the_operators_daemon_answers()})

    main(["--db", str(db_path), "--apply", correct_payout_amounts.TRUST_CHAIN_FLAG])

    printed = capsys.readouterr().out
    assert "3 row(s) were written from the chain DESPITE the quantizer disagreeing" in printed
    assert "rounding an over-precise amount half" in printed, (
        "the warning has to say WHAT disagreed, not merely that something did"
    )


# ---------------------------------------------------------------------------
# WHAT A SECOND RUN COSTS, AND WHY IT IS READABLE.
# ---------------------------------------------------------------------------


def test_a_SECOND_run_asks_the_chain_only_for_the_rows_that_still_need_correcting(tmp_path, monkeypatch):
    """Measured on the operator's host 2026-10-03: 3 chain reads on the second run, not 23.

    THE PROPERTY, and rule 3 says to measure it rather than assume it survives a
    refactor. plans_for() calls the pure decision TWICE: once with NOT_ASKED to
    learn whether the row would be corrected at all, and only then does it spend
    a network round trip. So an already-corrected database costs zero RPC calls,
    and after the first --apply the only rows still asked about are the ones that
    were refused.

    A `for` LOOP MOVED ONE LINE would turn a silent no-op run into a read per
    row, with nothing on screen to say so, which is why the assertion is on the
    daemon's recorded call list and not on the output.
    """
    db_path = seeded(tmp_path)
    first = gridcoin_as_the_operators_daemon_answers()
    monkeypatch.setattr(correct_payout_amounts, "build_adapters", lambda rpc: {"GRC": first})
    main(["--db", str(db_path), "--apply"])
    assert len(first.calls) == len(grc_rows()), "the first run asks about every GRC row needing a fix"

    second = gridcoin_as_the_operators_daemon_answers()
    monkeypatch.setattr(correct_payout_amounts, "build_adapters", lambda rpc: {"GRC": second})
    main(["--db", str(db_path)])

    assert sorted(second.calls) == sorted(txid_for(payout_id) for payout_id in GRIDCOIN_ROUNDED_UP), (
        f"the second run asked the chain {len(second.calls)} time(s); only the "
        f"{len(GRIDCOIN_ROUNDED_UP)} unresolved rows need asking, and every other row was already "
        f"corrected so the quantizer agrees with it before any network call"
    )


def test_an_ALREADY_CORRECT_row_and_a_PROPOSAL_row_skip_for_VISIBLY_DIFFERENT_reasons():
    """What makes a second run readable: 20 SKIPs that are not 20 of the same thing.

    After --apply the SKIP section holds both kinds at once -- the rows this tool
    corrected, which now agree with their chain, and the eight failed rows with no
    txid that it will never touch. Collapsing those into one sentence would make
    the output of a successful run indistinguishable from the output of a run that
    refused everything.
    """
    already = plan_for_payout(row_for(3, "GRC", 55.52645238, txid_for(3), "broadcast"), NOT_ASKED)
    proposal = plan_for_payout(row_for(1, "GRC", 143.39622296118311, None, "failed"), NOT_ASKED)

    assert already.verdict == proposal.verdict == SKIP
    assert already.why != proposal.why
    assert "already what GRC's chain can express" in already.why
    assert "this is the row state a second run of this tool sees" in already.why
    assert "PROPOSAL" in proposal.why
    assert "already what" not in proposal.why, "a proposal is not an already-correct row"
    assert "PROPOSAL" not in already.why, "an already-correct row never recorded a proposal"


def test_every_chain_this_terminal_can_quantize_for_can_also_be_READ_BACK():
    """A chain in one table and not the other is a gap worth failing on (rule 11).

    MUTATION: drop XRP from either table and this fails -- which is the state that
    would otherwise correct an XRP row from arithmetic forever while printing a
    confident ARITHMETIC ONLY line nobody would question.
    """
    assert set(READERS) == set(QUANTIZERS), (
        f"only quantized: {sorted(set(QUANTIZERS) - set(READERS))}; only readable: "
        f"{sorted(set(READERS) - set(QUANTIZERS))}"
    )
