"""A warning that says it is said ONCE per process is said once. Measured, not asserted.

Role: test
Reads: swap_terminal/failure_signature.py, swap_terminal/services/payout_service.py,
       and a 127.0.0.1 socket this file creates to generate REAL requests exceptions
Writes: a temporary SQLite database under pytest's tmp_path
Can move funds: no
Mainnet-safe: yes -- NO CHAIN, NO NETWORK. The only endpoint is a loopback socket
       this test opens and closes itself, and it exists solely to make the
       `requests` library raise a genuine ConnectTimeout rather than this test
       hand-writing one.

WHAT WENT WRONG, from the operator's host 2026-10-11, cycles 69 to 80.

    payout_worker cycle=70 IDLE ... in 50.3µfn (60.8s)
    21:20:10 WARNING wallet inventory for LTC NOT refreshed ... and this is said
             ONCE per process rather than every cycle: ...
    21:20:40 WARNING wallet inventory for GRC NOT refreshed ... and this is said
             ONCE per process rather than every cycle: ...
    payout_worker cycle=71 IDLE ... in 50.1µfn (60.6s)
    21:21:21 WARNING wallet inventory for LTC NOT refreshed ... and this is said
             ONCE per process rather than every cycle: ...

Eleven LTC and eleven GRC in fourteen minutes. The suppression keyed on
`(asset, str(exc)[:200])`, and `str()` of a requests ConnectTimeout embeds the
CPython address of a per-attempt HTTPConnection at index 182 -- inside the slice,
new every cycle -- while the reason it carries begins at index 205 and was cut off.
failure_signature.py's header has the full account and the six measured addresses.

WHY THESE TESTS USE A REAL SOCKET AND THE REAL FUNCTION.

A hand-written `Exception("... at 0x7f0000000000 ...")` would pass against the fix
and would ALSO have passed against the defect, because the defect is not in the
shape of the text -- it is in the fact that CPython hands out a different address
each time an object is built. Only a genuine exception from a genuine failed
connection reproduces that. So:

  _real_connect_timeouts()  makes N real requests.post() calls against a port whose
                            SYNs are dropped, and returns the exceptions. The first
                            assertion below is that their str() values DIFFER, which
                            is the defect's mechanism measured directly rather than
                            described.
  the end-to-end test       drives the real refresh_wallet_inventory() against a real
                            SQLite database with the real schema, counts real log
                            records through caplog, and asserts on the count. Not a
                            reimplementation of the suppression in the test.

CLAUDE.md's verify-by-behavior section asks for exactly this and names the failure
mode it prevents: "Never accept 'the code contains a check for X' as evidence X is
enforced." The old code contained the check. The check never fired.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
import time
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from db import SCHEMA, connect_db  # noqa: E402
from failure_signature import (  # noqa: E402
    MESSAGE_KEY_LENGTH,
    failure_signature,
    normalize_failure_text,
)

from swap_terminal.services import payout_service  # noqa: E402

#: Short enough that three real connect attempts cost under a second between them.
#: The DURATION is irrelevant to what is being measured -- only that each attempt
#: builds its own HTTPConnection object and so gets its own address.
TEST_CONNECT_TIMEOUT_SECONDS = 0.2


class _DroppingEndpoint:
    """A loopback port whose SYNs are DROPPED, via a filled listen backlog.

    THE SAME MECHANISM AS tests/test_rpc_connect_timeout._DroppingEndpoint, and rule 8
    requires both sites to say so. It is duplicated rather than imported because the
    two files measure different things -- that one measures the TIMEOUT, this one
    measures the EXCEPTION OBJECT -- and a shared fixture would couple a test about log
    cadence to a test about socket budgets. That file's docstring records the two
    mechanisms that do NOT produce a timeout (a refused port, an unroutable address)
    and why.
    """

    def __init__(self):
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._server.listen(1)
        self._fillers = []
        for _ in range(12):
            filler = socket.socket()
            filler.setblocking(False)
            # BlockingIOError IS the success case: an incomplete non-blocking connect
            # is what fills the accept queue.
            with contextlib.suppress(BlockingIOError):
                filler.connect(("127.0.0.1", self.port))
            self._fillers.append(filler)
        time.sleep(0.3)

    def close(self):
        for filler in self._fillers:
            filler.close()
        self._server.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _real_connect_timeouts(count: int) -> list[Exception]:
    """`count` GENUINE requests exceptions from `count` genuine failed connections.

    Each call builds its own HTTPConnection, which is the whole point: the addresses
    differ because CPython put the objects in different places, which is a fact about
    the runtime that no hand-written string can stand in for.
    """
    caught: list[Exception] = []
    with _DroppingEndpoint() as endpoint:
        for _ in range(count):
            try:
                requests.post(
                    f"http://127.0.0.1:{endpoint.port}/",
                    timeout=(TEST_CONNECT_TIMEOUT_SECONDS, 5.0),
                )
            except Exception as exc:  # noqa: BLE001 -- the exception IS the specimen this test collects. Narrowing it would mean naming the class in advance, and the assertion below checks which class actually arrived, so a surprise reads as a surprise.
                caught.append(exc)
    return caught


# --- the defect's mechanism, measured ---------------------------------------------


def test_two_real_connect_failures_produce_DIFFERENT_text():
    """The premise of the whole fix: identical failures do not have identical str().

    IF THIS TEST EVER FAILS, THE FIX BELOW IS UNNECESSARY AND THE REST OF THIS FILE IS
    MEASURING NOTHING. That is why it is first and why it is separate: it pins the
    runtime behavior the suppression has to survive, rather than assuming it.
    """
    failures = _real_connect_timeouts(3)
    assert len(failures) == 3, (
        f"expected 3 failed connections, got {len(failures)}. The backlog-full endpoint is "
        "not dropping -- see tests/test_rpc_connect_timeout.py for the mechanism."
    )
    assert all(type(exc).__name__ == "ConnectTimeout" for exc in failures), (
        f"expected ConnectTimeout, got {[type(e).__name__ for e in failures]}. A "
        "ConnectionError means the port refused instead of dropping, and a refusal's "
        "message has no object address in it, so this file would prove nothing."
    )

    texts = {str(exc) for exc in failures}
    assert len(texts) == 3, (
        "three real connect failures produced fewer than three distinct messages, so the "
        "per-attempt object address is no longer in the text. The suppression no longer "
        "NEEDS normalizing -- but failure_signature() is still correct, and this test "
        "should be rewritten to say that rather than deleted."
    )
    # And the thing that makes them differ is the address, not the reason.
    assert len({normalize_failure_text(str(exc)) for exc in failures}) == 1, (
        "normalizing the object address did not collapse three identical failures into one "
        "signature, so something ELSE in the message is varying. Print them and look: the "
        "fix normalizes addresses only, deliberately, and a second volatile field would "
        "need its own measurement before being added."
    )


def test_the_object_address_sits_inside_the_old_200_character_window():
    """Both halves of the defect, on a real exception: the address is in, the reason is out.

    This is the arithmetic from failure_signature.py's header, measured against a live
    exception rather than quoted from the operator's log. If a future `requests` or
    `urllib3` reorders its message, this test says so instead of the fix silently
    becoming unnecessary or insufficient.
    """
    exc = _real_connect_timeouts(1)[0]
    text = str(exc)
    address_at = text.index("at 0x")
    assert address_at < 200, (
        f"the object address is at index {address_at}, outside the old 200-char key. The "
        "repetition had a different cause than the one recorded, and the record should be "
        "corrected rather than trusted."
    )
    assert "timed out" in text[200:] or "timeout" in text[200:], (
        "the reason no longer falls outside the old 200-char window. The second half of the "
        f"defect no longer reproduces; the message is now {len(text)} characters."
    )
    # The new window holds the whole thing, which is what MESSAGE_KEY_LENGTH was sized for.
    assert len(text) <= MESSAGE_KEY_LENGTH, (
        f"the real message is {len(text)} characters and MESSAGE_KEY_LENGTH is "
        f"{MESSAGE_KEY_LENGTH}, so the key truncates the reason again -- the exact defect "
        "being fixed, one size up."
    )


# --- the signature itself ----------------------------------------------------------


def test_the_same_failure_twice_is_one_signature():
    """Real exceptions, real addresses, one key."""
    first, second = _real_connect_timeouts(2)
    assert str(first) != str(second), "the premise test above explains why this must hold"
    assert failure_signature("LTC", first) == failure_signature("LTC", second)


def test_two_assets_failing_the_same_way_are_two_signatures():
    """GRC failing must not silence LTC. This is the operator's exact case: both were down."""
    exc = _real_connect_timeouts(1)[0]
    assert failure_signature("LTC", exc) != failure_signature("GRC", exc)


def test_a_different_exception_type_for_one_asset_reports_again():
    """A daemon that stops refusing and starts timing out is NEWS, not a repeat.

    This is the hole that keying on the exception type ALONE would have left, and the
    hole that keying on a truncated message alone left: two failures whose text agrees
    inside the window but whose cause differs must not collapse.
    """
    one = ConnectionRefusedError("same text")
    other = TimeoutError("same text")
    assert failure_signature("LTC", one) != failure_signature("LTC", other)


def test_a_hex_value_in_the_message_itself_is_NOT_erased():
    """A txid or a principal is part of WHAT failed and must survive normalization.

    The pattern is anchored on `at 0x` with a word boundary precisely so this holds.
    A bare `0x[0-9a-f]+` substitution would have erased the identifier that tells an
    operator WHICH transaction or account the failure was about -- information this
    tree's RPC errors genuinely carry.
    """
    with_txid = RuntimeError("txid 0xdeadbeef not found")
    assert "0xdeadbeef" in failure_signature("BTC", with_txid)[2]
    # And the address form is still erased in the same string.
    both = RuntimeError("txid 0xdeadbeef failed on <Conn at 0x7f1234567890>")
    signature = failure_signature("BTC", both)[2]
    assert "0xdeadbeef" in signature
    assert "0x7f1234567890" not in signature
    assert "at 0xADDR" in signature


# --- end to end: the real function, a real database, counted log records -----------


class _AlwaysFails:
    """An adapter whose get_balance raises a pre-made REAL exception.

    The exceptions are real and distinct; only the delivery is a stub, because making
    refresh_wallet_inventory open two dropping sockets per cycle would make this test
    about sockets instead of about log cadence.
    """

    def __init__(self, failures: list[Exception]):
        self._failures = list(failures)

    def get_balance(self):
        raise self._failures.pop(0)


def test_ten_cycles_of_one_failing_chain_log_ONE_warning(tmp_path, caplog):
    """THE ASSERTION THE OPERATOR'S LOG WOULD HAVE FAILED. Ten cycles, one line.

    Drives the real refresh_wallet_inventory() against a real SQLite database with the
    real schema, ten times, with a real distinct ConnectTimeout each time -- the shape
    of cycles 69 to 80. Before the fix this logged ten warnings while each one claimed
    to be said once.
    """
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    db = connect_db(str(tmp_path / "t.db"), create=True)
    db.executescript(SCHEMA)

    failures = _real_connect_timeouts(10)
    adapters = {"LTC": _AlwaysFails(failures)}

    with caplog.at_level(logging.WARNING, logger="services.payout_service"):
        for _ in range(10):
            refresh = payout_service.refresh_wallet_inventory
            refresh(db, adapters)

    warnings = [r for r in caplog.records if "NOT refreshed" in r.getMessage()]
    assert len(warnings) == 1, (
        f"ten cycles of one unreachable chain produced {len(warnings)} warnings, each of which "
        "says it is said ONCE per process. That is the defect measured on the operator's host "
        "2026-10-11 (eleven LTC warnings in fourteen minutes), and it means the suppression key "
        "is varying again -- check what str() of the exception now embeds."
    )
    assert "LTC" in warnings[0].getMessage()


def test_a_second_asset_still_gets_its_own_warning(tmp_path, caplog):
    """Both of the operator's chains were down and both must be reported. Once each."""
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    db = connect_db(str(tmp_path / "t.db"), create=True)
    db.executescript(SCHEMA)

    adapters = {
        "LTC": _AlwaysFails(_real_connect_timeouts(5)),
        "GRC": _AlwaysFails(_real_connect_timeouts(5)),
    }
    with caplog.at_level(logging.WARNING, logger="services.payout_service"):
        for _ in range(5):
            payout_service.refresh_wallet_inventory(db, adapters)

    warnings = [r.getMessage() for r in caplog.records if "NOT refreshed" in r.getMessage()]
    assert len(warnings) == 2, f"expected one warning per asset, got {len(warnings)}"
    assert sum("LTC" in w for w in warnings) == 1
    assert sum("GRC" in w for w in warnings) == 1


def test_the_row_still_keeps_its_previous_values(tmp_path):
    """The suppression changed, the BEHAVIOR did not: a failed poll leaves the row alone.

    Asserted because a dedup fix has no business touching what the function writes, and
    the only way to know it did not is to look at the table. Rule: query the real output
    table and assert on what is actually there.
    """
    payout_service._REPORTED_INVENTORY_FAILURES.clear()
    db = connect_db(str(tmp_path / "t.db"), create=True)
    db.executescript(SCHEMA)
    db.execute(
        "INSERT INTO wallet_inventory (asset, hot_confirmed, hot_reserved, hot_available, "
        "updated_at) VALUES (?, ?, ?, ?, ?)",
        ("LTC", 12.5, 2.0, 10.5, "2026-10-01T00:00:00Z"),
    )
    db.commit()

    adapters = {"LTC": _AlwaysFails(_real_connect_timeouts(3))}
    for _ in range(3):
        payout_service.refresh_wallet_inventory(db, adapters)

    row = db.execute("SELECT * FROM wallet_inventory WHERE asset = 'LTC'").fetchone()
    assert row["hot_confirmed"] == pytest.approx(12.5)
    assert row["hot_reserved"] == pytest.approx(2.0)
    assert row["hot_available"] == pytest.approx(10.5)
    assert row["updated_at"] == "2026-10-01T00:00:00Z", (
        "the row's updated_at moved on a poll that FAILED, which would make a stale balance "
        "look freshly confirmed -- the opposite of what this function promises when a chain "
        "is unreachable."
    )


def test_the_call_site_no_longer_keys_on_a_raw_exception_string():
    """The gate: no suppression in payout_service keys on `str(exc)[:N]` again.

    Read as text and NOT through code_without_prose here, deliberately: the comment at
    the call site quotes the old expression in order to explain it, so a code-only scan
    would pass while the raw-text scan would fail. The pattern below is therefore
    anchored on the ASSIGNMENT -- `signature = (asset, str(exc)` -- which the comment
    does not contain in that form.
    """
    source = (REPO_ROOT / "swap_terminal" / "services" / "payout_service.py").read_text()
    assert "signature = (asset, str(exc)" not in source, (
        "the suppression key is built from a raw exception string again. str() of a requests "
        "exception embeds a per-attempt object address, so the key never matches itself and "
        "the 'ONCE per process' promise in the message is false. Use "
        "failure_signature.failure_signature(asset, exc)."
    )
    assert "failure_signature(asset, exc)" in source
