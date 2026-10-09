"""What the workers say while they run (CLAUDE.md rule 14).

Role: test (read-only)
Reads: swap_terminal/workers/common.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- nothing here constructs an adapter or opens a socket.

Rule 14's clauses are testable as strings, and they are tested as strings here
because the operator reads the screen, not the source. The one that matters
most is the last: `test_the_banner_never_prints_a_credential` seeds a
recognizable password into Config.RPC and asserts it does not come out. The
credential sits one dict key away from the host and port that DO get printed,
so "we just won't print it" is a property that has to be checked rather than
intended.
"""

import sqlite3
import time
from pathlib import Path

import pytest
import workers.deposit_watcher as watcher
from config import Config
from conftest import poisoned_rpc_table
from db import SCHEMA
from services.deposit_service import ACTIVE_STATUSES
from workers import common, deposit_watcher
from workers.common import (
    CycleFailures,
    announce_start,
    cycle_line,
    endpoint_lines,
    sleep_until_next_cycle,
)


def test_idle_and_working_cycles_do_not_share_a_line():
    """"A poll that found nothing and a poll that paid someone must not share
    a success line." The marker is the difference and it comes first."""
    idle = cycle_line("payout_worker", 3, 0.2, {"pending_at_start": 0, "broadcast": 0})
    worked = cycle_line("payout_worker", 4, 1.5, {"pending_at_start": 1, "broadcast": 1})

    assert "IDLE" in idle
    assert "WORKED" not in idle
    assert "WORKED" in worked
    assert "IDLE" not in worked


def test_a_standing_condition_does_not_make_an_idle_cycle_claim_it_worked():
    """A halted swap sits there. Every cycle that sees it did not therefore do work.

    Measured 2026-09-26, the day after deposit_watcher gained HALTED_for_review:
    one halted swap made every cycle print WORKED with every count that describes
    work at zero, once every fifteen seconds, for as long as the swap sat there.
    That is "skipped plus success in the same output" -- and it was happening on
    exactly the cycles somebody was reading because something was wrong.

    MUTATION: drop the `if key not in STANDING_COUNTS` filter from cycle_line()
    and this test alone fails; the two assertions above it still pass, because
    they use counts no worker has declared standing.
    """
    halted_only = cycle_line(
        "deposit_watcher", 7, 0.04,
        {"active_swaps": 0, "refreshed": 0, "now_payout_pending": 0, "HALTED_for_review": 1},
    )
    assert "IDLE" in halted_only, "no swap was refreshed and none was queued; the cycle did nothing"
    assert "HALTED_for_review=1" in halted_only, "and the standing condition still has to be on screen"

    # A cycle that BOTH holds a halt and does work still reads WORKED: the
    # exclusion removes one count from the verdict, it does not suppress it.
    assert "WORKED" in cycle_line(
        "deposit_watcher", 8, 0.04,
        {"active_swaps": 1, "refreshed": 1, "now_payout_pending": 0, "HALTED_for_review": 1},
    )


def test_a_cycle_line_carries_the_duration_in_microfortnights():
    line = cycle_line("deposit_watcher", 1, 2.8, {"active_swaps": 0})
    assert "2.3µfn (2.8s)" in line
    assert "ufn" not in line


def test_empty_counts_print_none_rather_than_a_blank_gap():
    # Rule 14: "(none)" is a result; a blank gap is ambiguous between zero rows
    # and a query that broke.
    assert "(none)" in cycle_line("reconcile_worker", 9, 0.1, {})


def test_counts_carry_their_own_interpretation():
    line = cycle_line(
        "payout_worker",
        2,
        0.1,
        {"pending_at_start": 0},
        notes="pending_at_start=0 while swaps are open may mean deposit_watcher is not running",
    )
    # "State what the number means, next to the number."
    assert "may mean deposit_watcher is not running" in line


def test_endpoint_lines_report_confirmations_as_blocks_never_as_microfortnights():
    """Rule 6's hard boundary: six confirmations is six confirmations.

    THIS TEST WAS WIDENED ON 2026-09-25 AND IS STRICTER THAN IT WAS. It used to
    assert that EVERY line contains `min_confirmations=` and the word `blocks`,
    which was true while every chain here was proof-of-work. Solana is not: its
    threshold is a rung on a commitment ladder (chains/solana_units.py), and a
    line reading `min_confirmations=3 blocks` for a Solana endpoint would be a
    sentence that is not true -- rule 6's unit laundering arriving as a status
    line rather than as a number, and one an operator would read all morning
    without noticing.

    So the invariant is no longer "every line says blocks". It is the stronger
    one that was always the point (CLAUDE.md rule 9: a test pinning replaced
    behavior "changes to pin the stronger invariant"):

      every line states its own unit, and states the RIGHT one,
      and no line renders a settlement threshold in microfortnights.

    The block-count chains must still say `blocks`; the Solana line must say it
    is a commitment rank and must say it is NOT blocks; and the µfn ban covers
    all of them as before.
    """
    lines = endpoint_lines()
    assert lines, "endpoint_lines() returned nothing; the banner would be silent (rule 14)"
    for line in lines:
        # Unchanged and unconditional: a block count is not a duration, on any
        # chain, configured or not.
        assert "µfn" not in line
    for asset in ("BTC", "LTC", "GRC"):
        matching = [line for line in lines if line.strip().startswith(asset)]
        assert len(matching) == 1, f"expected exactly one {asset} line, got {matching}"
        # A threshold is reported only by a CONFIGURED chain, and this distinction
        # arrived 2026-09-26 when these three gained an unconfigured state. An
        # unconfigured chain has no adapter, watches nothing, and has no threshold
        # to report -- printing `min_confirmations=2 blocks` beside "not configured"
        # would describe a watcher that does not exist.
        #
        # The ONE line every chain must still satisfy is the µfn ban above, which is
        # unconditional: a block count is not a duration whether or not the chain is
        # configured. That is the invariant this test is actually for.
        if "not configured" in matching[0]:
            assert "min_confirmations=" not in matching[0], (
                f"an unconfigured chain must not report a threshold: {matching[0]}"
            )
            continue
        assert "min_confirmations=" in matching[0]
        assert "blocks" in matching[0]
    solana = [line for line in lines if line.strip().startswith("SOL")]
    assert len(solana) == 1, f"expected exactly one SOL line, got {solana}"
    # It must never claim blocks -- that is the whole reason it is not printed
    # through the same f-string as the three above.
    assert "blocks" not in solana[0]


def test_the_banner_never_prints_a_credential(monkeypatch, capsys):
    """The property that has to be checked rather than intended.

    `user` and `password` live in the same per-chain dict as `host` and `port`,
    which the banner does print. A status line that formatted the whole dict --
    or that was written with `**rpc` in an f-string one day -- would put wallet
    RPC credentials into every log the worker writes, and nothing would fail.
    """
    # ONE DEFINITION, SHARED WITH tests/test_supervisor.py's identical check. Both
    # built this table with the same comprehension; see conftest.poisoned_rpc_table()
    # for why it is a loop that ASSERTS rather than one that filters.
    monkeypatch.setattr(Config, "RPC", poisoned_rpc_table(Config.RPC))

    announce_start("payout_worker", 10, pid=4242)
    printed = capsys.readouterr().out

    assert "canary-rpc-password" not in printed
    assert "canary-rpc-user" not in printed
    # And it still says the things it is supposed to say.
    assert "database" in printed
    assert "poll interval" in printed
    assert "10.0s" in printed
    assert "pid             4242" in printed


def test_the_canary_table_refuses_an_entry_it_cannot_poison():
    """The positive control for the test above, and it cannot be written as one.

    THE PROBLEM THIS SOLVES. The canary test asserts an ABSENCE -- that no credential
    reached the banner. An entry that never received a canary in the first place
    produces exactly the same green as an entry that received one and kept it out of
    the output. So "every chain was checked" is not provable from that test's own
    result, at any level of care, and the only place it can be established is where
    the table is built.

    THAT IS WHY conftest.poisoned_rpc_table() ASSERTS RATHER THAN FILTERS. The
    obvious way to satisfy pyright on `{**values}` over a TypedDict's .items() --
    which yields `object` since Config.RPC became one on 2026-10-09 -- is
    `if isinstance(values, Mapping)` in the comprehension. That silently drops the
    entry, the canary test still passes, and one chain stops being covered with
    nothing anywhere saying so. This is the test that fails if anyone makes that
    change.

    It is also the shape that burned me earlier in this same session: three mutations
    that made a scanner check NOTHING all passed, because "no crossings found" and
    "nothing was looked at" are the same green.
    """
    with pytest.raises(AssertionError) as raised:
        poisoned_rpc_table({"GRC": {"user": "u"}, "BTC": "not-a-mapping"})
    message = str(raised.value)
    assert "BTC" in message, "the refusal must name the chain that could not be poisoned"
    assert "str" in message, "and what it found instead"

    with pytest.raises(AssertionError, match="empty"):
        poisoned_rpc_table({})

    # And the honest case still returns every key it was given, with the canaries in.
    poisoned = poisoned_rpc_table({"GRC": {"user": "real", "port": 25715}})
    assert set(poisoned) == {"GRC"}
    assert poisoned["GRC"]["user"] == "canary-rpc-user", "the canary did not replace the real value"
    assert poisoned["GRC"]["port"] == 25715, "a non-credential field must survive untouched"


def test_the_banner_refuses_to_claim_a_network_it_has_not_checked(capsys):
    """Rule 17 inside rule 14.

    A port number is a reason to believe, not a check. The banner prints the
    endpoint and says outright that the network is unverified, rather than
    printing "testnet" because 18332 is testnet's default port.
    """
    announce_start("deposit_watcher", 15, pid=1)
    printed = capsys.readouterr().out
    assert "NOT VERIFIED" in printed


def test_sleep_returns_immediately_once_a_stop_is_requested():
    """A stop must not have to wait out a 60s poll interval.

    If it did, supervisor.py would escalate to SIGKILL on a worker that was
    perfectly willing to exit -- and SIGKILL on the payout worker is the
    mid-broadcast window this design exists to avoid.
    """
    started = time.monotonic()
    sleep_until_next_cycle(60, should_stop=lambda: True)
    assert time.monotonic() - started < 1.0


def test_sleep_actually_sleeps_when_no_stop_is_requested():
    started = time.monotonic()
    sleep_until_next_cycle(0.3, should_stop=lambda: False)
    assert time.monotonic() - started == pytest.approx(0.3, abs=0.25)


def test_the_deposit_watcher_reports_halted_swaps():
    """A halt was invisible until 2026-09-26, and it is the one outcome that waits on a person.

    The operator sent 1 XRP to a swap expecting 5. The tolerance check correctly
    refused to credit a wrong amount and moved the swap to 'under_review' -- which
    is NOT in ACTIVE_STATUSES, so the swap left the polled set. The cycle line
    printed:

        active_swaps=1 refreshed=1 now_payout_pending=0

    and nothing else. The only signal was a 0 where a reader had to already know to
    expect 1.

    A halted swap will not resolve on its own -- it exists precisely to wait for a
    person -- so a cycle that halted a customer's swap must not read like one that
    found nothing to do (rule 14). Asserted over the module SOURCE because the
    counter is built inside the worker's loop, which cannot run without a database
    and a chain; what is checkable here is that the field and its explanation exist
    and travel together.
    """
    source = Path(deposit_watcher.__file__).read_text()

    assert "HALTED_for_review" in source, "the cycle line must carry a halted count"
    assert "under_review" in source, "it must be counted from the real status"
    assert "waiting on a PERSON" in source, (
        "the note must say what the number MEANS -- a bare count does not tell a reader "
        "that nothing will resolve it (rule 14)"
    )


def test_a_cumulative_failure_total_does_not_make_an_idle_payout_cycle_claim_it_worked():
    """The same defect as the test above, one field over, measured 2026-10-01.

    From the operator's own payout_worker.log while they were between steps of a
    devnet SOL -> testnet GRC rehearsal. Three payouts had failed earlier in the
    week, so every cycle printed:

        payout_worker cycle=14 WORKED pending_at_start=0 broadcast=0
        failed_total=3 in 0.0µfn (0.0s)  <- ... failed_total is cumulative, not
        this cycle

    WORKED, with both counts that describe work at zero, every ten seconds. The
    line's own note says the figure is cumulative, so the worker was explaining in
    prose why the marker beside it was wrong.

    WORSE THAN THE HALTED CASE IT MIRRORS, and that is why it gets its own test
    rather than a parameter on the one above. HALTED_for_review returns to zero
    when somebody resolves the swap; `failed_total` counts every payout that has
    EVER failed and never returns to zero, so this latched the first time any
    payout failed and could not unlatch. On that host it latched 2026-09-26 and
    nobody noticed for five days.

    STANDING_COUNTS is keyed by the count's NAME and its comment claimed that was
    enough -- "a second worker reporting the same field gets the same treatment
    without anybody remembering to ask for it". True, and not sufficient: this is
    a DIFFERENT field with the identical property, and nobody remembered.

    MUTATION: remove "failed_total" from STANDING_COUNTS and this fails while the
    halted test still passes.
    """
    failures_only = cycle_line(
        "payout_worker", 14, 0.04,
        {"pending_at_start": 0, "broadcast": 0, "failed_total": 3},
    )
    assert "IDLE" in failures_only, "nothing was pending and nothing was broadcast; the cycle did nothing"
    assert "failed_total=3" in failures_only, "and the standing total still has to be on screen"

    # A cycle that broadcasts while the total stands still reads WORKED: the
    # exclusion removes one count from the verdict, it does not suppress it.
    assert "WORKED" in cycle_line(
        "payout_worker", 15, 0.04,
        {"pending_at_start": 1, "broadcast": 1, "failed_total": 3},
    )
    # AND A PAYOUT THAT FAILS THIS CYCLE STILL READS WORKED, which is the thing
    # excluding a cumulative total could plausibly have broken and does not.
    # `broadcast` stays 0 on a failure, so the verdict rests on
    # `pending_at_start` -- and that is a sound signal: a payout can only fail if
    # one was pending, so the cycle genuinely had something to do.
    #
    # Checked rather than assumed. The first version of this test asserted IDLE
    # here and reasoned that "the only thing that could carry it is the standing
    # total" -- which was wrong, because it forgot the count sitting next to it.
    # Recorded because the wrong version would have pinned a defect as intended
    # behavior: a failed payout on a credited swap is the most serious routine
    # outcome this worker has, and a line reading IDLE for it would be the
    # did-nothing-looks-like-did-work defect pointing the other way.
    assert "WORKED" in cycle_line(
        "payout_worker", 16, 0.04,
        {"pending_at_start": 1, "broadcast": 0, "failed_total": 4},
    )


# --- a cycle that raises must not kill the worker -----------------------------
#
# MEASURED ON THE OPERATOR'S HOST 2026-10-01. A devnet DNS lookup failed for a
# moment:
#
#     SolanaRPCError: getSignaturesForAddress could not reach
#     https://api.devnet.solana.com: Failed to resolve 'api.devnet.solana.com'
#     ([Errno -2] Name or service not known)
#
# and the deposit watcher DIED. Counted at the time: zero `try` and zero `except`
# in any of the three workers. One chain briefly unreachable terminated the
# process and stopped deposits being credited on EVERY chain -- and payout_worker
# went on printing `IDLE pending_at_start=0`, which is exactly what it prints when
# there is genuinely nothing to pay. Nothing said deposits had stopped. A
# customer's money would arrive, confirm, and sit.

def test_a_failed_cycle_says_FAILED_and_never_IDLE():
    """The two must not read the same, which is the whole defect.

    `IDLE` means the cycle looked and found nothing. A cycle that could not look
    found nothing for a completely different reason, and an operator skimming for
    trouble has to be able to tell them apart (rule 14).
    """
    failures = CycleFailures("deposit_watcher")
    line = failures.record(7, 0.04, RuntimeError("chain unreachable"))

    assert "FAILED" in line
    assert "IDLE" not in line
    assert "RuntimeError" in line, "the exception type, because a DNS error and a bad row differ"
    assert "chain unreachable" in line, "and its reason"


def test_the_failed_line_says_the_worker_is_still_running():
    """An operator reading FAILED must not conclude the process is gone.

    That conclusion is what makes somebody restart a healthy worker, and on this
    system a restart loses nothing but costs the one thing the line is for: the
    information that the chain was briefly unreachable and recovered on its own.
    """
    line = CycleFailures("payout_worker").record(1, 0.04, OSError("boom"))

    assert "still running" in line
    assert "try again at the next poll" in line
    assert "credited nothing" in line, "it has to say what the cycle did NOT do"


def test_consecutive_failures_are_counted_and_reset():
    """One failure is a blip; two hundred is an outage. They must not read alike.

    MUTATION: drop the counter and a worker that has been failing all night looks
    exactly like one that failed once a second ago -- the cried-wolf shape that
    gets a repeated line ignored.
    """
    failures = CycleFailures("deposit_watcher")

    assert "failed_cycles_in_a_row=1" in failures.record(1, 0.0, RuntimeError("a"))
    assert "failed_cycles_in_a_row=2" in failures.record(2, 0.0, RuntimeError("b"))
    assert "failed_cycles_in_a_row=3" in failures.record(3, 0.0, RuntimeError("c"))
    assert failures.consecutive == 3

    failures.clear()
    assert failures.consecutive == 0
    assert "failed_cycles_in_a_row=1" in failures.record(4, 0.0, RuntimeError("d")), (
        "after a success the count starts over, so it means consecutive and not total"
    )


def test_an_exception_with_no_message_still_names_itself():
    """`str(exc)` is empty for a bare `RuntimeError()`, and a blank reason is useless.

    Rule 14's "never let an empty result print nothing", on the one line that
    explains why a cycle did no work.
    """
    line = CycleFailures("deposit_watcher").record(1, 0.0, RuntimeError())

    assert "RuntimeError" in line
    assert "FAILED RuntimeError: RuntimeError" in line, "the class name stands in for the message"


def test_the_deposit_watcher_survives_a_raising_cycle_and_keeps_polling(monkeypatch, capsys):
    """THE BEHAVIORAL TEST. The real main loop, a real exception, a live worker.

    The unit tests above check the LINE; this checks that the loop does not die,
    which is the thing that actually cost an uncredited deposit. Asserted by
    running deposit_watcher.main() with process_active_swaps raising and a stop
    predicate that ends it after three cycles -- so a worker that died on the
    first one returns early and the cycle count gives it away.

    MUTATION: remove the try/except from the loop and this raises instead of
    returning 0 -- which is precisely what happened on the operator's host.
    """
    seen = {"cycles": 0}

    def exploding_process(*_args, **_kwargs):
        seen["cycles"] += 1
        raise RuntimeError("Failed to resolve 'api.devnet.solana.com'")

    monkeypatch.setattr(watcher, "process_active_swaps", exploding_process)
    monkeypatch.setattr(watcher, "install_stop_handler", lambda: (lambda: seen["cycles"] >= 3))
    monkeypatch.setattr(watcher, "sleep_until_next_cycle", lambda *_a, **_k: None)
    monkeypatch.setattr(watcher, "build_adapters_from_config", dict)

    assert watcher.main(poll_seconds=0) == 0, "a worker whose cycle raises must still exit cleanly"

    out = capsys.readouterr().out
    assert seen["cycles"] == 3, f"the loop must keep polling after a failure, ran {seen['cycles']}"
    assert out.count("FAILED") == 3, "every failed cycle reports"
    assert "failed_cycles_in_a_row=3" in out, "and the count climbs across them"
    assert "api.devnet.solana.com" in out, "the real reason reaches the operator's screen"
    assert "stopped cleanly" in out, "and the worker still shuts down properly"


def test_a_cycle_that_recovers_stops_saying_failed(monkeypatch, capsys):
    """The other half: a chain that comes back is picked up with no intervention.

    A guard that caught forever and never cleared would leave a healthy worker
    claiming failure, which is the same unreadable-output defect pointing the
    other way.
    """
    state = {"cycles": 0}

    def flaky_process(*_args, **_kwargs):
        state["cycles"] += 1
        if state["cycles"] == 1:
            raise RuntimeError("briefly unreachable")
        return []

    monkeypatch.setattr(watcher, "process_active_swaps", flaky_process)
    monkeypatch.setattr(watcher, "install_stop_handler", lambda: (lambda: state["cycles"] >= 2))
    monkeypatch.setattr(watcher, "sleep_until_next_cycle", lambda *_a, **_k: None)
    monkeypatch.setattr(watcher, "build_adapters_from_config", dict)

    assert watcher.main(poll_seconds=0) == 0
    out = capsys.readouterr().out

    assert out.count("FAILED") == 1, "only the cycle that actually failed"
    assert "IDLE" in out, "and the recovered cycle reports normally again"


def test_the_consecutive_count_restarts_after_a_recovery(monkeypatch, capsys):
    """fail, succeed, fail -- the second failure must say 1, not 2.

    MEASURED: a mutation deleting `failures.clear()` from the loop SURVIVED every
    test above, because none of them failed again AFTER recovering. The count was
    only ever read on a rising run, so a stale one was invisible.

    It matters because the number is what distinguishes a blip from an outage. A
    counter that never resets turns "failed once an hour ago, fine since" into
    "failed_cycles_in_a_row=2", and an operator deciding whether to investigate
    reads that as a worsening trend.
    """
    state = {"cycles": 0}

    def alternating(*_args, **_kwargs):
        state["cycles"] += 1
        if state["cycles"] in (1, 3):
            raise RuntimeError(f"failure {state['cycles']}")
        return []

    monkeypatch.setattr(watcher, "process_active_swaps", alternating)
    monkeypatch.setattr(watcher, "install_stop_handler", lambda: (lambda: state["cycles"] >= 3))
    monkeypatch.setattr(watcher, "sleep_until_next_cycle", lambda *_a, **_k: None)
    monkeypatch.setattr(watcher, "build_adapters_from_config", dict)

    assert watcher.main(poll_seconds=0) == 0
    out = capsys.readouterr().out

    assert out.count("failed_cycles_in_a_row=1") == 2, (
        "both failures are the first of their run; a count of 2 means the success between "
        "them did not clear it"
    )
    assert "failed_cycles_in_a_row=2" not in out


def test_a_stop_signal_is_not_swallowed_by_the_guard(monkeypatch):
    """`except Exception`, not BaseException. KeyboardInterrupt must still end it.

    A Ctrl-C that only logged a failed cycle and carried on would be a worker the
    operator cannot stop, which is worse than the crash this guard replaces
    (rule 13: a stop that cannot prove it worked is not a stop).

    MUTATION: catch BaseException and this hangs instead of raising.
    """
    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    # THE STOP PREDICATE IS BOUNDED, and that is a fix to this test rather than a
    # detail. The first version used `lambda: False`, so a mutation that caught
    # BaseException made the loop run forever and the SUITE HUNG instead of
    # failing -- measured: pytest had to be killed by a timeout. A hang is a
    # detectable failure and a terrible one; it gives no name, no line and no
    # diff. Counting cycles means the mutation completes the loop and fails on
    # pytest.raises, which is a test result somebody can read.
    attempts = {"n": 0}

    def interrupted_and_counted(*args, **kwargs):
        attempts["n"] += 1
        return interrupted(*args, **kwargs)

    monkeypatch.setattr(watcher, "process_active_swaps", interrupted_and_counted)
    monkeypatch.setattr(watcher, "install_stop_handler", lambda: (lambda: attempts["n"] >= 5))
    monkeypatch.setattr(watcher, "sleep_until_next_cycle", lambda *_a, **_k: None)
    monkeypatch.setattr(watcher, "build_adapters_from_config", dict)

    with pytest.raises(KeyboardInterrupt):
        watcher.main(poll_seconds=0)
    assert attempts["n"] == 1, "the interrupt must end the loop on its first cycle, not be caught"


# --- the database a worker actually opened --------------------------------------
#
# Added 2026-10-01 after three workers ran for an hour against the wrong file.
# SWAP_DB_PATH pointed at repo_root/runtime/swap_terminal.db instead of
# repo_root/swap_terminal/swap_terminal.db -- a path I handed the operator in a
# block -- and every cycle printed
#
#     deposit_watcher cycle=57 IDLE  active_swaps=0 refreshed=0 now_payout_pending=0
#
# while THREE swaps sat in awaiting_deposit in the database every root tool reads.
# Nothing failed. The banner printed the path, correctly, and that was not enough:
# a path is only wrong relative to what you expected, and `active_swaps=0` carried
# a note saying it "is expected only when no swap is open", which was true of the
# file being read and false of the system.


def test_a_missing_database_is_named_as_such_before_the_first_cycle(tmp_path):
    """The one state most worth reporting, and connect() would destroy the evidence.

    db.connect_db() is a bare sqlite3.connect() (db.py:561): it CREATES a missing
    file rather than refusing, and no worker applies SCHEMA. So a typo in a path
    does not fail -- it manufactures an empty database and polls it forever. The
    census has to look BEFORE anything connects, which is why it is exists()-first
    and not try/except around a query.
    """
    verdict = common.database_census(str(tmp_path / "never-created.db"))
    assert "DOES NOT EXIST YET" in verdict
    assert "CREATES a missing file" in verdict
    assert "SWAP_DB_PATH is pointing somewhere you did not mean" in verdict


def test_a_file_with_no_swaps_table_does_not_read_as_an_empty_one(tmp_path):
    """Created-but-never-initialized and healthy-and-empty are different facts.

    They give the same answer to "how many swaps are open" -- none -- and only one
    of them means every cycle below will fail. show_swap.py and open_swap.py both
    draw this distinction for the same reason; the workers did not draw it at all.
    """
    path = tmp_path / "blank.db"
    sqlite3.connect(path).close()
    verdict = common.database_census(str(path))
    assert "HAS NO SWAPS TABLE" in verdict
    assert "no such table: swaps" in verdict
    assert "NO SWAPS AT ALL" not in verdict, "the two states must not render the same way"


def test_an_initialized_empty_database_says_IDLE_will_be_correct_for_this_file(tmp_path):
    """Rule 14: "(none)" is a result, and it has to say what it is a result ABOUT.

    An empty-but-healthy database is the normal state of a fresh install, so it
    must not read as an alarm -- while still telling a reader that the IDLE cycles
    they are about to see describe THIS file and nothing else.
    """
    path = tmp_path / "fresh.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()
    verdict = common.database_census(str(path))
    assert "NO SWAPS AT ALL" in verdict
    assert "correct for THIS file" in verdict
    assert "compare this path against the one your other tools print" in verdict


def test_the_census_tallies_by_status_so_a_mismatch_is_visible_on_one_line(tmp_path):
    """THE LINE THAT WOULD HAVE CAUGHT IT IN A SECOND.

    Two awaiting_deposit here against three in the file the operator meant is a
    comparison a human makes instantly and a cycle counter cannot make at all.
    """
    path = tmp_path / "rows.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    for index, status in enumerate(("awaiting_deposit", "awaiting_deposit", "completed")):
        connection.execute(
            "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
            " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"q{index}", "SOL", "GRC", 0.01, 9000.0, 150, 0.01, 88.0, "x", "x"),
        )
        connection.execute(
            "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
            " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve, output_amount_estimate,"
            " status, min_confirmations, created_at, updated_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"s{index}", f"q{index}", "SOL", "GRC", "a", "b", 0.01, 9000.0, 150, 0.01, 88.0,
             status, 3, "x", "x", "x"),
        )
    connection.commit()
    connection.close()

    verdict = common.database_census(str(path))
    assert "awaiting_deposit 2" in verdict
    assert "completed 1" in verdict
    # The vocabulary comes from services/deposit_service.ACTIVE_STATUSES rather than
    # a second spelling here (rule 11), so the census and the counter cannot come to
    # disagree about what "active" means.
    for status in ACTIVE_STATUSES:
        assert status in verdict


def test_the_banner_prints_the_census_and_the_paths_provenance(tmp_path, monkeypatch, capsys):
    """Both halves, because neither alone identified the wrong file.

    The path alone was already being printed and did not catch it. The census
    alone would not say whether the path came from an export or a default -- and
    "I exported that" is what makes a wrong path recognizable.
    """
    path = tmp_path / "banner.db"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.commit()
    connection.close()

    monkeypatch.setattr(common.Config, "DB_PATH", str(path), raising=False)
    monkeypatch.setenv("SWAP_DB_PATH", str(path))
    common.announce_start("deposit_watcher", 30.0, 4242)
    out = capsys.readouterr().out

    assert f"  database        {path}" in out
    assert "<- SWAP_DB_PATH" in out, "the provenance of the path is half the signal"
    assert "  it holds        " in out
    assert "NO SWAPS AT ALL" in out
