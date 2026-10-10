"""One unreachable chain must not stop deposits being credited on every other chain.

Role: tests (services/deposit_service.process_active_swaps()' per-unit isolation)
Reads: a throwaway SQLite database under tmp_path; stub adapters that open no socket
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes

=============================================================================
THE DEFECT, AND IT WAS LIVE ON THE OPERATOR'S HOST
=============================================================================

process_active_swaps() read every ACTIVE swap `ORDER BY created_at ASC` and refreshed
them in a bare list comprehension:

    processed = [refresh_swap_from_chain(db, config, adapters, swap, scans=scans)
                 for swap in swaps]

refresh_swap_from_chain() calls adapter.find_deposits_to_address() with nothing around
it. So the oldest swap on an unreachable chain raised, the comprehension aborted, and
EVERY SWAP POSITIONED AFTER IT was never refreshed -- on any chain. The ORDER BY is
stable, so it was the same swaps every cycle, deterministically, for as long as one
daemon was down.

OPEN_FINDINGS.md finding 1 measured exactly that precondition on this host:
`BTC did not answer: ... port=18443 ... [Errno 111] Connection refused`, with BTC and
LTC bound loopback-only and the container reaching the host from 172.18.0.0/16.

THE CATCH WAS NOT MISSING; IT WAS ONE LEVEL TOO HIGH. workers/deposit_watcher.py
already wrapped the cycle in `except Exception` and printed FAILED with a
consecutive-failure count, so the process did not die -- it printed a failed cycle,
tried again, and failed again, forever, while swaps behind the dead chain were never
credited. That is the distinction this file is about: a broad catch at the wrong
altitude isolates nothing.

AND THE SHARED-SCAN HALF IS WORSE. scan_shared_accounts() runs BEFORE the loop, so an
unreachable XRP or SOL endpoint there meant not one swap in the cycle was refreshed on
any chain -- including a BTC deposit already sitting at its confirmation threshold with
nothing left to do but be credited.
"""

from __future__ import annotations

import db as db_module
import pytest
from config import Config
from db import SCHEMA, apply_migrations, connect_db
from services import deposit_service
from workers.common import unreachable_note

CONFIG = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}


@pytest.fixture
def conn(tmp_path):
    connection = connect_db(str(tmp_path / "isolation.db"), create=True)
    connection.executescript(SCHEMA)
    apply_migrations(connection)
    return connection


def seed_awaiting(conn, swap_id: str, asset: str, address: str, created: str):
    """One awaiting_deposit swap, created at `created` so the ORDER BY is controlled.

    SIX CONFIRMATIONS, FIXED. It was a keyword argument until ruff's PLR0913 pointed
    at the signature, and no caller had ever varied it -- a parameter nobody passes is
    rule 9's vestige, and deleting it is the answer to the ceiling rather than raising
    it (rule 19). The healthy adapter below reports 6, so every seeded swap's threshold
    is met the moment it is looked at, which is what makes "was it looked at" the only
    variable in these tests.

    THE CREATION ORDER IS THE WHOLE EXPERIMENT. The defect dropped every swap AFTER the
    raising one, so a test whose failing swap sorts last would pass against the broken
    code. Each caller below states which swap is older and why that is the one that must
    raise.
    """
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES (?,?,'GRC',1.0,1.0,150,0.01,1.0,'2999-01-01T00:00:00+00:00',?)",
        (f"q_{swap_id}", asset, created),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES (?,?,?,'GRC',?,'Spayout',1.0,1.0,150,0.01,1.0,'awaiting_deposit',?,"
        "'2999-01-01T00:00:00+00:00',?,?)",
        (swap_id, f"q_{swap_id}", asset, address, 6, created, created),
    )
    conn.commit()


class RefusingAdapter:
    """A chain whose RPC refuses the connection. NOT a mock of the service.

    ConnectionRefusedError with errno 111 is the exact exception the operator's host
    produced for BTC on 18443, rather than a generic Exception -- so the test exercises
    the error a real dead daemon raises.
    """

    def __init__(self, asset: str):
        self.asset = asset
        self.calls = 0

    def find_deposits_to_address(self, address, skip_txids=frozenset()):
        self.calls += 1
        raise ConnectionRefusedError(111, "Connection refused")

    def deposit_confirmations(self, *_args, **_kwargs):
        return 0


class PayingAdapter:
    """A healthy chain holding one fully confirmed deposit for whatever it is asked about."""

    def __init__(self, asset: str, *, amount: float = 1.0, confirmations: int = 6):
        self.asset = asset
        self.amount = amount
        self.confirmations = confirmations
        self.calls = 0

    def find_deposits_to_address(self, address, skip_txids=frozenset()):
        self.calls += 1
        return [{"txid": f"{self.asset.lower()}_tx", "vout": 0, "address": address,
                 "amount": self.amount, "confirmations": self.confirmations}]

    def deposit_confirmations(self, *_args, **_kwargs):
        return self.confirmations


def statuses(conn) -> dict:
    return {row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM swaps ORDER BY created_at").fetchall()}


def test_a_newer_swap_is_credited_although_an_older_swaps_chain_refused(conn):
    """THE REGRESSION TEST. MUTATION: restore the list comprehension and this FAILS.

    The BTC swap is created FIRST so that it sorts first under `ORDER BY created_at
    ASC`, which is what made the comprehension abort before the LTC swap was ever
    looked at. The LTC deposit is at its full confirmation threshold -- there is
    nothing left for it to wait for except being looked at.
    """
    seed_awaiting(conn, "s_btc_older", "BTC", "bcrt1qolder", "2026-10-01T00:00:00+00:00")
    seed_awaiting(conn, "s_ltc_newer", "LTC", "tltc1qnewer", "2026-10-02T00:00:00+00:00")

    dead, live = RefusingAdapter("BTC"), PayingAdapter("LTC")
    processed = deposit_service.process_active_swaps(conn, CONFIG, {"BTC": dead, "LTC": live})

    assert statuses(conn)["s_ltc_newer"] == "payout_pending", (
        "the LTC swap behind the unreachable BTC chain was not refreshed; its deposit was "
        "confirmed and ready to credit"
    )
    assert live.calls == 1, "the healthy chain was never asked"
    assert len(processed) == 1, "only the reachable swap should be counted as refreshed"


def test_the_unreachable_swap_is_left_untouched_rather_than_declared_absent(conn):
    """A chain that could not be asked must not read as a chain that answered "nothing".

    THE DANGEROUS WRONG FIX is to swallow the error and return an empty event list. The
    swap would then be refreshed, counted, and recorded as having no deposit -- which is
    indistinguishable from a customer who did not pay, and on a tolerance check that
    distinction is the whole game (rule 12's `except Exception: return 0` note). So the
    swap must stay exactly as it was, with nothing written.
    """
    seed_awaiting(conn, "s_btc", "BTC", "bcrt1qonly", "2026-10-01T00:00:00+00:00")

    deposit_service.process_active_swaps(conn, CONFIG, {"BTC": RefusingAdapter("BTC")})

    row = conn.execute("SELECT * FROM swaps WHERE id = 's_btc'").fetchone()
    assert row["status"] == "awaiting_deposit"
    assert row["actual_input_amount"] is None, "an unreachable chain was recorded as paying 0"
    assert conn.execute("SELECT COUNT(*) AS n FROM deposit_events").fetchone()["n"] == 0


def test_the_failure_is_reported_with_the_asset_named(conn):
    """A quietly short `refreshed` count is the new way to be silent, so it is closed too.

    Per-unit guards fix the crediting and create a fresh rule 14 problem: the cycle now
    SUCCEEDS with `refreshed=1` where two swaps are open, and without this nothing says
    why. `2 asset(s)` would send an operator nowhere; `BTC` tells them which daemon to
    look at.
    """
    seed_awaiting(conn, "s_btc", "BTC", "bcrt1qa", "2026-10-01T00:00:00+00:00")
    seed_awaiting(conn, "s_ltc", "LTC", "tltc1qb", "2026-10-02T00:00:00+00:00")

    processed = deposit_service.process_active_swaps(
        conn, CONFIG, {"BTC": RefusingAdapter("BTC"), "LTC": PayingAdapter("LTC")}
    )

    assert [(asset, which) for asset, which, _exc in processed.unreachable] == [("BTC", "s_btc")]

    note = unreachable_note(processed.unreachable)
    assert "BTC" in note, "the asset is not named, so the operator cannot act on it"
    assert "ConnectionRefusedError" in note
    assert "LTC" not in note, "a healthy chain must not appear in the unreachable list"


def test_a_clean_cycle_says_nothing_so_the_line_does_not_grow_a_permanent_clause(conn):
    """The normal case must be unchanged. MUTATION: return a constant string.

    A note printed on every cycle is a note nobody reads, which would spend exactly the
    attention the real case needs.
    """
    seed_awaiting(conn, "s_ltc", "LTC", "tltc1qb", "2026-10-02T00:00:00+00:00")

    processed = deposit_service.process_active_swaps(conn, CONFIG, {"LTC": PayingAdapter("LTC")})

    assert processed.unreachable == ()
    assert unreachable_note(processed.unreachable) == ""


def test_every_failing_swap_is_reported_not_just_the_first(conn):
    """Two dead chains are two facts. MUTATION: `break` instead of `continue`.

    The pre-fix behavior stopped at the first raise; a fix that recorded only the first
    would keep that shape while looking fixed.
    """
    seed_awaiting(conn, "s_btc", "BTC", "bcrt1qa", "2026-10-01T00:00:00+00:00")
    seed_awaiting(conn, "s_ltc", "LTC", "tltc1qb", "2026-10-02T00:00:00+00:00")
    seed_awaiting(conn, "s_grc", "GRC", "Sgrcaddr", "2026-10-03T00:00:00+00:00")

    processed = deposit_service.process_active_swaps(
        conn, CONFIG,
        {"BTC": RefusingAdapter("BTC"), "LTC": RefusingAdapter("LTC"), "GRC": PayingAdapter("GRC")},
    )

    assert sorted(asset for asset, _which, _exc in processed.unreachable) == ["BTC", "LTC"]
    assert statuses(conn)["s_grc"] == "payout_pending", "the one healthy chain was still not credited"
    note = unreachable_note(processed.unreachable)
    assert "BTC" in note and "LTC" in note


def test_six_swaps_behind_one_dead_chain_are_one_fact_in_the_note(conn):
    """Deduplicated by asset, or the second dead chain is buried under the first.

    Six swaps on one unreachable daemon is one thing to fix. Printing it six times is
    how a line stops being read.
    """
    for index in range(6):
        seed_awaiting(conn, f"s_btc_{index}", "BTC", f"bcrt1q{index}", f"2026-10-0{index + 1}T00:00:00+00:00")
    seed_awaiting(conn, "s_ltc", "LTC", "tltc1qb", "2026-10-07T00:00:00+00:00")

    processed = deposit_service.process_active_swaps(
        conn, CONFIG, {"BTC": RefusingAdapter("BTC"), "LTC": RefusingAdapter("LTC")}
    )

    assert len(processed.unreachable) == 7, "every failing swap is still recorded individually"
    note = unreachable_note(processed.unreachable)
    assert note.count("BTC") == 1, f"BTC is named {note.count('BTC')} times in one note"
    assert note.count("LTC") == 1


def test_a_keyboard_interrupt_still_ends_the_cycle(conn):
    """`except Exception` and NOT BaseException -- rule 13.

    A watcher that logged a failed swap and carried on through Ctrl-C is a worker the
    operator cannot stop, which is worse than the crash the guard replaces.
    """
    seed_awaiting(conn, "s_btc", "BTC", "bcrt1qa", "2026-10-01T00:00:00+00:00")

    class Interrupting:
        asset = "BTC"

        def find_deposits_to_address(self, address, skip_txids=frozenset()):
            raise KeyboardInterrupt

        def deposit_confirmations(self, *_a, **_k):
            return 0

    with pytest.raises(KeyboardInterrupt):
        deposit_service.process_active_swaps(conn, CONFIG, {"BTC": Interrupting()})


def test_the_result_is_still_a_list_so_no_existing_caller_changes(conn):
    """CycleRefresh is a list subclass on purpose, and that is load-bearing.

    workers/deposit_watcher.py, workers/reconcile_worker.py and six test files read this
    return value with len() or by iterating it. A NamedTuple would have meant editing
    every one of them for a field only the cycle line reads, on a path that credits
    money -- so the extra information is an ATTRIBUTE and the contract is unchanged.

    getattr WITH A DEFAULT at both call sites is the other half: a caller holding a
    plain list from an older path reads () rather than raising.
    """
    seed_awaiting(conn, "s_ltc", "LTC", "tltc1qb", "2026-10-02T00:00:00+00:00")
    processed = deposit_service.process_active_swaps(conn, CONFIG, {"LTC": PayingAdapter("LTC")})

    assert isinstance(processed, list)
    assert len(processed) == 1
    assert list(processed) == [*processed]
    assert getattr(processed, "unreachable", "MISSING") == ()
    assert getattr([], "unreachable", ()) == (), "a plain list must read as no failures"


def test_db_module_is_importable_without_flask(conn):
    """Sanity: the service under test reaches db.py, which guards its Flask import.

    Here because this file builds a connection directly rather than through the app, and
    a regression in that guard would make every test above fail for an unrelated reason.
    """
    assert db_module.PAYMENT_UNIQUE_KEY[0] == "asset"
