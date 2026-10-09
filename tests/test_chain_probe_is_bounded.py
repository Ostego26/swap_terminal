#!/usr/bin/env python3
"""The chain probe answers within the worker's patience, or says which it skipped.

Role: file (entry point -- `python3 -m pytest tests/test_chain_probe_is_bounded.py`)
Reads: services/admin_view.py, gunicorn.conf.py
Writes: nothing
Can move funds: no
Live-safe: yes

MEASURED ON THE OPERATOR'S HOST, 2026-10-08:

    all chain probes : HTTP 500  60.182023s total
    --- what each daemon said ---
    (empty)

60.18s is gunicorn's 60s worker timeout, not a chain answering slowly. Six
configured chains, up to two read-only RPCs each, 30s per RPC by the adapter's
default = up to 360s in one request. probe_chains() was a list comprehension
that built every row before returning, so the kill took the whole answer with
it: the probe whose entire job is to name WHICH chain is unreachable named none
of them, and spent one of two workers doing it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from services import admin_view  # noqa: E402 -- after the sys.path.insert, same as every test here


class _SlowAdapter:
    """An adapter whose probe consumes the whole budget. Nothing sleeps."""

    def __init__(self, clock: list[float], cost: float):
        self._clock, self._cost = clock, cost

    def network(self):
        self._clock[0] += self._cost
        return "regtest"


def _clock_and_probe(cost: float, assets: tuple[str, ...], budget: float):
    clock = [1000.0]
    adapters = {asset: _SlowAdapter(clock, cost) for asset in assets}
    rows = admin_view.probe_chains(adapters, assets, budget_seconds=budget, now=lambda: clock[0])
    return rows, clock[0] - 1000.0


def test_every_chain_gets_a_row_even_when_the_budget_runs_out():
    """A MISSING ROW READS AS FINE, which is why the loop does not simply stop.

    Five rows where six were expected is rule 14's blank gap: the operator cannot
    tell "this chain was not asked" from "this chain is not in the table", and
    only one of those is a reason to go looking.
    """
    assets = ("BTC", "GRC", "ICP", "LTC", "SOL", "XRP")
    rows, _spent = _clock_and_probe(cost=30.0, assets=assets, budget=45.0)
    assert [row["asset"] for row in rows] == list(assets), (
        f"the probe returned {len(rows)} rows for {len(assets)} chains: "
        f"{[row['asset'] for row in rows]}"
    )


def test_the_chains_it_never_asked_say_so_rather_than_reporting_unreachable():
    """`reachable=False` would be a claim. Nobody asked, and that is the answer."""
    rows, _spent = _clock_and_probe(cost=30.0, assets=("BTC", "GRC", "ICP"), budget=45.0)
    skipped = [row for row in rows if row["detail"].startswith("NOT PROBED")]
    assert skipped, f"a 30s-per-chain probe on a 45s budget skipped nothing: {rows}"
    for row in skipped:
        assert row["reachable"] is None, (
            f"{row['asset']} was never asked and is reported reachable={row['reachable']}. "
            f"False is a claim about the chain; None is the truth about the probe"
        )
        assert row["probed"] is False, row
        assert "nobody asked" in row["detail"], (
            f"the row must say nobody asked, not merely omit an answer: {row['detail']}"
        )
        assert "at least one chain above did not answer" in row["detail"], (
            f"and must point at where the time actually went, or the operator reads the "
            f"skipped chain as the problem: {row['detail']}"
        )


def test_the_total_stays_inside_the_budget():
    """The bound itself, over the real loop."""
    _rows, spent = _clock_and_probe(cost=30.0, assets=("BTC", "GRC", "ICP", "LTC", "SOL", "XRP"), budget=45.0)
    assert spent <= 45.0 + 30.0, (
        f"six chains at 30s each spent {spent}s against a 45s budget. Unbounded this is 180s "
        f"(360s with the second RPC a Bitcoin-style daemon needs) and gunicorn kills at 60"
    )


def test_a_fast_chain_is_not_skipped_merely_because_others_exist():
    """A budget that refuses work it could afford is a different defect.

    Six chains answering in a second each must ALL be probed -- otherwise the
    bound has been bought by making the diagnostic useless, which is the trade
    this file exists to refuse.
    """
    rows, spent = _clock_and_probe(cost=1.0, assets=("BTC", "GRC", "ICP", "LTC", "SOL", "XRP"), budget=45.0)
    assert not [row for row in rows if row["detail"].startswith("NOT PROBED")], (
        f"chains were skipped with budget to spare ({spent}s of 45s used): {rows}"
    )
    assert all(row["probed"] for row in rows), rows


def test_an_unconfigured_chain_costs_no_budget():
    """It is answered from memory, so masking it with "not probed" loses precision for free."""
    clock = [1000.0]
    adapters = {"BTC": _SlowAdapter(clock, 60.0), "GRC": None}
    rows = admin_view.probe_chains(adapters, ("BTC", "GRC"), budget_seconds=45.0, now=lambda: clock[0])
    grc = next(row for row in rows if row["asset"] == "GRC")
    assert "not configured" in grc["detail"], (
        f"an unconfigured chain reported as budget-skipped loses the real reason: {grc['detail']}"
    )


def test_the_budget_is_under_the_timeout_that_kills_the_worker():
    """TWO NUMBERS IN TWO FILES, and the whole fix is the relationship between them.

    Read out of gunicorn.conf.py's own default rather than restated, so raising
    the server's timeout cannot quietly make this vacuous.
    """
    source = (REPO_ROOT / "gunicorn.conf.py").read_text(encoding="utf-8")
    match = re.search(r'timeout\s*=\s*int\(os\.getenv\(\s*"GUNICORN_TIMEOUT_SECONDS"\s*,\s*"(\d+)"', source)
    assert match, "gunicorn.conf.py no longer sets `timeout` in the shape this test reads"
    assert int(match.group(1)) > admin_view.CHAIN_PROBE_BUDGET_SECONDS, (
        f"the chain-probe budget is {admin_view.CHAIN_PROBE_BUDGET_SECONDS}s and gunicorn kills "
        f"a worker at {match.group(1)}s. Not strictly under it bounds nothing -- the request "
        f"dies as a dropped connection with an empty body, which is what was measured"
    )
