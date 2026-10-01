"""show_swap.py: what a halted swap looks like from a terminal, and what it never does.

Role: test (seeded temporary databases; opens no socket and builds no adapter)
Reads: show_swap.py, and through it services/admin_view.py,
       services/swap_view.py and services/swap_service.py; plus
       workers/deposit_watcher.py's note, which is the thing that points at the
       tool
Writes: a temporary database per test, seeded by these tests. The tool under
       test writes nothing, and two tests here prove that rather than assume it.
Can move funds: no
Mainnet-safe: yes

WHY THE ASSERTIONS ARE ON REAL ROWS AND REAL STDOUT.

The defect this tool was written for was not a wrong number -- it was an
instrument that reported a problem and then handed over a SQL fragment to an
operator with no way to run it. That failure is only visible in what reaches the
screen, so every test here seeds real rows into the real schema, runs the real
entry point, and asserts on what was printed. A test that called halted_swaps()
and checked a dict would pass while the tool printed nothing at all.

THE TWO PROPERTIES THAT MATTER MOST, AND THEY ARE BOTH NEGATIVE.

  the note must name something RUNNABLE   -- a command, with the id already in
      it, naming a file that exists. test_the_named_tool_exists_where_the_note_
      says_it_is is deliberately a filesystem check: a rename of show_swap.py is
      exactly the kind of break no import graph sees (rule 2), because the only
      thing that references it is a string in a print.
  the tool must change NOTHING            -- test_the_tool_changes_not_one_byte_
      of_the_database hashes the file before and after, and
      test_it_refuses_a_missing_database_and_does_not_create_one checks that a
      read-only tool did not leave an empty database behind by way of
      sqlite3.connect().
"""

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

# Both path inserts have to run first: the tool is at the repository root, which
# conftest.py does not put on sys.path, and the tool's own imports are rootless
# out of swap_terminal/.
from config import Config
from db import SCHEMA, dict_factory
from report_block import LABEL_WIDTH
from services.admin_view import halted_swaps, swaps_in_flight
from services.swap_view import HALTED_STATUSES
from workers.common import (
    STANDING_COUNTS,
    get_config_dict,
    root_tool_command,
)
from workers.deposit_watcher import HALTED_REVIEW_COMMAND, halted_note

import show_swap
from show_swap import main

NOW = "2026-09-26T12:00:00+00:00"

# A real XRP classic address and a Gridcoin-shaped payout address. Nothing here
# validates either -- no test in this file constructs an adapter -- so they only
# have to be the shape an operator would read on screen.
XRP_ACCOUNT = "rN7n7otQDd6FczFgLdSqtcsAUxDkw6fzRH"
GRC_ADDRESS = "S67nL5CVnLmHrGJPZM4Zp1Kk1234567890"
TXID = "E3FE6EA3D48F0C2B639448020EA4F03D4F4F8FFDB243A852A0F59177921B4879"


def seed_db(path: Path) -> sqlite3.Connection:
    """A real database on the real schema. No fixture fakes a cursor here."""
    conn = sqlite3.connect(path)
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def seed_swap(conn, swap_id: str, status: str, **kw) -> None:
    """One quote and one swap, with every column the tool reads spelled out.

    Defaults describe the halt this tool was written for and that the operator
    actually hit on 2026-09-26: a swap expecting 5 XRP that received 1.
    """
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, expires_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"q_{swap_id}", "XRP", "GRC", 5.0, 57.5, 150, 0.01, 283.0, "2026-09-26T11:10:00+00:00",
         "2026-09-26T11:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, deposit_tag, payout_address,"
        " expected_input_amount, actual_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, deposit_txid, payout_txid, created_at, updated_at,"
        " credited_at, completed_at, expires_at, failed_reason)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            swap_id, f"q_{swap_id}", kw.get("from_asset", "XRP"), "GRC", XRP_ACCOUNT, kw.get("tag", 4242),
            GRC_ADDRESS, kw.get("expected", 5.0), kw.get("actual"), 57.5, 150, 0.01, 283.0, status,
            kw.get("min_confirmations", 1), kw.get("deposit_txid"), None, "2026-09-26T11:00:00+00:00",
            kw.get("updated_at", "2026-09-26T11:05:00+00:00"), None, None, "2026-09-26T11:10:00+00:00",
            kw.get("failed_reason"),
        ),
    )
    conn.commit()


def seed_deposit(conn, swap_id: str, amount: float, confirmations: int, vout: int = 0) -> None:
    conn.execute(
        "INSERT INTO deposit_events (swap_id, asset, txid, vout, address, amount, confirmations, first_seen_at,"
        " last_seen_at, credited_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (swap_id, "XRP", TXID, vout, XRP_ACCOUNT, amount, confirmations, "2026-09-26T11:04:00+00:00",
         "2026-09-26T11:05:00+00:00", None),
    )
    conn.commit()


@pytest.fixture
def halted(tmp_path):
    """The operator's actual 2026-09-26 halt: 1 XRP sent to a swap expecting 5."""
    path = tmp_path / "swap_terminal.db"
    conn = seed_db(path)
    seed_swap(
        conn, "s_halt", "under_review", actual=1.0, deposit_txid=TXID,
        failed_reason="Confirmed amount 1.0 outside tolerance for expected 5.0",
    )
    seed_deposit(conn, "s_halt", 1.0, 4)
    conn.close()
    return path


def run_tool(arguments) -> int:
    return main(arguments)


# --- the halt is legible ------------------------------------------------------


def test_a_halted_swap_is_named_with_the_reason_it_halted(halted, capsys):
    """The one question the watcher's count could not answer: which swap, and why.

    MUTATION: drop the `reason` line from halted_swap_block() and this test alone
    fails, naming the missing sentence. Everything else about the block survives
    -- which is the point: the id without the reason is the halt still being
    illegible, just with a name on it.
    """
    assert run_tool(["--db", str(halted)]) == 0
    out = capsys.readouterr().out

    assert "s_halt" in out, "the halted swap's id has to be on screen; it is the thing the count was about"
    assert "Confirmed amount 1.0 outside tolerance for expected 5.0" in out, (
        "swaps.failed_reason is the authority's own sentence about WHY this halted, and it is the only place "
        "the two amounts the gate compared are written down"
    )
    assert "XRP -> GRC" in out
    assert "5.0 XRP" in out, "the expected amount"
    assert "1.0 XRP" in out, "what was actually seen"
    assert "halted swaps: 1" in out, "the count on screen must match the watcher's HALTED_for_review"


def test_the_seen_total_is_distinguished_from_the_figure_the_gate_compared(halted, capsys):
    """SEEN and CONFIRMED are two different sums, and a reader not told assumes one.

    services/deposit_service.refresh_swap_from_chain() writes actual_input_amount
    from seen_total -- every deposit row -- and then compares confirmed_total,
    which counts only rows at or past min_confirmations. They are equal often
    enough that the difference is invisible until the one time it is not, and
    then the halt reads as arithmetic that does not add up.

    MUTATION: delete the "the gate compared the CONFIRMED total" clause and this
    test fails while the block still prints both numbers -- which is exactly the
    state that misleads.
    """
    run_tool(["--db", str(halted)])
    out = capsys.readouterr().out
    assert "swaps.actual_input_amount is the SEEN total" in out, "the printed figure must be named as the SEEN sum"
    assert "the gate compared the CONFIRMED total" in out, (
        "and the OTHER sum must be named as the one that decided, or the reader is left to assume they are one "
        "number. Asserting on the whole clause rather than on the word CONFIRMED: that word also appears in the "
        "header's tolerance line, so a looser assertion passed with this sentence deleted -- measured by "
        "deleting it (mutation M14, 2026-09-26)"
    )


def test_a_halt_with_no_recorded_reason_says_so_rather_than_printing_a_blank(tmp_path, capsys):
    """(none) is a result; a blank gap is ambiguous between "no reason" and "broken".

    A swap can sit in under_review with failed_reason NULL --
    migrate_deposit_vouts.py's own notice describes a swap keeping a STALE
    reason, and a status set by any path other than the tolerance check leaves
    the column empty. The report must not render that as an empty line.

    MUTATION: replace the `or (...)` fallback in halted_swap_block() with
    `row.get("failed_reason")` and this test fails on the blank.
    """
    path = tmp_path / "swap_terminal.db"
    conn = seed_db(path)
    seed_swap(conn, "s_quiet", "under_review", actual=1.0, failed_reason=None)
    conn.close()

    run_tool(["--db", str(path)])
    out = capsys.readouterr().out
    assert "(none) -- swaps.failed_reason is empty" in out
    assert "swap_audit_log" in out, "it must say where the transition is recorded when the reason is not"


def test_no_halted_swap_prints_none_and_says_what_to_check(tmp_path, capsys):
    """An empty result is a result, and here it has a second job.

    If deposit_watcher says HALTED_for_review=1 and this says (none), the two are
    reading different databases -- the one conclusion an operator cannot reach
    from a blank space.

    MUTATION: return [] from halted_lines() when there are no rows and this test
    fails; the run would otherwise print a header, a blank gap and nothing else.
    """
    path = tmp_path / "swap_terminal.db"
    conn = seed_db(path)
    seed_swap(conn, "s_open", "awaiting_deposit")
    conn.close()

    assert run_tool(["--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "halted swaps: (none)" in out
    assert "awaiting_deposit 1" in out, "the status tally answers 'is this even the right database'"
    assert "SWAP_DB_PATH" in out and "different files" in out, (
        "the empty case must name the one explanation for disagreeing with the watcher"
    )


# --- the hand-over is runnable ------------------------------------------------


def test_the_listing_hands_over_a_command_with_the_real_id_and_no_placeholder(halted, capsys):
    """A placeholder in a pasted command has cost this project three mis-runs.

    MUTATION: change detail_command() to return "--swap <id>" and this test fails
    on the literal `<`.
    """
    run_tool(["--db", str(halted)])
    out = capsys.readouterr().out

    command = next(line for line in out.splitlines() if "--swap" in line)
    assert "s_halt" in command, "the real id must already be in the command"
    assert "<" not in command and ">" not in command, f"a placeholder reached a pasteable line: {command!r}"
    assert "python3 /" in command, (
        "the path must be absolute: the watcher prints from cwd=swap_terminal/ and the operator's shell may be "
        "anywhere, so a relative command silently names a file that is not there"
    )
    assert f"{Path(show_swap.__file__).resolve()} --swap s_halt" in command, (
        "the command has to name this tool and this swap, so it runs as pasted"
    )


def test_the_watcher_note_carries_the_command_only_when_something_is_halted():
    """The note grows a command exactly when there is something to act on.

    Rule 14 both ways: a halt must hand over something runnable, and a cycle with
    nothing halted must not print a command every fifteen seconds -- which is how
    an operator learns to skim the tail of the line where the command lives.

    MUTATION: return the same string from both branches of halted_note() and one
    of the two assertions below fails whichever way it is collapsed.
    """
    quiet = halted_note(0)
    loud = halted_note(1)

    assert "waiting on a PERSON" in quiet, "the field's meaning is explained on every cycle"
    assert "python3" not in quiet, "a cycle with nothing halted must not print a command"
    assert HALTED_REVIEW_COMMAND in loud, "a halted cycle must end with the command that shows the halt"
    assert loud.rstrip().endswith(HALTED_REVIEW_COMMAND), (
        "the command goes LAST so it survives a line that gets truncated or skimmed"
    )
    assert "1 is waiting now" in loud, "the count travels with the command (rule 14)"
    assert "WHERE status=" not in loud, (
        "the note used to end with a SQL fragment, which is what this change replaced -- an operator in a shell "
        "has nothing to run it in"
    )


def test_the_named_tool_exists_where_the_note_says_it_is():
    """A command naming a file that is not there is worse than no command.

    Deliberately a filesystem check. The only thing referencing show_swap.py from
    the watcher is a STRING in a print, so a rename breaks it in a way no import
    graph and no linter can see -- rule 2's "grep for the NAME, not the import
    graph", as a test.

    MUTATION: pass "show_swaps.py" to root_tool_command() in deposit_watcher.py
    and this test fails naming the missing path.
    """
    named = HALTED_REVIEW_COMMAND.split()[-1]
    assert Path(named).is_file(), f"the deposit watcher's note names {named}, which does not exist"
    assert named == str(Path(show_swap.__file__).resolve()), (
        "the note must name THIS tool, not another file that happens to exist"
    )
    assert root_tool_command("show_swap.py") == HALTED_REVIEW_COMMAND


def test_the_watcher_counts_exactly_the_statuses_this_tool_lists():
    """The count and the listing must never be able to disagree.

    A count of 2 beside a list of 1 would send an operator looking for a swap
    this tool cannot show. The watcher's SQL uses a literal (an `IN (?)` built
    from a tuple's length would need SQL assembled by interpolation, and rule 19
    forbids adding the suppression that comes with it), so the agreement is
    pinned here instead.

    MUTATION: add a status to STATUS_MEANINGS with kind "halted" and this fails
    until the watcher's query is widened to match.
    """
    assert HALTED_STATUSES, "an empty tuple would make admin_view's `IN ()` a syntax error"
    assert HALTED_STATUSES == ("under_review",)

    source = Path(sys.modules["workers.deposit_watcher"].__file__).read_text()
    assert "status = 'under_review'" in source, "the watcher must count the status this tool lists"

    # The count's NAME is spelled in two files -- the watcher's counts dict and
    # workers/common.STANDING_COUNTS, which is what keeps a standing halt from
    # making every idle cycle print WORKED. Two spellings of one key drift
    # silently: the field would simply start claiming work again, and nothing
    # would fail. This is the pin.
    assert '"HALTED_for_review": halted' in source, "the watcher's count key changed spelling"
    assert "HALTED_for_review" in STANDING_COUNTS, (
        "the count the watcher prints is no longer the one common.py excludes from the IDLE/WORKED verdict, so a "
        "standing halt will report as work on every cycle again"
    )


# --- it writes nothing --------------------------------------------------------


def test_the_tool_changes_not_one_byte_of_the_database(halted, capsys):
    """Proven by hashing the file, not by reading the source for INSERTs.

    Reading the file and seeing only SELECTs proves nothing about a future edit,
    and a read-only claim in a header is worth exactly what a test makes it
    worth.

    MUTATION: add any UPDATE to read_report() and this fails on the hash.
    """
    before = hashlib.sha256(halted.read_bytes()).hexdigest()
    run_tool(["--db", str(halted)])
    run_tool(["--db", str(halted), "--swap", "s_halt"])
    capsys.readouterr()

    assert hashlib.sha256(halted.read_bytes()).hexdigest() == before, "the database changed"
    siblings = sorted(p.name for p in halted.parent.iterdir())
    assert siblings == ["swap_terminal.db"], f"reading left something behind: {siblings}"


def test_it_refuses_a_missing_database_and_does_not_create_one(tmp_path, capsys):
    """sqlite3.connect() CREATES the file, so a read-only tool must not reach it.

    MUTATION: delete the Path.exists() check in read_report() and this fails --
    not on the exit code, which stays 2 either way, but on the empty database
    left in the directory.
    """
    path = tmp_path / "never_created.db"
    assert run_tool(["--db", str(path)]) == 2
    captured = capsys.readouterr()

    assert not path.exists(), "a tool that says it writes nothing created a database"
    assert "there is no database at" in captured.err
    assert "Nothing was written, including that file." in captured.err


def test_an_uninitialized_database_is_refused_rather_than_read_as_no_halts(tmp_path, capsys):
    """"No such table" must never render as "no swap is halted".

    They are the same shape on screen and they mean opposite things: one is a
    healthy quiet system, the other is a database nothing has ever initialized.

    MUTATION: catch sqlite3.OperationalError and return [] and this test fails,
    because the run would print `halted swaps: (none)` for a file with no schema.
    """
    path = tmp_path / "empty_file.db"
    path.touch()

    assert run_tool(["--db", str(path)]) == 2
    captured = capsys.readouterr()
    assert "could not be queried" in captured.err
    assert "no such table" in captured.err
    assert "(none)" not in captured.out, "an unreadable database must not print the same answer as an empty one"


def test_it_refuses_an_unknown_swap_id_and_says_how_to_list(halted, capsys):
    assert run_tool(["--db", str(halted), "--swap", "s_not_a_swap"]) == 2
    captured = capsys.readouterr()
    assert "no swap with id s_not_a_swap" in captured.err
    assert "with no --swap" in captured.err, "a refusal has to name the run that would have worked"


def test_nothing_it_prints_is_a_command_that_writes(halted, capsys):
    """Rule 16, as a test over the actual output.

    The tempting ending for a report like this is a ready-made
    `UPDATE swaps SET status=...` for the operator to paste. It would be pasted
    at the exact moment somebody is tired and staring at a stuck swap, and it
    moves money in a direction this tool has no way to know is right.

    MUTATION: add such a line to resolution_lines() and this test fails.
    """
    run_tool(["--db", str(halted)])
    run_tool(["--db", str(halted), "--swap", "s_halt"])
    out = capsys.readouterr().out

    for verb in ("UPDATE ", "DELETE ", "INSERT "):
        assert verb not in out, f"the report printed something that writes: {verb.strip()}"
    assert "--resolve" not in out.replace("There is no --resolve", ""), "no resolve flag may be advertised"


# --- it reuses the existing view logic ----------------------------------------


def test_the_halted_listing_and_the_in_flight_table_are_one_query(tmp_path):
    """Rule 8 behaviorally: the same SELECT, two status sets, identical columns.

    halted_swaps() and swaps_in_flight() both call swaps_with_status(). If one of
    them is ever given its own SELECT, the columns will differ the first time
    somebody adds one to the other -- and the failure is silent, because the
    report just stops mentioning a field.

    MUTATION: give halted_swaps() its own SELECT without `deposit_rows` and this
    test fails on the key sets.
    """
    path = tmp_path / "swap_terminal.db"
    conn = seed_db(path)
    seed_swap(conn, "s_moving", "confirming")
    seed_swap(conn, "s_stopped", "under_review", failed_reason="outside tolerance")

    moving = swaps_in_flight(conn, NOW)
    stopped = halted_swaps(conn, NOW)
    conn.close()

    assert [row["id"] for row in moving] == ["s_moving"]
    assert [row["id"] for row in stopped] == ["s_stopped"]
    assert set(moving[0]) == set(stopped[0]), "the two callers must be reading the same columns"
    assert stopped[0]["attention"]["level"] == "halted", (
        "the verdict comes from services/swap_view.attention(), the same function the customer's page uses"
    )


def test_the_detail_view_marks_which_deposit_rows_the_gate_counted(tmp_path, capsys):
    """Per row, because the distinction is what makes a halt make sense.

    MUTATION: drop the confirmations comparison in deposit_event_lines() and this
    test fails: both rows would read the same way, and a reader could not see
    that only one of them is in the figure the gate compared.
    """
    path = tmp_path / "swap_terminal.db"
    conn = seed_db(path)
    seed_swap(conn, "s_two", "under_review", actual=2.0, min_confirmations=3,
              failed_reason="Confirmed amount 1.0 outside tolerance for expected 5.0")
    seed_deposit(conn, "s_two", 1.0, 5, vout=0)
    seed_deposit(conn, "s_two", 1.0, 1, vout=1)
    conn.close()

    run_tool(["--db", str(path), "--swap", "s_two"])
    out = capsys.readouterr().out

    assert "COUNTED by the gate" in out
    assert "NOT counted -- below min_confirmations=3" in out
    assert "deposit rows    2" in out, "the section header carries its own count rather than a blank"


def test_an_unrecognized_status_is_reported_as_unrecognized(tmp_path, capsys):
    """A status this display does not know must be named, not quietly styled as one.

    services/swap_view.status_meaning() returns `known: False` for it and puts the
    raw value in the headline. The terminal has to carry that through: a status
    rendered as nothing in particular means the code that writes statuses and the
    code that reads them have gone out of step, and that is a fact about the
    system rather than about the swap.

    MUTATION: drop the `if not view["status_known"]` branch from swap_lines() and
    this test fails -- and it is the branch that also pins the label width for a
    label no other test prints.
    """
    path = tmp_path / "swap_terminal.db"
    conn = seed_db(path)
    seed_swap(conn, "s_odd", "reticulating_splines")
    conn.close()

    assert run_tool(["--db", str(path), "--swap", "s_odd"]) == 0
    out = capsys.readouterr().out

    assert "reticulating_splines" in out, "the raw status has to be on screen so it can be searched for"
    assert "status unknown" in out
    assert "gone out of step" in out
    for line in out.splitlines():
        if line.startswith("  ") and line[2:3] not in ("", " "):
            assert line[LABEL_WIDTH + 1] == " ", f"the label column overflows: {line!r}"


def test_the_detail_view_shows_the_same_headline_the_customer_page_shows(halted, capsys):
    """One vocabulary. The terminal and /swap/<id> read the same swap_display()."""
    run_tool(["--db", str(halted), "--swap", "s_halt"])
    out = capsys.readouterr().out

    assert "Held for review" in out, "services/swap_view.STATUS_MEANINGS' own headline"
    assert "a human decides what happens next" in out
    assert "destination tag 4242" in out, "an XRP deposit is attributed by tag, and the tag is the swap's own"


# --- house rules --------------------------------------------------------------


def test_durations_are_microfortnights_and_never_ascii_ufn(halted, capsys):
    """Rule 6: the micro sign, no space before the unit, seconds in parentheses."""
    run_tool(["--db", str(halted)])
    out = capsys.readouterr().out

    assert "µfn (" in out, "a duration must be microfortnights with the seconds beside it"
    assert "ufn" not in out, "an ASCII u in printed output is a defect, not a rendering fallback"
    assert " µfn" not in out, "no space between the number and the unit"
    assert "confirmation(s)" not in out.split("halted since")[0], "confirmations are counts, never durations"


def test_every_printed_label_leaves_a_gap_before_its_value(halted, capsys):
    """The label column, checked rather than eyeballed.

    A label that fills LABEL_WIDTH exactly prints `status vocabulary` hard against
    its value -- still true, still unreadable, and exactly what the first draft of
    swap_lines() did with one of its labels.

    Only lines of the form `  <label>` are checked: a swap heading starts at
    column 0, prose and continuation rows are indented past the column, and none
    of those is a label row.

    MUTATION: rename "status unknown" back to "status vocabulary" and seed an
    unknown status, and this test alone fails.
    """
    run_tool(["--db", str(halted)])
    run_tool(["--db", str(halted), "--swap", "s_halt"])

    checked = 0
    for line in capsys.readouterr().out.splitlines():
        if not line.startswith("  ") or line[2:3] in ("", " "):
            continue
        checked += 1
        assert line[LABEL_WIDTH + 1] == " ", f"the label column overflows: {line!r}"
    assert checked > 15, f"only {checked} label rows were checked, so this asserted almost nothing"


# --- the ready-made command has to run as printed ------------------------------
#
# ANOTHER ONE I CHECKED BY HAND AND DID NOT TEST. Two reviewers flagged that
# detail_command() dropped --db; I fixed it and the mutation run that put the bug
# back failed NOTHING. Same shape as open_swap.py's apply_command(), which DOES have
# this test -- so the defect and the gap in coverage were both duplicated.


def test_the_detail_command_carries_db_when_one_was_given(tmp_path):
    """Without it, the printed command answers about Config.DB_PATH instead -- a
    different database, silently, from a line whose only job is to be copied.

    tmp_path rather than a literal under /tmp: ruff's S108 flags the hardcoded form,
    and the fixture is the answer rather than a noqa (rule 19). Nothing here touches
    the filesystem; the path only has to be a path.

    MUTATION: drop the db_path branch in detail_command(). This fails.
    """
    command = show_swap.detail_command("s_abc", str(tmp_path / "else.db"))

    assert "--swap s_abc" in command
    assert f"--db {tmp_path / 'else.db'}" in command
    assert "<" not in command, "no placeholder may survive into a pasted command"


def test_the_detail_command_omits_db_when_none_was_given():
    """Paired with the test above, so "carry --db" cannot be satisfied by always
    printing one -- which would name a path the operator never chose."""
    command = show_swap.detail_command("s_abc")

    assert "--swap s_abc" in command
    assert "--db" not in command


def test_a_database_path_with_a_space_survives_the_printed_command(tmp_path):
    """shlex.quote, because a path is not guaranteed to be one shell word."""
    spaced = tmp_path / "two words" / "swap.db"

    command = show_swap.detail_command("s_abc", str(spaced))

    assert f"'{spaced}'" in command, command


def test_the_header_says_which_source_the_path_came_from(tmp_path, monkeypatch):
    """Three sources, three answers -- and it used to have two.

    It annotated every path as SWAP_DB_PATH, which is false whenever --db won. An
    operator comparing that line against their environment would find it
    disagreeing and have no way to know the flag had taken precedence.

    THE THIRD CASE WAS ADDED 2026-10-01 AFTER IT REACHED THE OPERATOR, through
    show_fees.py, which had copied this function. In a shell with SWAP_DB_PATH
    unset it printed

        database   .../swap_terminal/swap_terminal.db  <- SWAP_DB_PATH

    The path was right and the provenance was invented: that value is
    config.DB_PATH's BUILT-IN DEFAULT, and the line asserted an environment
    variable the shell did not have. The two-case version could not say otherwise,
    because it inferred the answer from `db_path != Config.DB_PATH` -- so "equals
    the default" was read as "came from the environment", which is exactly
    backwards for the one case where nothing is set.

    The flag's value is PASSED now rather than inferred, which also fixes the
    reachable case the inference got wrong in the other direction: --db pointed at
    the default value reported as the environment.
    """
    elsewhere = str(tmp_path / "elsewhere.db")

    # The real config, because header_lines() reads several keys and a hand-built
    # dict missing one fails as a KeyError rather than as the thing under test --
    # which is the fixture-narrower-than-reality pattern this session has hit four
    # times already, arriving here as a two-line test.
    config = get_config_dict()

    monkeypatch.setenv("SWAP_DB_PATH", str(Config.DB_PATH))
    from_environment = show_swap.header_lines(str(Config.DB_PATH), config)
    flagged = show_swap.header_lines(elsewhere, config, elsewhere)
    # --db holding the SAME value as the default: the case the old inference called
    # the environment, because it compared values instead of observing the flag.
    flagged_at_default = show_swap.header_lines(str(Config.DB_PATH), config, str(Config.DB_PATH))

    monkeypatch.delenv("SWAP_DB_PATH", raising=False)
    from_default = show_swap.header_lines(str(Config.DB_PATH), config)

    # THE ANNOTATION MARKER, not the bare word. Several lines mention SWAP_DB_PATH
    # in their prose, so a substring test passes on the explanation and proves
    # nothing. My first version of this assertion did exactly that and failed
    # against correct code.
    assert any("<- SWAP_DB_PATH." in line for line in from_environment)
    assert any("<- --db." in line for line in flagged)
    assert any("<- --db." in line for line in flagged_at_default), (
        "--db was passed, so the source is the flag whatever value it holds"
    )
    assert not any("<- SWAP_DB_PATH." in line for line in flagged), (
        "the path came from --db, so annotating it as SWAP_DB_PATH would disagree with the operator's "
        "own environment and give them no way to know the flag had won"
    )
    assert any("IS NOT SET in this shell" in line for line in from_default), (
        "with nothing set the path is a built-in default, and claiming it came from an unset environment "
        "variable is the defect that reached the operator"
    )
    assert not any("<- SWAP_DB_PATH." in line for line in from_default)
