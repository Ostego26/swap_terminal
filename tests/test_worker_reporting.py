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

import time
from pathlib import Path

import pytest
from config import Config
from workers import deposit_watcher
from workers.common import announce_start, cycle_line, endpoint_lines, sleep_until_next_cycle


def test_idle_and_working_cycles_do_not_share_a_line():
    """"A poll that found nothing and a poll that paid someone must not share
    a success line." The marker is the difference and it comes first."""
    idle = cycle_line("payout_worker", 3, 0.2, {"pending_at_start": 0, "broadcast": 0})
    worked = cycle_line("payout_worker", 4, 1.5, {"pending_at_start": 1, "broadcast": 1})

    assert "IDLE" in idle
    assert "WORKED" not in idle
    assert "WORKED" in worked
    assert "IDLE" not in worked


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
    poisoned = {
        asset: {**values, "user": "canary-rpc-user", "password": "canary-rpc-password"}
        for asset, values in Config.RPC.items()
    }
    monkeypatch.setattr(Config, "RPC", poisoned)

    announce_start("payout_worker", 10, pid=4242)
    printed = capsys.readouterr().out

    assert "canary-rpc-password" not in printed
    assert "canary-rpc-user" not in printed
    # And it still says the things it is supposed to say.
    assert "database" in printed
    assert "poll interval" in printed
    assert "10.0s" in printed
    assert "pid             4242" in printed


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
