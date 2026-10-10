"""The ICP idempotency key derivation. Wrong here means a second transfer.

Role: test (pure; no clock, no socket, no replica, no ledger)
Reads: swap_terminal/services/helpers.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS FUNCTION IS WORTH A TEST FILE OF ITS OWN.

The ICP ledger deduplicates a `transfer` on
(from, to, amount, fee, memo, created_at_time) for 24 hours, so
`created_at_time` IS the idempotency key. chains/icp.send_to_address() refuses to
send without one, and its docstring names what the value must be: the swap's
recorded creation time, reused unchanged on every retry.

So the property under test is not "is this the right instant". It is **is this
the SAME number every time, from every code path**. A derivation that is correct
to the microsecond but varies in its low digits across platforms gives a retry a
different key, and a different key is not a dedup -- it is a second transfer of
the same amount to the same address, which on a payout path is the double-pay
this repository has already measured once (CLAUDE.md's closing section: 2 sends,
1 swap_id, 2 'broadcast' rows).

MEASURED 2026-10-10, and this is the assertion the file exists for:

    "2026-10-10T17:37:52.186023+00:00"
      integer arithmetic   1791653872186023000
      round(ts * 1e9)      1791653872186022912     <- 88ns adrift

datetime.timestamp() returns a float64 with ~15-16 significant digits, and epoch
nanoseconds is 19 digits today. The low digits are not slightly wrong, they are
gone. The float route is the obvious implementation and it is the bug.
"""

from __future__ import annotations

import pytest
from services.helpers import iso_to_epoch_nanos, parse_iso, utc_now_iso

#: (timestamp, exact nanoseconds). Pinned as literals rather than recomputed,
#: because a test that derives the expected value the same way as the subject
#: passes under every possible implementation, including a broken one.
EXACT = [
    ("1970-01-01T00:00:00+00:00", 0),
    ("2026-10-10T17:37:52+00:00", 1791653872000000000),
    ("2026-10-10T17:37:52.186023+00:00", 1791653872186023000),
]


@pytest.mark.parametrize(("value", "expected"), EXACT)
def test_the_key_is_exactly_these_nanoseconds(value, expected):
    """MUTATION: swap the body for `round(parse_iso(value).timestamp() * 1e9)`."""
    assert iso_to_epoch_nanos(value) == expected


def test_the_float_route_is_measurably_wrong_and_this_one_is_not():
    """The 88ns the float64 mantissa cannot hold, asserted rather than described.

    THE POINT IS NOT THE SIZE OF THE ERROR. 88 nanoseconds is nothing as a time.
    As a dedup key it is everything: the ledger compares the field byte for byte,
    so two paths that disagree in the low digits produce two transfers.
    """
    value = "2026-10-10T17:37:52.186023+00:00"
    float_route = round(parse_iso(value).timestamp() * 1e9)
    assert float_route == 1791653872186022912, (
        "the float route's value changed, which means this platform's float handling "
        "differs from the one measured on 2026-10-10 -- which is itself the argument "
        "for not using it"
    )
    assert iso_to_epoch_nanos(value) == 1791653872186023000
    assert iso_to_epoch_nanos(value) != float_route


def test_the_same_timestamp_always_gives_the_same_key():
    """Idempotency is the whole product here, so it is asserted directly."""
    value = "2026-10-10T17:37:52.186023+00:00"
    assert iso_to_epoch_nanos(value) == iso_to_epoch_nanos(value)


def test_a_key_derived_from_created_at_always_ends_in_three_zeros():
    """A datetime carries microseconds and no finer.

    THIS IS HOW HANDOFF.md SECTION 4'S RECORDED KEY IS KNOWN NOT TO HAVE COME
    FROM `created_at`. It records 1791646582186023936 for a live ICP swap, and
    that is not a multiple of 1000, so no value this function can return. It came
    from a `time.time_ns()` call in a session instead.

    The consequence is operational rather than theoretical: retrying that swap
    through this function produces a DIFFERENT key, and a different key is a
    second transfer. It has to be retried with the original ad-hoc key or not at
    all. Asserting the property here is what makes that reasoning checkable
    rather than a claim in a document.
    """
    assert iso_to_epoch_nanos(utc_now_iso()) % 1000 == 0
    for value, _ in EXACT:
        assert iso_to_epoch_nanos(value) % 1000 == 0
    assert 1791646582186023936 % 1000 != 0, (
        "HANDOFF.md section 4's key is a multiple of 1000 after all, which would "
        "refute the reasoning in this test's docstring -- re-read it before editing"
    )


@pytest.mark.parametrize(
    "naive",
    [
        "2026-10-10T17:37:52.186023",
        "2026-10-10T17:37:52",
        "2026-10-10 17:37:52",
    ],
)
def test_a_naive_timestamp_is_refused_rather_than_assumed_to_be_utc(naive):
    """MUTATION: drop the tzinfo guard and let fromisoformat's naive value through.

    Guessing the zone shifts the key by up to 14 hours. For a swap more than ten
    hours old that pushes `created_at_time` outside the ledger's 24-hour window
    and earns `TxTooOld` -- AFTER the operator has been told the send is
    idempotent. Refusing names the real problem: the string did not come from
    this system, because helpers.utc_now() is `datetime.now(UTC)` and everything
    it writes is offset-aware.
    """
    with pytest.raises(ValueError, match="no UTC offset"):
        iso_to_epoch_nanos(naive)


def test_what_this_system_actually_writes_round_trips():
    """utc_now_iso() is the producer; this function is the consumer. Pinned together.

    Not a tautology: it asserts the two agree on FORMAT. If utc_now_iso ever
    stopped emitting an offset -- a switch to utcnow(), say -- this fails here
    rather than at a send, where the symptom would be TxTooOld on a swap the
    operator was told was fine.
    """
    key = iso_to_epoch_nanos(utc_now_iso())
    assert isinstance(key, int)
    # 2026 is ~1.79e18ns; this is a sanity band, not a clock assertion.
    assert 1.7e18 < key < 2.5e18


def test_an_offset_that_is_not_utc_is_converted_rather_than_truncated():
    """Same instant, same key, whatever offset the string carries.

    A caller that normalized by STRIPPING the offset instead of converting would
    pass the naive guard above and still produce the wrong number. These two
    strings are the same moment written two ways.
    """
    assert iso_to_epoch_nanos("2026-10-10T17:37:52+00:00") == iso_to_epoch_nanos(
        "2026-10-10T19:37:52+02:00"
    )
