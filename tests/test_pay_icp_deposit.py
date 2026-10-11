"""pay_icp_deposit's refusals and its reading of the ledger's reply.

Role: test (seeded rows and seeded reply strings; no replica, no ledger, no dfx,
      no network, and nothing is ever sent)
Reads: pay_icp_deposit.py, db.py's SCHEMA
Writes: an in-memory SQLite database per test
Can move funds: no. Every function exercised here is pure or reads a temporary
      database. The send path is NOT called: `send_outcome()` is given reply
      strings directly, which is the whole reason it was extracted.
Mainnet-safe: yes

WHY THE REFUSALS ARE THE THING UNDER TEST RATHER THAN THE SEND.

The send is four lines and the ledger is the authority on whether it worked. What
this tool adds over `dfx canister call ... transfer` is the set of conditions it
refuses under, and each of those is a failure somebody already paid for --
pay_test_deposit.py's header records four from one afternoon, including 82.65
tGRC to an address nobody held the key for because "the newest awaiting_deposit
row" was a swap from three hours earlier.

So a test that sent successfully would prove the easy half. These prove the half
that costs money when it is missing.

AND ONE OF THEM WAS PROVING A REFUSAL WHOSE REASON WAS FALSE (2026-10-11).
test_an_expired_swap_is_refused asserted on the word "expired" and its docstring
repeated the claim the code made -- "credits into review, not into a payout" --
which measurement showed to be the opposite of what this system does. A test whose
docstring restates the code's own sentence cannot catch that sentence being wrong.
See test_the_lapsed_quote_refusal_says_what_actually_happens for the figures.

THE LAPSED-QUOTE WORK WAS MUTATION-CHECKED, one change at a time, reverted between
each, 39 of 39 green before and after every one:

    mutation applied to pay_icp_deposit.py   tests that then FAILED
    ---------------------------------------  ---------------------------------
    the original FALSE sentence restored     3: the default-refusal test, the
    verbatim                                 says-what-happens test, and the
                                             preflight-silence test
    --honor-lapsed-quote returns [] from     1: the only-that-one test, on the
    refuse() entirely                        swap that already has a deposit
                                             event -- i.e. the flag would have
                                             allowed a second send
    lapsed_quote_lines() dropped from        1: the preflight-prints-it test
    preflight()'s print list
    the banner's ", HONORING A LAPSED        1: the banner test
    QUOTE" suffix removed
    lapsed_quote_lines() ignores the flag    2: the announced-vs-not test and
    and always announces                     the preflight-silence test
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from db import SCHEMA, dict_factory
from services.helpers import iso_to_epoch_nanos
from valid_addresses import GRC_PAYOUT

import pay_icp_deposit as subject

#: A swap that is ready to pay. Each test mutates one field away from this, so
#: the thing under test is always the single difference.
GOOD = {
    "id": "s_1234567890abcdef",
    "quote_id": "q_1234567890abcdef",
    "from_asset": "ICP",
    "to_asset": "GRC",
    "deposit_address": "d220b5a9955e7667fb429419a2eca19a82c84c1fef034789b4d5930db19b601d",
    # GRC_PAYOUT, NOT A PASTED STRING. tests/test_address_literals_are_valid.py
    # caps address literals in the tree and names the remedy: "use
    # tests/valid_addresses.py rather than writing one -- a derived address
    # cannot be mistyped and says what it is for". My first draft pasted a real
    # testnet address from HANDOFF.md and pushed that ceiling over its limit,
    # which is the gate working.
    "payout_address": GRC_PAYOUT,
    "expected_input_amount": 2.42621078,
    # ADDED 2026-10-11, AND ITS ABSENCE WAS A GAP IN THIS FIXTURE RATHER THAN A
    # CHOICE. find_swap() does `SELECT *` and db.py declares
    # `quoted_rate REAL NOT NULL`, so every row this tool can ever be handed has
    # one -- seeded_db() below already inserts 1.0. A fixture missing a NOT NULL
    # column is a fixture that cannot catch a reader of it, which is what the
    # lapsed-quote refusal became when it started naming the rate it is asking
    # the operator to honor. 1.0 to match seeded_db(), so the two agree.
    "quoted_rate": 1.0,
    "status": "awaiting_deposit",
    "created_at": "2026-10-10T17:00:00+00:00",
    "expires_at": "2026-10-10T17:10:00+00:00",
}

#: Inside the quote window and inside the ledger's 24h dedup window.
NOW = iso_to_epoch_nanos("2026-10-10T17:05:00+00:00")

#: One minute PAST the quote window, and still well inside the ledger's 24h dedup
#: window -- so a test using it isolates the lapse from the TxTooOld refusal.
LAPSED = iso_to_epoch_nanos("2026-10-10T17:11:00+00:00")


def swap(**overrides) -> dict:
    return {**GOOD, **overrides}


# ---------------------------------------------------------------------------
# REFUSALS


def test_a_ready_swap_is_not_refused():
    """The control. Without it, a refusal that fires on everything would pass."""
    assert subject.refuse(swap(), [], NOW) == []


def test_a_non_icp_deposit_leg_is_refused():
    """Sending ICP to a swap expecting BTC is an unattributable deposit."""
    reasons = subject.refuse(swap(from_asset="BTC"), [], NOW)
    assert any("not ICP" in r for r in reasons)


@pytest.mark.parametrize(
    "status", ["deposit_seen", "confirming", "payout_pending", "paying", "completed", "failed"]
)
def test_any_status_past_awaiting_is_refused(status):
    """MUTATION: widen AWAITING to fee_sweep.py's active list.

    `deposit_seen` and `confirming` mean a deposit has ALREADY ARRIVED. Sending
    again is the duplicate this file exists to prevent, and it is the one that
    reads as reasonable -- the swap is still "active", so a looser check waves it
    through.
    """
    reasons = subject.refuse(swap(status=status), [], NOW)
    assert any(status in r for r in reasons)


def test_an_existing_deposit_event_is_refused_and_the_amount_is_named():
    """That is money already sent. A second send is not a retry."""
    deposits = [
        {"txid": "a" * 64, "vout": 0, "amount": 2.42621078, "confirmations": 1, "credited_at": None}
    ]
    reasons = subject.refuse(swap(), deposits, NOW)
    assert any("already recorded" in r and "2.42621078" in r for r in reasons)


def test_a_lapsed_quote_window_is_refused_by_default():
    """The default posture is unchanged: nobody pays a lapsed swap by accident.

    THE NAME AND THE DOCSTRING BOTH CHANGED ON 2026-10-11 AND THE OLD ONES WERE
    THE DEFECT. It was called test_an_expired_swap_is_refused and said "A deposit
    against a stale quote credits into review, not into a payout" -- which is what
    the refusal under test SAID, and which is false. See
    test_the_lapsed_quote_refusal_says_what_actually_happens below for the
    measurement. A test whose docstring repeats the claim the code makes cannot
    catch the claim being wrong, and this one did not for as long as it existed.

    It also asserted on the word "expired", which is the other half of the same
    problem: `expired` is a STATUS in this tree (expire_swap.py writes it, with
    --apply, and nothing else does), and a swap whose quote window has passed is
    not in it. The wording now says "lapsed", matching
    services/swap_view.quote_window()'s own vocabulary, and this assertion moved
    with it.
    """
    reasons = subject.refuse(swap(), [], LAPSED)
    assert any("lapsed" in r for r in reasons)


def test_the_lapsed_quote_refusal_says_what_actually_happens():
    """MEASURED 2026-10-11, and the refusal used to describe the opposite.

    Seeded an ICP -> GRC swap with expires_at three hours in the past plus one
    confirmed deposit event of exactly expected_input_amount into the real db.py
    SCHEMA, then ran the real services/deposit_service.process_active_swaps():

        status                 payout_pending      <- NOT under_review
        credited_at            2026-10-11T00:26:53.677702+00:00
        actual_input_amount    2.44081155
        failed_reason          (none)
        quoted_rate            123.45               <- unchanged, and honored

    So the sentence "sends the swap to review rather than to a payout" was false.
    This asserts the TRUE consequence is the one printed, and asserts the false
    one is NOT -- because the whole failure mode was an operator reading a
    confident wrong sentence and creating a new swap, which costs this swap's ICP
    subaccount index permanently (db.py's BEFORE DELETE trigger).

    MUTATION: restore "which sends the swap to review rather than to a payout" and
    this fails on both assertions at once.
    """
    reason = next(r for r in subject.refuse(swap(), [], LAPSED) if "lapsed" in r)

    assert "payout_pending" in reason, (
        f"the refusal has to say where a lapsed deposit actually goes: {reason!r}"
    )
    assert "review" not in reason, (
        f"nothing in the tree sends a lapsed swap to review; advance_deposit_status() has "
        f"no expiry arm at all: {reason!r}"
    )
    assert "--honor-lapsed-quote" in reason, (
        f"a refusal an operator can lift has to name how: {reason!r}"
    )
    assert str(GOOD["quoted_rate"]) in reason, (
        f"the rate is the thing being honored, so it is the number that belongs next to "
        f"the decision (rule 14): {reason!r}"
    )


def test_honoring_a_lapsed_quote_lifts_that_refusal_and_only_that_one():
    """The opt-in, and the measurement that it is narrow.

    MUTATION: make the flag return [] from refuse() early -- the obvious way to
    implement "proceed anyway" -- and the second half of this test fails, because a
    swap that is BOTH lapsed and already has a deposit event would then be paid
    twice. That is the duplicate this whole file exists to prevent, and it is why
    the flag gates one `if` rather than the function.
    """
    assert subject.refuse(swap(), [], LAPSED, honor_lapsed_quote=True) == [], (
        "with the flag, a swap whose ONLY problem is a lapsed window may proceed"
    )

    deposits = [
        {"txid": "a" * 64, "vout": 0, "amount": 2.42621078, "confirmations": 1, "credited_at": None}
    ]
    still_refused = subject.refuse(swap(), deposits, LAPSED, honor_lapsed_quote=True)
    assert any("already recorded" in r for r in still_refused), (
        f"the flag is about the RATE, not about sending twice: {still_refused}"
    )

    past_window = subject.refuse(
        swap(), [], NOW + subject.DEDUP_WINDOW_NANOS, honor_lapsed_quote=True
    )
    assert any("TxTooOld" in r for r in past_window), (
        f"the flag cannot make an un-idempotent send idempotent: {past_window}"
    )

    wrong_leg = subject.refuse(swap(from_asset="BTC"), [], LAPSED, honor_lapsed_quote=True)
    assert any("not ICP" in r for r in wrong_leg), f"nor change which chain this is: {wrong_leg}"


def test_the_flag_changes_nothing_on_a_swap_inside_its_window():
    """So the flag cannot become something an operator leaves on by habit.

    A swap inside its window is not refused either way, and
    lapsed_quote_lines() prints nothing for it -- asserted below. Without this,
    `--honor-lapsed-quote` could acquire a second meaning nobody asked for.
    """
    assert subject.refuse(swap(), [], NOW) == []
    assert subject.refuse(swap(), [], NOW, honor_lapsed_quote=True) == []


def test_lapsed_seconds_is_signed_and_exact_at_the_boundary():
    """The arithmetic the refusal and the announcement both read, so they cannot disagree.

    The boundary matters because it replaced `expires_nanos <= now_nanos`: a swap
    at EXACTLY its expiry nanosecond was lapsed before and must still be.
    """
    at_expiry = iso_to_epoch_nanos(GOOD["expires_at"])

    assert subject.lapsed_seconds(swap(), at_expiry) == 0.0, "the boundary itself is lapsed"
    assert subject.lapsed_seconds(swap(), at_expiry - 1) < 0, "one nanosecond before is not"
    assert subject.lapsed_seconds(swap(), LAPSED) == pytest.approx(60.0), (
        "17:11:00 is sixty seconds past a 17:10:00 window"
    )


def test_an_honored_lapse_is_announced_and_an_ordinary_run_is_not():
    """Rule 14: an opt-in that moves money must not be invisible in the output.

    MUTATION: delete the lapsed_quote_lines() spread from preflight()'s print
    loop, or make the function return [] unconditionally, and the first assertion
    fails -- while every refusal test above still passes, because suppressing the
    refusal is the half that is easy to get right.
    """
    honored = subject.lapsed_quote_lines(swap(), LAPSED, honor_lapsed_quote=True)
    joined = " ".join(honored)

    assert honored, "an honored lapse has to say so"
    assert "HONORED" in joined
    assert str(GOOD["quoted_rate"]) in joined, "the rate being honored is the point"
    assert GOOD["expires_at"] in joined, "and when it was quoted"

    assert subject.lapsed_quote_lines(swap(), LAPSED, honor_lapsed_quote=False) == [], (
        "without the flag the lapse is refuse()'s to report, not this function's -- both "
        "speaking would print it twice"
    )
    assert subject.lapsed_quote_lines(swap(), NOW, honor_lapsed_quote=True) == [], (
        "a swap inside its window has nothing to say; a line on every ordinary run is the "
        "noise an operator learns to scroll past"
    )


def test_a_swap_older_than_the_ledgers_dedup_window_is_refused():
    """MUTATION: drop the 24h check.

    created_at is the idempotency key, so a swap older than the window cannot be
    paid idempotently at all -- the ledger answers TxTooOld and nothing moves.
    Paying it needs a key from somewhere other than the swap, which forfeits
    dedup, and that is a live-posture decision rather than this tool's.
    """
    much_later = NOW + subject.DEDUP_WINDOW_NANOS
    reasons = subject.refuse(swap(), [], much_later)
    assert any("TxTooOld" in r for r in reasons)


def test_the_dedup_window_is_the_measured_twenty_four_hours():
    """Pinned against the ledger's own reply, recorded in chains/icp.py."""
    assert subject.DEDUP_WINDOW_NANOS == 86_400_000_000_000


@pytest.mark.parametrize("amount", [0, 0.0, None])
def test_a_swap_with_no_amount_to_send_is_refused(amount):
    reasons = subject.refuse(swap(expected_input_amount=amount), [], NOW)
    assert any("no amount to send" in r for r in reasons)


def test_every_reason_is_reported_not_just_the_first():
    """An operator who fixes one reason and meets a second has learned nothing.

    This swap is wrong three ways at once, and all three must be named in one
    run.
    """
    reasons = subject.refuse(
        swap(from_asset="BTC", status="completed", expected_input_amount=0), [], NOW
    )
    assert len(reasons) >= 3


# ---------------------------------------------------------------------------
# THE LOOKUP, AND THE ABSENCE OF A RECENCY FALLBACK


def seeded_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = dict_factory
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps, "
        "network_fee_reserve, output_amount_estimate, expires_at, created_at) "
        "VALUES (?, 'ICP', 'GRC', 2.42621078, 1.0, 150, 0.01, 2.0, ?, ?)",
        (GOOD["quote_id"], GOOD["expires_at"], GOOD["created_at"]),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address, "
        "expected_input_amount, quoted_rate, fee_bps, network_fee_reserve, "
        "output_amount_estimate, status, min_confirmations, created_at, updated_at, expires_at) "
        "VALUES (?, ?, 'ICP', 'GRC', ?, ?, ?, 1.0, 150, 0.0001, 2.0, ?, 1, ?, ?, ?)",
        (
            GOOD["id"], GOOD["quote_id"], GOOD["deposit_address"], GOOD["payout_address"],
            GOOD["expected_input_amount"], GOOD["status"], GOOD["created_at"],
            GOOD["created_at"], GOOD["expires_at"],
        ),
    )
    return conn


def test_the_swap_is_found_by_its_deposit_address():
    """The selector the operator is actually reading off the page."""
    conn = seeded_db()
    try:
        found = subject.find_swap(conn, deposit_address=GOOD["deposit_address"])
        assert found is not None
        assert found["id"] == GOOD["id"]
    finally:
        conn.close()


def test_the_swap_is_found_by_its_id():
    conn = seeded_db()
    try:
        assert subject.find_swap(conn, swap_id=GOOD["id"])["id"] == GOOD["id"]
    finally:
        conn.close()


def test_an_unknown_address_returns_none_rather_than_the_newest_swap():
    """MUTATION: add an `ORDER BY created_at DESC LIMIT 1` fallback.

    THIS IS THE 82.65 tGRC TEST. pay_test_deposit.py's header records what the
    fallback cost: with no new swap created, "the newest awaiting_deposit row"
    was a swap from three hours earlier with an autofilled Bitcoin testnet payout
    address, and the payment went to it. `ismine: false`.

    A database holding exactly one swap is the case where a recency fallback
    looks harmless and is not: it returns that swap for ANY address typed.
    """
    conn = seeded_db()
    try:
        assert subject.find_swap(conn, deposit_address="d" * 64) is None
        assert subject.find_swap(conn, swap_id="s_nonexistent") is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# READING THE LEDGER'S REPLY


def test_a_block_index_reads_as_sent():
    moved, lines = subject.send_outcome("(variant { Ok = 42 : nat64 })")
    assert moved
    assert any("block index 42" in line for line in lines)


def test_txduplicate_reads_as_sent_because_it_is():
    """The ledger is saying "this exact transfer already happened; here is where".

    chains/icp.py shipped a version that RAISED on this and records why that was
    a defect: a worker retrying after a timeout marked a payout that SUCCEEDED as
    failed. The whole point of the idempotency key is to produce this reply.
    """
    reply = "(variant { Err = variant { TxDuplicate = record { duplicate_of = 7 : nat64 } } })"
    moved, lines = subject.send_outcome(reply)
    assert moved, "TxDuplicate must read as success; it is what dedup looks like"
    assert any("7" in line for line in lines)


def test_a_reply_with_no_block_index_does_not_claim_either_outcome():
    """MUTATION: make this say "failed".

    "Whether anything moved is NOT established" is the honest reading, and the
    difference matters: a caller told "failed" retries with a fresh key.
    """
    moved, lines = subject.send_outcome("(variant { Err = variant { Something = null } })")
    assert not moved
    joined = " ".join(lines)
    assert "NOT established" in joined
    assert "failed" not in joined.lower()


def test_badfee_is_named_and_not_retried():
    """A retry with a different fee is a different transfer under the dedup key."""
    reply = "(variant { Err = variant { BadFee = record { expected_fee = record { e8s = 10_000 } } } })"
    moved, lines = subject.send_outcome(reply)
    assert not moved
    assert any("BadFee" in line and "NOT retried" in line for line in lines)


# ---------------------------------------------------------------------------
# THE BANNER, AND THE EXIT CODES


class Args:
    """What parse_args() hands preflight() and banner_lines(), as a stub.

    THE SELECTORS WERE MISSING UNTIL 2026-10-11 and nothing noticed, because the
    only test that called preflight() with this stub refused on the ledger id
    first and never reached the lookup. The two tests that now run preflight far
    enough to print found it immediately -- an AttributeError on
    `args.deposit_address`. A fixture that cannot reach the code it stands in for
    is a fixture that looks complete and is not.

    Both default EMPTY, matching parse_args()' own defaults. The real parser makes
    the pair mutually exclusive AND required, so a real run always has exactly
    one; a stub with neither is only ever used where load_swap is replaced.
    """

    def __init__(self, apply=False, identity="", honor_lapsed_quote=False,
                 deposit_address="", swap=""):
        self.apply = apply
        self.identity = identity
        self.deposit_address = deposit_address
        self.swap = swap
        #: Defaulted False here for the same reason parse_args() defaults it False:
        #: a stub that defaulted it True would make every banner test assert the
        #: honoring form, and the one state that must never be reached by accident
        #: would be the one the fixtures exercise.
        self.honor_lapsed_quote = honor_lapsed_quote


ICP_SETTINGS = {
    "ledger_canister_id": "bkyz2-fmaaa-aaaaa-qaaaq-cai",
    "service": "icp-replica",
    "timeout": 60.0,
}


def test_the_banner_says_whether_funds_will_move():
    """Rule 14: the parameter that decides the answer, before anything happens."""
    assert "DRY RUN" in subject.banner_lines(Args(apply=False), ICP_SETTINGS)[0]
    assert "funds WILL move" in subject.banner_lines(Args(apply=True), ICP_SETTINGS)[0]


def test_the_banner_says_when_a_lapsed_quote_is_being_honored():
    """Rule 14: echo the parameters that decide the answer, in the line read first.

    Pasted output has to be self-describing a day later, and "did the desk agree
    to honor a stale rate" is the question a reader of that paste will have.

    MUTATION: drop the `lapsed` suffix from banner_lines() and this fails while
    every other banner test passes.
    """
    assert "LAPSED" not in subject.banner_lines(Args(), ICP_SETTINGS)[0]
    honoring = subject.banner_lines(Args(honor_lapsed_quote=True), ICP_SETTINGS)[0]
    assert "HONORING A LAPSED QUOTE" in honoring
    assert "DRY RUN" in honoring, "the mode must survive beside it"


def test_the_flag_is_off_unless_it_is_typed():
    """The default is the refusal, and it is asserted through the real parser.

    Through parse_args() rather than the Args stub, because the stub's default is
    a test fixture's opinion and this is the production one. A flag that moves
    money must not be reachable by forgetting something.
    """
    required = ["--deposit-address", GOOD["deposit_address"]]

    assert subject.parse_args(required).honor_lapsed_quote is False
    assert subject.parse_args([*required, "--honor-lapsed-quote"]).honor_lapsed_quote is True


def test_the_banner_names_the_ledger_and_distinguishes_it_from_mainnet():
    """The IC has no testnet, so the ledger id IS the network statement."""
    joined = " ".join(subject.banner_lines(Args(), ICP_SETTINGS))
    assert "bkyz2-fmaaa-aaaaa-qaaaq-cai" in joined
    assert "ryjl3-tyaaa-aaaaa-aaaba-cai" in joined, "mainnet's id must be named to contrast with"


def test_the_banner_says_which_identity_signs():
    assert "dfx default identity" in " ".join(subject.banner_lines(Args(), ICP_SETTINGS))
    named = " ".join(subject.banner_lines(Args(identity="desk"), ICP_SETTINGS))
    assert "--identity desk" in named


def test_configuration_faults_exit_2_and_state_faults_exit_1():
    """So a missing ledger id does not read like an expired swap."""
    assert subject.Refused(2, ["config"]).code == 2
    assert subject.Refused(1, ["state"]).code == 1


def test_preflight_actually_prints_the_honored_lapse(monkeypatch, capsys):
    """The wiring, not the function -- asserted on what preflight() put on the screen.

    THE GAP THIS CLOSES WAS REAL AND I LEFT IT OPEN FOR A WHILE.
    test_an_honored_lapse_is_announced_and_an_ordinary_run_is_not() calls
    lapsed_quote_lines() directly, so deleting the spread that puts those lines in
    preflight()'s print loop passed every test in this file. A function that
    returns the right sentences and a tool that prints them are two different
    claims, and "the code contains a check for X" is never evidence X happened
    (CLAUDE.md's "Verify by behavior" section).

    load_swap and build_adapters are replaced so this reaches the print without a
    database or a replica: the swap is handed in, and the adapter build then
    refuses with code 2, which is the real refusal for a tree that cannot build an
    ICP adapter. Everything between -- swap_lines(), lapsed_quote_lines(), refuse()
    -- is the real code running in the real order.

    MUTATION: delete `*lapsed_quote_lines(...)` from preflight()'s list and this
    fails; every other test in this file still passes.
    """
    monkeypatch.setattr(subject, "load_swap", lambda *_a, **_k: (swap(), []))
    monkeypatch.setattr(subject, "build_adapters", lambda *_a, **_k: {})
    # The clock, so the swap is PAST its window rather than depending on today's date.
    monkeypatch.setattr(subject, "utc_now_iso", lambda: "2026-10-10T17:11:00+00:00")

    with pytest.raises(subject.Refused) as caught:
        subject.preflight(Args(honor_lapsed_quote=True), ICP_SETTINGS)

    printed = capsys.readouterr().out
    assert caught.value.code == 2, (
        "it got PAST refuse() -- the lapse was honored -- and stopped on the adapter build"
    )
    assert "LAPSED QUOTE" in printed, f"the honored lapse has to reach the screen:\n{printed}"
    assert "HONORED" in printed
    assert str(GOOD["quoted_rate"]) in printed


def test_preflight_says_nothing_about_a_lapse_it_is_not_honoring(monkeypatch, capsys):
    """The other direction: the refusal reports it, so the swap block must not.

    Both speaking would print the lapse twice in one run, which is the noise half
    of rule 14 -- an operator counting problems reads two.
    """
    monkeypatch.setattr(subject, "load_swap", lambda *_a, **_k: (swap(), []))
    monkeypatch.setattr(subject, "build_adapters", lambda *_a, **_k: {})
    monkeypatch.setattr(subject, "utc_now_iso", lambda: "2026-10-10T17:11:00+00:00")

    with pytest.raises(subject.Refused) as caught:
        subject.preflight(Args(), ICP_SETTINGS)

    printed = capsys.readouterr().out
    assert caught.value.code == 1, "a lapsed swap with no flag is a STATE refusal, not config"
    assert "LAPSED QUOTE  HONORED" not in printed
    assert printed.count("lapsed") == 0, (
        f"refuse() reports it, into Refused.lines, which main() prints to stderr -- not here:"
        f"\n{printed}"
    )
    assert any("lapsed" in line for line in caught.value.lines), (
        "and it IS reported, as a refusal"
    )


def test_an_unset_ledger_id_refuses_with_code_2_and_says_how_to_read_it():
    """Environment state, so there is no default worth guessing."""
    with pytest.raises(subject.Refused) as caught:
        subject.preflight(Args(), {**ICP_SETTINGS, "ledger_canister_id": ""})
    assert caught.value.code == 2
    assert any("canister_ids.json" in line for line in caught.value.lines)


# ---------------------------------------------------------------------------
# THE COMMANDS THIS TOOL PRINTS MUST ACTUALLY RUN


def test_the_confirm_command_matches_show_swaps_real_interface():
    """MUTATION: drop the `--swap` and print the id positionally.

    MEASURED 2026-10-10, ON THE OPERATOR'S HOST, ONE LINE AFTER A SUCCESSFUL
    SEND: this tool printed `python3 show_swap.py s_968a69b37c3da5c9` and the
    operator got back

        usage: show_swap.py [-h] [--swap ID] [--db DB]
        show_swap.py: error: unrecognized arguments: s_968a69b37c3da5c9

    show_swap.py takes `--swap ID`. The positional form never worked.

    WHY THIS IS WORSE THAN AN ORDINARY WRONG COMMENT. It is the line an operator
    copies at the one moment they most want confirmation -- funds have just left,
    and the next thing they do is ask where the swap went. A command that errors
    there reads as the SWAP having gone wrong rather than the instruction, which
    is the opposite of what rule 14 asks output to do.

    Asserted against show_swap.py's own argparse source rather than a literal, so
    renaming the flag there fails HERE rather than on somebody's terminal.
    """
    root = Path(__file__).resolve().parent.parent
    show_swap = (root / "show_swap.py").read_text()
    assert '"--swap"' in show_swap, (
        "show_swap.py no longer declares --swap, so this tool's printed confirm command "
        "needs updating to whatever replaced it"
    )

    printed = [
        line
        for line in (root / "pay_icp_deposit.py").read_text().splitlines()
        if "show_swap.py" in line and not line.strip().startswith("#")
    ]
    assert printed, "the confirm line vanished; an operator now has nothing to run after a send"
    for line in printed:
        assert "show_swap.py --swap" in line, (
            f"this tool prints a show_swap.py command without --swap, which show_swap.py "
            f"rejects as an unrecognized argument: {line.strip()}"
        )


def test_every_script_this_tool_tells_an_operator_to_run_exists():
    """A printed command pointing at a missing file reads as the operator's error.

    Same class as the confirm-flag defect: the tool names fund_desk.py in its
    top-up hint and show_swap.py in its confirm line, and a rename of either
    would otherwise be discovered by somebody pasting it.

    SCOPED TO `print(` LINES, AND THE FIRST VERSION WAS NOT. It scanned every
    word in the file ending `.py`, which swept up `db.py` out of an import and
    failed looking for it at the repository root -- where it correctly is not,
    since it lives in swap_terminal/. A detector that reads the whole file to
    answer a question about printed output is the prose-reading mistake this tree
    has made five times (HANDOFF.md section 6), and I made it a sixth time here
    before the test ran. What this tool PRINTS is what a `print(` line contains.
    """
    root = Path(__file__).resolve().parent.parent
    printed = [
        line
        for line in (root / "pay_icp_deposit.py").read_text().splitlines()
        if "print(" in line and ".py" in line
    ]
    assert printed, "the tool prints no script names at all, so this gate measures nothing"

    named = set()
    for line in printed:
        named.update(
            word.strip("'\"`,()")
            for word in line.split()
            if word.strip("'\"`,()").endswith(".py")
        )
    assert named, f"no .py name parsed out of {len(printed)} printed line(s)"
    for script in sorted(named):
        assert (root / script).is_file(), (
            f"pay_icp_deposit.py PRINTS {script} for an operator to run, but it is not at "
            f"the repository root"
        )
