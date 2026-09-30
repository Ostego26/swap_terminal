"""The customer-facing display decisions, called with seeded swaps.

Role: test (behavioral verification of services/swap_view.py)
Reads: nothing -- every input is a dict built in this file
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- no socket, no database, no chain

WHAT THESE TESTS ARE FOR. services/swap_view.py decides what a customer sees
while they wait on a blockchain, and the failures worth catching are silent
ones: a status that renders as nothing, a stalled payout that renders like a
healthy wait, an XRP swap that renders an address box it should refuse to show.
None of those raises, so only an assertion finds them.

MUTATION-CHECKED. Every guard below was broken on purpose, one at a time, and
the test named beside it failed. The list is in the commit message; each test's
docstring names the edit it catches.
"""

from datetime import datetime, timedelta

import pytest
from services import swap_view
from services.swap_service import (
    DEPOSIT_TAG_COLUMN,
    TAG_ATTRIBUTED_ASSETS,
    TAG_ATTRIBUTION,
)
from services.swap_view import (
    STAGE_ORDER,
    STALL_AFTER_SECONDS,
    attention,
    confirmation_progress,
    deposit_instruction,
    quote_window,
    shouted_discriminator,
    stage_rail,
    status_meaning,
    swap_display,
    threshold_note,
)
from valid_addresses import BTC_PARTICIPANT, GRC_PAYOUT, XRP_HOT_ACCOUNT
from workers.payout_worker import DEFAULT_POLL_SECONDS as PAYOUT_POLL

NOW = "2026-09-26T12:00:00+00:00"


def seconds_before(seconds: float) -> str:
    """An ISO timestamp `seconds` before NOW, for seeding `updated_at`."""
    return (datetime.fromisoformat(NOW) - timedelta(seconds=seconds)).isoformat()


def make_swap(**overrides) -> dict:
    swap = {
        "id": "s_0000000000000001",
        "status": "awaiting_deposit",
        "from_asset": "GRC",
        "to_asset": "BTC",
        "deposit_address": GRC_PAYOUT,
        "payout_address": BTC_PARTICIPANT,
        "expected_input_amount": 1000.0,
        "actual_input_amount": None,
        "output_amount_estimate": 0.001,
        "min_confirmations": 6,
        "updated_at": seconds_before(30),
        "created_at": seconds_before(600),
        "expires_at": seconds_before(-600),
        "deposit_events": [],
        "payouts": [],
    }
    swap.update(overrides)
    return swap


# --- the statuses are the REAL ones -----------------------------------------


def test_every_status_the_code_writes_has_a_meaning():
    """The eight statuses actually written to swaps.status all render.

    These were read out of services/deposit_service.py,
    services/swap_service.py and services/payout_service.py, not guessed. If a
    future change adds a ninth, this list is what has to be updated with it --
    and status_meaning() reports an unmapped value AS unmapped rather than
    hiding it, which the next test pins.
    """
    written_by_the_application = (
        "awaiting_deposit",
        "deposit_seen",
        "confirming",
        "payout_pending",
        "under_review",
        "paying",
        "completed",
        "failed",
    )
    for status in written_by_the_application:
        meaning = status_meaning(status)
        assert meaning["known"] is True, status
        assert meaning["headline"], status
        assert meaning["detail"], status
        assert meaning["kind"] in {"waiting", "working", "done", "halted", "failed"}, status


def test_expired_is_not_a_swap_status_in_this_tree():
    """`expired` belongs to the Express bridge's intents table, not to swaps.

    Measured by grep: every occurrence of the literal is in
    swap_intents_schema.py or migrate_swap_intents.py. Nothing sets
    swaps.status = 'expired'. So this module must treat it as unrecognized
    rather than quietly giving it a friendly meaning -- which would tell a
    customer their swap was canceled by a clock that does not exist.
    """
    assert status_meaning("expired")["known"] is False


def test_an_unknown_status_renders_as_unknown_and_shows_the_raw_value():
    """MUTATION: make status_meaning() fall back to the awaiting_deposit entry.

    A `.get(status, STATUS_MEANINGS["awaiting_deposit"])` reads as a harmless
    default and is the exact failure rule 14 forbids: a status nobody mapped
    would render as a normal wait, forever, for every customer holding one. This
    test fails on that edit.
    """
    meaning = status_meaning("something_new")
    assert meaning["known"] is False
    assert meaning["kind"] == "unknown"
    assert "something_new" in meaning["headline"]

    # An empty status is also unknown, and does not render an empty headline.
    assert status_meaning("")["known"] is False
    assert "(empty)" in status_meaning("")["headline"]


# --- the rail ---------------------------------------------------------------


def test_the_rail_marks_done_current_and_future_in_order():
    rail = stage_rail("confirming")
    states = [step["state"] for step in rail]
    assert states == ["done", "done", "current", "future", "future", "future"]
    assert [step["key"] for step in rail] == list(STAGE_ORDER)


def test_a_status_off_the_rail_marks_every_step_skipped():
    """MUTATION: make stage_rail() fall through to position 0 for under_review.

    Drawing a halted swap as "on step one of six" says it is progressing. Every
    step must read `skipped`, and swap_display() must report on_rail=False so
    the template can say the sequence is not running.
    """
    for status in ("under_review", "failed", "not_a_status"):
        rail = stage_rail(status)
        assert {step["state"] for step in rail} == {"skipped"}, status
        assert swap_display(make_swap(status=status), NOW)["on_rail"] is False, status

    assert swap_display(make_swap(status="confirming"), NOW)["on_rail"] is True


# --- confirmations are counts ----------------------------------------------


def test_confirmation_progress_reports_the_highest_depth_and_the_threshold():
    swap = make_swap(
        status="confirming",
        min_confirmations=6,
        deposit_events=[
            {"confirmations": 1, "amount": 500.0, "txid": "a", "vout": 0},
            {"confirmations": 4, "amount": 500.0, "txid": "a", "vout": 1},
        ],
    )
    progress = confirmation_progress(swap)
    assert progress["seen"] == 4
    assert progress["threshold"] == 6
    assert progress["remaining"] == 2
    assert progress["met"] is False
    assert progress["rows"] == 2


def test_zero_confirmations_renders_as_zero_and_not_as_nothing():
    """`0 of 6` is a measurement; a blank is not (rule 14).

    MUTATION: make confirmation_progress() return None when there are no
    deposit rows. The template would then render an empty panel, which reads as
    a broken page rather than as "nothing has arrived yet".
    """
    progress = confirmation_progress(make_swap(deposit_events=[]))
    assert progress["seen"] == 0
    assert progress["threshold"] == 6
    assert progress["rows"] == 0
    assert progress["percent"] == 0


def test_the_threshold_is_never_rendered_as_a_duration():
    """Rule 6: confirmations are counts. No microfortnight symbol anywhere near them.

    MUTATION: pass the threshold through microfortnights.format_duration(). A
    threshold rendered as "5.0µfn" invents a precision the chain does not have,
    and it is exactly the kind of edit that looks like consistency.
    """
    progress = confirmation_progress(make_swap(deposit_events=[{"confirmations": 2, "amount": 1.0, "txid": "a", "vout": 0}]))
    rendered = " ".join(str(value) for value in progress.values())
    assert "µfn" not in rendered
    assert "ufn" not in rendered


@pytest.mark.parametrize(
    ("asset", "threshold", "must_contain", "must_not_contain"),
    [
        ("GRC", 6, "blocks", "commitment"),
        ("BTC", 2, "blocks", "validated ledger"),
        ("SOL", 3, "commitment ladder", "blocks must"),
        # The XRP note DOES contain the word "blocks" -- in the clause saying
        # there is no number of them to wait for. What must never appear is the
        # CLAIM, "blocks must be mined", so that is what is asserted against,
        # rather than the substring. A test that banned the word would force the
        # explanation to stop explaining.
        ("XRP", 1, "validated ledger", "blocks must"),
    ],
)
def test_the_threshold_note_says_what_the_number_means_per_chain(asset, threshold, must_contain, must_not_contain):
    """Three different things share the name min_confirmations (rule 6, rule 11).

    MUTATION: delete the XRP branch of threshold_note(). The customer's
    confirmation panel then reads "1 blocks must be mined on top of the deposit"
    over an XRP swap, which is false about a ledger that does not reorganize --
    and false quietly, because the page still renders.
    """
    note = threshold_note(asset, threshold)
    assert must_contain in note
    assert must_not_contain not in note


def test_a_missing_threshold_says_so_rather_than_reading_as_zero():
    assert "no threshold is configured" in threshold_note("GRC", None)


# --- deposit attribution differs per chain ---------------------------------


def test_a_bitcoin_style_chain_attributes_by_address():
    deposit = deposit_instruction(make_swap(from_asset="GRC"))
    assert deposit["model"] == "address"
    assert deposit["address"] == GRC_PAYOUT
    assert deposit["tag"] is None
    assert deposit["problem"] == ""


def test_xrp_attributes_by_destination_tag_and_refuses_without_one():
    """MUTATION: give XRP the "address" model, or default an absent tag to 0.

    Either edit produces a page that invites a customer to send XRP that cannot
    be matched to their swap -- the first by showing an address box for a chain
    where the address is shared, the second by printing a tag that was never
    issued. `0` is a legal DestinationTag (README.md), so a default of 0 is not
    a harmless placeholder, it shadows a real value.

    THE KEY IS DERIVED FROM DEPOSIT_TAG_COLUMN, and that is the whole repair here.

    This test used to seed `destination_tag=4242` -- a key the schema does not
    have. The column is `deposit_tag`. So the test was written against the same
    guess as the code it checked, and confirmed it: both agreed on a name neither
    had read from db.py. The suite was green while a real XRP swap rendered "NO
    DESTINATION TAG HAS BEEN ISSUED" and told the customer not to send anything,
    found on 2026-09-26 by the operator opening the page on a swap they created.

    It failed SAFE -- the page refused to show a send target rather than showing a
    wrong one -- but the swap was unusable, and no test could have caught it while
    the fixture spelled the key the same wrong way as the reader.

    Seeding through the constant means a future rename breaks this test at the
    rename rather than at the customer.
    """
    # XRP_HOT_ACCOUNT, not a spelled-out `r...` literal, and not make_swap()'s GRC default.
    # Since 2026-09-28 this branch reports an account that cannot receive a deposit (the
    # `address` branch has since 2026-09-27), and make_swap()'s default deposit_address is
    # GRC_PAYOUT -- a base58 testnet address, which is not an XRP account at all. Every
    # assertion below about `problem` would otherwise have been reading an ADDRESS complaint
    # while claiming to measure the TAG. Derived in tests/valid_addresses.py.
    swap = make_swap(from_asset="XRP", deposit_address=XRP_HOT_ACCOUNT)
    deposit = deposit_instruction(swap)
    # THE MODEL NO LONGER KEEPS THE XRP LEDGER'S OWN TERM, and this line used to say it did.
    # `destination_tag` as a model name is what kept Solana out of the model it belongs to: SOL
    # attributes by a Memo instruction, has no destination tag, and fell into the template's
    # {% else %} -- a page explaining attribution with no send target on it. The chain's own
    # word arrives per asset now, from services/swap_service.TAG_ATTRIBUTION, and the
    # CUSTOMER-FACING WORDING BELOW IS UNCHANGED, which is the assertion that matters here.
    assert deposit["model"] == "tag"
    assert deposit["discriminator"] == "DestinationTag", "the chain's own field name, per asset"
    assert deposit["tag"] is None
    assert deposit["problem"], "a swap with no tag issued must say so, loudly"
    assert "NO DESTINATION TAG" in deposit["problem"]

    # The shape services/swap_service.allocate_destination_tag() actually writes.
    with_tag = deposit_instruction(make_swap(from_asset="XRP", deposit_address=XRP_HOT_ACCOUNT, **{DEPOSIT_TAG_COLUMN: 4242}))
    assert with_tag["tag"] == 4242
    assert with_tag["problem"] == ""

    # Tag 0 is a REAL tag and must not be reported as missing.
    zero = deposit_instruction(make_swap(from_asset="XRP", deposit_address=XRP_HOT_ACCOUNT, **{DEPOSIT_TAG_COLUMN: 0}))
    assert zero["tag"] == 0
    assert zero["problem"] == ""

    # And the wrong spelling must NOT work, or this test would pass again the day
    # somebody reintroduces it.
    wrong = deposit_instruction(make_swap(from_asset="XRP", deposit_address=XRP_HOT_ACCOUNT, destination_tag=4242))
    assert wrong["tag"] is None, "only the real column may satisfy the reader"
    assert "NO DESTINATION TAG" in wrong["problem"]




def test_a_chain_with_no_attribution_model_refuses_to_say_where_to_send():
    """MUTATION: make ATTRIBUTION_MODELS.get() default to "address".

    A chain whose custody model has not been decided would then render a send target derived
    from a column that has no meaning for it. Refusing is the only safe answer.

    SEEDED WITH A CHAIN THAT DOES NOT EXIST, AND IT USED TO BE SEEDED WITH SOL. That was
    correct on 2026-09-27 and false by 2026-09-30: the operator chose the one-account-plus-memo
    strategy on 2026-09-29 (commit d22b2a1), SOL joined TAG_ATTRIBUTED_ASSETS, and this test
    was still asserting its attribution was unknown -- pinning a claim the code had already
    refuted. Rule 2: a test for behavior that has been replaced changes to pin the stronger
    invariant, and the stronger invariant is the one below rather than this one.
    """
    deposit = deposit_instruction(make_swap(from_asset="ZZZ"))
    assert deposit["model"] == "unknown"
    assert deposit["problem"]


def test_no_tag_attributed_asset_can_render_as_undecided():
    """THE STRONGER INVARIANT, and the one that would have caught this twice.

    ATTRIBUTION_MODELS' tag entries are derived from TAG_ATTRIBUTED_ASSETS, so a chain cannot
    be credited by tag in services/deposit_service and simultaneously told "we do not know how
    your deposit is attributed" on its own page. That is not a hypothetical pairing: it is
    exactly what SOL did between 2026-09-29 and 2026-09-30, and what a different chain did on
    2026-09-27, both because this mapping was hand-maintained beside a set that already knew.

    MUTATION: write ATTRIBUTION_MODELS' tag entries out as literals again -- which is how both
    defects arrived -- and adding the third tag chain fails here instead of on a customer's page.
    """
    for asset in sorted(TAG_ATTRIBUTED_ASSETS):
        deposit = deposit_instruction(make_swap(from_asset=asset, deposit_tag=7))
        assert deposit["model"] == "tag", f"{asset} is credited by tag and must render as such"
        assert asset in deposit["note"]

        # EACH CHAIN'S OWN FIELD NAME, AND NOT ANY OTHER CHAIN'S. Asserting only that
        # `discriminator` is non-empty let a mutation through: hardcoding "DestinationTag" at
        # the call site survived the whole suite, which is the precise defect this rename
        # exists to prevent -- a Solana customer told to set a field the Solana blockchain does
        # not have, on the page that tells them where to send money. Compared against
        # TAG_ATTRIBUTION per asset, and every OTHER chain's field name is asserted absent,
        # because "contains the right word" does not exclude also containing the wrong one.
        _, expected, network = TAG_ATTRIBUTION[asset]
        assert deposit["discriminator"] == expected
        assert deposit["network"] == network
        assert shouted_discriminator(expected) in deposit["note"]
        for other, (_, foreign, _) in TAG_ATTRIBUTION.items():
            if other != asset and foreign != expected:
                assert foreign not in deposit["note"], (
                    f"{asset}'s deposit page names {other}'s field {foreign!r}"
                )
                assert shouted_discriminator(foreign) not in deposit["note"]


# --- waiting versus stalled -------------------------------------------------


def test_a_fresh_wait_is_not_slow_and_says_whether_anything_is_happening():
    """Within the threshold the level is the status's own kind, not a flat "waiting".

    MUTATION: return a constant "waiting" for every non-terminal status, which
    is what this function did until 2026-09-26 -- caught by
    tests/test_web_surfaces.py asserting on the rendered class, not here. A swap
    waiting on a CUSTOMER to send coins then renders identically to one whose
    confirmations are actively arriving. Both are healthy; only one of them is
    something happening, and rule 14's "make 'did nothing' look different from
    'did work'" is that difference.
    """
    for status in ("deposit_seen", "confirming", "payout_pending", "paying"):
        verdict = attention(make_swap(status=status, updated_at=seconds_before(5)), NOW)
        assert verdict["level"] == "working", status

    idle = attention(make_swap(status="awaiting_deposit", updated_at=seconds_before(5)), NOW)
    assert idle["level"] == "waiting"


def test_awaiting_deposit_never_goes_slow_however_long_it_waits():
    """It is waiting on a person, and a person is allowed to take a week.

    MUTATION: add "awaiting_deposit" to STALL_AFTER_SECONDS. Every unfunded swap
    would then eventually display an alarm about a system that is working
    perfectly, which is the log that cries wolf in page form.
    """
    assert "awaiting_deposit" not in STALL_AFTER_SECONDS
    verdict = attention(make_swap(status="awaiting_deposit", updated_at=seconds_before(86400 * 7)), NOW)
    assert verdict["level"] == "waiting"


def test_a_payout_pending_swap_goes_slow_past_the_threshold():
    """MUTATION: change the `>` in attention() to `<`, or drop the comparison.

    This is the single most valuable signal on the page. A swap credited and
    waiting on a payout worker that is not running looks, without this, exactly
    like a swap credited four seconds ago -- and that is the shape of every
    incident in this repository's history: success-looking output over work that
    is not happening.
    """
    limit = STALL_AFTER_SECONDS["payout_pending"]
    ok = attention(make_swap(status="payout_pending", updated_at=seconds_before(limit - 1)), NOW)
    slow = attention(make_swap(status="payout_pending", updated_at=seconds_before(limit + 1)), NOW)
    assert ok["level"] == "working"
    assert slow["level"] == "slow"
    assert "payout worker" in slow["detail"]
    # The elapsed figure and the threshold are BOTH on screen, in microfortnights
    # with the seconds beside them (rule 6, rule 14).
    assert "µfn" in slow["detail"]
    assert "ufn" not in slow["detail"].replace("µfn", "")


def test_the_worker_side_thresholds_are_longer_than_the_workers_poll_interval():
    """The thresholds are DERIVED from the workers, not picked (rule 3).

    Imported from the worker modules rather than restated here, so there is one
    authority for a poll interval. If somebody slows the payout worker to five
    minutes, this fails -- instead of the page quietly calling every healthy
    swap stalled.
    """
    assert STALL_AFTER_SECONDS["payout_pending"] > PAYOUT_POLL
    # And by a real margin, not by one second: a threshold barely above the poll
    # interval fires on ordinary jitter.
    assert STALL_AFTER_SECONDS["payout_pending"] >= PAYOUT_POLL * 5
    assert STALL_AFTER_SECONDS["paying"] > STALL_AFTER_SECONDS["payout_pending"]


def test_terminal_statuses_get_a_verdict_and_no_stall_clock():
    assert attention(make_swap(status="completed", updated_at=seconds_before(999999)), NOW)["level"] == "ok"
    assert attention(make_swap(status="under_review", updated_at=seconds_before(999999)), NOW)["level"] == "halted"
    assert attention(make_swap(status="failed", updated_at=seconds_before(999999)), NOW)["level"] == "failed"
    for status in ("completed", "under_review", "failed"):
        assert attention(make_swap(status=status), NOW)["waited"] is None


def test_done_waiting_slow_halted_and_failed_are_five_distinct_levels():
    """MUTATION: collapse `halted` into `failed` in attention().

    Held-for-review is a swap that stopped ON PURPOSE with nothing sent and
    nothing lost; failed is a payout that could not be broadcast. Rendering them
    the same tells a customer their money is gone when it is sitting safely in a
    queue for a human.
    """
    levels = {
        attention(make_swap(status=status, updated_at=seconds_before(1)), NOW)["level"]
        for status in ("completed", "awaiting_deposit", "confirming", "under_review", "failed")
    }
    assert levels == {"ok", "waiting", "working", "halted", "failed"}


def test_an_unreadable_updated_at_does_not_read_as_just_now():
    """MUTATION: return 0.0 instead of None from elapsed_seconds().

    Zero reads as "this happened a moment ago", so a corrupt timestamp would
    make a swap that has been stuck for hours display as freshly updated. The
    honest answer is to have no elapsed figure at all.
    """
    assert swap_view.elapsed_seconds("not-a-timestamp", NOW) is None
    assert swap_view.elapsed_seconds(None, NOW) is None
    verdict = attention(make_swap(status="payout_pending", updated_at="not-a-timestamp"), NOW)
    assert verdict["waited"] is None
    assert verdict["level"] == "working"


# --- the quote window is not an expiry --------------------------------------


def test_a_passed_quote_window_says_the_swap_was_not_canceled():
    """MUTATION: have quote_window() say the swap has expired.

    Measured: nothing in this tree sets swaps.status = 'expired' and no worker
    reads swaps.expires_at. A page that says "expired" over a swap whose deposit
    would still be credited tells a customer their money is lost, which is both
    false and the worst possible thing to be false about.
    """
    passed = quote_window(make_swap(expires_at=seconds_before(3600)), NOW)
    assert passed["passed"] is True
    assert "still detected and still credited" in passed["note"]
    assert "cancel" in passed["note"]

    live = quote_window(make_swap(expires_at=seconds_before(-300)), NOW)
    assert live["passed"] is False
    assert "µfn" in live["display"]


def test_an_unreadable_expiry_is_reported_as_unknown():
    window = quote_window(make_swap(expires_at="garbage"), NOW)
    assert window["known"] is False
    assert window["display"] == "(unknown)"
