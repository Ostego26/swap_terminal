#!/usr/bin/env python3
"""The booked reserve against the fee the chain actually charged, over seeded rows.

Role: tests (read-only)
Reads: show_payout_fees.py's own functions, a temporary SQLite database it seeds,
      and a stub adapter. No network, no daemon, no live database.
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes

WHAT THIS IS FOR. The operator asked 2026-10-03 what the four
*_NETWORK_FEE_RESERVE figures should be. Nothing in the tree could answer it: the
three that exist are bare defaults with no provenance, and the only one anybody
had ever checked against a chain -- GRC -- was wrong by a factor of ten
(services/quote_service.get_network_fee_reserve() carries the 2026-10-01
measurement: "the real fee was 0.001 GRC, not 0.01").

show_payout_fees.py does that check for every payout in the database at once. It
reaches a live daemon, so the live run is the operator's; THIS pins the arithmetic
and the four reporting cases with seeded rows, which is the half that can be
verified from here (rule 16's line between a fix and a proposal).

THE RATIO IS THE FIGURE THAT MATTERS and it is why measured_vs_booked() returns
one. "0.01 booked against 0.001 charged" is a sentence an operator has to do
arithmetic on; "10.00x" is not.
"""

from __future__ import annotations

import sqlite3

import pytest
from config import Config

import show_payout_fees
from show_payout_fees import (
    MEASURABLE,
    UNMEASURABLE_HERE,
    ask_base_fee,
    load_payouts,
    main,
    measured_vs_booked,
    read_fees,
    report_asset,
)


class _Wallet:
    """A chain whose gettransaction answers a NEGATIVE fee, as a real one does.

    THE SIGN IS THE POINT, not an incidental. A Bitcoin wallet reports `fee` as a
    debit, so an implementation that forgot abs() would report every fee as
    negative and every ratio as negative -- which reads as a credit. Seeded
    negative here so that mistake cannot pass.
    """

    def __init__(self, fee=None, raises=False, omit=False):
        self._fee, self._raises, self._omit = fee, raises, omit

    def get_transaction(self, _txid):
        if self._raises:
            raise RuntimeError("the daemon refused")
        return {} if self._omit else {"fee": self._fee}


def _assets_without_a_reserve() -> list[str]:
    """Every TRADEABLE asset that has no `<ASSET>_NETWORK_FEE_RESERVE` on Config.

    DERIVED FROM Config.ALLOWED_PAIRS, NOT SPELLED. Until 2026-10-06 three tests in
    this file each carried

        [asset for asset in ("SOL", "XRP", "BTC", "LTC", "GRC") if not hasattr(...)]

    which is rule 8's failure in the place it does the most damage: a test whose
    whole job is to find an asset that lacks a reserve, looking only in a list of
    assets somebody typed. The list was already wrong when this was written -- ICP
    became tradeable on 2026-10-06 (ten pairs) and is absent from all three copies,
    so an ICP without a reserve would have been invisible to the very tests that
    exist to catch it. They would have skipped, said "every asset has a reserve
    now", and been believed.

    That is the reassuring direction of failure again, and it is the second instance
    found today: the same shape as duplicate_live_payouts() hand-spelling the status
    list its own index derives.

    MEASURED 2026-10-06, after deriving it:

        tradeable assets        BTC GRC ICP LTC SOL XRP   (6)
        with a reserve          BTC GRC ICP LTC SOL XRP   (6)
        without                 (none)

    So all three call sites skip today, and that is now a TRUE statement about every
    asset this terminal trades rather than about five of the six. The `(absent)`
    branch in show_payout_fees.report_asset() is deliberately NOT deleted as dead
    code: the next asset to be enabled reaches it before its reserve is set, which is
    exactly the window these tests cover.
    """
    assets = {asset for pair in Config.ALLOWED_PAIRS for asset in pair}
    return sorted(asset for asset in assets if not hasattr(Config, f"{asset}_NETWORK_FEE_RESERVE"))


def _collect():
    lines = []
    return lines, lines.append


def test_the_GRC_overstatement_reads_as_a_ratio_and_not_as_two_numbers():
    """The exact 2026-10-01 finding, as this tool would have printed it.

    MUTATION: drop abs() in read_fees(). The mean goes negative, the ratio goes
    negative, and this fails -- a negative ratio reads as the chain paying the desk.
    """
    rows = [{"asset": "GRC", "txid": "a" * 64, "amount": 143.4, "booked": 0.01, "sent_at": "t"}]
    per_asset = read_fees(rows, {"GRC": _Wallet(fee=-0.001)}, ask_chain=True, say=lambda _t: None)

    verdict = measured_vs_booked(per_asset["GRC"]["fees"], per_asset["GRC"]["booked"])
    assert verdict["n"] == 1
    assert verdict["measured"] == pytest.approx(0.001), "the fee must be reported as a positive cost"
    assert verdict["booked"] == pytest.approx(0.01)
    assert verdict["ratio"] == pytest.approx(10.0), (
        "the ratio is the whole output: 0.01 booked against 0.001 charged is 10x, and that is the "
        "number the operator acts on"
    )


def test_a_fee_the_chain_would_not_give_is_UNREAD_and_never_counted_as_zero():
    """Three ways a read fails, and none of them may become a measurement.

    A zero fee is a CLAIM -- that the payout was free. "The daemon refused", "the
    wallet has no fee for this" and "there is no adapter" are not that claim, and
    folding any of them into the mean would understate the real cost and make the
    reserve look too high, which is the direction that loses money.

    MUTATION: append 0.0 to `fees` in any of the three branches. The mean drops,
    `unread` empties, and both assertions below fail.
    """
    rows = [{"asset": "GRC", "txid": "b" * 64, "amount": 1.0, "booked": 0.01, "sent_at": "t"},
            {"asset": "GRC", "txid": "c" * 64, "amount": 1.0, "booked": 0.01, "sent_at": "t"},
            {"asset": "LTC", "txid": "d" * 64, "amount": 1.0, "booked": 0.001, "sent_at": "t"}]
    # GRC raises on the first and omits `fee` on the second only if one adapter did
    # both; two assets instead, so each failure mode is attributable to its own row.
    per_asset = read_fees(rows, {"GRC": _Wallet(raises=True)}, ask_chain=True, say=lambda _t: None)

    assert per_asset["GRC"]["fees"] == [], "a refused read became a measured fee"
    assert len(per_asset["GRC"]["unread"]) == 2, "both refusals must be recorded with a reason"
    assert all(why for _txid, why in per_asset["GRC"]["unread"]), "an unread row with no reason is a blank gap"
    # LTC had no adapter at all, which is its own reason and not a silent omission.
    assert per_asset["LTC"]["unread"] == [("d" * 64, "no LTC adapter in this process")]

    verdict = measured_vs_booked(per_asset["GRC"]["fees"], per_asset["GRC"]["booked"])
    assert verdict["n"] == 0 and verdict["measured"] is None, (
        "0 of 2 payouts answered, so there is no mean -- and None prints as '(none read)' where a 0.0 "
        "would print as a free payout"
    )


def test_a_wallet_that_omits_the_fee_field_is_unread_rather_than_free():
    """Separated from the raise, because the two are different wallet answers."""
    rows = [{"asset": "GRC", "txid": "e" * 64, "amount": 1.0, "booked": 0.01, "sent_at": "t"}]
    per_asset = read_fees(rows, {"GRC": _Wallet(omit=True)}, ask_chain=True, say=lambda _t: None)
    assert per_asset["GRC"]["fees"] == []
    assert "no `fee` field" in per_asset["GRC"]["unread"][0][1]


def test_the_two_chains_this_tool_cannot_ask_print_a_figure_and_its_provenance():
    """XRP and SOL are NOT MEASURABLE here, and must not read as having no payouts.

    THE SILENT-OMISSION FAILURE THIS GUARDS. A table that simply left XRP out
    would read as "no XRP payouts", which is a different fact from "this tool
    cannot ask XRP's chain" -- and the operator asking what to set
    XRP_NETWORK_FEE_RESERVE to is the exact reader who would be misled.

    MUTATION: remove XRP from UNMEASURABLE_HERE. The block falls through to the
    n==0 branch and prints "(none read)", and this fails.
    """
    assert set(UNMEASURABLE_HERE) == {"XRP", "SOL"}, (
        "the unmeasurable set changed. If a chain became measurable it belongs in MEASURABLE and its "
        "real fee belongs in the report; if one became unmeasurable it needs a figure and a provenance"
    )
    assert not set(UNMEASURABLE_HERE) & set(MEASURABLE), "a chain cannot be both"

    lines, say = _collect()
    report_asset("XRP", {"fees": [], "booked": [0.0], "unread": []}, say)
    body = "\n".join(lines)
    assert "NOT MEASURABLE FROM HERE" in body, body
    assert "0.00001 XRP" in body, "the figure the tree knows must be printed, not omitted"
    assert "autofilled" in body, "a figure with no provenance becomes the provenance for a reserve"
    assert "(none read)" not in body, (
        "an unmeasurable chain printed the same line as a measurable one that answered nothing -- those "
        "are different facts"
    )


def test_an_absent_reserve_says_the_quote_REFUSES_rather_than_printing_None():
    """An asset with no reserve must say what that COSTS, not print None.

    `None` beside a setting name tells the operator nothing. What they need is that
    every quote paying out in that asset refuses -- which is what they hit on
    2026-10-02 and the reason they asked the question this tool answers.

    THE EXAMPLE MOVED FROM XRP TO SOL ON 2026-10-03 and the property did not. This
    test used XRP, whose reserve the operator then set from a figure read off their
    own rippled; SOL's is still absent, so it is the live instance now. Written
    against whichever asset ACTUALLY lacks one rather than a hardcoded ticker, so
    the next reserve to be set moves the example again instead of failing the test
    for the wrong reason -- and the skip says so out loud if none is left.
    """
    absent = _assets_without_a_reserve()
    if not absent:
        pytest.skip("every asset has a reserve now, so there is no absent one to report -- and this "
                    "test and the (absent) branch it covers should be reconsidered together")
    lines, say = _collect()
    report_asset(absent[0], {"fees": [], "booked": [0.0], "unread": []}, say)
    assert "REFUSES" in lines[0], lines[0]
    assert "None" not in lines[0]


def test_load_payouts_opens_the_database_READ_ONLY(tmp_path):
    """The header claims it writes nothing; mode=ro is what makes that checkable.

    MUTATION: drop `?mode=ro` from the URI. This test fails, because the write
    below then succeeds on the same connection shape the tool uses.
    """
    db_path = tmp_path / "ro.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE payouts (id INTEGER PRIMARY KEY, swap_id TEXT, amount REAL, "
                       "txid TEXT, status TEXT, sent_at TEXT);"
                       "CREATE TABLE swaps (id TEXT PRIMARY KEY, to_asset TEXT, network_fee_reserve REAL);")
    conn.execute("INSERT INTO swaps VALUES ('s1', 'GRC', 0.01)")
    conn.execute("INSERT INTO payouts VALUES (1, 's1', 143.4, 'f', 'broadcast', 't')")
    conn.commit()
    conn.close()

    rows = load_payouts(str(db_path))
    assert [row["asset"] for row in rows] == ["GRC"]
    assert rows[0]["booked"] == pytest.approx(0.01)

    read_only = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_only.execute("INSERT INTO swaps VALUES ('s2', 'BTC', 0.1)")
    finally:
        read_only.close()


def test_a_pending_payout_is_not_a_cost_yet(tmp_path):
    """Only `broadcast` rows with a txid count. A queued payout has charged no fee.

    MUTATION: drop the status filter from PAYOUT_SQL. The pending row appears, its
    txid is NULL, and the tool asks the chain about None.
    """
    db_path = tmp_path / "pending.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE payouts (id INTEGER PRIMARY KEY, swap_id TEXT, amount REAL, "
                       "txid TEXT, status TEXT, sent_at TEXT);"
                       "CREATE TABLE swaps (id TEXT PRIMARY KEY, to_asset TEXT, network_fee_reserve REAL);")
    conn.executescript("INSERT INTO swaps VALUES ('s1', 'GRC', 0.01);"
                       "INSERT INTO swaps VALUES ('s2', 'GRC', 0.01);"
                       "INSERT INTO payouts VALUES (1, 's1', 1.0, 'sent', 'broadcast', 't1');"
                       "INSERT INTO payouts VALUES (2, 's2', 1.0, NULL, 'pending', 't2');")
    conn.commit()
    conn.close()

    rows = load_payouts(str(db_path))
    assert [row["txid"] for row in rows] == ["sent"], "a pending payout has not charged a fee yet"


# --- THE CALL SITE, NOT THE BRANCH ---------------------------------------------
#
# Every test above calls report_asset() or read_fees() directly, and ALL OF THEM
# PASSED while the tool printed this to the operator on 2026-10-03:
#
#     XRP  payouts=0  <- nothing paid out on this chain, so no fee to measure
#
# No reserve line, no "absent", no 10-drop figure -- for the reader who had just
# asked what to set XRP_NETWORK_FEE_RESERVE to. main() had a SECOND loop for the
# assets with no payouts, so report_asset()'s whole block was unreachable for
# exactly those assets and a unit test on the branch could not see it.
#
# That is this repository's recurring shape: a correct function whose CALL SITE
# discards the result. The tests below drive main() end to end over a seeded
# database with --no-chain, which is the only shape that would have failed.


def _seeded_db(tmp_path, rows=()):
    """A database with just enough schema for load_payouts(), plus the given rows."""
    db_path = tmp_path / "main.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE payouts (id INTEGER PRIMARY KEY, swap_id TEXT, amount REAL, "
                       "txid TEXT, status TEXT, sent_at TEXT);"
                       "CREATE TABLE swaps (id TEXT PRIMARY KEY, to_asset TEXT, network_fee_reserve REAL);")
    for index, (asset, txid, reserve) in enumerate(rows, start=1):
        conn.execute("INSERT INTO swaps VALUES (?, ?, ?)", (f"s{index}", asset, reserve))
        conn.execute("INSERT INTO payouts VALUES (?, ?, ?, ?, 'broadcast', ?)",
                     (index, f"s{index}", 1.0, txid, f"t{index}"))
    conn.commit()
    conn.close()
    return db_path


def test_main_gives_EVERY_asset_its_reserve_line_including_the_ones_with_no_payouts(tmp_path, capsys):
    """The defect the operator saw, pinned at the call site.

    MUTATION: restore main()'s second loop -- a bare "payouts=0" print for the
    assets missing from the fee table. XRP then has no reserve line and this fails
    on the first assertion.
    """
    db_path = _seeded_db(tmp_path, [("GRC", "a" * 64, 0.01)])
    assert main(["--db", str(db_path), "--no-chain"]) == 0
    body = capsys.readouterr().out

    for asset in ("GRC", "BTC", "LTC", "SOL", "XRP"):
        assert f"{asset}_NETWORK_FEE_RESERVE=" in body, (
            f"{asset} got no reserve line. An asset with no payouts still has a reserve, or lacks one, "
            f"and that is the fact an operator setting them needs"
        )
    # SOL'S IS THE ONE THAT IS ABSENT, and the consequence has to be on the screen.
    # This was XRP's until 2026-10-03, when the operator set XRP_NETWORK_FEE_RESERVE
    # from a figure read off their own rippled. Derived from Config rather than
    # hardcoded, so setting SOL's moves the example instead of failing this test.
    absent = _assets_without_a_reserve()
    for asset in absent:
        assert f"{asset}_NETWORK_FEE_RESERVE=(absent -- every quote paying out in this asset REFUSES)" in body, body
    # And the figures, which must print whether or not a reserve is configured: the
    # question "what should this be set to" is asked about the ones already set too.
    assert "0.00001 XRP" in body, "XRP printed no figure, so the question that prompted this tool is unanswered"
    assert "0.000005 SOL" in body, "SOL printed no figure"


def test_main_does_not_report_an_unasked_chain_the_same_way_as_an_asked_one(tmp_path, capsys):
    """"No payouts" and "could not ask" are different facts and must read differently.

    BTC is measurable and has no payouts; XRP has no payouts and could not have
    been asked either way. Both say there is no fee to measure; only XRP says the
    second thing.
    """
    db_path = _seeded_db(tmp_path, [("GRC", "b" * 64, 0.01)])
    assert main(["--db", str(db_path), "--no-chain"]) == 0
    lines = capsys.readouterr().out.splitlines()

    def block(asset):
        start = next(i for i, line in enumerate(lines) if line.startswith(f"{asset}  payouts="))
        rest = [line for line in lines[start + 1:] if line.startswith("  ")]
        return "\n".join(rest[:3])

    # THE WORDING MOVED 2026-10-03 AND THE INVARIANT DID NOT, which is why this
    # matches on "could not read a paid fee" rather than on the sentence it used to
    # assert ("could not ask anyway"). That phrase was written when nothing asked a
    # chain anything; ask_base_fee() now asks XRP for its current base fee, so the
    # line it prints depends on whether that read succeeded. What must stay true is
    # the DISTINCTION: BTC is a chain this tool can read a paid fee from and has no
    # payout, XRP is a chain it cannot read one from at all, and the two must not
    # print the same way.
    assert "no payouts on this chain yet" in block("BTC"), block("BTC")
    assert "could not read a paid fee" not in block("BTC"), "BTC is measurable; saying otherwise is false"
    assert "could not read a paid fee" in block("XRP"), block("XRP")


def test_main_on_an_empty_database_says_none_rather_than_printing_nothing(tmp_path, capsys):
    """Rule 14: a blank gap is ambiguous between zero rows and a query that broke."""
    db_path = _seeded_db(tmp_path)
    assert main(["--db", str(db_path), "--no-chain"]) == 0
    body = capsys.readouterr().out
    assert "(none)" in body
    assert "broadcast payouts with a txid: 0" in body


def test_main_reports_a_duration_that_is_not_a_bare_zero(tmp_path, capsys):
    """The second thing wrong on the operator's screen, also at the call site.

    Seven RPC round trips printed `done in 0.0µfn (0.0s)`. At one decimal anything
    under 0.05s renders as zero in both halves, so a real zero and a fast-but-real
    duration were the same string. microfortnights.format_duration() now reaches
    for more precision when the value is greater than zero.

    MUTATION: revert that guard. This run takes well under 0.05s, so the line goes
    back to "0.0µfn (0.0s)" and this fails.
    """
    db_path = _seeded_db(tmp_path, [("GRC", "c" * 64, 0.01)])
    assert main(["--db", str(db_path), "--no-chain"]) == 0
    done = next(line for line in capsys.readouterr().out.splitlines() if "done in" in line)
    assert "0.0µfn (0.0s)" not in done, (
        f"a run that did measurable work reported a bare zero, which is the string an exact zero owns: "
        f"{done!r}"
    )
    assert "µfn" in done and "ufn" not in done, "the unit is µfn (rule 6)"


# --- ASKING THE CHAIN, FOR A CHAIN WITH NO PAYOUT YET ---------------------------


class _Server:
    """An XRP adapter that answers server_parameters() the way chains/xrp.py does."""

    def __init__(self, drops=10, source="server_info.validated_ledger.base_fee_xrp", raises=False):
        self._drops, self._source, self._raises = drops, source, raises

    def server_parameters(self):
        if self._raises:
            raise RuntimeError("rippled is unreachable")
        return {"fee_drops": self._drops, "fee_source": self._source}


def test_a_fee_READ_from_the_server_is_labeled_as_read_and_outranks_the_tree():
    """The point of asking: the operator can tell a measurement from a typed figure.

    MUTATION: have ask_base_fee() fall back to UNMEASURABLE_HERE's figure when the
    server answers nothing. The "READ FROM THE SERVER" label then appears over a
    number nobody read, which is rule 17's failure with a label attached -- and
    test_a_failed_ask_prints_its_reason_and_NO_figure below fails.
    """
    lines, say = _collect()
    report_asset("XRP", {"fees": [], "booked": [], "unread": []}, say, adapter=_Server(drops=10))
    body = "\n".join(lines)

    assert "0.000010 XRP (10 drops)" in body, body
    assert "READ FROM THE SERVER, not the tree" in body, (
        "a figure read from the chain must say so, or it is indistinguishable from the reference value "
        "this function exists to replace"
    )
    assert "validated_ledger.base_fee_xrp" in body, "a read figure with no provenance is a typed figure"
    # The tree's figure still prints, now as a comparison rather than as the answer.
    assert "for comparison" in body, body


def test_a_failed_ask_prints_its_reason_and_NO_figure():
    """A broad catch here returns the REASON, never a number (rule 12's BLE001).

    If rippled cannot be reached the operator must read that, not a fee. A figure
    produced by a failed read is the worst of both: it looks measured and is not.
    """
    lines, say = _collect()
    report_asset("XRP", {"fees": [], "booked": [], "unread": []}, say, adapter=_Server(raises=True))
    body = "\n".join(lines)

    assert "FAILED" in body and "rippled is unreachable" in body, body
    assert "READ FROM THE SERVER" not in body, "a failed ask claimed to have read the server"
    assert "drops)" not in body.split("FAILED")[1].split("\n")[0], "a failed ask printed a figure"


def test_a_server_that_omits_the_fee_field_is_a_failed_ask_and_not_a_zero():
    """chains/xrp.py documents base_fee_xrp as OPTIONAL, so this case is real."""
    lines, say = _collect()
    report_asset("XRP", {"fees": [], "booked": [], "unread": []}, say,
                 adapter=_Server(drops=None))
    body = "\n".join(lines)
    assert "FAILED" in body and "without a fee figure" in body, body


def test_SOL_IS_ASKED_NOW_AND_NEVER_ANSWERS_FROM_THE_REFERENCE_CONSTANT():
    """SOL's fee comes from getFeeForMessage, and a failed ask yields no number.

    THIS TEST WAS test_SOL_is_NOT_asked_and_the_reason_is_in_the_code_rather_than_a_gap
    AND IT PASSED FOR A CHANGED REASON, which is why it is rewritten rather than
    left. It asserted `ask_base_fee("SOL", ...) is None` and went on passing after
    chains/solana_fee_quote.py was wired in -- because the stub it was handed has no
    latest_blockhash(), so the ask bailed before reaching the cluster. Green for a
    reason that had nothing to do with what it claimed, and its docstring said SOL
    "is NOT asked", which was then a wrong comment (rule 16).

    WHAT IS ACTUALLY TRUE NOW, and each half is asserted:

      an adapter that cannot be asked   bare None -- no blockhash getter means no
                                        message to price, and that is not a failure
                                        to report, it is a chain this tool cannot
                                        reach
      a cluster that refuses            (None, reason) -- and the reason must carry
                                        no figure
      never the constant                chains/solana_units.py:513's
                                        SIGNATURE_FEE_LAMPORTS = 5_000 calls ITSELF
                                        a "reference value; getFeeForMessage is the
                                        authority", so answering from it under the
                                        "READ FROM THE SERVER" label would make the
                                        label false

    MUTATION: have _ask_sol() return SIGNATURE_FEE_LAMPORTS when the cluster
    refuses. The last assertion fails, and the label over it would be a lie.
    """
    # No blockhash getter: this adapter cannot be asked at all.
    assert ask_base_fee("SOL", _Server()) is None, (
        "an adapter with no latest_blockhash() was asked anyway, so the ask reached a cluster call it "
        "had no message to make"
    )

    class _Refusing:
        """Has the shape, and the cluster is unreachable."""

        def latest_blockhash(self):
            raise RuntimeError("the cluster is unreachable")

    answer = ask_base_fee("SOL", _Refusing())
    assert answer is not None, "a refusing cluster must be REPORTED, not treated as unaskable"
    figure, reason = answer
    assert figure is None, f"a refused ask produced a figure: {figure!r}"
    assert reason, "a refused ask gave no reason, so the operator reads a blank"
    assert "5000" not in reason.replace(",", "") and "0.000005" not in reason, (
        f"the refusal carries the reference constant, which is the one outcome worse than no figure -- "
        f"a number that looks measured: {reason!r}"
    )

    lines, say = _collect()
    report_asset("SOL", {"fees": [], "booked": [], "unread": []}, say, adapter=_Refusing())
    body = "\n".join(lines)
    assert "READ FROM THE SERVER" not in body, "a failed SOL ask claimed to have read the cluster"
    assert "FAILED" in body, body


def test_a_chain_with_payouts_is_never_asked_because_it_was_MEASURED():
    """GRC has real fees, so the server's current quote is not the figure that matters.

    What a payout COST is a fact; what a chain WOULD charge now is a different one,
    and printing the second beside seven readings of the first would invite the
    reader to average them.
    """
    lines, say = _collect()
    report_asset("GRC", {"fees": [0.001] * 7, "booked": [0.01] * 7, "unread": []}, say,
                 adapter=_Server())
    body = "\n".join(lines)
    assert "booked/measured: 10.00x" in body, body
    assert "READ FROM THE SERVER" not in body, "a measured chain was also asked for a current quote"


# --- HISTORY VS CONFIGURATION ---------------------------------------------------
#
# The operator set GRC_NETWORK_FEE_RESERVE to the measured 0.001 and this tool
# printed, on one screen:
#
#     GRC  payouts=7  GRC_NETWORK_FEE_RESERVE=0.001
#       measured fee: mean 0.00100000 ...
#       booked/measured: 10.00x
#
# Both figures were correct and together they read as "you changed it and nothing
# happened". `booked` is swaps.network_fee_reserve, stamped on each swap AT QUOTE
# TIME, and all seven were quoted while the config said 0.01. A config change
# cannot reach into a completed swap, and the output must not look like it should
# have.


def test_the_HISTORICAL_ratio_and_the_CONFIGURED_one_are_both_reported_and_labeled():
    """Seven swaps booked 0.01; the config now says 0.001. Both facts, each labeled.

    MUTATION: delete the configured/measured line. The output goes back to a single
    10.00x beside a setting reading 0.001, which is the screen that reads as a
    change that did not take.
    """
    lines, say = _collect()
    # Exactly the operator's host on 2026-10-03: seven payouts that each booked 0.01
    # and each cost 0.001, against a config that now says 0.001.
    report_asset("GRC", {"fees": [0.001] * 7, "booked": [0.01] * 7, "unread": []}, say)
    body = "\n".join(lines)

    assert "booked at quote time: mean 0.01000000 over 7 payouts" in body, body
    assert "booked/measured: 10.00x" in body, "the historical ratio must still be reported"
    assert "HISTORICAL" in body, (
        "the 10.00x is a statement about seven completed payouts and must say so, or it reads as a "
        "live disagreement with the setting printed three lines above it"
    )
    assert "Changing the setting cannot alter this" in body, (
        "nothing told the reader why the ratio did not move when they changed the setting"
    )
    # And the figure a change DOES move, against the real Config value.
    expected = float(Config.GRC_NETWORK_FEE_RESERVE) / 0.001
    assert f"configured/measured: {expected:.2f}x" in body, body
    assert "WHAT THE NEXT SWAP WILL BOOK" in body, (
        "the configured ratio is the only one an operator can act on and must be named as such"
    )


def test_the_configured_figure_is_marked_as_NOW_on_the_header_line():
    """`SETTING=0.001` alone invites the reader to compare it with a historical ratio."""
    lines, say = _collect()
    report_asset("GRC", {"fees": [0.001], "booked": [0.01], "unread": []}, say)
    assert "CONFIGURED NOW" in lines[0], lines[0]


def test_an_asset_with_no_configured_reserve_reports_no_configured_ratio():
    """SOL has no reserve, so there is no configured figure to divide by.

    A 0.00x or a crash here would both be worse than the line's absence -- and the
    header still says the reserve is absent and what that costs, which is the fact
    that reader needs.
    """
    absent = _assets_without_a_reserve()
    if not absent:
        pytest.skip("every asset has a reserve now, so there is no absent one to report")
    lines, say = _collect()
    report_asset(absent[0], {"fees": [0.000005], "booked": [0.0], "unread": []}, say)
    body = "\n".join(lines)
    assert "configured/measured" not in body, "a configured ratio was reported for an absent reserve"
    assert "REFUSES" in body, "the absence's consequence left the screen"


# --- the forward-looking fee, which is the one that answers "what do I set it to"


class FeeMeasuringAdapter:
    """An adapter that answers measure_send_fee and records what it was asked."""

    def __init__(self, fee=0.0000282, how="measured", valid=True):
        self._fee, self._how, self._valid = fee, how, valid
        self.asked = []

    def validate_address(self, address):
        return self._valid

    def measure_send_fee(self, address, amount):
        self.asked.append((address, amount))
        if self._fee is None:
            return None, self._how
        return self._fee, self._how

    def owns_address(self, address):
        """Not the desk's -- these fixtures all supply a CUSTOMER payout address.

        Added 2026-10-04 with the ownership gate in services/swap_service.
        refuse_unusable_payout_address(). A destination stub without it raises
        AttributeError, so the gate could not be exercised and every create_swap
        test failed on the harness instead of on its own subject. False is the
        honest default here; a test that wants the refusal returns True.
        """
        return False


def test_the_live_fee_line_compares_the_measurement_against_the_reserve(monkeypatch):
    """"we need to match the scaling here" -- the operator, 2026-10-03.

    Two fees read off their node that day were wrong against the configured
    reserves in OPPOSITE directions:

        BTC   configured 0.00002   measured 0.00002820   41% too low
        LTC   configured 0.001     measured 0.00021483   4.7x too high

    and the transactions say why no constant fits both: the BTC send spent ONE
    input, the LTC send FIFTEEN, because that wallet holds many small mining
    outputs. Bytes times a rate, and bytes scale with input count.
    """
    monkeypatch.setattr(show_payout_fees.Config, "BTC_NETWORK_FEE_RESERVE", 0.00002, raising=False)
    adapter = FeeMeasuringAdapter(fee=0.0000282)

    line = show_payout_fees.live_fee_line("BTC", adapter, "an-address", 0.001)

    assert "0.0000282" in line
    assert "0.00002" in line, "the configured figure has to be beside the measured one"
    assert "1.41x" in line, "and the ratio, so nobody has to divide"
    assert "BELOW the measured fee" in line, (
        "the DIRECTION is the part that costs money: a reserve under the real fee under-protects both "
        "the fee floor and the funding gate"
    )


def test_a_reserve_above_the_measured_fee_says_that_is_the_cheaper_error(monkeypatch):
    """Both directions, and they are not equally bad.

    LTC's reserve is 4.7x its measured fee, which only makes the fee floor
    stricter than it needs to be -- that is why GRC->BTC demands 12,232 GRC. A
    line that called both "wrong" without saying which way would leave the
    operator to work out which one to fix first.
    """
    monkeypatch.setattr(show_payout_fees.Config, "LTC_NETWORK_FEE_RESERVE", 0.001, raising=False)

    line = show_payout_fees.live_fee_line("LTC", FeeMeasuringAdapter(fee=0.00021483), "an-address", 1.2)

    assert "0.21x" in line
    assert "above the measured fee" in line
    assert "stricter" in line


def test_a_fee_that_cannot_be_measured_says_NOT_ESTABLISHED_and_why():
    """Never a fabricated number: a reserve chosen from a made-up figure is the defect.

    This is the GRC case until somebody checks it. The evidence says Gridcoin is
    FLAT rather than scaled -- 7 of 7 payouts at exactly 0.001 with zero variance,
    and the one transaction read in full shows vin.size=1 with a 0.001 fee -- but
    nobody has sent a GRC payout spending fifteen inputs, so "flat" is a reading
    and not a measurement (rule 17). If Gridcoin's daemon has no
    fundrawtransaction, this is the line the operator sees.
    """
    adapter = FeeMeasuringAdapter(fee=None, how="fundrawtransaction refused (RPCError: Method not found)")

    line = show_payout_fees.live_fee_line("GRC", adapter, "an-address", 100.0)

    assert "NOT ESTABLISHED" in line
    assert "Method not found" in line


def test_an_address_for_another_chain_is_NOT_ASKED_rather_than_measured():
    """One --fee-address is offered to every configured chain, and only one accepts it."""
    adapter = FeeMeasuringAdapter(valid=False)

    line = show_payout_fees.live_fee_line("LTC", adapter, "a-btc-address", 0.001)

    assert "NOT ASKED" in line
    assert adapter.asked == [], "a wrong-chain address must not reach the measurement"


def test_without_the_flags_the_line_says_what_to_pass_and_why_both_are_needed():
    """Rule 14: a line that just goes missing is indistinguishable from one that broke."""
    line = show_payout_fees.live_fee_line("BTC", FeeMeasuringAdapter(), "", 0.0)

    assert "NOT ASKED" in line
    assert "--fee-address" in line and "--fee-amount" in line
    assert "inputs" in line, "and why the amount matters, or it reads as busywork"
