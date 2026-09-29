"""The root entry point for market context: does it write only when told, and say what it did?

Role: test (read-only against the tree; writes only to tmp_path databases)
Reads: market_context_report.py, run as the real module with a stubbed fetch
Writes: nothing outside pytest's tmp_path
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

WHAT IS TESTED HERE, AND WHY IT IS THE ENTRY POINT AND NOT THE LIBRARY

services/market_context.py's own tests cover the verdicts, the parser and the SQL. What they
cannot cover is the thing that made this file necessary: the library had no caller, so
nothing proved the pieces FIT. The defect class is specific and has bitten this repository
twice in two days -- a script referenced target.daemon_port after a rename
and ruff could not see it because no test reached main(), and atomic_swap.py shipped a
function whose arguments no test ever supplied. Both were plumbing, both looked right, and
both failed on the operator's first run.

So these tests call main() with a real argv and a real (temporary) database, and assert on
what got written and what got printed. The CoinGecko fetch is stubbed because egress to
api.coingecko.com is denied from this container (403 to CONNECT) -- so the one thing NOT
proven here is that CoinGecko's response keys are spelled as the parser expects. That is said
in the entry point's own header too, rather than left for a reader to discover.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import time

import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))
sys.path.insert(0, str(REPOSITORY_ROOT))

from config import Config  # noqa: E402  same
from db import connect_db  # noqa: E402  same
from services.pricing import MarketSnapshot  # noqa: E402  same

import market_context_report  # noqa: E402  the path shims above must run first


def _snapshot(asset: str = "GRC", *, price: float = 0.0123, cap: float | None = 9_000_000.0,
              volume: float | None = 45_000.0, change: float | None = 1.25) -> MarketSnapshot:
    now = time.time()
    return MarketSnapshot(
        asset=asset, coingecko_id=asset.lower(), price_usd=price, market_cap_usd=cap,
        volume_24h_usd=volume, change_24h_pct=change, source_updated_at=int(now) - 20,
        fetched_at=now,
    )


@pytest.fixture
def stub_fetch(monkeypatch):
    """Replace both fetch entry points the report can reach, and COUNT the calls.

    Both, because `--record` goes through collect_and_record() and the read-only path goes
    through fetch_market_context() directly -- patching one would leave the other making a
    real request, which from here fails at the proxy and from the live host would be a
    surprise network call in a test.
    """
    calls: list[str] = []
    snapshots = [_snapshot("GRC"), _snapshot("BTC", price=64000.0, cap=1.2e12, volume=2.5e10)]

    def fake_fetch(_ttl=None):
        calls.append("fetch")
        return list(snapshots)

    monkeypatch.setattr(market_context_report, "fetch_market_context", fake_fetch)
    monkeypatch.setattr("services.market_context.fetch_market_context", fake_fetch)
    return {"calls": calls, "snapshots": snapshots}


def _run(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["market_context_report.py", *argv])
    return market_context_report.main()


def _rows(db_path: str) -> list[dict]:
    conn = connect_db(db_path)
    try:
        return conn.execute("SELECT * FROM market_context ORDER BY id").fetchall()
    finally:
        conn.close()


def test_a_bare_run_writes_nothing(tmp_path, monkeypatch, stub_fetch, capsys):
    """The default must not write, for the same reason atomic_swap.py refuses without --run:
    the first thing anyone does with an unfamiliar script is run it to see what it says.

    Asserted on the ROWS, not on the absence of a log line -- a write that happened and was
    not printed is exactly the failure this is guarding."""
    db = str(tmp_path / "swap.db")
    assert _run(monkeypatch, ["--db", db]) == 0
    assert _rows(db) == []
    printed = capsys.readouterr().out
    assert "read only -- nothing will be written" in printed


def test_record_appends_one_row_per_asset_and_says_how_many(tmp_path, monkeypatch, stub_fetch,
                                                            capsys):
    """--record is the whole point of the file: without something running this, market_context
    has no writer and "keep track of the market cap" is a claim about zero rows."""
    db = str(tmp_path / "swap.db")
    assert _run(monkeypatch, ["--db", db, "--record"]) == 0
    rows = _rows(db)
    assert [row["asset"] for row in rows] == ["GRC", "BTC"]
    assert rows[0]["market_cap_usd"] == pytest.approx(9_000_000.0)
    assert "recorded 2 rows at " in capsys.readouterr().out


def test_two_records_accumulate_rather_than_overwrite(tmp_path, monkeypatch, stub_fetch):
    """History is the reason the table exists rather than a print. An UPDATE-shaped writer
    would leave the file looking identical and the question -- what was GRC doing an hour ago
    -- unanswerable."""
    db = str(tmp_path / "swap.db")
    _run(monkeypatch, ["--db", db, "--record"])
    _run(monkeypatch, ["--db", db, "--record"])
    assert len(_rows(db)) == 4


def test_it_creates_the_table_in_a_database_that_predates_it(tmp_path, monkeypatch, stub_fetch):
    """A live swap_terminal.db was created before market_context existed. The report runs
    SCHEMA first, which is what every worker does at startup, so an older database grows the
    table instead of failing on the first SELECT."""
    db = str(tmp_path / "old.db")
    sqlite3.connect(db).executescript("CREATE TABLE quotes (id INTEGER PRIMARY KEY);")
    assert _run(monkeypatch, ["--db", db, "--record"]) == 0
    assert len(_rows(db)) == 2


def test_an_empty_history_prints_none_and_why_rather_than_a_blank_section(tmp_path, monkeypatch,
                                                                         stub_fetch, capsys):
    """Rule 14: `(none)` is a result, a blank gap is ambiguous between zero rows and a query
    that broke -- and on a fresh table zero is the EXPECTED answer, so the line has to say
    what would make it non-zero."""
    db = str(tmp_path / "swap.db")
    _run(monkeypatch, ["--db", db])
    printed = capsys.readouterr().out
    assert "(none)  <- 0 rows" in printed
    assert "--record" in printed


def test_the_history_section_appears_once_rows_exist(tmp_path, monkeypatch, stub_fetch, capsys):
    """The comparison over time, which is what "better establish grc prices" asks for."""
    db = str(tmp_path / "swap.db")
    _run(monkeypatch, ["--db", db, "--record"])
    capsys.readouterr()
    _run(monkeypatch, ["--db", db])
    printed = capsys.readouterr().out
    assert "GRC  1 rows" in printed
    assert "(none)  <- 0 rows" not in printed


def test_the_window_follows_config_when_config_is_retuned(monkeypatch):
    """QuoteWindow has no defaults on purpose, and THE OBVIOUS TEST FOR THAT DOES NOT WORK.

    Reading the three values back and comparing them to Config passes whether they came from
    Config or were spelled as 600/30/150 -- because those ARE Config's current values. A
    mutation check proved it: replacing two of them with literals equal to the defaults left
    that version of this test green. It was asserting today's numbers, not the wiring.

    So Config is RETUNED to values nobody would write by accident, and the window has to
    follow. That is the property the operator actually depends on: retuning QUOTE_TTL_SECONDS
    must change what counts as a stale price, silently and immediately."""
    monkeypatch.setattr(Config, "QUOTE_TTL_SECONDS", 1234)
    monkeypatch.setattr(Config, "RATE_CACHE_SECONDS", 77)
    monkeypatch.setattr(Config, "DEFAULT_FEE_BPS", 311)
    window = market_context_report.quote_window()
    assert (window.quote_ttl_seconds, window.rate_cache_seconds, window.fee_bps) == (
        1234.0, 77.0, 311.0)
    assert window.exposure_seconds == 1311.0, "the sum, which is the whole subtlety"


def test_the_block_echoes_the_parameters_that_decide_the_answer(tmp_path, monkeypatch,
                                                                stub_fetch, capsys):
    """Pasted output is read a day later, so it has to be self-describing: the database, both
    windows and the fee the drift is measured against. A verdict without them is a word."""
    db = str(tmp_path / "swap.db")
    _run(monkeypatch, ["--db", db])
    printed = capsys.readouterr().out
    assert f"db={db}" in printed
    assert "RATE_CACHE_SECONDS=" in printed and "QUOTE_TTL_SECONDS=" in printed
    assert "DEFAULT_FEE_BPS=" in printed


def test_every_duration_it_prints_is_microfortnights(tmp_path, monkeypatch, stub_fetch, capsys):
    """Rule 6, and the unit is µ (U+00B5) -- an ASCII 'u' in displayed output is a defect the
    same as a wrong number, and every script written for this rule has drifted to "ufn"
    because ASCII is what fingers type and nothing failed when it did."""
    db = str(tmp_path / "swap.db")
    _run(monkeypatch, ["--db", db, "--record"])
    capsys.readouterr()
    _run(monkeypatch, ["--db", db])
    printed = capsys.readouterr().out
    assert "µfn" in printed, "some duration is reported, so the rest of this test means something"
    assert "ufn" not in printed, "an ASCII u in displayed output is a defect, not a fallback"
    assert " µfn" not in printed, "no space between the number and the unit"

    # ASSERTED PER DURATION, NOT ONCE FOR THE WHOLE BLOCK, because "µfn appears somewhere" is
    # not the rule. A mutation that printed the history age in raw seconds SURVIVED the weaker
    # version of this test: the block's other durations still carried µfn, so the substring
    # check passed while a column printed "1.4s". Rule 6 allows seconds only in parentheses
    # beside the µfn figure, so every labeled duration is now checked on its own.
    checked = 0
    for label in ("age=", "feed_age="):
        for line in printed.splitlines():
            for field in line.split(label)[1:]:
                value = field.split()[0]
                assert "µfn" in value or value == "(none)", (
                    f"{label}{value} reports a duration without µfn"
                )
                checked += 1
    assert checked >= 2, "both labeled durations must appear, or this loop proves nothing"


def test_one_fetch_per_run_and_never_two(tmp_path, monkeypatch, stub_fetch):
    """--record must not fetch once to write and again to display. Two requests against a
    rate-limited free tier is how a five-minute cron entry starts getting 429s, and the second
    answer would disagree with the row just written."""
    db = str(tmp_path / "swap.db")
    _run(monkeypatch, ["--db", db, "--record"])
    assert stub_fetch["calls"] == ["fetch"]
