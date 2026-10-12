#!/usr/bin/env python3
"""The report that answers "nothing works in the UI" with a condition and a count.

Role: tests (read-only)
Reads: pair_verdicts.py through conftest.root_entry_point(), and a database it
       creates under tmp_path. No socket: adapters are {} throughout, so every
       verdict short-circuits on `missing` and the one condition that touches a
       network is never reached.
Writes: files under tmp_path only
Can move funds: no
Mainnet-safe: yes

WHAT THE TOOL IS FOR, because it decides what is worth asserting here.

The operator's screen on 2026-10-11 read NONE / 0/5 out on all six coins. That is
thirty refusals rendered as one fact, on a surface built to withhold the
operator-facing remedy (services/wizard.unavailable_note(), which exists because
screen 2 was printing "SOL_RPC_URL is unset ..." to an unauthenticated reader). So
the question "is this ONE cause or thirty" had no answer anywhere, and the six
answers it could have are six different places to go.

THE TWO THINGS THAT MUST NOT DRIFT, and they are what this file pins:

  the coverage   pair_verdicts.WHAT_TO_DO is keyed by services/pair_view.REFUSAL_KEYS
                 and is read with [] rather than .get(), so a seventh condition is a
                 KeyError at the moment of printing -- an operator's report, mid-run.
                 Asserting the coverage here turns that into a red test instead.
                 This is the same gap that let `unreachable` ship unnamed through
                 open_swap.blocked_by(); see tests/test_pair_refusal_causes.py.
  the partition  pair_serviceability() short-circuits, so AT MOST ONE condition is
                 non-empty per row and the six counts partition the unavailable
                 pairs. That is what makes them addable, and a reader adding them is
                 the point of printing them. If a future change ever evaluated two
                 conditions on one row, the counts would sum past the total and quietly
                 stop being a partition -- numbers that look fine and do not add up.

WHY THE END-TO-END RUN USES EMPTY ADAPTERS. Provoking all six conditions for real
needs six whole postures, four of which already have a file that builds them
(tests/test_xrp_pair_reasons.py, tests/test_allowed_pairs_are_serviceable.py,
tests/test_chain_reachability.py, tests/test_an_available_pair_can_actually_be_quoted.py).
What is NOT covered anywhere is whether the REPORT is readable and exits on the
verdict rather than on having printed, and an empty adapters dict is enough to
measure both while guaranteeing no socket opens.
"""

from __future__ import annotations

import sqlite3

from config import Config
from conftest import root_entry_point
from db import init_db
from services.pair_view import REFUSAL_KEYS

verdicts = root_entry_point("pair_verdicts.py", "pair_verdicts_entry")


def _db(tmp_path, rows=()):
    """A database carrying exactly the given chain_reachability rows.

    THE REAL SCHEMA, applied by db.init_db() the way every other tool gets it, rather
    than a hand-written CREATE TABLE here. A test that invents the table measures its
    own invention: the column set is what reachability_rows() reads with `SELECT *`,
    so a column renamed in db.SCHEMA and not here would pass this file and fail in
    production (BEHAVIORAL_VERIFICATION_PRINCIPLE -- run the real thing).
    """
    path = tmp_path / "swap_terminal.db"
    init_db(str(path))
    conn = sqlite3.connect(str(path))
    with conn:
        for asset, reachable, asked, detail, checked_at in rows:
            conn.execute(
                "INSERT INTO chain_reachability (asset, reachable, asked, detail, checked_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (asset, reachable, asked, detail, checked_at),
            )
    conn.close()
    return str(path)


def test_every_condition_has_a_sentence_saying_what_to_do_about_it():
    """THE GATE. WHAT_TO_DO is read with [], so a gap is a KeyError mid-report.

    Exhaustive in both directions: a condition with no clause would crash the report,
    and a clause for a condition that no longer exists is dead prose that reads as a
    cause an operator can go and act on.
    """
    assert set(verdicts.WHAT_TO_DO) == set(REFUSAL_KEYS), (
        "pair_verdicts.WHAT_TO_DO must name every condition in "
        "services/pair_view.REFUSAL_KEYS and no others. It is indexed with [] while "
        "printing a report, so a missing key raises at the operator rather than here."
    )


def test_the_counts_partition_the_unavailable_pairs_rather_than_overlapping():
    """Six counts that ADD UP to the number of refused pairs, because one condition wins.

    Seeded rows with exactly one refusal each, which is the shape
    pair_serviceability() produces: it short-circuits, so a row carrying two refusals
    cannot come out of it. If that ever changes, these numbers stop being addable and
    the report's central block becomes six figures that look fine and sum past the
    denominator.
    """
    rows = [
        {**dict.fromkeys(REFUSAL_KEYS, ""), "missing": [], "serviceable": False, "unreachable": "LTC is down"},
        {**dict.fromkeys(REFUSAL_KEYS, ""), "missing": [], "serviceable": False, "unreachable": "GRC is down"},
        {**dict.fromkeys(REFUSAL_KEYS, ""), "missing": ["BTC"], "serviceable": False},
        {**dict.fromkeys(REFUSAL_KEYS, ""), "missing": [], "serviceable": True},
    ]
    counts = dict(verdicts.condition_counts(rows))

    assert counts["unreachable"] == 2
    assert counts["missing"] == 1
    assert sum(counts.values()) == 3, "three rows refuse, so the six counts must sum to three"
    assert [key for key, _ in verdicts.condition_counts(rows)] == list(REFUSAL_KEYS), (
        "evaluation order, so a reader sees the configuration causes before the network ones"
    )
    assert len(counts) == len(REFUSAL_KEYS), "every condition is listed, zeroes included (rule 14)"


def test_a_database_that_cannot_be_opened_is_NOT_CONSIDERED_and_not_an_outage(tmp_path):
    """The gate's own failure must not become a refusal.

    An unreadable table means reachability was not considered -- which is what
    `reachability=None` means to pair_serviceability() -- and emphatically not "every
    chain is down". Returning {} is what makes every pair get judged on configuration
    alone instead of all thirty being blamed on a missing file.
    """
    rows, headline, detail = verdicts.reachability_or_empty(str(tmp_path / "nope.db"))

    assert rows == {}
    assert headline.startswith("NOT CONSIDERED")
    # db.connect_db()'s whole remedy survives into the detail rather than being
    # truncated into the headline: a mistyped SWAP_DB_PATH is exactly what it is for.
    assert "SWAP_DB" in detail


def test_an_empty_table_says_no_worker_has_run_rather_than_printing_nothing(tmp_path):
    """Rule 14: `(none)` is a result, and a blank gap is ambiguous between two causes.

    An empty chain_reachability table and an unopenable database are different
    problems with different fixes, so they do not share a sentence.
    """
    rows, headline, detail = verdicts.reachability_or_empty(_db(tmp_path))

    assert rows == {}
    assert "no chain_reachability rows yet" in headline
    assert "payout and reconcile workers" in detail


def test_a_recorded_reading_is_reported_with_its_age_and_what_it_said(tmp_path):
    """Age is printed because STALENESS IS NOT A REFUSAL.

    reachability_refusal() goes silent on a row older than STALE_AFTER_SECONDS, so a
    chain that is genuinely down but whose reading has aged out appears as a cause
    NOWHERE in the report. A reader who cannot see the age cannot notice that.
    """
    path = _db(tmp_path, rows=[
        ("LTC", 0, "getblockchaininfo", "Connection refused", "2026-10-12T00:00:00+00:00"),
        ("SOL", 1, "getVersion", "", "2026-10-12T00:00:01+00:00"),
    ])
    rows, headline, detail = verdicts.reachability_or_empty(path)

    assert set(rows) == {"LTC", "SOL"}
    assert "2 chain(s) recorded" in headline
    assert detail == ""

    lines = verdicts.reachability_lines(rows)
    assert any("LTC: did NOT answer" in line and "Connection refused" in line for line in lines)
    assert any("SOL: ANSWERED" in line for line in lines)
    assert all("ago" in line for line in lines), "every row states its age (rule 14)"


def test_an_unreadable_timestamp_says_so_rather_than_reading_as_fresh(tmp_path):
    """A row of unknown age refuses nothing, and the line says that.

    row_age_seconds() returns None rather than 0.0 precisely so a corrupt timestamp
    cannot look like the freshest possible reading; this is the half of that decision
    the operator can see.
    """
    rows, _, _ = verdicts.reachability_or_empty(
        _db(tmp_path, rows=[("GRC", 0, "getblockchaininfo", "", "not a timestamp")])
    )

    line, = verdicts.reachability_lines(rows)
    assert "age UNKNOWN" in line
    assert "refuses nothing" in line


def test_the_report_exits_on_the_verdict_and_not_on_having_printed(tmp_path, capsys):
    """Rule 13: "skipped" plus "success" in one output is a defect in the output.

    Run for real, with adapters that cannot be built (no chain is configured in this
    process), so every pair refuses on `missing` and nothing opens a socket. A report
    that printed thirty refusals and exited 0 could not be read by a script, which is
    the whole reason `swap_stack.py pairs` can report a verdict at all.
    """
    code = verdicts.main(["--db", _db(tmp_path)])
    printed = capsys.readouterr().out

    assert code == 1, "pairs are unavailable, so the exit code is a failure"
    assert "allowed pairs can be quoted right now" in printed
    assert "blocked by" in printed
    # THE DENOMINATOR BESIDE THE NUMERATOR (rule 3), for every condition including the
    # zeroes -- five zeroes and one large number is the entire answer to "nothing
    # works in the UI", and the zeroes are what make the one number trustworthy.
    for key in REFUSAL_KEYS:
        assert f"{key:<18}" in printed, f"{key} has no count line"
    assert " of 30" in printed, "counts are printed out of the number of pairs evaluated"


def test_the_banner_names_a_network_for_every_swappable_chain(tmp_path, capsys):
    """Rule 14: "always print the network", and a line that omits one gets read wrong.

    ICP IS THE ONE THIS CAUGHT. network_target.startup_lines() iterated the literal
    ("SOL", "XRP") beside a table of three, so ICP -- ten of thirty allowed directions
    -- had no line in any banner this tree printed. A first draft of pair_verdicts.py
    printed `Config.NETWORK` instead, which does not exist: it would have read
    "(not stated)" forever, which is the wrong-comment bug (rule 16) in a field whose
    whole job is to say mainnet or testnet.
    """
    verdicts.main(["--db", _db(tmp_path)])
    printed = capsys.readouterr().out

    for asset in sorted({leg for pair in Config.ALLOWED_PAIRS for leg in pair}):
        assert f"\n{' ' * 18}{asset:4}" in printed or f"{asset:4} NOT CONFIGURED" in printed, (
            f"{asset} is a leg of Config.ALLOWED_PAIRS and the report names no network for it"
        )


def test_no_pair_at_all_is_a_failure_rather_than_a_clean_run(tmp_path, capsys, monkeypatch):
    """Six dark lamps over an empty ALLOWED_PAIRS is CORRECT, and still not success.

    The distinction matters because it is the one reading where the page is not lying:
    nothing is offered because nothing is allowed. An exit code of 0 there would tell
    a script the desk is fine.
    """
    monkeypatch.setattr(verdicts, "allowed_pair_rows", lambda *args, **kwargs: [])

    assert verdicts.main(["--db", _db(tmp_path)]) == 1
    assert "ALLOWED_PAIRS is empty" in capsys.readouterr().out
