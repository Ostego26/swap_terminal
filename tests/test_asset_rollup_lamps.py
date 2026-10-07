"""One lamp per coin: green when every direction works, amber when some, red when none.

Role: tests (read-only)
Reads: swap_terminal/services/pair_view.py -- asset_rollups() and ASSET_ROLLUP_STATES
Writes: nothing
Can move funds: no. No config, no adapters, no chain, no database -- asset_rollups()
        is pure and takes the rows allowed_pair_rows() already built.
Mainnet-safe: yes

Operator, 2026-10-07: "grc led ltc led, etc should indicate which pairs are available
for that coin. if all are available green. if some but not all, yellow. none....red."

WHAT THESE TESTS ARE REALLY ABOUT, beyond the three colours. A single lamp per coin
collapses two questions that have opposite answers, and ICP is the live instance:
measured 2026-10-07, ICP -> BTC/GRC/LTC quote and NOTHING -> ICP does, because an ICP
payout needs the desk's dfx identity and a read does not. An amber lamp over that is
true and useless -- it says "some" to a customer whose question is "can I get ICP out"
or "can I get ICP in". So the split is asserted as hard as the colour.
"""

from __future__ import annotations

from services.pair_view import ASSET_ROLLUP_STATES, asset_rollups


def _rows(*specs: tuple[str, str, bool]) -> list[dict]:
    """Rows shaped like allowed_pair_rows()' output, with only the keys the rollup reads."""
    return [
        {"from_asset": source, "to_asset": destination, "enabled": enabled, "label": f"{source} -> {destination}"}
        for source, destination, enabled in specs
    ]


def _by_asset(rows):
    return {lamp["asset"]: lamp for lamp in asset_rollups(rows)}


def test_a_coin_whose_every_direction_works_is_GREEN():
    lamps = _by_asset(_rows(("BTC", "LTC", True), ("LTC", "BTC", True)))
    for asset in ("BTC", "LTC"):
        assert lamps[asset]["key"] == "all"
        assert lamps[asset]["level"] == "ok"
        assert lamps[asset]["available"] == lamps[asset]["total"] == 2


def test_a_coin_with_no_working_direction_is_RED():
    lamps = _by_asset(_rows(("BTC", "SOL", False), ("SOL", "BTC", False)))
    assert lamps["SOL"]["key"] == "none"
    assert lamps["SOL"]["level"] == "halted"
    assert lamps["SOL"]["available"] == 0


def test_the_ICP_SHAPE_is_AMBER_and_the_lamp_says_WHICH_HALF():
    """The measured case, and the reason the split exists.

    ICP out works, ICP in does not. The colour alone is "some", which is true and
    answers neither of the two questions a customer actually has.
    """
    lamps = _by_asset(
        _rows(
            ("ICP", "BTC", True), ("ICP", "GRC", True), ("ICP", "LTC", True),
            ("BTC", "ICP", False), ("GRC", "ICP", False), ("LTC", "ICP", False),
        )
    )
    icp = lamps["ICP"]
    assert icp["key"] == "some"
    assert icp["level"] == "waiting"
    assert (icp["out_available"], icp["out_total"]) == (3, 3), "every outbound direction works"
    assert (icp["in_available"], icp["in_total"]) == (0, 3), "and no inbound one does"
    assert "3 of 3 outbound" in icp["detail"]
    assert "0 of 3 inbound" in icp["detail"]


def test_a_coin_that_is_only_a_DESTINATION_in_a_broken_pair_is_still_counted():
    """BTC appears here only as a source; its inbound total is zero and must not vacuously pass.

    A coin with no inbound direction at all has in_available == in_total == 0, and
    "all of zero" is the vacuous truth that would paint a lamp green for a coin nobody
    can send TO. The overall key is what guards it -- it is driven by the combined
    counts -- and this pins that the zero is reported rather than hidden.
    """
    lamps = _by_asset(_rows(("BTC", "LTC", True)))
    assert lamps["BTC"]["out_total"] == 1
    assert lamps["BTC"]["in_total"] == 0
    assert lamps["LTC"]["in_total"] == 1
    assert lamps["LTC"]["out_total"] == 0


def test_no_rows_at_all_is_NONE_rather_than_vacuously_ALL():
    """An empty set satisfies "every direction works" and must not read as green.

    It cannot arise from ALLOWED_PAIRS today, which is exactly why it is pinned: the
    case nothing can currently produce is the case nobody notices the day something
    can (rule 17's vacuous-pass trap, in a view).
    """
    assert asset_rollups([]) == []

    orphan = _rows(("BTC", "BTC", False))
    lamps = _by_asset(orphan)
    assert lamps["BTC"]["key"] == "none"


def test_the_lamp_states_are_DERIVED_and_cover_what_a_lamp_can_be():
    """The page's key is built from the same table the lamps are, or it drifts (rule 8)."""
    keys = {state["key"] for state in ASSET_ROLLUP_STATES}
    assert keys == {"all", "some", "none"}
    for state in ASSET_ROLLUP_STATES:
        assert state["word"] and state["note"], f"{state['key']} has nothing a reader can look up"
        assert state["level"] in {"ok", "waiting", "halted"}, (
            "the lamp levels must reuse the pair vocabulary so one stylesheet colors both"
        )


def test_a_lamp_can_never_be_greener_than_the_rows_it_came_from():
    """The rollup is a MIRROR of allowed_pair_rows(), never a second evaluation.

    This module's own header records what a second evaluation cost: admin_view's
    pair_rows() once carried the first of three serviceability conditions and told the
    operator a pair was ENABLED that the customer page was refusing in the same
    process. Mutation-shaped: flipping any row to unavailable must be able to move the
    lamp, and no lamp may claim more available than the rows contain.
    """
    rows = _rows(("BTC", "LTC", True), ("LTC", "BTC", True))
    assert _by_asset(rows)["BTC"]["key"] == "all"

    rows[0]["enabled"] = False
    moved = _by_asset(rows)["BTC"]
    assert moved["key"] == "some", "a row going unavailable must move the lamp off green"
    assert moved["available"] <= moved["total"]
