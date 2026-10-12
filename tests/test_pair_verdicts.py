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
  the exit code  a verdict and a CRASH must not read alike. The operator ran this with
                 the system interpreter on 2026-10-12, it died at import on a missing
                 `bech32`, CPython exited 1, and swap_stack printed "at least one allowed
                 pair cannot be quoted" about thirty pairs nothing had evaluated. The
                 verdict codes start at 3 for that reason.
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
from stack_authority import PAIR_EXIT_CODES, pair_exit_code

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


def test_the_report_exits_on_the_verdict_and_not_on_having_printed(tmp_path, capsys):
    """Rule 13: "skipped" plus "success" in one output is a defect in the output.

    Run for real, with adapters that cannot be built (no chain is configured in this
    process), so every pair refuses on `missing` and nothing opens a socket. A report
    that printed thirty refusals and exited 0 could not be read by a script, which is
    the whole reason `swap_stack.py pairs` can report a verdict at all.
    """
    code = verdicts.main(["--db", _db(tmp_path)])
    printed = capsys.readouterr().out

    assert code == pair_exit_code("some_unavailable"), (
        "pairs are unavailable, so the exit code is the VERDICT code and not 1 -- 1 is "
        "what CPython exits on an uncaught exception, and the two read alike on the "
        "operator's screen until 2026-10-12 (stack_authority.PAIR_EXIT_CODES)"
    )
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

    assert verdicts.main(["--db", _db(tmp_path)]) == pair_exit_code("no_pairs")
    assert "ALLOWED_PAIRS is empty" in capsys.readouterr().out


def test_the_verdict_codes_leave_1_and_2_to_cpython_and_argparse():
    """A crash must not be reportable as a verdict, and 1 is what a crash exits with.

    THE MEASUREMENT IS THE OPERATOR'S OWN RUN. `swap_stack.py pairs` under the system
    interpreter: pair_verdicts.py died at IMPORT (`No module named 'bech32'`), before
    main() could run, so nothing inside this file could catch it; CPython exited 1; and
    cmd_pairs() printed "exit 1  at least one allowed pair cannot be quoted -- the
    `blocked by` block above says which condition". There was no block.

    So no verdict may use 1 (CPython's uncaught-exception code) or 2 (argparse's). That
    is a property of the SET, which is why it is asserted over the set rather than over
    the three call sites: a fourth verdict added at 1 would pass three per-code tests.
    """
    assert 1 not in PAIR_EXIT_CODES.values(), (
        "1 is CPython's exit code for an uncaught exception, so a verdict using it is "
        "indistinguishable from the report having crashed"
    )
    assert 2 not in PAIR_EXIT_CODES.values(), "2 is argparse's code for a bad flag"
    assert pair_exit_code("all_quotable") == 0, "success is still 0, as every tool's is"
    assert len(set(PAIR_EXIT_CODES.values())) == len(PAIR_EXIT_CODES), (
        "two verdicts sharing a code cannot be told apart by a script"
    )
