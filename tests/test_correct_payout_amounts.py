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
